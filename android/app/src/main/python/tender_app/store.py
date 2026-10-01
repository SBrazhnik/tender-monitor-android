# -*- coding: utf-8 -*-
"""Хранилище SQLite: настройки, заказчики, критерии, закупки, документы, учётки площадок."""
from __future__ import annotations

import base64
import copy
import datetime as dt
import json
import sqlite3
import sys
import threading
from pathlib import Path

from .sections import DIRECTION_INFO, SECTIONS, assign_section
from .util import load_yaml, log

DEFAULT_SETTINGS = {
    "sources": {"eis": True, "sber": True, "b2b": True, "rosatom": True},
    "modes": {"customers": True, "market": True, "plans": True},
    "market": {"min_price": 0, "days_back": 14, "max_pages": 3},
    "plans": {"max_pages": 60, "market_pages": 2},
    "docs": {"enabled": True, "max_tenders_per_run": 60, "max_file_mb": 25, "customers_all": True},
    "schedule": {"enabled": False, "every_hours": 3, "from_hour": 8, "to_hour": 20},
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "network": {"request_delay": 1.5, "timeout": 40, "proxy": "", "ssl_verify": True},
    "ui": {"port": 8765, "open_browser": True},
}

DEFAULT_CUSTOMERS = [
    {"name": "АО «Росатом Инфраструктурные решения» (АО «РИР»)", "inn": "7706757331",
     "aliases": 'АО "РИР"\nАО «РИР»\nАО "Росатом Инфраструктурные решения"'},
    {"name": "АО «РИР Энерго»", "inn": "6829012680", "aliases": 'РИР Энерго\nАО "РИР Энерго"'},
]

