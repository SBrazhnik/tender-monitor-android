# -*- coding: utf-8 -*-
"""Коннекторы площадок: ЕИС, Сбербанк-АСТ, B2B-Center, портал закупок Росатома.

Каждый коннектор возвращает закупки в общем виде (dict):
  key, source, number, url, title, customer, customer_inn, law, status, published,
  deadline, price, platform, region, positions[], extra{...}, kind ('notice'|'plan')
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from .util import DATE_RE, OKPD_RE, Http, clean_position, fmt_d, log, norm, parse_date, parse_price

EIS = "https://zakupki.gov.ru"
SBER = "https://www.sberbank-ast.ru"
B2B_URL = "https://www.b2b-center.ru"
ROSATOM = "https://zakupki.rosatom.ru"


def _t(el) -> str:
    return norm(el.get_text(" ")) if el is not None else ""


def _txt(el) -> str:
    """Текст без вставки пробелов внутрь слов: площадки подсвечивают найденное
    тегами (<span>сервер</span>а) — get_text(" ") превратил бы это в «сервер а»."""
    if el is None:
        return ""
    for br in el.find_all(["br", "p", "div", "li"]):
        br.insert_after(" ")
    return norm(el.get_text(""))


def is_active_notice(t: dict, today: dt.date, max_age_days: int = 45) -> bool:
    st = norm(t.get("status")).lower()
    if st and not any(s in st for s in ("подача", "прием", "приём", "опубликован", "приостановлен", "многолот", "план")):
        return False
    d = parse_date(t.get("deadline"))
    if d:
        return d >= today
    p = parse_date(t.get("published"))
    return bool(p and (today - p).days <= max_age_days)


# ═══════════════════════════════ ЕИС ═══════════════════════════════
class Eis:
    SEARCH = EIS + "/epz/order/extendedsearch/results.html"
    PLANS = EIS + "/epz/orderplan/search/results.html"
    PAGE = 50

    def __init__(self, http: Http):
        self.http = http
        self._plan_customer: dict[str, tuple[str, str]] = {}

    # ---------- извещения ----------
    def search(self, text: str, max_pages: int = 20, deadline_from: dt.date | None = None,
               published_from: dt.date | None = None, min_price: float = 0) -> list[dict]:
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            params = {
                "searchString": text, "morphology": "on", "fz44": "on", "fz223": "on", "ppRf615": "on",
                "af": "on", "currencyIdGeneral": "-1", "recordsPerPage": f"_{self.PAGE}", "pageNumber": str(page),
                "sortBy": "UPDATE_DATE", "sortDirection": "false", "showLotsInfoHidden": "false",
            }
            if deadline_from:
                params["applSubmissionCloseDateFrom"] = fmt_d(deadline_from)
            if published_from:
                params["publishDateFrom"] = fmt_d(published_from)
            if min_price:
                params["priceFromGeneral"] = str(int(min_price))
            r = self.http.get(self.SEARCH, params=params)
            items = self.parse_results(r.text)
            out += items
            if len(items) < self.PAGE:
                break
        return out

    @staticmethod
    def parse_results(page_html: str) -> list[dict]:
        soup = BeautifulSoup(page_html, "html.parser")
        res = []
        for c in soup.select(".search-registry-entry-block"):
            num_a = c.select_one(".registry-entry__header-mid__number a")
            number = re.sub(r"[^\d]", "", num_a.get_text()) if num_a else ""
            if not number:
                continue
            blocks, org_inn = {}, ""
            for b in c.select(".registry-entry__body-block"):
                tt = b.select_one(".registry-entry__body-title")
                vv = b.select_one(".registry-entry__body-value, .registry-entry__body-href")
                if tt and vv:
                    blocks[norm(tt.get_text())] = _txt(vv)
                a = b.select_one("a[href*='inn=']")
                if a and not org_inn:
                    m = re.search(r"inn=(\d+)", a.get("href", ""))
                    org_inn = m.group(1) if m else ""
            dates = {}
            for t_el in c.select(".data-block__title"):
                v_el = t_el.find_next_sibling(class_="data-block__value")
                if v_el:
                    dates[norm(t_el.get_text())] = norm(v_el.get_text())
            links = {a.get_text(strip=True): a.get("href") for a in c.select("a[href]")}
            print_a = c.select_one("a[href*='printForm/view']")
            docs_a = c.select_one("a[href*='documents.html']")
            res.append({
                "key": f"ЕИС:{number}", "source": "ЕИС", "number": number, "kind": "notice",
                "url": urljoin(EIS, num_a.get("href", "")),
                "title": blocks.get("Объект закупки", ""),  # см. _txt ниже
                "customer": blocks.get("Заказчик") or blocks.get("Организация, осуществляющая размещение") or "",
                "customer_inn": org_inn,
                "law": _t(c.select_one(".registry-entry__header-top__title")),
                "status": _t(c.select_one(".registry-entry__header-mid__title")),
                "published": dates.get("Размещено", ""),
                "deadline": next((v for k, v in dates.items() if "Окончание подачи" in k), ""),
                "price": parse_price(_t(c.select_one(".price-block__value"))),
                "positions": [], "platform": "",
                "extra": {"print_url": urljoin(EIS, print_a["href"]) if print_a else "",
                          "docs_url": urljoin(EIS, docs_a["href"]) if docs_a else ""},
            })
            _ = links
        return res

    @staticmethod
    def parse_print(page_html: str) -> dict:
        soup = BeautifulSoup(page_html, "html.parser")
        positions: list[str] = []
        for tr in soup.select("table.item-information tr"):
            tds = tr.find_all("td")
            if len(tds) >= 2:
                p = clean_position(tds[1].get_text(" "))
                if p and p not in positions:
                    positions.append(p)
        lines = [norm(x) for x in soup.get_text("\n").split("\n") if norm(x)]
        if not positions:
            for i, ln in enumerate(lines):
                if OKPD_RE.match(ln) and not DATE_RE.match(ln):
                    val = f"{ln} {lines[i + 1]}" if len(ln) < 20 and i + 1 < len(lines) else ln
                    val = clean_position(val)
                    if val not in positions and "ФЗ" not in val and "КоАП" not in val:
                        positions.append(val)
        platform = ""
        for i, ln in enumerate(lines):
            if re.search(r"(Адрес электронной площадки|Наименование электронной площадки|Место предоставления документации)", ln):
                rest = ln.split(":", 1)[1].strip() if ":" in ln else ""
                platform = rest or (lines[i + 1] if i + 1 < len(lines) else "")
                break
        m = re.search(r"https?://[^\s/]+", platform)
        platform = m.group(0) if m else platform
        deadline = ""
        for i, ln in enumerate(lines):
            if re.search(r"окончания (срока )?подачи заявок", ln, re.I):
                m = re.search(r"\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2})?", " ".join(lines[i:i + 2]))
                if m:
                    deadline = m.group(0)
                    break
        return {"positions": positions[:60], "platform": platform[:120], "deadline": deadline}

    def enrich(self, t: dict) -> None:
        url = (t.get("extra") or {}).get("print_url")
        if not url:
            return
        info = self.parse_print(self.http.get(url).text)
        t["positions"] = info["positions"] or t.get("positions") or []
        t["platform"] = info["platform"] or t.get("platform", "")
        if info["deadline"] and (not t.get("deadline") or parse_date(info["deadline"]) == parse_date(t["deadline"])):
            t["deadline"] = info["deadline"]

    # ---------- документация ----------
    def list_docs(self, t: dict) -> list[tuple[str, str]]:
        url = (t.get("extra") or {}).get("docs_url")
        if not url:
            return []
        soup = BeautifulSoup(self.http.get(url).text, "html.parser")
        files, seen = [], set()
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if "filestore" in href and "download" in href:
                u = urljoin(EIS, href)
                if u in seen:
                    continue
                seen.add(u)
                files.append((norm(a.get("title") or a.get_text(" ")), u))
        return files

    # ---------- планы закупок ----------
    def plan_numbers(self, inn: str) -> list[dict]:
        r = self.http.get(self.PLANS, params={"searchString": inn, "morphology": "on", "fz44": "on", "fz223": "on",
                                              "recordsPerPage": "_50"})
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for c in soup.select(".search-registry-entry-block"):
            txt = _t(c)
            num_a = c.select_one(".registry-entry__header-mid__number a")
            num = re.sub(r"[^\d]", "", num_a.get_text()) if num_a else ""
            m = re.search(r"с (\d{2}\.\d{2}\.\d{4}) по (\d{2}\.\d{2}\.\d{4})", txt)
            org = c.select_one("a[href*='inn=']")
            org_inn = re.search(r"inn=(\d+)", org["href"]).group(1) if org and "inn=" in org.get("href", "") else ""
            law = "44-ФЗ" if "44-ФЗ" in txt else "223-ФЗ"
            if num:
                out.append({"number": num, "law": law, "to": parse_date(m.group(2)) if m else None,
                            "customer": _t(org), "customer_inn": org_inn})
        return out

    def plan_positions(self, search: str, law: str | None, max_pages: int, plan_number: str = "") -> list[dict]:
        out = []
        for page in range(1, max_pages + 1):
            params = {"searchString": search, "morphology": "on", "searchType": "true",
                      "recordsPerPage": "_50", "pageNumber": str(page)}
            if plan_number:
                params.update({"selectedOrderPlanName": plan_number, "isPg2020": "true"})
            if law in (None, "44-ФЗ"):
                params["fz44"] = "on"
            if law in (None, "223-ФЗ"):
                params["fz223"] = "on"
            r = self.http.get(self.PLANS, params=params)
            items = self.parse_positions(r.text)
            out += items
            if len(items) < self.PAGE:
                break
        return out

    @staticmethod
    def parse_positions(page_html: str) -> list[dict]:
        soup = BeautifulSoup(page_html, "html.parser")
        res = []
        for c in soup.select(".search-registry-entry-block"):
            a = c.select_one(".registry-entry__header-mid__number a")
            if not a:
                continue
            href = urljoin(EIS, a.get("href", ""))
            m = re.search(r"guid=([\w-]+)", href)
            info = re.search(r"infoGuid=([\w-]+)", href)
            fields = {}
            for it in c.select(".lots-wrap-content__body--item"):
                fields[_t(it.select_one(".lots-wrap-content__body__title"))] = _t(it.select_one(".lots-wrap-content__body__val"))
            dates = {}
            for t_el in c.select(".data-block__title"):
                v_el = t_el.find_next_sibling(class_="data-block__value")
                if v_el:
                    dates[norm(t_el.get_text())] = norm(v_el.get_text())
            law_txt = _t(c.select_one(".registry-entry__header-top__title"))
            key_id = m.group(1) if m else href
            res.append({
                "key": f"ЕИС-план:{key_id}", "source": "ЕИС (план)", "kind": "plan",
                "number": "позиция " + re.sub(r"[^\d]", "", a.get_text()), "url": href,
                "title": _txt(c.select_one(".registry-entry__body-value")),
                "law": ("44-ФЗ" if "44" in law_txt else "223-ФЗ") + " · план · " + fields.get("Способ размещения закупки", ""),
                "status": "План", "published": dates.get("Размещено", ""),
                "deadline": "", "plan_month": fields.get("Срок проведения закупки", "") or fields.get(
                    "Планируемый срок размещения извещения", ""),
                "price": parse_price(_t(c.select_one(".price-block__value"))),
                "customer": "", "customer_inn": "", "positions": [], "platform": "",
                "extra": {"plan_guid": info.group(1) if info else ""},
            })
        return res

    def plan_customer(self, plan_guid: str) -> tuple[str, str]:
        if not plan_guid:
            return "", ""
        if plan_guid not in self._plan_customer:
            try:
                soup = BeautifulSoup(self.http.get(
                    f"{EIS}/epz/orderplan/purchase-plan/card/common-info.html", params={"guid": plan_guid}).text,
                    "html.parser")
                a = soup.select_one("a[href*='inn=']")
                inn = re.search(r"inn=(\d+)", a["href"]).group(1) if a else ""
                self._plan_customer[plan_guid] = (_t(a), inn)
            except Exception as e:  # noqa: BLE001
                log.debug("План %s: заказчик не определён: %s", plan_guid, e)
                self._plan_customer[plan_guid] = ("", "")
        return self._plan_customer[plan_guid]


def plan_month_future(month: str, today: dt.date) -> bool:
    m = re.match(r"(\d{2})\.(\d{4})", month or "")
    if not m:
        return True
    return (int(m.group(2)), int(m.group(1))) >= (today.year, today.month)


# ═══════════════════════════════ Сбербанк-АСТ ═══════════════════════════════
class Sber:
    API = SBER + "/api/Processing/main"
    PAGE = 50

    def __init__(self, http: Http):
        self.http = http

    @staticmethod
    def _xml_escape(s: str) -> str:
        return html.escape(s, quote=False)

    def search(self, text: str, deadline_from: dt.date | None, max_pages: int = 3,
               published_from: dt.date | None = None) -> list[dict]:
        out = []
        for page in range(max_pages):
            q = ("<query><targetPageCode>OTUnitedPurchaseList</targetPageCode><searchbyorg>1</searchbyorg>"
                 f"<pagesize>{self.PAGE}</pagesize><pagenum>{page}</pagenum><sortOrder>desc</sortOrder>"
                 f"<searchBarType>anyWord</searchBarType><mainInput>{self._xml_escape(text)}</mainInput>"
                 "<fields><field>hasSt14</field></fields><sortType></sortType>"
                 f"<publicDate><startPublicDate>{fmt_d(published_from)}</startPublicDate><endPublicDate></endPublicDate></publicDate>"
                 f"<requestEndDate><startRequestDate>{fmt_d(deadline_from)}</startRequestDate><endRequestDate></endRequestDate>"
                 "</requestEndDate></query>")
            body = {"windowCode": "/EsOpenUnitedPurchaseList", "actionType": "MONITOR", "actionCode": "default",
                    "documentBody": "", "parm": "es", "filterBody": q, "options": {}}
            r = self.http.post(self.API, json=body, headers={"Content-Type": "application/json",
                                                             "Accept": "application/json, text/plain, */*",
                                                             "Origin": SBER, "Referer": SBER + "/UnitedPurchaseList.html"})
            items = self.parse(r.text)
            out += items
            if len(items) < self.PAGE:
                break
        return out

    @staticmethod
    def parse(text: str) -> list[dict]:
        j = json.loads(text)
        pl = j.get("PurchaseList") or {}
        hits = pl.values() if isinstance(pl, dict) else pl
        res = []
        for h in hits:
            x = h.get("_source", h) if isinstance(h, dict) else {}
            # часть полей площадка отдаёт списками — склеиваем в строку
            x = {k: (", ".join(str(i) for i in v) if isinstance(v, list) else v) for k, v in x.items()}
            code = norm(x.get("purchCode"))
            if not code:
                continue
            deadline = x.get("RequestDate") or ""
            if deadline.startswith("01.01.2079") or deadline.startswith("01.01.0001"):
                deadline = ""
            oos = x.get("OOSHref") or ""
            eis_num = ""
            m = re.search(r"regNumber=(\d+)", oos)
            if m:
                eis_num = m.group(1)
            price = x.get("purchAmount") or x.get("purchAmountRUB")
            try:
                price = float(price) if price not in (None, "") else None
            except (TypeError, ValueError):
                price = None
            names = x.get("productNames") or ""
            res.append({
                "key": f"ЕИС:{eis_num}" if eis_num else f"Сбербанк-АСТ:{code}",
                "source": "Сбербанк-АСТ", "number": code, "kind": "notice",
                "url": x.get("objectHrefTerm") or oos, "title": norm(x.get("purchName") or x.get("BidName")),
                "customer": norm(x.get("CustomerFullName") or x.get("OrgFullName")),
                "customer_inn": norm(x.get("CustomerInn") or x.get("OrgInn")),
                "law": norm(x.get("SourceTerm")) + (" · " + norm(x.get("PurchaseTypeName")) if x.get("PurchaseTypeName") else ""),
                "status": norm(x.get("PurchaseStageTerm")), "published": norm(x.get("PublicDate")),
                "deadline": norm(deadline), "price": price, "platform": "sberbank-ast.ru",
                "region": norm(x.get("RegionName")),
                "positions": [p for p in [norm(names)] if p and p != norm(x.get("purchName"))][:1],
                "extra": {"eis_number": eis_num, "eis_url": oos, "sber_url": x.get("objectHrefTerm") or ""},
            })
        return res

    def list_docs(self, t: dict) -> list[tuple[str, str]]:
        url = (t.get("extra") or {}).get("sber_url") or ""
        if "utp.sberbank-ast.ru" not in url:
            return []
        soup = BeautifulSoup(self.http.get(url).text, "html.parser")
        files = []
        for a in soup.select("a[href*='DownloadFile']"):
            tr = a.find_parent("tr")
            name = norm(tr.get_text(" ")).replace("Сохранить", "").strip() if tr else ""
            files.append((name, urljoin(url, a["href"])))
        return files


# ═══════════════════════════════ B2B-Center ═══════════════════════════════
class B2B:
    def __init__(self, http: Http):
        self.http = http
        self.logged_in = False
        self.requests = 0          # сколько страниц поиска запрошено
        self.rows = 0              # сколько процедур на них разобрано
        self.odd: list[str] = []   # заголовки страниц, не похожих на результаты поиска

    def note(self) -> str:
        """Пояснение для журнала, если площадка отвечает, но результатов нет."""
        if not self.requests:
            return ""
        if self.odd:
            return (f"из {self.requests} запросов {len(self.odd)} вернули не страницу поиска "
                    f"(«{self.odd[0]}») — возможно, площадка показывает проверку или требует вход")
        if not self.rows:
            return f"на {self.requests} запросов площадка не вернула ни одной процедуры"
        return ""

    def diagnose(self, login: str, password: str) -> str:
        """Проверка для вкладки «Площадки»: вход и пробный поиск."""
        parts = []
        try:
            parts.append("вход: " + self.login(login, password))
        except Exception as e:  # noqa: BLE001
            parts.append(f"вход: ошибка — {e}")
        try:
            n = len(self.search("сервер"))
            parts.append(f"пробный поиск «сервер»: найдено {n}" + (f" ({self.note()})" if not n and self.note() else ""))
        except Exception as e:  # noqa: BLE001
            parts.append(f"пробный поиск: ошибка — {e}")
        return "; ".join(parts)

    def login(self, login: str, password: str) -> str:
        """Вход по логину и паролю. Возвращает текст статуса."""
        if not (login and password):
            return "учётные данные не заданы — работаю без входа (видны первые 20 результатов)"
        url = B2B_URL + "/auth/credentials_ajax_login.html"
        soup = BeautifulSoup(self.http.get(url).text, "html.parser")
        form = soup.select_one("form")
        if not form:
            return "форма входа не найдена"
        data = {i.get("name"): i.get("value", "") for i in form.select("input[name]")}
        data["login_form[login]"] = login
        data["login_form[password]"] = password
        data["login_form[remember_me]"] = "1"
        r = self.http.post(url, data=data, headers={"X-Requested-With": "XMLHttpRequest", "Referer": B2B_URL + "/market/"})
        check = self.http.get(B2B_URL + "/market/")
        self.logged_in = "auth_ajax_modal" not in check.text or "Выйти" in check.text
        if self.logged_in:
            return "вход выполнен"
        m = re.search(r'class="[^"]*error[^"]*"[^>]*>([^<]{5,200})<', r.text)
        return "вход не выполнен" + (f": {norm(m.group(1))}" if m else " (проверьте логин и пароль)")

    def search(self, text: str, firm_id: str = "", date_from: dt.date | None = None) -> list[dict]:
        # сортировка «Опубликовано» по убыванию — без входа площадка отдаёт первые 20
        params = {"f_keyword": text, "searching": "1", "order_by": "1"}
        if firm_id:
            params["firm_id"] = firm_id
        r = self.http.get(B2B_URL + "/market/", params=params)
        res = self.parse(r.text)
        self.requests += 1
        self.rows += len(res)
        if not res and "f_keyword" not in r.text and "search-results" not in r.text:
            m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
            self.odd.append(norm(m.group(1))[:80] if m else f"ответ {len(r.text)} символов без заголовка")
        return res

    @staticmethod
    def parse(page_html: str) -> list[dict]:
        soup = BeautifulSoup(page_html, "html.parser")
        res = []
        for tr in soup.select("tr"):
            a = tr.select_one("a.search-results-title, a[href*='/tender-']")
            if not a or "/tender-" not in a.get("href", ""):
                continue
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            desc = a.select_one(".search-results-title-desc")
            head = norm(a.get_text(" ").replace(desc.get_text(" "), "") if desc else a.get_text(" "))
            desc_text = _txt(desc) if desc else ""
            m = re.search(r"№\s*(\d+)", head)
            num = m.group(1) if m else re.search(r"tender-(\d+)", a["href"]).group(1)
            kind = norm(head.split("№")[0])
            if re.search(r"продаж|Объявление о продаже", kind, re.I):
                continue                    # продажи нам не нужны
            cat = _t(tds[0].select_one("small"))
            href = urljoin(B2B_URL, a["href"].split("#")[0])
            res.append({
                "key": f"B2B-Center:{num}", "source": "B2B-Center", "number": num, "kind": "notice",
                "url": href, "title": desc_text or head, "customer": _t(tds[1]), "customer_inn": "",
                "law": kind + (f" · {cat}" if cat else ""), "status": "Прием предложений",
                "published": _t(tds[2]) if len(tds) > 2 else "", "deadline": _t(tds[3]) if len(tds) > 3 else "",
                "price": None, "platform": "b2b-center.ru", "positions": [], "extra": {},
            })
        return res

    def enrich(self, t: dict) -> None:
        """Карточка процедуры: описание, цена, организатор, файлы (если есть доступ)."""
        soup = BeautifulSoup(self.http.get(t["url"]).text, "html.parser")
        txt = soup.get_text("\n")
        m = re.search(r"Общая стоимость закупки:\s*([^\n]+)", txt)
        if m:
            t["price"] = parse_price(m.group(1))
        m = re.search(r"Дата окончания подачи заявок:\s*([^\n]+)", txt)
        if m:
            t["deadline"] = norm(m.group(1))
        body = soup.select_one(".expandable-text, .s2b-read-more, #tender_text")
        if body:
            t["positions"] = [norm(body.get_text(" "))[:1500]]
        files = []
        for a in soup.select("a[href]"):
            h = a.get("href", "")
            if re.search(r"(download|/file/|get_file|attachment)", h, re.I):
                files.append((norm(a.get_text(" ")) or "файл", urljoin(B2B_URL, h)))
        t.setdefault("extra", {})["files"] = files[:40]

    def list_docs(self, t: dict) -> list[tuple[str, str]]:
        files = (t.get("extra") or {}).get("files")
        if files is None:
            self.enrich(t)
            files = t["extra"].get("files") or []
        return [tuple(f) for f in files]


# ═══════════════════════════════ Портал закупок Росатома ═══════════════════════════════
class Rosatom:
    RSS = ROSATOM + "/rss/ru/tenders"

    def __init__(self, http: Http):
        self.http = http
        self._items: list[dict] | None = None

    @staticmethod
    def parse_rss(xml_text: str) -> list[dict]:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
        items = []
        for it in root.iter("item"):
            desc = html.unescape(it.findtext("description") or "")
            fields = {}
            for k, v in re.findall(r"<td[^>]*>\s*([^<]+?):?\s*</td>\s*<td[^>]*>(.*?)</td>", desc, re.S):
                fields[norm(k).rstrip(":")] = norm(re.sub(r"<[^>]+>", " ", v))
            items.append({"title": norm(it.findtext("title")), "link": norm(it.findtext("link")), **fields})
        return items

    def load(self) -> list[dict]:
        if self._items is None:
            r = self.http.get(self.RSS)
            r.encoding = r.encoding or "utf-8"
            self._items = self.parse_rss(r.text)
            log.info("Росатом: в RSS %s закупок", len(self._items))
        return self._items

    @staticmethod
    def to_tender(x: dict) -> dict:
        num = x.get("Номер закупки", "") or x["link"]
        method = x.get("Способ закупки", "")
        return {
            "key": f"Росатом:{num}", "source": "Росатом" + (" · БРИФ" if re.search(r"БРИФ|отбор", method, re.I) else ""),
            "number": num, "kind": "notice", "url": x["link"], "title": x["title"],
            "customer": x.get("Организатор закупки", ""), "customer_inn": "",
            "law": "ЕОСЗ Росатома · " + method, "status": x.get("Статус", ""),
            "published": x.get("Дата размещения", ""), "deadline": x.get("Дата окончания подачи заявок", ""),
            "price": parse_price(x.get("Начальная стоимость закупки (руб.)")), "platform": "zakupki.rosatom.ru",
            "positions": [], "extra": {},
        }

    @staticmethod
    def _norm_org(s: str) -> str:
        return re.sub(r"[\"«»'“”„\s]+", " ", norm(s)).strip().lower()

    def by_customer(self, names: list[str], title_patterns: list[str]) -> list[dict]:
        orgs = {self._norm_org(o) for o in names if o}
        pats = []
        for p in title_patterns:
            try:
                pats.append(re.compile(p, re.I))
            except re.error:
                pats.append(re.compile(re.escape(p), re.I))
        out = []
        for x in self.load():
            if self._norm_org(x.get("Организатор закупки", "")) in orgs or any(p.search(x["title"]) for p in pats):
                out.append(self.to_tender(x))
        return out

    def all_published(self) -> list[dict]:
        return [self.to_tender(x) for x in self.load() if x.get("Статус") == "Опубликована"]
