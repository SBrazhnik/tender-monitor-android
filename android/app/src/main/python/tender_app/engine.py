# -*- coding: utf-8 -*-
"""Прогон поиска: сбор с площадок, фильтр активных, классификация, документация, планы."""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .classify import Classifier
from .extract import SKIP_EXT, clean, extract_text
from .sources import B2B, Eis, Rosatom, Sber, is_active_notice, plan_month_future
from .store import Store
from .util import Http, log, norm, parse_date


class Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.stage = ""
        self.done = 0
        self.total = 0
        self.lines: list[str] = []
        self.started = ""
        self.finished = ""
        self.summary: dict = {}
        self.http: Http | None = None

    def log(self, msg: str) -> None:
        line = time.strftime("%H:%M:%S ") + msg
        with self.lock:
            self.lines.append(line)
            self.lines = self.lines[-600:]
        log.info(msg)

    def set(self, stage: str, total: int = 0) -> None:
        with self.lock:
            self.stage, self.total, self.done = stage, total, 0

    def tick(self) -> None:
        with self.lock:
            self.done += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "stage": self.stage, "done": self.done, "total": self.total,
                    "lines": self.lines[-200:], "started": self.started, "finished": self.finished,
                    "summary": self.summary}


def _names(c: dict) -> list[str]:
    names = [norm(c.get("name", ""))] + [norm(x) for x in (c.get("aliases") or "").split("\n")]
    out = []
    for n in names:
        n = re.sub(r"\s*\(.*?\)\s*", " ", n).strip()     # «АО X (АО Y)» → «АО X»
        if n and n not in out:
            out.append(n)
    return out


