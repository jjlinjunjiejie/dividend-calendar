"""Validated dividend providers and a bounded, last-known-good fallback cache.

Provider identity describes where a record was fetched. Position ``source`` still
selects the fund's distribution cadence; switching providers never changes it.
"""
from __future__ import annotations

import io
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from bs4 import BeautifulSoup
from pypdf import PdfReader

HOSTS = ("https://www.ishares.com", "https://www.blackrock.com")
API_PATH = "/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data"
SCREENER_PATH = (
    "/us/product-screener/product-screener-v3.1.jsn"
    "?dcrPath=/templatedata/config/product-screener-v3/data/en/us-ishares/"
    "ishares-product-screener-backend-config&siteEntryPassthrough=true"
)
SCHEDULE_PATH = "/us/literature/shareholder-letters/isharesandblackrocketfsdistributionschedule.pdf"
DATE_TOKEN = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{2}\b")
DATE_FIELDS = ("ex_date", "record_date", "payable_date")


class SourceError(RuntimeError):
    """A provider response is unavailable, incomplete, or unsafe to publish."""


def request_bytes(url: str, accept: str = "application/json") -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/129 Safari/537.36",
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.8",
    })
    # One bounded attempt per provider. The next provider is the retry, avoiding
    # an 8-position outage exhausting the Actions job before fallback can run.
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            raw = response.read(12_000_001)
        if not raw or len(raw) > 12_000_000:
            raise SourceError("Empty or oversized response")
        return raw
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"Request failed: {url}: {exc}") from exc


def request_json(url: str) -> dict[str, Any]:
    result = json.loads(request_bytes(url).decode("utf-8-sig"))
    if not isinstance(result, dict):
        raise SourceError("Expected a JSON object")
    return result


def number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("r") if value.get("r") not in (None, "", "-") else value.get("d")
    if isinstance(value, bool):
        return None
    try:
        result = float(str(value).strip().replace("$", "").replace(",", "").replace("%", ""))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d-%b-%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    return None


