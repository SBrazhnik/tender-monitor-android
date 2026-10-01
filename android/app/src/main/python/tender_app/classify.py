# -*- coding: utf-8 -*-
"""Классификатор ИТ / ИБ / АСУТП по ОКПД2, вендорам/продуктам и ключевым словам.

Правила построения шаблонов:
  * "re:..."  — регулярное выражение (без учёта регистра);
  * ключевые слова — начало слова(слов): «информационн систем» ловит «информационной системы»;
  * короткие и ЗАГЛАВНЫЕ термины (АРМ, КИП, SCADA, ASTRA) — целым словом; аббревиатуры до 8
    символов без строчных букв — с учётом регистра, чтобы «АРМ» не ловил «арматуру»;
  * вендоры/продукты — целым словом;
  * исключения — найденные фразы вырезаются из текста перед поиском по направлению.
"""
from __future__ import annotations

import re

from .util import OKPD_RE, norm


def compile_term(term: str, prefix: bool) -> re.Pattern | None:
    term = str(term or "").strip()
    if not term:
        return None
    try:
        if term.startswith("re:"):
            return re.compile(term[3:], re.IGNORECASE)
        t = norm(term)
        has_lower = any(c.islower() for c in t)
        abbrev = (not has_lower) and len(t) <= 8 and any(c.isalpha() for c in t)
        whole = (not prefix) or abbrev or len(t) <= 4
        words = [re.escape(w) for w in t.split(" ")]
        body = r"\w*\s+".join(words) if (prefix and not whole) else r"\s+".join(words)
        left = r"(?<![\w])" if t[0].isalnum() else ""
        right = r"(?![\w])" if (whole and t[-1].isalnum()) else ""
        return re.compile(left + body + right, 0 if abbrev else re.IGNORECASE)
    except re.error:
        return None


CATEGORY_GROUPS = {"СХД", "ИБП", "Печать", "Телефония и ВКС", "Радиосвязь", "Операторы связи", "Мониторинг/ITSM",
                   "Контейнеры/платформы", "Резервное копирование", "Российская виртуализация", "Антивирусы",
                   "Интеграторы ИБ", "Промышленная ИБ", "Учёт энергоресурсов", "Газоанализ/экомониторинг"}


def vendor_label(grp: str, alias: str) -> str:
    """«Astra / Группа Астра (Astra Linux)»; для групп-категорий — просто продукт: «YADRO»."""
    if not grp or grp == alias:
        return alias
    if grp in CATEGORY_GROUPS or "(" in grp or grp.startswith("Прочие") or "иностр" in grp:
        return alias
    return f"{grp} ({alias})"