def _core_name(n: str) -> str:
    """«АО «РИР Энерго»» → «рир энерго» — для сравнения названий."""
    n = norm(n).lower()
    n = re.sub(r"[\"«»'“”„]", " ", n)
    n = re.sub(r"\b(акционерное общество|публичное акционерное общество|общество с ограниченной ответственностью|"
               r"пао|ао|оао|зао|ооо|фгуп|гуп|муп|филиал)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


class Engine:
    def __init__(self, store: Store, base_dir: Path):
        self.store = store
        self.base_dir = base_dir
        self.progress = Progress()
        self._lock = threading.Lock()
        # необязательные обработчики (Android: служба переднего плана и уведомления)
        self.on_start = None
        self.on_finish = None
        self.notify_hook = None

    # ───────────────────────── запуск ─────────────────────────
    def start(self, only: dict | None = None) -> bool:
        with self._lock:
            if self.progress.running:
                return False
            self.progress = Progress()
            self.progress.running = True
            self.progress.started = time.strftime("%Y-%m-%d %H:%M")
        threading.Thread(target=self._run_safe, args=(only or {},), daemon=True).start()
        return True

    def stop(self) -> None:
        if self.progress.http:
            self.progress.http.cancel.set()

    def _run_safe(self, only: dict) -> None:
        P = self.progress
        run_id = self.store.x("INSERT INTO runs(started, status) VALUES(?, 'running')", (P.started,))
        status = "ok"
        if self.on_start:
            try:
                self.on_start()
            except Exception as e:  # noqa: BLE001
                log.debug("on_start: %s", e)
        try:
            self._run(only)
        except Exception as e:  # noqa: BLE001
            status = "error"
            P.log(f"ОШИБКА: {e}")
            log.debug(traceback.format_exc())
        finally:
            P.finished = time.strftime("%Y-%m-%d %H:%M")
            P.running = False
            P.stage = "Готово" if status == "ok" else "Ошибка"
            self.store.x("UPDATE runs SET finished=?, status=?, summary=?, log=? WHERE id=?",
                         (P.finished, status, json.dumps(P.summary, ensure_ascii=False), "\n".join(P.lines), run_id))
            if self.on_finish:
                try:
                    self.on_finish()
                except Exception as e:  # noqa: BLE001
                    log.debug("on_finish: %s", e)

    # ───────────────────────── основной сценарий ─────────────────────────
    def _run(self, only: dict) -> None:
        P = self.progress
        S = self.store.settings()
        net = S["network"]
        http = Http(net.get("request_delay", 1.5), net.get("timeout", 40), net.get("proxy", ""),
                    net.get("ssl_verify", True), ca_dir=self.base_dir)
        P.http = http
        clf = self.store.classifier()
        today = dt.date.today()
        now = time.strftime("%Y-%m-%d %H:%M")
        src = dict(S["sources"])
        modes = dict(S["modes"])
        modes["notices"] = True
        customers = [c for c in self.store.q("SELECT * FROM customers WHERE enabled=1")]
        queries = [q["text"] for q in self.store.q("SELECT text FROM queries WHERE enabled=1")]
        # ── поиск только в рамках выбранных фильтров
        scope = []
        if only.get("customer_id"):
            customers = [c for c in self.store.q("SELECT * FROM customers WHERE id=?", (int(only["customer_id"]),))]
            modes["market"] = False
            scope.append("заказчик: " + ", ".join(c["name"] or c["inn"] for c in customers))
        if only.get("modes"):
            for k, v in only["modes"].items():
                modes[k] = bool(v) and modes.get(k, True)
            names = {"customers": "мои заказчики", "market": "весь рынок", "plans": "планы", "notices": "закупки"}
            scope.append("режим: " + ", ".join(names[k] for k in ("customers", "market") if modes.get(k)) +
                         ("; только планы" if not modes.get("notices") else "; без планов" if not modes.get("plans") else ""))
        if only.get("sources"):
            for k in list(src):
                src[k] = bool(src.get(k)) and bool(only["sources"].get(k))
            scope.append("площадки: " + ", ".join(k for k, v in src.items() if v))
        if only.get("directions"):
            want = set(only["directions"])
            picked = []
            for q in queries:                       # фраза относится к направлению, если сама по нему классифицируется
                d, _ = clf.classify(q, [])
                if want & set(d):
                    picked.append(q)
            queries = picked
            scope.append("направления: " + ", ".join(clf.name(x) for x in want) + f" ({len(queries)} поисковых фраз)")
            if not queries and modes.get("market"):
                P.log("Среди поисковых фраз нет относящихся к выбранному направлению — добавьте их на вкладке «Направления»")
        if scope:
            P.log("Поиск по фильтру — " + "; ".join(scope))

        eis, sber, b2b, ros = Eis(http), Sber(http), B2B(http), Rosatom(http)
        errors: dict[str, list[str]] = {}
        found: dict[str, dict] = {}
        flock = threading.Lock()

        def add(items: list[dict], mode: str, watch_id=None, query: str = "") -> int:
            n = 0
            with flock:
                for t in items:
                    t.setdefault("mode", mode)
                    if watch_id:
                        t["watch_id"] = watch_id
                        t["mode"] = "customer"
                    if query and not t.get("query"):
                        t["query"] = query
                    old = found.get(t["key"])
                    if old:
                        # объединяем: ЕИС-запись главная, ссылки Сбербанк-АСТ добавляем
                        for k, v in (t.get("extra") or {}).items():
                            old.setdefault("extra", {}).setdefault(k, v)
                        if watch_id:
                            old["watch_id"], old["mode"] = watch_id, "customer"
                        if old["source"] != t["source"] and t["source"] not in old.get("also", []):
                            old.setdefault("also", []).append(t["source"])
                        for f in ("customer", "customer_inn", "deadline", "price", "region"):
                            if not old.get(f) and t.get(f):
                                old[f] = t[f]
                    else:
                        found[t["key"]] = t
                        n += 1
            return n

        def err(source: str, e: Exception) -> None:
            errors.setdefault(source, []).append(str(e)[:300])
            P.log(f"{source}: ошибка — {e}")

        if src.get("b2b"):
            login, pwd = self.store.get_credentials("b2b")
            try:
                P.log("B2B-Center: " + b2b.login(login, pwd))
            except Exception as e:  # noqa: BLE001
                err("B2B-Center", e)

        # ---------- 1. по заказчикам ----------
        if modes.get("notices") and modes.get("customers") and customers:
            P.set("Поиск по заказчикам", len(customers))

            def cust_eis(c):
                names = _names(c)
                if c.get("inn"):
                    items = eis.search(c["inn"], max_pages=10)
                    for t in items:
                        if t["customer_inn"] and t["customer_inn"] != c["inn"]:
                            t.setdefault("extra", {})["note"] = "Совместная закупка / через организатора"
                    add(items, "customer", c["id"])
                else:
                    for n in names[:3]:
                        items = [t for t in eis.search(f'"{n}"', max_pages=4)
                                 if _core_name(n) and _core_name(n) in _core_name(t["customer"])]
                        add(items, "customer", c["id"])

            def cust_sber(c):
                for n in _names(c)[:3]:
                    items = sber.search(n, deadline_from=today, max_pages=4)
                    if c.get("inn"):
                        items = [t for t in items if t["customer_inn"] == c["inn"]]
                    else:
                        items = [t for t in items if _core_name(n) in _core_name(t["customer"])]
                    add(items, "customer", c["id"])

            def cust_b2b(c):
                for n in _names(c)[:3]:
                    cn = _core_name(n)
                    items = [t for t in b2b.search(n) if cn and cn in _core_name(t["customer"])]
                    add(items, "customer", c["id"])

            def cust_ros(c):
                names = _names(c)
                pats = [re.escape(_core_name(n)).replace(r"\ ", r"[\s-]*") for n in names if len(_core_name(n)) >= 3]
                add(ros.by_customer(names, [rf"(?<![\w-]){p}(?![\w])" for p in pats]), "customer", c["id"])

            jobs = []
            for c in customers:
                if src.get("eis"):
                    jobs.append(("ЕИС", cust_eis, c))
                if src.get("sber"):
                    jobs.append(("Сбербанк-АСТ", cust_sber, c))
                if src.get("b2b"):
                    jobs.append(("B2B-Center", cust_b2b, c))
                if src.get("rosatom"):
                    jobs.append(("Росатом", cust_ros, c))
            self._parallel(jobs, err, f"Заказчики: {len(customers)}")

        # ---------- 2. по всему рынку ----------
        if modes.get("notices") and modes.get("market") and queries:
            m = S["market"]
            pub_from = today - dt.timedelta(days=int(m.get("days_back", 14)))
            pages = int(m.get("max_pages", 3))
            minp = float(m.get("min_price", 0) or 0)
            P.set("Поиск по рынку", len(queries))
            jobs = []
            for qtext in queries:
                if src.get("eis"):
                    jobs.append(("ЕИС", lambda q=qtext: add(eis.search(q, pages, today, pub_from, minp), "market", query=q), None))
                if src.get("sber"):
                    jobs.append(("Сбербанк-АСТ", lambda q=qtext: add(
                        [t for t in sber.search(q, today, pages, pub_from) if (t["price"] or 0) >= minp or not t["price"]],
                        "market", query=q), None))
                if src.get("b2b"):
                    jobs.append(("B2B-Center", lambda q=qtext: add(b2b.search(q, date_from=pub_from), "market", query=q), None))
            if src.get("rosatom"):
                jobs.append(("Росатом", lambda: add(ros.all_published(), "market", query="*"), None))
            self._parallel(jobs, err, f"Поисковых фраз: {len(queries)}")

        # ---------- 3. активные, классификация ----------
        P.set("Отбор активных и классификация", len(found))
        existing = {r["key"]: r for r in self.store.q(
            "SELECT key, positions, platform, deadline, docs_state, directions FROM tenders")}
        active = []
        stale = 0
        for t in found.values():
            if is_active_notice(t, today):
                active.append(t)
            else:
                stale += 1
        P.log(f"Найдено {len(found)}, активных {len(active)}, отброшено неактуальных {stale}")

        # позиции из печатной формы ЕИС: для новых закупок (для уже известных — из базы)
        need = []
        for t in active:
            ex = existing.get(t["key"])
            if ex and json.loads(ex["positions"] or "[]"):
                t["positions"] = t.get("positions") or json.loads(ex["positions"])
                t["platform"] = t.get("platform") or ex["platform"]
            elif (t.get("extra") or {}).get("print_url"):
                need.append(t)
        # сначала классифицируем по названию, чтобы понять, кого догружать на рынке
        for t in active:
            t["directions"], t["confidence"] = clf.classify(t["title"], t.get("positions") or [])
        need = [t for t in need if t.get("mode") == "customer" or t["directions"]] + \
               [t for t in need if t.get("mode") != "customer" and not t["directions"]][:40]
        if need and src.get("eis"):
            P.set("ЕИС: позиции и ОКПД2 из извещений", len(need))
            for t in need:
                if http.cancel.is_set():
                    break
                try:
                    eis.enrich(t)
                except Exception as e:  # noqa: BLE001
                    log.debug("печатная форма %s: %s", t["number"], e)
                P.tick()
        # закупки Сбербанк-АСТ, дублирующие ЕИС, но без ссылок ЕИС: ссылки на документацию подтянем позже
        for t in active:
            t["directions"], t["confidence"] = clf.classify(t["title"], t.get("positions") or [])
            t["deadline_iso"] = (parse_date(t.get("deadline")) or dt.date(2100, 1, 1)).isoformat()
            t["active"] = 1
            if t.get("also"):
                t.setdefault("extra", {})["also"] = t["also"]

        # на рынке сохраняем только релевантные и кандидатов для проверки документации
        keep = []
        cand = []
        for t in active:
            if t.get("mode") == "customer" or t["directions"]:
                keep.append(t)
            elif t.get("mode") == "market" and t.get("query") and t["query"] != "*":
                cand.append(t)                         # фраза нашлась, но не в названии — вероятно, в ТЗ
        new_keys = []
        for t in keep:
            if self.store.upsert_tender(t, now):
                new_keys.append(t["key"])
        P.log(f"Сохранено закупок: {len(keep)} (новых {len(new_keys)})")

        # ---------- 4. документация ----------
        D = S["docs"]
        docs_done = 0
        if D.get("enabled", True):
            limit = int(D.get("max_tenders_per_run", 60))
            known_done = {k for k, r in existing.items() if r["docs_state"] in ("done", "error")}
            queue = [t for t in keep if t["key"] not in known_done and (t.get("mode") == "customer" or t["directions"])]
            queue += [t for t in cand if t["key"] not in known_done]
            queue = queue[:limit]
            cand_keys = {t["key"] for t in cand}
            if queue:
                P.set("Документация: скачивание и поиск по вложениям", len(queue))
                for t in queue:
                    if http.cancel.is_set():
                        break
                    try:
                        found_dirs = self._scan_docs(t, eis, sber, b2b, http, clf, int(D.get("max_file_mb", 25)))
                        docs_done += 1
                        if t["key"] in cand_keys and found_dirs:
                            t["directions"] = found_dirs
                            t["confidence"] = "по документации"
                            if self.store.upsert_tender(t, now):
                                new_keys.append(t["key"])
                            self._merge_doc_dirs(t["key"], found_dirs, "done", "")
                        elif t["key"] in cand_keys:
                            pass                          # не релевантна — в базу не пишем
                        else:
                            self._merge_doc_dirs(t["key"], found_dirs, "done", t.get("_docs_note", ""))
                    except Exception as e:  # noqa: BLE001
                        if t["key"] not in cand_keys:
                            self._merge_doc_dirs(t["key"], {}, "error", str(e)[:200])
                        log.debug("документация %s: %s", t["key"], e)
                    P.tick()

        # ---------- 5. планы закупок ----------
        plans_n = 0
        if modes.get("plans") and src.get("eis"):
            pl = S["plans"]
            plan_items: list[dict] = []
            if modes.get("customers"):
                P.set("Планы закупок заказчиков", len(customers))
                for c in customers:
                    if not c.get("inn") or http.cancel.is_set() or len(errors.get("ЕИС (планы)", [])) >= 2:
                        P.tick()
                        continue
                    try:
                        for p in eis.plan_numbers(c["inn"]):
                            if (p["to"] and p["to"] < today) or (p["customer_inn"] and p["customer_inn"] != c["inn"]):
                                continue
                            pos = eis.plan_positions(p["number"], p["law"], int(pl.get("max_pages", 60)), p["number"])
                            for t in pos:
                                t["customer"], t["customer_inn"], t["watch_id"], t["mode"] = \
                                    p["customer"], p["customer_inn"], c["id"], "customer"
                            plan_items += pos
                            P.log(f"План {p['number']} ({c['name']}): позиций {len(pos)}")
                    except Exception as e:  # noqa: BLE001
                        err("ЕИС (планы)", e)
                    P.tick()
            plan_fails = len(errors.get("ЕИС (планы)", []))
            if modes.get("market") and queries and plan_fails < 2:
                P.set("Планы закупок: поиск по рынку", len(queries))
                for qtext in queries:
                    if http.cancel.is_set():
                        break
                    if plan_fails >= 2:
                        P.log("ЕИС: поиск по планам закупок сейчас не отвечает (сбой на стороне ЕИС) — "
                              "пропускаю планы до следующего запуска")
                        break
                    try:
                        pos = eis.plan_positions(qtext, None, int(pl.get("market_pages", 2)))
                        for t in pos:
                            t["mode"], t["query"] = "market", qtext
                        plan_items += pos
                        plan_fails = 0
                    except Exception as e:  # noqa: BLE001
                        plan_fails += 1
                        err("ЕИС (планы)", e)
                    P.tick()
            elif modes.get("market") and queries:
                P.log("ЕИС: поиск по планам закупок сейчас не отвечает (сбой на стороне ЕИС) — пропускаю до следующего запуска")
            seen = set()
            for t in plan_items:
                if t["key"] in seen or not plan_month_future(t.get("plan_month", ""), today):
                    continue
                seen.add(t["key"])
                t["directions"], t["confidence"] = clf.classify(t["title"], [])
                if not t["directions"]:
                    continue
                if not t.get("customer"):
                    t["customer"], t["customer_inn"] = eis.plan_customer((t.get("extra") or {}).get("plan_guid", ""))
                t["deadline"] = "план: " + (t.get("plan_month") or "")
                m = re.match(r"(\d{2})\.(\d{4})", t.get("plan_month") or "")
                t["deadline_iso"] = f"{m.group(2)}-{m.group(1)}-28" if m else "2100-01-01"
                t["active"] = 1
                t.setdefault("extra", {})["plan_month"] = t.get("plan_month", "")
                if self.store.upsert_tender(t, now):
                    new_keys.append(t["key"])
                plans_n += 1
            P.log(f"Планы: релевантных позиций {plans_n}")

        # ---------- 6. актуальность ранее найденного ----------
        for r in self.store.q("SELECT key, kind, status, deadline, published, extra FROM tenders WHERE active=1"):
            if r["kind"] == "plan":
                pm = json.loads(r["extra"] or "{}").get("plan_month", "")
                still = plan_month_future(pm, today)
            else:
                still = is_active_notice(r, today)
            if not still:
                self.store.x("UPDATE tenders SET active=0 WHERE key=?", (r["key"],))

        new_rel = self.store.q(
            f"SELECT * FROM tenders WHERE key IN ({','.join('?' * len(new_keys))}) AND directions != '{{}}'",
            new_keys) if new_keys else []
        P.summary = {
            "found": len(found), "active": len(active), "saved": len(keep), "new": len(new_keys),
            "new_relevant": len(new_rel), "docs_scanned": docs_done, "plans": plans_n,
            "errors": {k: v[:3] for k, v in errors.items()},
        }
        P.log(f"Итого: новых релевантных {len(new_rel)}; документация проверена у {docs_done}; позиций планов {plans_n}")
        first_run = self.store.kv("first_run_done") is None
        if new_rel and not first_run:
            self._notify(S, new_rel, clf)
            if self.notify_hook:
                try:
                    self.notify_hook(new_rel, clf)
                except Exception as e:  # noqa: BLE001
                    log.debug("notify_hook: %s", e)
        self.store.set_kv("first_run_done", True)
        self.store.set_kv("last_run", now)

    # ───────────────────────── вспомогательное ─────────────────────────
    def _parallel(self, jobs, err, title: str) -> None:
        """Задания разных площадок идут параллельно, внутри площадки — по очереди."""
        P = self.progress
        P.set(title, len(jobs))
        by_src: dict[str, list] = {}
        for src, fn, arg in jobs:
            by_src.setdefault(src, []).append((fn, arg))

        def worker(src, items):
            fails = 0
            for n, (fn, arg) in enumerate(items):
                if P.http and P.http.cancel.is_set():
                    return
                if fails >= 3:                       # площадка не отвечает — не тратим время до следующего запуска
                    P.log(f"{src}: {fails} ошибки подряд — пропускаю остальные {len(items) - n} запросов до следующего запуска")
                    for _ in items[n:]:
                        P.tick()
                    return
                try:
                    fn(arg) if arg is not None else fn()
                    fails = 0
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    err(src, e)
                P.tick()
        with ThreadPoolExecutor(max_workers=max(1, len(by_src))) as ex:
            for s, items in by_src.items():
                ex.submit(worker, s, items)

    def _scan_docs(self, t, eis: Eis, sber: Sber, b2b: B2B, http: Http, clf: Classifier, max_mb: int) -> dict:
        files: list[tuple[str, str]] = []
        extra = t.get("extra") or {}
        if t["source"].startswith("ЕИС") or extra.get("docs_url"):
            if not extra.get("docs_url") and extra.get("eis_number"):
                hit = [x for x in eis.search(extra["eis_number"], max_pages=1) if x["number"] == extra["eis_number"]]
                if hit:
                    extra.update({k: v for k, v in hit[0]["extra"].items() if v})
            files = eis.list_docs({"extra": extra})
        if not files and extra.get("eis_number") and not extra.get("docs_url"):
            hit = [x for x in eis.search(extra["eis_number"], max_pages=1) if x["number"] == extra["eis_number"]]
            if hit:
                extra.update({k: v for k, v in hit[0]["extra"].items() if v})
                files = eis.list_docs({"extra": extra})
        if not files and t["source"].startswith("Сбербанк"):
            files = sber.list_docs(t)
        if not files and t["source"] == "B2B-Center":
            files = b2b.list_docs(t)
        texts, notes = [], []
        self.store.x("DELETE FROM docs WHERE tender_key=?", (t["key"],))
        for name, url in files[:30]:
            ext = Path(name).suffix.lower()
            if ext in SKIP_EXT:
                continue
            try:
                data, real = http.download(url, max_mb * 1_000_000)
                name = name or real
                if real and not Path(name).suffix:
                    name = real
                text, note = extract_text(data, name or real)
                text = clean(text)
                self.store.x("INSERT INTO docs(tender_key, name, url, size, text, note) VALUES(?,?,?,?,?,?)",
                             (t["key"], name, url, len(data), text[:400_000], note))
                if text:
                    texts.append((name, text))
                elif note:
                    notes.append(f"{name}: {note}")
            except Exception as e:  # noqa: BLE001
                self.store.x("INSERT INTO docs(tender_key, name, url, size, text, note) VALUES(?,?,?,?,?,?)",
                             (t["key"], name, url, 0, "", str(e)[:200]))
                notes.append(f"{name}: {e}")
        if not files:
            notes.append("документация недоступна без входа или не опубликована")
        t["_docs_note"] = "; ".join(notes)[:400]
        return clf.classify_docs(texts) if texts else {}

    def _merge_doc_dirs(self, key: str, found_dirs: dict, state: str, note: str) -> None:
        r = self.store.q("SELECT directions FROM tenders WHERE key=?", (key,))
        if not r:
            return
        d = json.loads(r[0]["directions"] or "{}")
        for k, v in found_dirs.items():
            d[k] = list(dict.fromkeys((d.get(k) or []) + v))
        self.store.x("UPDATE tenders SET directions=?, docs_state=?, docs_note=?, confidence=CASE WHEN confidence='' "
                     "AND ?!='' THEN 'по документации' ELSE confidence END WHERE key=?",
                     (json.dumps(d, ensure_ascii=False), state, note, "x" if found_dirs else "", key))

    def _notify(self, S: dict, rows: list[dict], clf: Classifier) -> None:
        tg = S.get("telegram") or {}
        if not (tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id")):
            return
        import html as h
        parts = [f"🆕 Новые закупки ИТ/ИБ/АСУТП: {len(rows)}"]
        for r in rows[:25]:
            dirs = ", ".join(clf.name(k) for k in json.loads(r["directions"] or "{}"))
            price = f"{r['price']:,.0f}".replace(",", " ") + " ₽" if r["price"] else "—"
            parts.append(f"\n<b>[{dirs}]</b> {h.escape(r['title'][:220])}\n{h.escape((r['customer'] or '')[:80])} · "
                         f"{price} · до {r['deadline'] or '—'}\n<a href=\"{h.escape(r['url'])}\">{r['source']} № {r['number']}</a>")
        text = "\n".join(parts)
        try:
            for i in range(0, len(text), 3800):
                requests.post(f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                              data={"chat_id": tg["chat_id"], "text": text[i:i + 3800], "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"}, timeout=30)
        except Exception as e:  # noqa: BLE001
            self.progress.log(f"Telegram: {e}")