def ishares_rows(ticker: str, portfolio_id: int, host: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({
        "appSubType": "ISHARES", "appType": "PRODUCT_PAGE", "component": "fundDownload",
        "locale": "en_US", "portfolioId": portfolio_id, "targetSite": "us-ishares",
        "userType": "individual", "excludeContent": "true",
    })
    payload = request_json(f"{host}{API_PATH}?{query}")
    if str(payload.get("productId")) != str(portfolio_id) or payload.get("currencyCode") != "USD":
        raise SourceError("Wrong portfolio or currency in iShares response")
    points = payload["componentsByNameMap"]["fundDownload"]["containersByNameMap"]["distributions"]["dataPointsByNameMap"]
    columns = {key: points[key]["value"] for key in ("exDate", "recordDate", "payableDate", "totalDistribution")}
    if not all(isinstance(v, list) for v in columns.values()) or len({len(v) for v in columns.values()}) != 1:
        raise SourceError("Misaligned iShares distribution columns")
    return [{"ticker": ticker, "ex_date": parse_date(ex), "record_date": parse_date(record),
             "payable_date": parse_date(pay), "per_share": number(amount)}
            for ex, record, pay, amount in zip(*columns.values())]


def stockanalysis_rows(ticker: str) -> list[dict[str, Any]]:
    html = request_bytes(f"https://stockanalysis.com/etf/{ticker.lower()}/dividend/", "text/html").decode("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    if not heading or not re.search(rf"\b{re.escape(ticker)}\b", heading.get_text(" ", strip=True), re.I):
        raise SourceError("Wrong ticker or missing StockAnalysis heading")
    required = ("Ex-Div Date", "Amount", "Record Date", "Pay Date")
    for table in soup.find_all("table"):
        aliases = {"exdividenddate": "Ex-Div Date", "exdivdate": "Ex-Div Date",
                   "cashamount": "Amount", "amount": "Amount",
                   "recorddate": "Record Date", "paydate": "Pay Date"}
        headers = [aliases.get(re.sub(r"[^a-z]", "", c.get_text().lower()), "unknown")
                   for c in table.find_all("th")]
        if not all(name in headers for name in required):
            continue
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            if not cells:
                continue
            if len(cells) != len(headers):
                raise SourceError("Malformed StockAnalysis history row")
            values = dict(zip(headers, cells))
            rows.append({"ticker": ticker, "ex_date": parse_date(values["Ex-Div Date"]),
                         "record_date": parse_date(values["Record Date"]),
                         "payable_date": parse_date(values["Pay Date"]), "per_share": number(values["Amount"])})
        return rows
    raise SourceError("StockAnalysis dividend history table not found")


def nasdaq_rows(ticker: str) -> list[dict[str, Any]]:
    payload = request_json(f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(ticker)}/dividends?assetclass=etf")
    rows = []
    history = payload["data"]["dividends"]["rows"]
    if not isinstance(history, list) or not history:
        raise SourceError("Nasdaq returned no dividend history")
    for item in history:
        if item.get("currency", "USD") != "USD" or item.get("type") != "Cash":
            raise SourceError("Unsupported Nasdaq distribution currency/type")
        rows.append({"ticker": ticker, "ex_date": parse_date(item["exOrEffDate"]),
                     "record_date": parse_date(item["recordDate"]),
                     "payable_date": parse_date(item["paymentDate"]), "per_share": number(item["amount"])})
    return rows


def dividendhistory_rows(ticker: str) -> list[dict[str, Any]]:
    html = request_bytes(f"https://dividendhistory.org/payout/{urllib.parse.quote(ticker)}/", "text/html")
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    if not heading or not re.search(rf"\b{re.escape(ticker)}\b", heading.get_text(" ", strip=True)):
        raise SourceError("Wrong ticker or missing DividendHistory heading")
    table = soup.find("table", id="dividend-table")
    if table is None:
        raise SourceError("DividendHistory table not found")
    headers = [c.get_text(" ", strip=True) for c in table.find_all("th")]
    if headers != ["Ex-Dividend Date", "Payout Date", "Cash Amount", "Change / Status"]:
        raise SourceError("Unknown DividendHistory table structure")
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if not cells:
            continue
        if len(cells) != 4:
            raise SourceError("Malformed DividendHistory row")
        # This site mixes its own forecasts into the history table. They are
        # deliberately excluded, never promoted to announced dividends.
        if re.search(r"unconfirmed|estimated", cells[3], re.I) or "unconfirmed-div" in tr.get("class", []):
            continue
        if cells[3] and not re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", cells[3]):
            raise SourceError("Unknown DividendHistory confirmation status")
        rows.append({"ticker": ticker, "ex_date": parse_date(cells[0]), "record_date": None,
                     "payable_date": parse_date(cells[1]), "per_share": number(cells[2])})
    return rows


def validate_rows(rows: list[dict[str, Any]], ticker: str, info: dict[str, Any], today: date,
                  previous: list[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    monthly = info.get("source", "ishares") == "ishares"
    if len(rows) < (6 if monthly else 4):
        raise SourceError("Insufficient dividend history")
    seen = set()
    for row in rows:
        ex, pay, record = (row.get(k) for k in ("ex_date", "payable_date", "record_date"))
        amount = number(row.get("per_share"))
        if row.get("ticker") != ticker or not isinstance(ex, date) or not isinstance(pay, date):
            raise SourceError("Wrong ticker or missing ex/payment date")
        if amount is None or amount < 0 or not (ex <= pay <= ex + timedelta(days=120)):
            raise SourceError("Invalid dividend amount or date order")
        if record is not None and not ex - timedelta(days=7) <= record <= pay:
            raise SourceError("Invalid record date")
        if pay > today + timedelta(days=180):
            raise SourceError("Implausibly distant announced payment")
        key = (ex, pay)
        if key in seen:
            raise SourceError("Duplicate dividend record")
        seen.add(key)
    cutoff = min(today, date(int(info.get("maturity_year", 9999)), 12, 31))
    max_gap = 75 if monthly else 150
    if (cutoff - max(r["payable_date"] for r in rows)).days > max_gap:
        raise SourceError("Stale dividend history")
    beginning = cutoff - timedelta(days=365)
    if min(r["payable_date"] for r in rows) > beginning:
        raise SourceError("History does not cover the trailing year")
    recent = sorted({beginning, cutoff} | {r["payable_date"] for r in rows
                                           if beginning < r["payable_date"] < cutoff})
    if any((right - left).days > max_gap for left, right in zip(recent, recent[1:])):
        raise SourceError("Gap in trailing dividend history")
    # Protect previously announced distributions, including future announcements.
    # Ex-date is stable when a provider corrects a payable date.
    known = {r["ex_date"] for r in previous if r["payable_date"] >= today - timedelta(days=365)}
    if not known <= {r["ex_date"] for r in rows}:
        raise SourceError("Response dropped previously known distributions")
    return sorted(rows, key=lambda r: (r["payable_date"], r["ex_date"]))


def parse_schedule(pdf: bytes) -> list[date]:
    """Read only the Bond and Equity table and its regular date columns.

    Layout extraction preserves the column headed 'Potential Income'. Dates in
    that column (and tables for BALI/BALQ below it) must never become monthly pay.
    Unknown layouts fail closed and trigger the next schedule provider/cache.
    """
    for page in PdfReader(io.BytesIO(pdf)).pages:
        text = page.extract_text(extraction_mode="layout") or ""
        if not all(t in text for t in ("IBHF", "IBHG", "IBHK")):
            continue
        blocks = re.split(r"MONTHLY DISTRIBUTION", text)
        section = next((b for b in blocks[1:] if re.match(r"\s*[—–-]\s*Bond and Equity Funds", b)), None)
        if section is None:
            raise SourceError("Monthly Bond and Equity table not found")
        lines = section.splitlines()
        conditional = next((line.index("Potential Income") for line in lines if "Potential Income" in line), None)
        if conditional is None:
            raise SourceError("Conditional-distribution column not identified")
        dates = []
        for line in lines:
            if "PAY DATE:" not in line:
                continue
            # Text is aligned at column starts; a small tolerance handles glyph widths.
            regular = [m for m in DATE_TOKEN.finditer(line) if m.start() < conditional - 5]
            for match in regular:
                d = parse_date(match.group())
                if d is None or d.month == 1:
                    raise SourceError("Unexpected regular January distribution")
                dates.append(d)
        validate_schedule(dates)
        return sorted(dates)
    raise SourceError("iShares monthly-distribution page not found")


def validate_schedule(dates: list[date]) -> None:
    if len(dates) < 12 or len(set(dates)) != len(dates):
        raise SourceError("Incomplete or duplicated regular schedule")
    if any(not isinstance(d, date) or d.month == 1 or d.weekday() >= 5 for d in dates):
        raise SourceError("Invalid regular schedule date")
    for year in {d.year for d in dates}:
        annual = [d for d in dates if d.year == year]
        months = [d.month for d in annual]
        last_month = max(months)
        if any(months.count(month) != 1 for month in range(2, min(last_month, 11) + 1)):
            raise SourceError("Missing or duplicated regular monthly dates")
        if max(d.month for d in annual) == 12:
            if len(annual) != 12 or sum(d.month == 12 for d in annual) != 2:
                raise SourceError("Year-end regular schedule must contain two December payments")


def serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: (v.isoformat() if isinstance(v, date) else v)
             for k, v in row.items() if k in (*DATE_FIELDS, "ticker", "per_share")} for row in rows]


def deserialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, **{k: parse_date(r.get(k)) for k in DATE_FIELDS}} for r in rows]