class Classifier:
    """Совпадения подписываются разделом: «ПО и лицензии: Astra / Группа Астра (Astra Linux)»."""

    def __init__(self, directions: list[dict], terms: list[dict], sections: list[dict] | None = None):
        self.names = {d["code"]: d["name"] for d in directions}
        self.order = [d["code"] for d in sorted(directions, key=lambda d: d.get("sort") or 0) if d.get("enabled", 1)]
        off = {(x["direction"], x["name"]) for x in (sections or []) if not x.get("enabled", 1)}
        self.dirs: dict[str, dict] = {c: {"okpd": [], "kw": [], "vendor": [], "ex": []} for c in self.order}
        for t in terms:
            if not t.get("enabled", 1) or t["direction"] not in self.dirs:
                continue
            sec = t.get("section") or ""
            if (t["direction"], sec) in off:
                continue
            d = self.dirs[t["direction"]]
            kind, val = t["kind"], str(t["value"]).strip()
            if kind == "okpd":
                d["okpd"].append((val, sec))
            elif kind == "keyword":
                rx = compile_term(val, True)
                if rx:
                    d["kw"].append((val, rx, sec))
            elif kind == "vendor":
                rx = compile_term(val, False)
                if rx:
                    d["vendor"].append((t.get("grp") or val, val, rx, sec))
            elif kind == "exclude":
                rx = compile_term(val, True)
                if rx:
                    d["ex"].append(rx)
        for d in self.dirs.values():                 # у вендора сообщаем самый конкретный продукт
            d["vendor"].sort(key=lambda v: -len(v[1]))

    @staticmethod
    def _lab(prefix: str, sec: str, label: str) -> str:
        return f"{prefix}{sec + ' › ' if sec else ''}{label}"

    def match(self, text: str, codes: list[str] | None = None, prefix: str = "") -> dict[str, tuple[list[str], int, int]]:
        """Возвращает {направление: (совпадения, баллы, сильных признаков)}."""
        base = norm(text)
        codes = codes or []
        out: dict[str, tuple[list[str], int, int]] = {}
        for code in self.order:
            d = self.dirs[code]
            t = base
            for ex in d["ex"]:
                t = ex.sub(" ", t)
            hits: list[str] = []
            score = strong = 0
            for c in codes:
                for pref, sec in d["okpd"]:
                    if c == pref or c.startswith(pref + ".") or (len(pref) == 2 and c.startswith(pref)):
                        hits.append(self._lab(prefix, sec, f"ОКПД2 {c}"))
                        score += 3
                        strong += 1
                        break
            seen = set()
            for grp, alias, rx, sec in d["vendor"]:
                if grp in seen:
                    continue
                if rx.search(t):
                    seen.add(grp)
                    hits.append(self._lab(prefix, sec, vendor_label(grp, alias)))
                    score += 3
                    strong += 1
            kws: list[tuple[str, str]] = []
            for term, rx, sec in d["kw"]:
                m = rx.search(t)
                if m:
                    frag = m.group(0).strip()
                    if frag.lower() not in (k.lower() for k, _ in kws):
                        kws.append((frag, sec))
            if kws:
                kws = [(k, sc) for k, sc in kws if not any(k != o and k.lower() in o.lower() for o, _ in kws)]
                hits += [self._lab(prefix, sc, f"«{k}»") for k, sc in kws[:6]]
                score += len(kws)
            if hits:
                out[code] = (list(dict.fromkeys(hits)), score, strong)
        return out

    def classify(self, title: str, positions: list[str]) -> tuple[dict[str, list[str]], str]:
        codes = [m.group(1) for p in positions for m in [OKPD_RE.match(p)] if m]
        res = self.match(" | ".join([title] + list(positions)), codes)
        dirs = {k: v[0] for k, v in res.items()}
        score = max((v[1] for v in res.values()), default=0)
        conf = "" if not dirs else ("высокая" if score >= 3 else "средняя")
        return dirs, conf

    # Типовые фразы договоров и извещений, которые есть почти в любой документации
    DOC_BOILERPLATE = [re.compile(p, re.I) for p in [
        r"(усиленн\w*\s+)?(квалифицированн\w*\s+)?электронн\w*\s+подпис\w*",
        r"персональн\w*\s+данн\w*", r"152-ФЗ", r"официальн\w*\s+сайт\w*", r"сет\w*\s+[«\"]?интернет[»\"]?",
        r"информационно-телекоммуникационн\w*\s+сет\w*", r"единой\s+информационной\s+систем\w*",
        r"электронн\w*\s+площадк\w*", r"электронн\w*\s+документ\w*", r"адрес\w*\s+электронн\w*\s+почт\w*",
        r"программн\w*\s+(и\s+)?аппаратн\w*\s+средств\w*\s+электронной\s+площадки",
        r"сайт\w*\s+(заказчика|оператора|площадки)", r"www\.[\w.-]+", r"https?://\S+",
        r"коммерческ\w*\s+тайн\w*", r"конфиденциальн\w*\s+информаци\w*", r"безопасност\w*\s+(труда|дорожн)",
        r"пожарн\w*\s+безопасност\w*", r"промышленн\w*\s+безопасност\w*", r"антикоррупц\w*",
    ]]
    DOC_CODE_RE = re.compile(r"(?<![\d.])(\d{2}\.\d{2}\.\d{2}\.\d{3})(?![\d])")
    # в документации это почти всегда шаблон: форматы файлов, ЭП участника, браузер
    DOC_IGNORE = re.compile(r"^(Microsoft|Майкрософт|Windows|MS Office|Office 365|Microsoft 365|Exchange Server|Azure|"
                            r"Adobe|1С|1C|КриптоПро|CryptoPro|Крипто-Про|КриптоПро CSP|КриптоАРМ|Рутокен|Rutoken|"
                            r"Рутокен ЭЦП|Rutoken ECP|JaCarta|Компания Актив|Валидата|Сигнатура|Astra|ASTRA|Астра|"
                            r"Apple|IBM|Ростелеком|МТС|Билайн|МегаФон|Tele2)$", re.I)
    SPEC_RE = re.compile(r"техническ\w*\s+задани|\bТЗ\b|описани\w*\s+объекта\s+закупки|спецификац|"
                         r"ведомост|перечень\s+(товар|оборудован|работ|услуг)|технически\w*\s+требовани|"
                         r"характеристик\w*\s+(товар|оборудован)", re.I)

    def classify_docs(self, files) -> dict[str, list[str]]:
        """Классификация по документации. files — [(имя файла, текст)] или просто текст.

        Засчитываются: вендор/продукт (кроме шаблонных — Word, КриптоПро и т.п.) и коды ОКПД2/КТРУ
        из направления — в любом файле; ключевые слова — только в ТЗ/спецификациях и не меньше трёх
        разных. Иначе шаблонные фразы договоров дают ложные срабатывания."""
        if isinstance(files, str):
            files = [("", files)]
        strong: dict[str, list[str]] = {}
        kws: dict[str, list[str]] = {}
        for name, text in files:
            if not text:
                continue
            t = text
            for rx in self.DOC_BOILERPLATE:
                t = rx.sub(" ", t)
            is_spec = bool(self.SPEC_RE.search(name or "") or self.SPEC_RE.search(t[:1500]))
            codes = list(dict.fromkeys(self.DOC_CODE_RE.findall(t)))[:200]
            for k, (hits, score, n_strong) in self.match(t, codes, prefix="док: ").items():
                for h in hits:
                    label = h.split(" › ", 1)[-1]
                    if "«" in label:
                        if is_spec:
                            kws.setdefault(k, []).append(h)
                        continue
                    alias = label.split(" (")[-1].rstrip(")") if label.endswith(")") else label
                    if self.DOC_IGNORE.match(alias) or self.DOC_IGNORE.match(label):
                        continue
                    strong.setdefault(k, []).append(h)
        out = {}
        for k in set(strong) | set(kws):
            s_hits = list(dict.fromkeys(strong.get(k, [])))
            k_hits = list(dict.fromkeys(kws.get(k, [])))
            if s_hits or len(k_hits) >= 3:
                out[k] = (s_hits + k_hits)[:8]
        return out

    def name(self, code: str) -> str:
        return self.names.get(code, code)
