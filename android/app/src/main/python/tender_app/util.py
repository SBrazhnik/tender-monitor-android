# -*- coding: utf-8 -*-
"""Общие утилиты: YAML без зависимостей, разбор дат/цен, HTTP с сертификатами Windows."""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("tender")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

class _MiniYaml:
    @staticmethod
    def _strip_comment(line: str) -> str:
        q = None
        for i, ch in enumerate(line):
            if q:
                if q == '"' and ch == "\\":
                    continue
                if ch == q and not (q == '"' and i > 0 and line[i - 1] == "\\" and line[i - 2:i] != "\\\\"):
                    q = None
            elif ch in "\"'" and (i == 0 or line[i - 1] in " [,:-{"):
                q = ch
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                return line[:i].rstrip()
        return line.rstrip()

    @staticmethod
    def _split_top(s: str, sep: str) -> list[str]:
        out, cur, q, depth, i = [], "", None, 0, 0
        while i < len(s):
            ch = s[i]
            if q:
                cur += ch
                if q == '"' and ch == "\\" and i + 1 < len(s):
                    cur += s[i + 1]
                    i += 2
                    continue
                if ch == q:
                    if q == "'" and i + 1 < len(s) and s[i + 1] == "'":
                        cur += "'"
                        i += 2
                        continue
                    q = None
            elif ch in "\"'":
                q = ch
                cur += ch
            elif ch in "[{":
                depth += 1
                cur += ch
            elif ch in "]}":
                depth -= 1
                cur += ch
            elif ch == sep and depth == 0:
                out.append(cur)
                cur = ""
            else:
                cur += ch
            i += 1
        out.append(cur)
        return out

    @classmethod
    def _key_split(cls, s: str):
        q, i = None, 0
        while i < len(s):
            ch = s[i]
            if q:
                if q == '"' and ch == "\\":
                    i += 2
                    continue
                if ch == q:
                    q = None
            elif ch in "\"'" and i == 0:
                q = ch
            elif ch == ":" and (i + 1 == len(s) or s[i + 1] in " \t"):
                return cls._scalar(s[:i].strip()), s[i + 1:].strip()
            elif ch in "[{" and i == 0:
                return None
            i += 1
        return None

    @classmethod
    def _scalar(cls, s: str):
        s = s.strip()
        if s == "" or s in ("~", "null", "Null", "NULL"):
            return None
        if s[0] == '"' and s.endswith('"') and len(s) >= 2:
            body, out, i = s[1:-1], [], 0
            esc = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/", "0": "\0", " ": " "}
            while i < len(body):
                ch = body[i]
                if ch == "\\" and i + 1 < len(body):
                    nx = body[i + 1]
                    out.append(esc.get(nx, "\\" + nx))
                    i += 2
                    continue
                out.append(ch)
                i += 1
            return "".join(out)
        if s[0] == "'" and s.endswith("'") and len(s) >= 2:
            return s[1:-1].replace("''", "'")
        if s[0] == "[" and s.endswith("]"):
            inner = s[1:-1].strip()
            return [] if not inner else [cls._scalar(x) for x in cls._split_top(inner, ",") if x.strip()]
        if s[0] == "{" and s.endswith("}"):
            inner = s[1:-1].strip()
            d = {}
            for part in (cls._split_top(inner, ",") if inner else []):
                kv = cls._key_split(part.strip())
                if kv:
                    d[kv[0]] = cls._scalar(kv[1])
            return d
        low = s.lower()
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        if re.fullmatch(r"[-+]?\d+", s):
            return int(s)
        if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?", s):
            return float(s)
        return s

    @classmethod
    def load(cls, text: str):
        lines: list[tuple[int, str]] = []
        buf = None
        for raw in text.replace("\t", "    ").splitlines():
            if raw.strip().startswith("#") or not raw.strip() or raw.strip() == "---":
                continue
            ln = cls._strip_comment(raw)
            if not ln.strip():
                continue
            if buf is not None:                      # продолжение многострочного [ ... ]
                buf = (buf[0], buf[1] + " " + ln.strip())
                if buf[1].count("[") <= buf[1].count("]"):
                    lines.append(buf)
                    buf = None
                continue
            ind = len(ln) - len(ln.lstrip(" "))
            content = ln.strip()
            if content.count("[") > content.count("]") and ("[" in content):
                buf = (ind, content)
                continue
            lines.append((ind, content))
        if buf is not None:
            lines.append(buf)
        if not lines:
            return None
        val, _ = cls._block(lines, 0, lines[0][0])
        return val

    @classmethod
    def _is_item(cls, content: str) -> bool:
        return content == "-" or content.startswith("- ")

    @classmethod
    def _block(cls, L, i, ind):
        if cls._is_item(L[i][1]):
            return cls._list(L, i, ind)
        return cls._map(L, i, ind)

    @classmethod
    def _list(cls, L, i, ind):
        out = []
        while i < len(L) and L[i][0] == ind and cls._is_item(L[i][1]):
            rest = L[i][1][1:].strip()
            if not rest:
                if i + 1 < len(L) and L[i + 1][0] > ind:
                    v, i = cls._block(L, i + 1, L[i + 1][0])
                else:
                    v, i = None, i + 1
                out.append(v)
                continue
            kv = cls._key_split(rest)
            if kv is not None:
                sub_ind = ind + (len(L[i][1]) - len(rest))
                L = L[:i] + [(sub_ind, rest)] + L[i + 1:]
                v, i = cls._map(L, i, sub_ind)
                out.append(v)
                continue
            out.append(cls._scalar(rest))
            i += 1
        return out, i

    @classmethod
    def _map(cls, L, i, ind):
        out: dict = {}
        while i < len(L) and L[i][0] == ind and not cls._is_item(L[i][1]):
            kv = cls._key_split(L[i][1])
            if kv is None:
                raise ValueError(f"Не понимаю строку YAML: {L[i][1]!r}")
            key, rest = kv
            if rest == "":
                if i + 1 < len(L) and (L[i + 1][0] > ind or (L[i + 1][0] == ind and cls._is_item(L[i + 1][1]))):
                    v, i = cls._block(L, i + 1, L[i + 1][0])
                else:
                    v, i = None, i + 1
            else:
                v, i = cls._scalar(rest), i + 1
            out[key] = v
        return out, i


