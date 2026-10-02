# -*- coding: utf-8 -*-
"""Локальный веб-сервер приложения (только 127.0.0.1) + расписание."""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import re
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .classify import vendor_label
from .engine import Engine
from .sources import B2B
from .store import Store
from .util import Http, log

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"


class App:
    def __init__(self, data_dir: Path):
        dict_path = next((p for p in (Path(__file__).resolve().parent / "dictionaries.yaml",
                                      BASE_DIR / "dictionaries.yaml") if p.exists()), BASE_DIR / "dictionaries.yaml")
        self.on_settings_change = None
        self.platform = "desktop"
        self.store = Store(data_dir / "tenders.sqlite", dict_path)
        self.engine = Engine(self.store, data_dir)
        self.data_dir = data_dir
        if self.store.needs_reclassify:
            try:
                n = self.reclassify()
                log.info("Обновление: пересчитаны направления у %s закупок", n)
            except Exception as e:  # noqa: BLE001
                log.warning("Пересчёт после обновления: %s", e)

    # ───────── расписание ─────────
    def scheduler(self) -> None:
        while True:
            try:
                s = self.store.settings()["schedule"]
                if s.get("enabled") and not self.engine.progress.running:
                    now = dt.datetime.now()
                    if int(s.get("from_hour", 0)) <= now.hour < int(s.get("to_hour", 24)):
                        last = self.store.kv("last_run")
                        last_dt = dt.datetime.strptime(last, "%Y-%m-%d %H:%M") if last else None
                        if not last_dt or (now - last_dt).total_seconds() >= float(s.get("every_hours", 3)) * 3600:
                            log.info("Запуск по расписанию")
                            self.engine.start()
            except Exception as e:  # noqa: BLE001
                log.warning("Планировщик: %s", e)
            time.sleep(60)

    # ───────── API ─────────
    def api(self, method: str, path: str, qs: dict, body: dict):
        S = self.store
        g = lambda k, d="": (qs.get(k) or [d])[0]  # noqa: E731

        if path == "/api/state":
            counts = S.q("SELECT kind, COUNT(*) n, SUM(user_status='new') nw FROM tenders WHERE active=1 "
                         "AND directions!='{}' GROUP BY kind")
            return {"progress": self.engine.progress.snapshot(), "last_run": S.kv("last_run"), "platform": self.platform,
                    "counts": counts, "directions": S.q("SELECT * FROM directions ORDER BY sort")}

        if path == "/api/tenders":
            where = ["1=1"]
            args: list = []
            if g("active", "1") == "1":
                where.append("active=1")
            if g("relevant", "1") == "1":
                where.append("directions!='{}'")
            rows = S.q(f"SELECT key, source, number, url, title, customer, customer_inn, watch_id, mode, kind, law, "
                       f"status, published, deadline, deadline_iso, price, platform, region, positions, extra, "
                       f"directions, confidence, first_seen, last_seen, active, user_status, user_note, docs_state, "
                       f"docs_note, query FROM tenders WHERE {' AND '.join(where)} "
                       f"ORDER BY deadline_iso LIMIT 5000", args)
            for r in rows:
                for f in ("positions", "extra", "directions"):
                    r[f] = json.loads(r[f] or ("[]" if f == "positions" else "{}"))
            return {"rows": rows, "customers": S.q("SELECT id, name, inn FROM customers")}

        if path == "/api/tender":
            key = g("key")
            r = S.q("SELECT * FROM tenders WHERE key=?", (key,))
            docs = S.q("SELECT id, name, url, size, note, LENGTH(text) chars FROM docs WHERE tender_key=?", (key,))
            return {"tender": r[0] if r else None, "docs": docs}

        if path == "/api/doc":
            r = S.q("SELECT name, text, note FROM docs WHERE id=?", (int(g("id", "0")),))
            return r[0] if r else {}

        if path == "/api/search_docs":
            q = g("q").strip()
            if len(q) < 2:
                return {"rows": []}
            rows = S.q("SELECT d.tender_key key, d.id doc_id, d.name, t.title, t.source, t.number, t.url, t.deadline, "
                       "t.active, instr(lower(d.text), lower(?)) pos, substr(d.text, max(1, instr(lower(d.text), "
                       "lower(?)) - 120), 300) snippet FROM docs d JOIN tenders t ON t.key=d.tender_key "
                       "WHERE instr(lower(d.text), lower(?)) > 0 ORDER BY t.active DESC, t.deadline_iso LIMIT 300",
                       (q, q, q))
            return {"rows": rows}

        if path == "/api/tender/status" and method == "POST":
            S.x("UPDATE tenders SET user_status=COALESCE(?, user_status), user_note=COALESCE(?, user_note) WHERE key=?",
                (body.get("user_status"), body.get("user_note"), body["key"]))
            return {"ok": True}

        if path == "/api/tender/set_status" and method == "POST":
            keys = list(body.get("keys") or [])
            st = body.get("user_status")
            if st not in ("new", "viewed", "favorite", "hidden"):
                return {"error": "bad status"}
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                S.x(f"UPDATE tenders SET user_status=? WHERE key IN ({','.join('?' * len(part))})", [st] + part)
            return {"ok": True, "updated": len(keys)}
        if path == "/api/tender/mark_all_viewed" and method == "POST":
            S.x("UPDATE tenders SET user_status='viewed' WHERE user_status='new'")
            return {"ok": True}

        # заказчики
        if path == "/api/customers":
            new_id = None
            if method == "POST":
                c = body
                inn = re.sub(r"\D", "", c.get("inn") or "")
                if c.get("id"):
                    S.x("UPDATE customers SET name=?, inn=?, aliases=?, enabled=?, note=? WHERE id=?",
                        (c.get("name", ""), inn, c.get("aliases", ""), int(c.get("enabled", 1)), c.get("note", ""), c["id"]))
                    new_id = c["id"]
                else:
                    dup = S.q("SELECT id FROM customers WHERE inn!='' AND inn=?", (inn,)) if inn else []
                    if dup:
                        new_id = dup[0]["id"]
                    else:
                        new_id = S.x("INSERT INTO customers(name, inn, aliases, enabled, note) VALUES(?,?,?,?,?)",
                                     (c.get("name", ""), inn, c.get("aliases", ""), int(c.get("enabled", 1)),
                                      c.get("note", "")))
                    # привязываем уже найденные закупки этого заказчика
                    if inn:
                        S.x("UPDATE tenders SET watch_id=? WHERE customer_inn=? AND watch_id IS NULL", (new_id, inn))
            return {"rows": S.q("SELECT * FROM customers ORDER BY id"), "id": new_id}
        if path == "/api/customers/delete" and method == "POST":
            S.x("DELETE FROM customers WHERE id=?", (body["id"],))
            return {"ok": True}
        if path == "/api/lookup_inn":
            return self.lookup_inn(re.sub(r"\D", "", g("inn")))

        # поисковые фразы (режим «по рынку»)
        if path == "/api/queries":
            if method == "POST":
                if body.get("id"):
                    S.x("UPDATE queries SET text=COALESCE(?, text), enabled=COALESCE(?, enabled) WHERE id=?",
                        (body.get("text"), body.get("enabled"), body["id"]))
                else:
                    for t in [x.strip() for x in str(body.get("text", "")).split("\n") if x.strip()]:
                        if not S.q("SELECT 1 FROM queries WHERE lower(text)=lower(?)", (t,)):
                            S.x("INSERT INTO queries(text) VALUES(?)", (t,))
            return {"rows": S.q("SELECT * FROM queries ORDER BY id")}
        if path == "/api/queries/delete" and method == "POST":
            S.x("DELETE FROM queries WHERE id=?", (body["id"],))
            return {"ok": True}

        # состав направлений: направления → разделы → признаки
        if path == "/api/overview":
            dirs = S.q("SELECT * FROM directions ORDER BY sort")
            secs = S.q("SELECT * FROM sections ORDER BY direction, sort, id")
            cnt = {(r["direction"], r["section"], r["kind"]): r["n"] for r in S.q(
                "SELECT direction, section, kind, COUNT(*) n FROM terms WHERE enabled=1 GROUP BY 1,2,3")}
            samples: dict = {}
            limits = {"vendor": 9, "okpd": 3, "keyword": 8}
            for r in S.q("SELECT direction, section, kind, grp, value FROM terms WHERE enabled=1 AND kind!='exclude' "
                         "ORDER BY user DESC, CASE kind WHEN 'vendor' THEN 0 WHEN 'okpd' THEN 1 ELSE 2 END, id"):
                key = (r["direction"], r["section"])
                per = samples.setdefault(("_n",) + key, {})
                if per.get(r["kind"], 0) >= limits.get(r["kind"], 8):
                    continue
                lst = samples.setdefault(key, [])
                if r["kind"] == "vendor":
                    label = vendor_label(r["grp"] or "", r["value"])
                    label = label.split(" (")[0] if "(" in label and not label.startswith("(") else label
                elif r["kind"] == "okpd":
                    label = "ОКПД2 " + r["value"]
                else:
                    v = r["value"]
                    label = v + "…" if (any(ch.islower() for ch in v) and len(v) > 4 and v[-1].isalpha()
                                        and not v.startswith("re:")) else v
                if not label.startswith("re:") and label not in lst:
                    lst.append(label)
                    per[r["kind"]] = per.get(r["kind"], 0) + 1
            for d in dirs:
                d["sections"] = [dict(x, counts={k: cnt.get((d["code"], x["name"], k), 0)
                                                 for k in ("vendor", "keyword", "okpd")},
                                      samples=samples.get((d["code"], x["name"]), []))
                                 for x in secs if x["direction"] == d["code"]]
                d["excludes"] = cnt.get((d["code"], "", "exclude"), 0) + cnt.get((d["code"], None, "exclude"), 0)
            return {"directions": dirs}
        if path == "/api/terms":
            if method == "POST":
                vals = [v.strip() for v in str(body.get("value", "")).split("\n") if v.strip()]
                kind = body["kind"]
                sec = "" if kind == "exclude" else (body.get("section") or "").strip()
                if kind != "exclude" and not sec:           # раздел не указан — в первый раздел направления
                    first = S.q("SELECT name FROM sections WHERE direction=? ORDER BY sort, id LIMIT 1", (body["direction"],))
                    if first:
                        sec = first[0]["name"]
                    else:
                        sec = "Основное"
                        S.x("INSERT INTO sections(direction, name, sort) VALUES(?,?,0)", (body["direction"], sec))
                for v in vals:
                    grp = body.get("grp", "").strip() if kind == "vendor" else ""
                    if not S.q("SELECT 1 FROM terms WHERE direction=? AND kind=? AND lower(value)=lower(?) AND "
                               "COALESCE(section,'')=?", (body["direction"], kind, v, sec)):
                        S.x("INSERT INTO terms(direction, kind, grp, value, enabled, user, section) VALUES(?,?,?,?,1,1,?)",
                            (body["direction"], kind, grp or (v if kind == "vendor" else ""), v, sec))
                return {"ok": True, "added": len(vals)}
            d = g("direction")
            return {"rows": S.q("SELECT * FROM terms WHERE direction=? ORDER BY kind, grp, id", (d,)),
                    "sections": S.q("SELECT * FROM sections WHERE direction=? ORDER BY sort, id", (d,)),
                    "directions": S.q("SELECT * FROM directions ORDER BY sort")}
        if path == "/api/terms/update" and method == "POST":
            for f in ("enabled", "section", "value", "grp"):
                if f in body:
                    S.x(f"UPDATE terms SET {f}=? WHERE id=?", (body[f], body["id"]))
            return {"ok": True}
        if path == "/api/terms/delete" and method == "POST":
            S.x("DELETE FROM terms WHERE id=?", (body["id"],))
            return {"ok": True}
        if path == "/api/terms/test" and method == "POST":
            clf = S.classifier()
            dirs, conf = clf.classify(body.get("text", ""), [])
            return {"directions": {clf.name(k): v for k, v in dirs.items()}, "confidence": conf}
        if path == "/api/terms/reclassify" and method == "POST":
            return {"updated": self.reclassify()}
        if path == "/api/sections" and method == "POST":
            if body.get("delete"):
                sec = S.q("SELECT * FROM sections WHERE id=?", (body["id"],))
                if sec:
                    sec = sec[0]
                    if body.get("move_to"):
                        S.x("UPDATE terms SET section=? WHERE direction=? AND section=?",
                            (body["move_to"], sec["direction"], sec["name"]))
                    else:
                        S.x("DELETE FROM terms WHERE direction=? AND section=?", (sec["direction"], sec["name"]))
                    S.x("DELETE FROM sections WHERE id=?", (body["id"],))
            elif body.get("id"):
                old = S.q("SELECT * FROM sections WHERE id=?", (body["id"],))[0]
                name = (body.get("name") or old["name"]).strip()
                if name != old["name"]:
                    S.x("UPDATE terms SET section=? WHERE direction=? AND section=?", (name, old["direction"], old["name"]))
                S.x("UPDATE sections SET name=?, description=?, enabled=? WHERE id=?",
                    (name, body.get("description", old["description"]), int(body.get("enabled", old["enabled"])),
                     body["id"]))
            else:
                n = S.q("SELECT COUNT(*) n FROM sections WHERE direction=?", (body["direction"],))[0]["n"]
                S.x("INSERT INTO sections(direction, name, description, sort) VALUES(?,?,?,?)",
                    (body["direction"], body["name"].strip(), body.get("description", ""), n))
            return {"ok": True}
        if path == "/api/directions" and method == "POST":
            if body.get("delete"):
                S.x("DELETE FROM directions WHERE code=?", (body["code"],))
                S.x("DELETE FROM terms WHERE direction=?", (body["code"],))
                S.x("DELETE FROM sections WHERE direction=?", (body["code"],))
            elif body.get("code") and S.q("SELECT 1 FROM directions WHERE code=?", (body["code"],)):
                old = S.q("SELECT * FROM directions WHERE code=?", (body["code"],))[0]
                S.x("UPDATE directions SET name=?, description=?, enabled=? WHERE code=?",
                    (body.get("name", old["name"]), body.get("description", old["description"]),
                     int(body.get("enabled", old["enabled"])), body["code"]))
            else:
                code = re.sub(r"\W", "", body.get("code") or body.get("name", "")).upper()[:20] or "DIR"
                n = S.q("SELECT COUNT(*) n FROM directions")[0]["n"]
                S.x("INSERT INTO directions(code, name, enabled, sort, description) VALUES(?,?,1,?,?)",
                    (code, body.get("name", code), n, body.get("description", "")))
                S.x("INSERT INTO sections(direction, name, description, sort) VALUES(?,?,?,0)",
                    (code, "Основное", "Признаки направления «" + body.get("name", code) + "»"))
            return {"rows": S.q("SELECT * FROM directions ORDER BY sort")}

        # настройки и учётки
        if path == "/api/settings":
            if method == "POST":
                cur = S.settings()
                for k, v in body.items():
                    if isinstance(v, dict) and isinstance(cur.get(k), dict):
                        cur[k].update(v)
                    else:
                        cur[k] = v
                S.save_settings(cur)
                if self.on_settings_change:
                    try:
                        self.on_settings_change(S.settings())
                    except Exception as e:  # noqa: BLE001
                        log.debug("on_settings_change: %s", e)
            return S.settings()
        if path == "/api/credentials":
            if method == "POST":
                S.set_credentials(body["platform"], body.get("login", ""), body.get("password") or None)
                if body.get("clear"):
                    S.x("DELETE FROM credentials WHERE platform=?", (body["platform"],))
            return S.credentials_public()
        if path == "/api/credentials/test" and method == "POST":
            if body.get("platform") == "b2b":
                login, pwd = S.get_credentials("b2b")
                net = S.settings()["network"]
                http = Http(net["request_delay"], net["timeout"], net.get("proxy", ""), net.get("ssl_verify", True),
                            ca_dir=self.data_dir)
                return {"result": B2B(http).diagnose(login, pwd)}
            return {"result": "Для этой площадки проверка входа пока не реализована"}

        # запуск
        if path == "/api/run" and method == "POST":
            return {"started": self.engine.start(body or {})}
        if path == "/api/stop" and method == "POST":
            self.engine.stop()
            return {"ok": True}
        if path == "/api/runs":
            return {"rows": S.q("SELECT id, started, finished, status, summary FROM runs ORDER BY id DESC LIMIT 30")}
        if path == "/api/run_log":
            r = S.q("SELECT log FROM runs WHERE id=?", (int(g("id", "0")),))
            return {"log": r[0]["log"] if r else ""}
        return None

    def lookup_inn(self, inn: str) -> dict:
        """Название организации по ИНН: из ЕИС (карточки закупок и планов), затем со Сбербанк-АСТ."""
        if len(inn) not in (10, 12):
            return {"ok": False, "message": "ИНН — 10 или 12 цифр"}
        from .sources import Eis, Sber
        net = self.store.settings()["network"]
        http = Http(min(1.0, float(net.get("request_delay", 1.5))), 25, net.get("proxy", ""),
                    net.get("ssl_verify", True), ca_dir=self.data_dir)
        known = self.store.q("SELECT customer FROM tenders WHERE customer_inn=? AND customer!='' LIMIT 1", (inn,))
        if known:
            return {"ok": True, "name": known[0]["customer"], "source": "уже найденные закупки"}
        eis = Eis(http)
        try:
            for t in Eis.parse_results(http.get(eis.SEARCH, params={
                    "searchString": inn, "morphology": "on", "fz44": "on", "fz223": "on", "ppRf615": "on",
                    "recordsPerPage": "_20"}).text):
                if t["customer_inn"] == inn and t["customer"]:
                    return {"ok": True, "name": t["customer"], "source": "ЕИС"}
            for p in eis.plan_numbers(inn):
                if p["customer_inn"] == inn and p["customer"]:
                    return {"ok": True, "name": p["customer"], "source": "ЕИС, планы закупок"}
        except Exception as e:  # noqa: BLE001
            log.debug("lookup_inn ЕИС: %s", e)
        return {"ok": False, "message": "Название по ИНН не найдено — введите его вручную"}

    def reclassify(self) -> int:
        """Пересчитать направления у сохранённых закупок после правки критериев."""
        from .classify import Classifier
        S = self.store
        clf = S.classifier()
        n = 0
        for r in S.q("SELECT key, title, positions, directions, mode FROM tenders"):
            old = json.loads(r["directions"] or "{}")
            dirs, conf = clf.classify(r["title"], json.loads(r["positions"] or "[]"))
            texts = [(d["name"], d["text"]) for d in S.q("SELECT name, text FROM docs WHERE tender_key=?", (r["key"],))
                     if d["text"]]
            if texts:
                for k, v in clf.classify_docs(texts).items():
                    dirs[k] = list(dict.fromkeys((dirs.get(k) or []) + v))
                    conf = conf or "по документации"
            if not dirs and r["mode"] == "market":        # на рынке храним только релевантные
                S.x("DELETE FROM tenders WHERE key=?", (r["key"],))
                S.x("DELETE FROM docs WHERE tender_key=?", (r["key"],))
                n += 1
                continue
            if dirs != old:
                S.x("UPDATE tenders SET directions=?, confidence=? WHERE key=?",
                    (json.dumps(dirs, ensure_ascii=False), conf, r["key"]))
                n += 1
        return n

    def export_xlsx(self, qs: dict) -> bytes:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        keys = set(json.loads((qs.get("keys") or ["[]"])[0]))
        dirs = {d["code"]: d["name"] for d in self.store.q("SELECT * FROM directions")}
        rows = self.store.q("SELECT * FROM tenders WHERE active=1 ORDER BY deadline_iso")
        if keys:
            rows = [r for r in rows if r["key"] in keys]
        wb = Workbook()
        ws = wb.active
        ws.title = "Закупки"
        heads = ["Статус", "Направления", "Совпадения", "Источник", "Номер", "Тип", "Предмет", "Позиции", "Заказчик",
                 "ИНН", "НМЦ, ₽", "Размещено", "Подача до", "Площадка", "Режим", "Заметка", "Ссылка"]
        ws.append(heads)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="2F4A6D")
        st = {"new": "новая", "viewed": "просмотрена", "favorite": "в работе", "hidden": "скрыта"}
        for r in rows:
            d = json.loads(r["directions"] or "{}")
            ws.append([st.get(r["user_status"], r["user_status"]), ", ".join(dirs.get(k, k) for k in d),
                       "\n".join(f"{dirs.get(k, k)}: {', '.join(v)}" for k, v in d.items()), r["source"], r["number"],
                       "план" if r["kind"] == "plan" else "закупка", r["title"],
                       "\n".join(json.loads(r["positions"] or "[]")[:10]), r["customer"], r["customer_inn"], r["price"],
                       r["published"], r["deadline"], r["platform"], "заказчик" if r["mode"] == "customer" else "рынок",
                       r["user_note"], r["url"]])
            ws.cell(ws.max_row, 17).hyperlink = r["url"]
        for i, w in enumerate([11, 14, 40, 14, 22, 9, 60, 50, 40, 13, 16, 16, 18, 22, 10, 25, 40], 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = w
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()


def make_handler(app: App, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # тише в консоли
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _guard(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:           # защита от DNS rebinding
                self._send(403, b"forbidden", "text/plain")
                return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/export.xlsx":
                data = app.export_xlsx(parse_qs(u.query))
                name = f"zakupki_{dt.date.today().isoformat()}.xlsx"
                return self._send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            self._api("GET", u, {})

        def do_POST(self):
            if not self._guard():
                return
            if self.headers.get("X-App") != "tender":                   # защита от запросов с чужих сайтов
                return self._send(403, b"forbidden", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                body = {}
            self._api("POST", urlparse(self.path), body)

        def _api(self, method, u, body):
            try:
                res = app.api(method, u.path, parse_qs(u.query), body)
                if res is None:
                    return self._send(404, b'{"error":"not found"}', "application/json")
                self._send(200, json.dumps(res, ensure_ascii=False, default=str).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                log.error("API %s: %s\n%s", u.path, e, traceback.format_exc())
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
    return H


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Мониторинг закупок ИТ/ИБ/АСУТП — локальное приложение")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--data", default=str(BASE_DIR / "data"))
    ap.add_argument("--run-once", action="store_true", help="выполнить поиск без интерфейса и выйти")
    a = ap.parse_args(argv)
    data = Path(a.data)
    data.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    fh = logging.FileHandler(data / "app.log", mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    for noisy in ("urllib3", "charset_normalizer", "pypdf"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    app = App(data)
    if a.run_once:
        app.engine.start()
        while app.engine.progress.running:
            time.sleep(2)
        print(json.dumps(app.engine.progress.summary, ensure_ascii=False, indent=1))
        return 0
    port = a.port or int(app.store.settings()["ui"].get("port", 8765))
    srv = None
    for p in range(port, port + 10):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app, p))
            port = p
            break
        except OSError:
            continue
    if not srv:
        print("Не удалось занять порт для интерфейса")
        return 1
    threading.Thread(target=app.scheduler, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Мониторинг закупок запущен: {url}\n  Не закрывайте это окно, пока пользуетесь приложением.\n")
    if not a.no_browser and app.store.settings()["ui"].get("open_browser", True):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
