# -*- coding: utf-8 -*-
"""Чтение старых форматов Office (DOC, XLS) без сторонних библиотек.

* Контейнер OLE / Compound File Binary: FAT, MiniFAT, каталог, потоки.
* DOC: настоящий текст документа по таблице фрагментов (piece table) — без служебных
  строк вроде имён принтеров, шрифтов и «Microsoft Office Word», которые попадают
  в текст при «грубом» извлечении.
* XLS: строки из таблицы SST и ячеек потока Workbook.
"""
from __future__ import annotations

import re
import struct

ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF


class CFB:
    def __init__(self, data: bytes):
        if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise ValueError("не OLE-файл")
        self.d = data
        self.ss = 1 << struct.unpack_from("<H", data, 0x1E)[0]
        self.mss = 1 << struct.unpack_from("<H", data, 0x20)[0]
        n_fat = struct.unpack_from("<I", data, 0x2C)[0]
        dir_start = struct.unpack_from("<I", data, 0x30)[0]
        self.cutoff = struct.unpack_from("<I", data, 0x38)[0]
        minifat_start = struct.unpack_from("<I", data, 0x3C)[0]
        difat_start = struct.unpack_from("<I", data, 0x44)[0]
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        sec = difat_start
        guard = 0
        while sec not in (ENDOFCHAIN, FREESECT) and guard < 10000:
            vals = struct.unpack_from(f"<{self.ss // 4}I", data, self._off(sec))
            difat += list(vals[:-1])
            sec = vals[-1]
            guard += 1
        fat_secs = [s for s in difat if s not in (FREESECT, ENDOFCHAIN)][:n_fat]
        self.fat: list[int] = []
        for s in fat_secs:
            self.fat += struct.unpack_from(f"<{self.ss // 4}I", data, self._off(s))
        dir_data = self._chain(dir_start)
        self.entries = []
        for i in range(0, len(dir_data) - 127, 128):
            e = dir_data[i:i + 128]
            nlen = struct.unpack_from("<H", e, 64)[0]
            name = e[:max(0, nlen - 2)].decode("utf-16-le", "ignore")
            typ = e[66]
            start, size = struct.unpack_from("<IQ", e, 116)
            if self.ss == 512:
                size &= 0xFFFFFFFF
            self.entries.append({"name": name, "type": typ, "start": start, "size": size})
        root = self.entries[0] if self.entries else None
        self.ministream = self._chain(root["start"])[:root["size"]] if root and root["type"] == 5 else b""
        self.minifat: list[int] = []
        if minifat_start not in (ENDOFCHAIN, FREESECT):
            mf = self._chain(minifat_start)
            self.minifat = list(struct.unpack_from(f"<{len(mf) // 4}I", mf, 0))

    def _off(self, sec: int) -> int:
        return (sec + 1) * self.ss

    def _chain(self, start: int) -> bytes:
        out, sec, guard = bytearray(), start, 0
        while sec not in (ENDOFCHAIN, FREESECT) and sec < len(self.fat) and guard < 200000:
            o = self._off(sec)
            out += self.d[o:o + self.ss]
            sec = self.fat[sec]
            guard += 1
        return bytes(out)

    def _minichain(self, start: int) -> bytes:
        out, sec, guard = bytearray(), start, 0
        while sec not in (ENDOFCHAIN, FREESECT) and sec < len(self.minifat) and guard < 200000:
            o = sec * self.mss
            out += self.ministream[o:o + self.mss]
            sec = self.minifat[sec]
            guard += 1
        return bytes(out)

    def stream(self, name: str) -> bytes | None:
        for e in self.entries:
            if e["type"] == 2 and e["name"].lower() == name.lower():
                if e["size"] < self.cutoff:
                    return self._minichain(e["start"])[:e["size"]]
                return self._chain(e["start"])[:e["size"]]
        return None


def _clean_word(t: str) -> str:
    t = re.sub(r"\x13[^\x13\x14\x15]*\x14", "", t)        # коды полей: HYPERLINK, PAGE…
    t = re.sub(r"\x13[^\x13\x14\x15]*\x15", "", t)
    t = t.replace("\x15", "").replace("\x07", "\t").replace("\r", "\n").replace("\x0b", "\n").replace("\x0c", "\n")
    return re.sub(r"[\x00-\x08\x0e-\x1f]", "", t)