class DividendSources:
    def __init__(self, cache_dir: Path, now: datetime | None = None, max_cache_days: int = 7):
        self.cache_dir = cache_dir
        self.now = now or datetime.now(timezone.utc)
        self.max_cache_days = max_cache_days
        self.status: dict[str, Any] = {"checked_at": self.now.isoformat(), "success": False,
                                       "sources": {}, "warnings": []}
        self.cache: dict[str, Any] = {"version": 1, "funds": {}}
        try:
            cached = json.loads((cache_dir / "last-good.json").read_text())
            if cached.get("version") == 1 and isinstance(cached.get("funds"), dict):
                self.cache = cached
        except (OSError, ValueError, AttributeError):
            pass

    def warn(self, message: str) -> None:
        self.status["warnings"].append(message)
        print(f"WARNING: {message}")

    def fresh(self, entry: dict[str, Any]) -> bool:
        try:
            age = self.now - datetime.fromisoformat(entry["fetched_at"])
            return timedelta(0) <= age <= timedelta(days=self.max_cache_days)
        except (KeyError, TypeError, ValueError):
            return False

    def fetch(self, ticker: str, info: dict[str, Any]) -> list[dict[str, Any]]:
        identity = {"source": info.get("source", "ishares"), "portfolio_id": info.get("portfolio_id")}
        old = self.cache["funds"].get(ticker, {})
        if (not isinstance(old, dict) or old.get("identity") != identity
                or not isinstance(old.get("provider"), str)):
            old = {}
        previous = []
        try:
            previous = validate_rows(deserialize_rows(old["rows"]), ticker, info, self.now.date())
        except (KeyError, TypeError, ValueError, SourceError):
            old = {}
        providers: list[tuple[str, Callable]] = []
        if identity["source"] == "ishares":
            providers.extend((name, lambda host=host: ishares_rows(ticker, int(info["portfolio_id"]), host))
                             for name, host in zip(("iShares", "BlackRock"), HOSTS))
        providers.extend((("StockAnalysis", lambda: stockanalysis_rows(ticker)),
                          ("Nasdaq", lambda: nasdaq_rows(ticker)),
                          ("DividendHistory", lambda: dividendhistory_rows(ticker))))
        errors = []
        for name, fetch in providers:
            try:
                rows = validate_rows(fetch(), ticker, info, self.now.date(), previous)
                self.cache["funds"][ticker] = {"identity": identity, "provider": name,
                    "fetched_at": self.now.isoformat(), "rows": serialize_rows(rows)}
                self.status["sources"][ticker] = {"provider": name, "cached": False, "failures": errors}
                if errors:
                    self.warn(f"{ticker}: switched to {name}; {'; '.join(errors)}")
                return [{**r, "provider": name, "cached": False} for r in rows]
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        if previous and self.fresh(old):
            self.status["sources"][ticker] = {"provider": old["provider"], "cached": True,
                                               "fetched_at": old["fetched_at"], "failures": errors}
            self.warn(f"{ticker}: all online sources failed; using cache verified {old['fetched_at']}")
            return [{**r, "provider": old["provider"], "cached": True} for r in previous]
        self.status["sources"][ticker] = {"failed": True, "failures": errors}
        raise SourceError(f"{ticker}: all dividend sources failed and no valid cache: {'; '.join(errors)}")

    def schedule(self) -> tuple[list[date], str]:
        errors = []
        for name, host in zip(("iShares", "BlackRock"), HOSTS):
            try:
                dates = parse_schedule(request_bytes(host + SCHEDULE_PATH, "application/pdf"))
                if max(dates) < self.now.date():
                    raise SourceError("Official schedule has expired")
                self.cache["schedule"] = {"fetched_at": self.now.isoformat(), "dates": [d.isoformat() for d in dates]}
                self.status["sources"]["schedule"] = {"provider": name, "cached": False, "failures": errors}
                if errors:
                    self.warn(f"Schedule switched to {name}: {'; '.join(errors)}")
                return dates, "官方支付日"
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        old = self.cache.get("schedule", {})
        try:
            dates = [parse_date(d) for d in old["dates"]]
            validate_schedule(dates)
            if not self.fresh(old) or max(dates) < self.now.date():
                raise SourceError("Expired schedule cache")
            self.warn(f"Using cached official schedule verified {old['fetched_at']}")
            self.status["sources"]["schedule"] = {"cached": True, "failures": errors}
            return dates, "官方支付日（缓存）"
        except (KeyError, TypeError, ValueError, SourceError):
            self.warn(f"Official schedule unavailable; projecting dates from verified history. {'; '.join(errors)}")
            self.status["sources"]["schedule"] = {"projected": True, "failures": errors}
            return [], "推算支付日"

    def equity_schedule(self, ticker: str) -> dict[tuple[int, int], dict[str, Any]]:
        from equity_schedules import fetch_dates, validate_dates

        result = {}
        if ticker not in ("QQQ", "VOO"):
            return result
        years = [self.now.year]
        if ticker == "VOO" and self.now.month >= 9:
            years.append(self.now.year + 1)
        for year in years:
            key = f"{ticker}-schedule-{year}"
            old = self.cache.get(key, {})
            try:
                rows = fetch_dates(ticker, year, (self.now.month - 1) // 3 + 1)
                self.cache[key] = {"fetched_at": self.now.isoformat(), "rows": rows}
                label = "官方支付日"
                self.status["sources"][key] = {"cached": False, "rows": rows}
            except Exception as exc:
                try:
                    if not self.fresh(old):
                        raise SourceError("Expired equity schedule cache")
                    rows = validate_dates(old["rows"], year)
                    label = "官方支付日（缓存）"
                    self.status["sources"][key] = {"cached": True, "rows": rows,
                        "fetched_at": old["fetched_at"], "failure": str(exc)}
                    self.warn(f"{key}: using verified cache; {exc}")
                except (KeyError, TypeError, ValueError, SourceError):
                    self.status["sources"][key] = {"projected": True, "failure": str(exc)}
                    self.warn(f"{key}: official dates unavailable; retaining projected dates; {exc}")
                    continue
            for row in rows:
                result[tuple(row["cycle"])] = {"payable_date": parse_date(row["payable_date"]),
                                               "date_status": label}
        return result

    def screener(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        if not tickers:
            return {}
        for name, host in zip(("iShares", "BlackRock"), HOSTS):
            try:
                products = {}
                for raw in request_json(host + SCREENER_PATH).values():
                    if not isinstance(raw, dict):
                        continue
                    ticker = raw.get("localExchangeTicker")
                    if isinstance(ticker, dict):
                        ticker = ticker.get("r") or ticker.get("d")
                    if ticker in tickers:
                        price = number(raw.get("navAmount"))
                        sec = number(raw.get("thirtyDaySecYield"))
                        trailing = number(raw.get("twelveMonTrlYield"))
                        rate = sec if sec is not None and sec > 0 else trailing
                        if price is not None and price > 0 and rate is not None and 0 < rate < 100:
                            products[ticker] = {"price": price, "yield_pct": rate, "provider": name,
                                                "method": "30日SEC收益率" if rate == sec else "12个月股息率"}
                if not set(tickers) <= products.keys():
                    raise SourceError("Missing/invalid NAV or yield for one or more funds")
                self.status["sources"]["metrics"] = {"provider": name}
                return products
            except Exception as exc:
                self.warn(f"{name} market metrics unavailable: {exc}")
        self.status["sources"]["metrics"] = {"provider": "validated dividend history"}
        return {}

    def save(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / "last-good.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(target)

    def save_status(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "source-status.json").write_text(
            json.dumps(self.status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