DEFAULT_QUERIES = [
    # ИТ
    "Astra Linux", "РЕД ОС", "Альт Линукс", "операционная система", "программное обеспечение",
    "лицензии", "право использования программ", "сервер", "СХД", "система хранения данных",
    "коммутатор", "маршрутизатор", "Wi-Fi", "СКС", "ЛВС", "персональный компьютер", "ноутбук",
    "моноблок", "автоматизированное рабочее место", "МФУ", "принтер", "оргтехника",
    "виртуализация", "резервное копирование", "ЦОД", "1С", "информационная система",
    "техническая поддержка программного обеспечения", "видеоконференцсвязь", "IP-телефония", "АТС",
    # ИБ
    "информационная безопасность", "защита информации", "СЗИ", "SIEM", "межсетевой экран", "NGFW",
    "антивирус", "Kaspersky", "DLP", "VPN", "ViPNet", "Континент", "КИИ", "аттестация объектов информатизации",
    "криптографической защиты", "Positive Technologies",
    # АСУТП
    "АСУ ТП", "SCADA", "ПЛК", "программируемый логический контроллер", "контроллер", "ПТК",
    "телемеханика", "АИИС КУЭ", "АСКУЭ", "КИПиА", "контрольно-измерительные приборы",
    "релейная защита", "датчик давления", "расходомер", "уровнемер", "газоанализатор",
    "система автоматического контроля", "противоаварийная защита",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, name TEXT, inn TEXT, aliases TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS queries(id INTEGER PRIMARY KEY, text TEXT, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS directions(code TEXT PRIMARY KEY, name TEXT, enabled INTEGER DEFAULT 1, sort INTEGER);
CREATE TABLE IF NOT EXISTS terms(id INTEGER PRIMARY KEY, direction TEXT, kind TEXT, grp TEXT DEFAULT '',
    value TEXT, enabled INTEGER DEFAULT 1, user INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS terms_dir ON terms(direction, kind);
CREATE TABLE IF NOT EXISTS tenders(key TEXT PRIMARY KEY, source TEXT, number TEXT, url TEXT, title TEXT,
    customer TEXT DEFAULT '', customer_inn TEXT DEFAULT '', watch_id INTEGER, mode TEXT, kind TEXT DEFAULT 'notice',
    law TEXT DEFAULT '', status TEXT DEFAULT '', published TEXT DEFAULT '', deadline TEXT DEFAULT '',
    deadline_iso TEXT DEFAULT '', price REAL, platform TEXT DEFAULT '', region TEXT DEFAULT '',
    positions TEXT DEFAULT '[]', extra TEXT DEFAULT '{}', directions TEXT DEFAULT '{}', confidence TEXT DEFAULT '',
    first_seen TEXT, last_seen TEXT, active INTEGER DEFAULT 1, user_status TEXT DEFAULT 'new',
    user_note TEXT DEFAULT '', docs_state TEXT DEFAULT 'none', docs_note TEXT DEFAULT '', query TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS tenders_active ON tenders(active, kind);
CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, tender_key TEXT, name TEXT, url TEXT, size INTEGER,
    text TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS docs_tender ON docs(tender_key);
CREATE TABLE IF NOT EXISTS credentials(platform TEXT PRIMARY KEY, login TEXT, secret BLOB, updated TEXT);
CREATE TABLE IF NOT EXISTS sections(id INTEGER PRIMARY KEY, direction TEXT, name TEXT, description TEXT DEFAULT '',
    sort INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started TEXT, finished TEXT, status TEXT,
    summary TEXT DEFAULT '{}', log TEXT DEFAULT '');
"""


# ───────────────────────── шифрование паролей (Windows DPAPI) ─────────────────────────
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out))  # UI_FORBIDDEN
    if not ok:
        raise OSError("DPAPI: не удалось " + ("зашифровать" if protect else "расшифровать"))
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect(secret: str) -> bytes:
    raw = secret.encode("utf-8")
    if sys.platform == "win32":
        return b"DPAPI" + _dpapi(raw, True)
    return b"B64" + base64.b64encode(raw)          # не Windows: только обфускация


def unprotect(blob: bytes | None) -> str:
    if not blob:
        return ""
    blob = bytes(blob)
    if blob.startswith(b"DPAPI"):
        return _dpapi(blob[5:], False).decode("utf-8")
    if blob.startswith(b"B64"):
        return base64.b64decode(blob[3:]).decode("utf-8")
    return ""


# ───────────────────────── база ─────────────────────────
class Store:
    def __init__(self, path: Path, default_dict: Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.needs_reclassify = False
        self._migrate()
        self._seed(default_dict)
        self._ensure_sections()
        self._upgrade_data()

    # -- общее
    def q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> int:
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    def many(self, sql: str, rows) -> None:
        with self._lock:
            self.db.executemany(sql, rows)
            self.db.commit()

    # -- обновление структуры базы прежних версий
    def _migrate(self) -> None:
        cols = {r["name"] for r in self.q("PRAGMA table_info(terms)")}
        if "section" not in cols:
            self.x("ALTER TABLE terms ADD COLUMN section TEXT DEFAULT ''")
        cols = {r["name"] for r in self.q("PRAGMA table_info(directions)")}
        if "description" not in cols:
            self.x("ALTER TABLE directions ADD COLUMN description TEXT DEFAULT ''")

    def _ensure_sections(self) -> None:
        """Разделы по умолчанию и раскладка признаков, у которых раздела ещё нет."""
        with self._lock:
            for code, info in DIRECTION_INFO.items():
                self.db.execute("UPDATE directions SET description=? WHERE code=? AND (description IS NULL OR "
                                "description='')", (info, code))
            for d in self.q("SELECT code FROM directions"):
                code = d["code"]
                have = {r["name"] for r in self.q("SELECT name FROM sections WHERE direction=?", (code,))}
                if not have and code in SECTIONS:
                    for i, (name, desc) in enumerate(SECTIONS[code]):
                        self.db.execute("INSERT INTO sections(direction, name, description, sort) VALUES(?,?,?,?)",
                                        (code, name, desc, i))
            rows = self.q("SELECT id, direction, kind, grp, value FROM terms WHERE (section IS NULL OR section='') "
                          "AND kind!='exclude'")
            upd = []
            for r in rows:
                sec = assign_section(r["direction"], r["kind"], r["grp"] or "", r["value"])
                if not sec:
                    first = self.q("SELECT name FROM sections WHERE direction=? ORDER BY sort LIMIT 1", (r["direction"],))
                    sec = first[0]["name"] if first else "Общее"
                    if not first:
                        self.db.execute("INSERT INTO sections(direction, name, sort) VALUES(?,?,0)", (r["direction"], sec))
                upd.append((sec, r["id"]))
            self.db.executemany("UPDATE terms SET section=? WHERE id=?", upd)
            self.db.commit()

    # -- исправления данных между версиями
    DICT_PATCHES = [
        # (направление, вид, старое значение → новое значение | None=удалить)
        ("ASUTP", "keyword", "анализатор",
         "re:(?<![\\w-])анализатор\\w*\\s+(кислород|влажн|водород|газ|жидкост|растворенн|солесодерж|кремни|натри|"
         "фосфат|мутност|электропроводн|pH|рН|концентрац|механическ|нефтепродукт|хлор|аммиак|жесткост)"),
    ]
    DICT_ADD = [
        ("ASUTP", "exclude", "реагент"),
        ("ASUTP", "exclude", "re:ПТК\\s*-\\s*\\d"),
        ("ASUTP", "exclude", "гематологическ"),
        ("ASUTP", "exclude", "биохимическ"),
        ("ASUTP", "exclude", "иммунохимическ"),
        ("ASUTP", "exclude", "мочи"),
        ("IB", "exclude", "re:SIEM\\s*ENS"),
    ]

    def _upgrade_data(self) -> None:
        ver = self.kv("data_version", 1)
        if ver < 2:
            # 1) документация, разобранная прежней версией, давала ложные «ИТ» (служебные строки .doc,
            #    шаблоны договоров) — сбрасываем её, при следующем поиске она скачается и разберётся заново
            with self._lock:
                self.db.execute("DELETE FROM docs")
                rows = self.db.execute("SELECT key, directions, mode, confidence FROM tenders").fetchall()
                for r in rows:
                    d = json.loads(r["directions"] or "{}")
                    nd = {k: [h for h in v if not str(h).startswith("док:")] for k, v in d.items()}
                    nd = {k: v for k, v in nd.items() if v}
                    if not nd and r["mode"] == "market":
                        self.db.execute("DELETE FROM tenders WHERE key=?", (r["key"],))
                        continue
                    conf = r["confidence"] if nd and r["confidence"] != "по документации" else ("средняя" if nd else "")
                    self.db.execute("UPDATE tenders SET directions=?, confidence=?, docs_state='none', docs_note='' "
                                    "WHERE key=?", (json.dumps(nd, ensure_ascii=False), conf, r["key"]))
                # 2) поправки словаря (только стандартные признаки, свои правки пользователя не трогаем)
                for d, kind, old, new in self.DICT_PATCHES:
                    if new is None:
                        self.db.execute("DELETE FROM terms WHERE direction=? AND kind=? AND value=? AND user=0", (d, kind, old))
                    else:
                        self.db.execute("UPDATE terms SET value=? WHERE direction=? AND kind=? AND value=? AND user=0",
                                        (new, d, kind, old))
                for d, kind, val in self.DICT_ADD:
                    if not self.db.execute("SELECT 1 FROM terms WHERE direction=? AND kind=? AND value=?",
                                           (d, kind, val)).fetchone():
                        self.db.execute("INSERT INTO terms(direction, kind, grp, value, section) VALUES(?,?,?,?, '')",
                                        (d, kind, "", val))
                self.db.commit()
            self.set_kv("data_version", 2)
            self.needs_reclassify = True

    # -- первичное заполнение
    def _seed(self, default_dict: Path | None) -> None:
        if not self.q("SELECT 1 FROM settings WHERE key='main'"):
            self.x("INSERT INTO settings VALUES('main', ?)", (json.dumps(DEFAULT_SETTINGS, ensure_ascii=False),))
        if not self.q("SELECT 1 FROM customers LIMIT 1") and not self.q("SELECT 1 FROM settings WHERE key='seeded_customers'"):
            for c in DEFAULT_CUSTOMERS:
                self.x("INSERT INTO customers(name, inn, aliases) VALUES(?,?,?)", (c["name"], c["inn"], c["aliases"]))
            self.x("INSERT OR REPLACE INTO settings VALUES('seeded_customers','1')")
        if not self.q("SELECT 1 FROM queries LIMIT 1") and not self.q("SELECT 1 FROM settings WHERE key='seeded_queries'"):
            self.many("INSERT INTO queries(text) VALUES(?)", [(t,) for t in DEFAULT_QUERIES])
            self.x("INSERT OR REPLACE INTO settings VALUES('seeded_queries','1')")
        if not self.q("SELECT 1 FROM directions LIMIT 1") and default_dict and Path(default_dict).exists():
            self.import_dictionary(Path(default_dict))

    def import_dictionary(self, path: Path, replace: bool = False) -> int:
        data = load_yaml(path) or {}
        rows = []
        with self._lock:
            if replace:
                self.db.execute("DELETE FROM terms WHERE user=0")
            for i, (code, d) in enumerate((data.get("directions") or {}).items()):
                self.db.execute("INSERT OR IGNORE INTO directions(code, name, enabled, sort, description) "
                                "VALUES(?,?,1,?,?)", (code, d.get("name", code), i, DIRECTION_INFO.get(code, "")))
                for v in d.get("okpd2") or []:
                    rows.append((code, "okpd", "", str(v)))
                for v in d.get("keywords") or []:
                    rows.append((code, "keyword", "", str(v)))
                for grp, aliases in (d.get("vendors") or {}).items():
                    for a in dict.fromkeys(aliases or []):
                        rows.append((code, "vendor", str(grp), str(a)))
                for v in d.get("exclude") or []:
                    rows.append((code, "exclude", "", str(v)))
            self.db.executemany("INSERT INTO terms(direction, kind, grp, value) VALUES(?,?,?,?)", rows)
            self.db.commit()
        return len(rows)

    def classifier(self):
        from .classify import Classifier
        return Classifier(self.q("SELECT * FROM directions"), self.q("SELECT * FROM terms"),
                          self.q("SELECT * FROM sections"))

    # -- настройки
    def settings(self) -> dict:
        row = self.q("SELECT value FROM settings WHERE key='main'")
        cur = json.loads(row[0]["value"]) if row else {}
        merged = copy.deepcopy(DEFAULT_SETTINGS)
        for k, v in cur.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return merged

    def save_settings(self, s: dict) -> None:
        self.x("INSERT OR REPLACE INTO settings VALUES('main', ?)", (json.dumps(s, ensure_ascii=False),))

    def kv(self, key: str, default=None):
        r = self.q("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def set_kv(self, key: str, value) -> None:
        self.x("INSERT OR REPLACE INTO settings VALUES(?, ?)", (key, json.dumps(value, ensure_ascii=False)))

    # -- учётки
    def set_credentials(self, platform: str, login: str, password: str | None) -> None:
        old = self.q("SELECT secret FROM credentials WHERE platform=?", (platform,))
        secret = protect(password) if password else (old[0]["secret"] if old else None)
        self.x("INSERT OR REPLACE INTO credentials VALUES(?,?,?,?)",
               (platform, login, secret, dt.datetime.now().strftime("%Y-%m-%d %H:%M")))

    def get_credentials(self, platform: str) -> tuple[str, str]:
        r = self.q("SELECT login, secret FROM credentials WHERE platform=?", (platform,))
        if not r or not r[0]["login"]:
            return "", ""
        try:
            return r[0]["login"], unprotect(r[0]["secret"])
        except Exception as e:  # noqa: BLE001
            log.warning("Пароль %s не расшифрован: %s", platform, e)
            return r[0]["login"], ""

    def credentials_public(self) -> dict:
        return {r["platform"]: {"login": r["login"], "has_password": bool(r["secret"]), "updated": r["updated"]}
                for r in self.q("SELECT platform, login, secret, updated FROM credentials")}

    # -- закупки
    def upsert_tender(self, t: dict, now: str) -> bool:
        """Сохраняет закупку. Возвращает True, если она новая."""
        with self._lock:
            old = self.db.execute("SELECT key, user_status, docs_state, directions, extra, watch_id, mode "
                                  "FROM tenders WHERE key=?", (t["key"],)).fetchone()
            fields = ["source", "number", "url", "title", "customer", "customer_inn", "watch_id", "mode", "kind",
                      "law", "status", "published", "deadline", "deadline_iso", "price", "platform", "region",
                      "positions", "extra", "directions", "confidence", "active", "query"]
            vals = {f: t.get(f) for f in fields}
            for f in ("positions", "extra", "directions"):
                if not isinstance(vals[f], str):
                    vals[f] = json.dumps(vals[f] if vals[f] is not None else ({} if f != "positions" else []),
                                         ensure_ascii=False)
            if old:
                if vals["watch_id"] is None and old["watch_id"]:      # привязку к заказчику не теряем
                    vals["watch_id"], vals["mode"] = old["watch_id"], old["mode"]
                # направления, найденные в документации, не теряем
                if old["docs_state"] == "done":
                    od = json.loads(old["directions"] or "{}")
                    nd = json.loads(vals["directions"])
                    for k, v in od.items():
                        if any(str(h).startswith("док:") for h in v):
                            nd.setdefault(k, [])
                            nd[k] = list(dict.fromkeys(nd[k] + [h for h in v if str(h).startswith("док:")]))
                    vals["directions"] = json.dumps(nd, ensure_ascii=False)
                oe = json.loads(old["extra"] or "{}")
                ne = json.loads(vals["extra"])
                oe.update({k: v for k, v in ne.items() if v})
                vals["extra"] = json.dumps(oe, ensure_ascii=False)
                sets = ", ".join(f"{f}=?" for f in fields)
                self.db.execute(f"UPDATE tenders SET {sets}, last_seen=? WHERE key=?",
                                [vals[f] for f in fields] + [now, t["key"]])
                self.db.commit()
                return False
            self.db.execute(f"INSERT INTO tenders({', '.join(fields)}, key, first_seen, last_seen) "
                            f"VALUES({', '.join('?' * len(fields))}, ?, ?, ?)",
                            [vals[f] for f in fields] + [t["key"], now, now])
            self.db.commit()
            return True
