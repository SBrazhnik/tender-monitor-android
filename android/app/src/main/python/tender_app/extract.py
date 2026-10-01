# -*- coding: utf-8 -*-
"""Извлечение текста из документации закупок.

Поддерживается: DOCX, XLSX, PPTX, ODT/ODS, PDF (нужен pypdf), DOC/XLS (эвристика
по бинарному файлу — без Word), RTF, TXT/CSV/HTML/XML, ZIP (вложенно),
RAR/7Z — если установлен 7-Zip.
"""
from __future__ import annotations

import html
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_TEXT = 3_000_000          # символов с одной закупки
MAX_ARCHIVE_DEPTH = 2
TEXT_EXT = {".txt", ".csv", ".htm", ".html", ".xml"}
DOC_EXT = {".docx", ".docm", ".doc", ".xlsx", ".xlsm", ".xls", ".pptx", ".pdf", ".rtf", ".odt", ".ods",
           ".zip", ".rar", ".7z"} | TEXT_EXT
SKIP_EXT = {".sig", ".sgn", ".p7s", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".dwg", ".dxf", ".exe"}


def _xml_text(data: bytes) -> str:
    """Текст из XML офисных форматов (DOCX/XLSX/PPTX/ODF)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return re.sub(r"<[^>]+>", " ", data.decode("utf-8", "ignore"))
    out: list[str] = []
    text_tags = ("}t", "}v", "}p", "}span", "}h", "}a", "}s")
    para_tags = ("}p", "}tr", "}si", "}row", "}h")

    def walk(el):
        tag = el.tag if isinstance(el.tag, str) else ""
        if el.text and tag.endswith(text_tags):
            out.append(el.text)
        if tag.endswith(("}tab", "}tc", "}c", "}table-cell")):
            out.append("\t")
        for ch in el:
            walk(ch)
            if ch.tail and isinstance(ch.tag, str) and ch.tag.endswith(("}span", "}s", "}a")):
                out.append(ch.tail)
        if tag.endswith(para_tags):
            out.append("\n")
    walk(root)
    return "".join(out)


def _from_zip_office(z: zipfile.ZipFile) -> str | None:
    names = z.namelist()
    parts: list[str] = []
    if "word/document.xml" in names:
        for n in names:
            if re.match(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$", n):
                parts.append(_xml_text(z.read(n)))
        return "\n".join(parts)
    if "xl/workbook.xml" in names:
        if "xl/sharedStrings.xml" in names:
            parts.append(_xml_text(z.read("xl/sharedStrings.xml")))
        for n in names:                     # числа и встроенные строки из листов
            if re.match(r"xl/worksheets/sheet\d+\.xml$", n):
                raw = z.read(n)
                parts += re.findall(r"<t[^>]*>([^<]{2,})</t>", raw.decode("utf-8", "ignore"))
        return html.unescape("\n".join(parts))
    if any(n.startswith("ppt/slides/slide") for n in names):
        for n in sorted(names):
            if re.match(r"ppt/slides/slide\d+\.xml$", n):
                parts.append(_xml_text(z.read(n)))
        return "\n".join(parts)
    if "content.xml" in names and "mimetype" in names:
        return _xml_text(z.read("content.xml"))
    return None


def _ole_text(data: bytes) -> str:
    """Старые DOC/XLS без Word: вытаскиваем строки UTF-16 и ASCII.
    Для поиска вендоров и позиций этого достаточно."""
    out: list[str] = []
    for m in re.finditer(rb"(?:[\x20-\x7e\xa0-\xff][\x00]|[\x00-\xff][\x04]){4,}", data):
        try:
            s = m.group(0).decode("utf-16-le", "ignore")
        except Exception:  # noqa: BLE001
            continue
        s = re.sub(r"[^\w\s.,;:()«»\"'/\\№%+\-–—]", " ", s)
        if len(re.sub(r"\W", "", s)) >= 3:
            out.append(s)
    meta = re.compile(r"Microsoft|Times New Roman|Arial|Calibri|Normal\.dot|Symbol|Wingdings|Cambria|Tahoma|"
                      r"LaserJet|Kyocera|Canon|Xerox|Brother|Epson|Ricoh|Konica|PDF|Обычный|Заголовок \d|Сетка таблицы")
    return "\n".join(x for x in out if not meta.search(x))


def _rtf_text(data: bytes) -> str:
    s = data.decode("latin-1", "ignore")
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1251", "ignore"), s)
    s = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), s)
    s = re.sub(r"\\(par|line|row|cell)\b", "\n", s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", s)
    s = re.sub(r"[{}]", "", s)
    return s


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        r = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in r.pages[:200])
    except Exception:  # noqa: BLE001
        return ""


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


def find_7zip() -> str | None:
    for p in (shutil.which("7z"), shutil.which("7za"),
              r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if p and os.path.exists(p):
            return p
    return None


def extract_text(data: bytes, name: str = "", depth: int = 0) -> tuple[str, str]:
    """Возвращает (текст, примечание). Примечание — почему текста нет/мало."""
    ext = Path(name).suffix.lower()
    if ext in SKIP_EXT:
        return "", "не текстовый файл"
    head = data[:8]
    try:
        if head.startswith(b"PK"):
            z = zipfile.ZipFile(io.BytesIO(data))
            office = _from_zip_office(z)
            if office is not None:
                return office, ""
            if depth >= MAX_ARCHIVE_DEPTH:
                return "", "архив слишком глубоко вложен"
            texts, notes = [], []
            for info in z.infolist()[:200]:
                if info.is_dir() or info.file_size > 30_000_000:
                    continue
                fname = info.filename
                try:                                  # имена в ZIP из Windows часто в cp866
                    fname = fname.encode("cp437").decode("cp866")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
                t, n = extract_text(z.read(info), fname, depth + 1)
                if t:
                    texts.append(f"[{fname}]\n{t}")
                elif n:
                    notes.append(f"{fname}: {n}")
            return "\n".join(texts), "; ".join(notes[:5])
        if head.startswith(b"%PDF"):
            t = _pdf_text(data)
            if not t.strip():
                try:
                    import pypdf  # noqa: F401
                    return "", "PDF без текстового слоя (скан)"
                except ImportError:
                    return "", "для PDF нужна библиотека pypdf"
            return t, ""
        if head.startswith(b"\xd0\xcf\x11\xe0"):
            from .cfb import doc_text, xls_text
            for fn in ((xls_text, doc_text) if ext in (".xls", ".xlt") else (doc_text, xls_text)):
                try:
                    t = fn(data)
                    if t.strip():
                        return t, ""
                except Exception:  # noqa: BLE001
                    continue
            return _ole_text(data), "текст извлечён приблизительно"
        if head.startswith(b"{\\rtf"):
            return _rtf_text(data), ""
        if head.startswith(b"Rar!") or head.startswith(b"7z\xbc\xaf"):
            if depth >= MAX_ARCHIVE_DEPTH:
                return "", "архив слишком глубоко вложен"
            seven = find_7zip()
            if not seven:
                return "", "RAR/7Z: установите 7-Zip, чтобы читать такие архивы"
            with tempfile.TemporaryDirectory() as td:
                src = Path(td) / ("a" + (ext or ".bin"))
                src.write_bytes(data)
                out = Path(td) / "x"
                subprocess.run([seven, "x", "-y", f"-o{out}", str(src)], capture_output=True, timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                texts = []
                for p in out.rglob("*"):
                    if p.is_file() and p.stat().st_size < 30_000_000:
                        t, _ = extract_text(p.read_bytes(), p.name, depth + 1)
                        if t:
                            texts.append(f"[{p.name}]\n{t}")
                return "\n".join(texts), ""
        if ext in TEXT_EXT or not ext:
            t = _decode_text(data)
            if ext in {".htm", ".html", ".xml"}:
                t = re.sub(r"<[^>]+>", " ", t)
            return t, ""
        return "", f"формат {ext or '?'} не поддерживается"
    except Exception as e:  # noqa: BLE001
        return "", f"ошибка чтения: {e}"


def clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text[:MAX_TEXT]