def load_yaml(path: Path):
    return _MiniYaml.load(Path(path).read_text(encoding="utf-8-sig"))




DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
OKPD_RE = re.compile(r"^\s*(\d{2}(?:\.\d{1,2}){0,3}(?:\.\d{3})?)(?![\d.])")


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("\xa0", " ").replace("ё", "е").replace("Ё", "Е")
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s: str | None) -> dt.date | None:
    m = DATE_RE.search(s or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def parse_price(s: str | None) -> float | None:
    if not s:
        return None
    s = norm(s).replace("₽", "").replace("руб", "").replace(" ", "")
    m = re.search(r"\d+(?:[.,]\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def fmt_price(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")


def clean_position(s: str) -> str:
    s = norm(s)
    s = re.sub(r"(Ограничение|Запрет|Преимущество)\s*$", "", s).strip()
    return s



def build_ca_bundle(dest: Path) -> str | None:
    """Файл сертификатов = стандартный набор Python (certifi) + хранилище Windows.

    Нужен, когда антивирус / корпоративный прокси проверяет HTTPS своим
    сертификатом или сайт использует российский корневой сертификат:
    браузер им доверяет (они есть в Windows), а Python по умолчанию — нет.
    """
    import os
    import ssl
    if not hasattr(ssl, "enum_certificates"):       # не Windows
        dirs = [d for d in ("/apex/com.android.conscrypt/cacerts", "/system/etc/security/cacerts",
                            "/data/misc/user/0/cacerts-added") if os.path.isdir(d)]
        if not dirs:
            return None
        pems = []
        try:
            import certifi
            pems.append(Path(certifi.where()).read_text(encoding="ascii", errors="ignore"))
        except Exception:  # noqa: BLE001
            pass
        n = 0
        for d in dirs:                               # Android: системные и установленные пользователем сертификаты
            try:
                for f in os.listdir(d):
                    try:
                        txt = Path(d, f).read_text(encoding="ascii", errors="ignore")
                    except OSError:
                        continue
                    for m in re.finditer(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", txt, re.S):
                        pems.append(m.group(0))
                        n += 1
            except OSError:
                continue
        if not n:
            return None
        dest.write_text("\n".join(pems), encoding="ascii", errors="ignore")
        return str(dest)
    pems: list[str] = []
    try:
        import certifi
        pems.append(Path(certifi.where()).read_text(encoding="ascii", errors="ignore"))
    except Exception:  # noqa: BLE001
        pass
    added = 0
    for store in ("ROOT", "CA"):
        try:
            for cert, enc, trust in ssl.enum_certificates(store):
                if enc != "x509_asn":
                    continue
                if trust is not True and "1.3.6.1.5.5.7.3.1" not in trust:
                    continue
                pems.append(ssl.DER_cert_to_PEM_cert(cert))
                added += 1
        except Exception as e:  # noqa: BLE001
            log.debug("Хранилище сертификатов %s: %s", store, e)
    if not added:
        return None
    dest.write_text("\n".join(pems), encoding="ascii", errors="ignore")
    log.debug("Сертификатов из Windows добавлено: %s", added)
    return str(dest)


class Http:
    """Сеанс HTTP с паузами между запросами к одному сайту, повторами и
    сертификатами из хранилища Windows (как у браузера)."""

    def __init__(self, delay: float = 1.5, timeout: float = 40, proxy: str = "",
                 ssl_verify=True, ca_dir: Path | None = None):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
        })
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.delay = float(delay)
        self.timeout = float(timeout)
        self.verify = ssl_verify
        if self.verify is True and ca_dir:
            try:
                self.verify = build_ca_bundle(Path(ca_dir) / ".ca_bundle.pem") or True
            except Exception as e:  # noqa: BLE001
                log.debug("Сертификаты Windows не загружены: %s", e)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._tb_logged = False
        self.cancel = threading.Event()

    def _wait(self, url: str) -> None:
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        with self._lock:
            wait = self.delay - (time.time() - self._last.get(host, 0))
            self._last[host] = time.time() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)

    def request(self, method: str, url: str, retries: int = 3, **kw) -> requests.Response:
        err: Exception | None = None
        kw.setdefault("timeout", self.timeout)
        for attempt in range(1, retries + 1):
            if self.cancel.is_set():
                raise RuntimeError("Остановлено пользователем")
            self._wait(url)
            try:
                r = self.s.request(method, url, verify=self.verify, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r
            except Exception as e:  # noqa: BLE001
                err = e
                log.debug("%s %s — попытка %s: %r", method, url, attempt, e, exc_info=not self._tb_logged)
                self._tb_logged = True
                if (not isinstance(e, requests.HTTPError) and self.verify not in (True, False)
                        and attempt == 1 and "SSL" in repr(e)):
                    self.verify = True
                    continue
                if isinstance(e, requests.HTTPError) and getattr(e.response, "status_code", 0) in (400, 401, 403, 404):
                    break
                if "HTTP 500" in str(e) and attempt >= 2:        # сервер площадки упал — дольше не ждём
                    break
                time.sleep(self.delay * attempt * 2)
        raise RuntimeError(f"Не удалось загрузить {url}: {err!r}")

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)

    def download(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        """Скачивает файл целиком (но не больше max_bytes). Возвращает (данные, имя из заголовка)."""
        r = self.request("GET", url, stream=True)
        name = filename_from_cd(r.headers.get("content-disposition", ""))
        buf = bytearray()
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) > max_bytes:
                r.close()
                raise RuntimeError(f"файл больше {max_bytes // 1_000_000} МБ — пропущен")
        return bytes(buf), name


def filename_from_cd(cd: str) -> str:
    """Имя файла из Content-Disposition, в т.ч. кривые кодировки ЕИС/Сбербанк-АСТ."""
    if not cd:
        return ""
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1)).strip('"')
    m = re.search(r'filename="?([^";]+)"?', cd)
    if not m:
        return ""
    raw = m.group(1)
    for enc in ("utf-8", "cp1251"):
        try:
            fixed = raw.encode("latin-1").decode(enc)
            if re.search(r"[А-Яа-я]", fixed) or enc == "cp1251":
                return fixed
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return raw


def today() -> dt.date:
    return dt.date.today()


def fmt_d(d: dt.date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""