def doc_text(data: bytes) -> str:
    c = CFB(data)
    wd = c.stream("WordDocument")
    if not wd or struct.unpack_from("<H", wd, 0)[0] != 0xA5EC:
        raise ValueError("не документ Word")
    flags = struct.unpack_from("<H", wd, 0x0A)[0]
    table = c.stream("1Table" if flags & 0x0200 else "0Table")
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x01A2)
    if not table or not lcb_clx or fc_clx + lcb_clx > len(table):
        raise ValueError("нет таблицы фрагментов")
    clx = table[fc_clx:fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 0x01:
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    if i >= len(clx) or clx[i] != 0x02:
        raise ValueError("нет Pcdt")
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + lcb]
    n = (lcb - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    parts = []
    for k in range(n):
        fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * k + 2)[0]
        length = cps[k + 1] - cps[k]
        if length <= 0:
            continue
        if fc & 0x40000000:
            off = (fc & 0x3FFFFFFF) // 2
            parts.append(wd[off:off + length].decode("cp1252", "ignore"))
        else:
            parts.append(wd[fc:fc + 2 * length].decode("utf-16-le", "ignore"))
    return _clean_word("".join(parts))


def xls_text(data: bytes) -> str:
    """Строки книги Excel 97-2003: таблица SST (с продолжениями) и текстовые ячейки."""
    c = CFB(data)
    wb = c.stream("Workbook") or c.stream("Book")
    if not wb:
        raise ValueError("нет потока Workbook")
    # собираем SST вместе с CONTINUE
    out: list[str] = []
    i = 0
    recs = []
    while i + 4 <= len(wb):
        rid, ln = struct.unpack_from("<HH", wb, i)
        recs.append((rid, wb[i + 4:i + 4 + ln]))
        i += 4 + ln
    k = 0
    while k < len(recs):
        rid, body = recs[k]
        if rid == 0x00FC:                                     # SST
            chunks = [body]
            j = k + 1
            while j < len(recs) and recs[j][0] == 0x003C:
                chunks.append(recs[j][1])
                j += 1
            out += _sst_strings(chunks)
            k = j
            continue
        k += 1
    return "\n".join(s for s in out if s.strip())


def _sst_strings(chunks: list[bytes]) -> list[str]:
    strings: list[str] = []
    ci, pos = 0, 8
    data = chunks[0]
    total = struct.unpack_from("<I", data, 4)[0] if len(data) >= 8 else 0

    def need(n):
        nonlocal ci, pos, data
        if pos + n <= len(data):
            return True
        return False

    for _ in range(total):
        if pos >= len(data):
            ci += 1
            if ci >= len(chunks):
                break
            data, pos = chunks[ci], 0
        if not need(3):
            break
        cch = struct.unpack_from("<H", data, pos)[0]
        flags = data[pos + 2]
        pos += 3
        rich = flags & 0x08
        ext = flags & 0x04
        n_runs = ext_len = 0
        if rich:
            n_runs = struct.unpack_from("<H", data, pos)[0]
            pos += 2
        if ext:
            ext_len = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        wide = flags & 0x01
        chars = []
        remaining = cch
        while remaining > 0:
            size = 2 if wide else 1
            avail = (len(data) - pos) // size
            take = min(avail, remaining)
            raw = data[pos:pos + take * size]
            chars.append(raw.decode("utf-16-le" if wide else "latin-1", "ignore"))
            pos += take * size
            remaining -= take
            if remaining > 0:
                ci += 1
                if ci >= len(chunks):
                    break
                data, pos = chunks[ci], 0
                wide = data[0] & 0x01                          # у продолжения свой флаг
                pos = 1
        strings.append("".join(chars))
        skip = n_runs * 4 + ext_len
        while skip > 0:
            avail = len(data) - pos
            if skip <= avail:
                pos += skip
                skip = 0
            else:
                skip -= avail
                ci += 1
                if ci >= len(chunks):
                    return strings
                data, pos = chunks[ci], 0
    return strings
