#!/usr/bin/env python3
"""Build an Apple-compatible subscribed dividend calendar for the portfolio."""

from __future__ import annotations

import calendar as monthcalendar
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
POSITIONS_FILE = ROOT / "positions.json"
OUTPUT_FILE = ROOT / "dividends.ics"

HISTORY_MONTHS = 3
FUTURE_MONTHS = 12
LOCAL_TIMEZONE = ZoneInfo("Asia/Tokyo")
UTC = ZoneInfo("UTC")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36"
)

ISHARES_API_PATH = "/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data"
ISHARES_API_HOSTS = ("https://www.ishares.com", "https://www.blackrock.com")
ISHARES_SCREENER_URL = (
    "https://www.ishares.com/us/product-screener/product-screener-v3.1.jsn"
    "?dcrPath=/templatedata/config/product-screener-v3/data/en/us-ishares/"
    "ishares-product-screener-backend-config&siteEntryPassthrough=true"
)
ISHARES_DISTRIBUTION_SCHEDULE_URL = (
    "https://www.ishares.com/us/literature/shareholder-letters/"
    "isharesandblackrocketfsdistributionschedule.pdf"
)
DATE_TOKEN_RE = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{2}\b")


def request_bytes(url: str, accept: str, attempts: int = 3) -> bytes:
    errors: list[str] = []
    for attempt in range(attempts):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": accept,
                "Accept-Language": "en-US,en;q=0.8",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            errors.append(f"attempt {attempt + 1}: {exc}")
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Unable to fetch {url}: {'; '.join(errors)}")


def request_json(url: str, attempts: int = 3) -> dict[str, Any]:
    raw = request_bytes(url, "application/json,text/plain,*/*", attempts=attempts)
    return json.loads(raw.decode("utf-8-sig"))


def ishares_api_url(host: str, portfolio_id: int) -> str:
    query = urllib.parse.urlencode(
        {
            "appSubType": "ISHARES",
            "appType": "PRODUCT_PAGE",
            "component": "fundDownload",
            "locale": "en_US",
            "portfolioId": str(portfolio_id),
            "targetSite": "us-ishares",
            "userType": "individual",
            "excludeContent": "true",
        }
    )
    return f"{host}{ISHARES_API_PATH}?{query}"


def fetch_ishares_json(portfolio_id: int) -> dict[str, Any]:
    errors: list[str] = []
    for host in ISHARES_API_HOSTS:
        try:
            return request_json(ishares_api_url(host, portfolio_id), attempts=2)
        except (RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"{host}: {exc}")
    raise RuntimeError(
        f"Unable to fetch iShares data for portfolio {portfolio_id}: {'; '.join(errors)}"
    )


def fetch_ishares_screener() -> dict[str, Any]:
    return request_json(ISHARES_SCREENER_URL, attempts=3)


def scalar(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("r") not in (None, "", "-"):
            return value.get("r")
        return value.get("d")
    return value


def parse_number(value: Any) -> float | None:
    value = scalar(value)
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return float(text)
    except ValueError:
        return None


def parse_date(value: Any) -> date | None:
    if value in (None, "", "-"):
        return None
    text = str(int(value)) if isinstance(value, (int, float)) else str(value).strip()
    for fmt in (
        "%Y%m%d",
        "%Y-%m-%d",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d-%b-%y",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def ishares_data_points(payload: dict[str, Any]) -> dict[str, Any]:
    return (
        payload.get("componentsByNameMap", {})
        .get("fundDownload", {})
        .get("containersByNameMap", {})
        .get("distributions", {})
        .get("dataPointsByNameMap", {})
    )


def column(points: dict[str, Any], name: str) -> list[Any]:
    value = points.get(name, {}).get("value", [])
    return value if isinstance(value, list) else []


def ishares_distributions_for(
    ticker: str, portfolio_id: int, shares: float
) -> list[dict[str, Any]]:
    points = ishares_data_points(fetch_ishares_json(portfolio_id))
    names = [
        "exDate",
        "recordDate",
        "payableDate",
        "totalDistribution",
        "incomeAmount",
        "shortTermCapitalGain",
        "longTermCapitalGain",
        "returnOnCapital",
    ]
    count = max((len(column(points, name)) for name in names), default=0)
    rows: list[dict[str, Any]] = []

    for i in range(count):
        def cell(name: str) -> Any:
            values = column(points, name)
            return values[i] if i < len(values) else None

        total = parse_number(cell("totalDistribution"))
        payable = parse_date(cell("payableDate"))
        ex_date = parse_date(cell("exDate"))
        record = parse_date(cell("recordDate"))
        if total is None or payable is None:
            continue
        rows.append(
            {
                "ticker": ticker,
                "shares": shares,
                "source": "ishares",
                "ex_date": ex_date,
                "record_date": record,
                "payable_date": payable,
                "per_share": total,
                "amount": shares * total,
            }
        )
    return rows


def stockanalysis_data(
    ticker: str, shares: float
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Read QQQ/VOO price, yield and dividend history from StockAnalysis."""
    url = f"https://stockanalysis.com/etf/{ticker.lower()}/dividend/"
    html = request_bytes(url, "text/html,application/xhtml+xml", attempts=3).decode(
        "utf-8", errors="replace"
    )
    soup = BeautifulSoup(html, "html.parser")
    page_text = " ".join(soup.stripped_strings)

    yield_match = re.search(r"Dividend Yield\s+([0-9]+(?:\.[0-9]+)?)%", page_text, re.I)
    annual_match = re.search(r"Annual Dividend\s+\$([0-9]+(?:\.[0-9]+)?)", page_text, re.I)
    price_match = re.search(
        r"Real-Time Price\s*(?:·|\u00b7)?\s*USD\s+([0-9,]+(?:\.[0-9]+)?)",
        page_text,
        re.I,
    )

    yield_pct = float(yield_match.group(1)) if yield_match else None
    annual_dividend = float(annual_match.group(1)) if annual_match else None
    price = parse_number(price_match.group(1)) if price_match else None

    rows: list[dict[str, Any]] = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        ex_date = parse_date(cells[0])
        amount = parse_number(cells[1])
        record_date = parse_date(cells[2])
        pay_date = parse_date(cells[3])
        if ex_date is None or amount is None or pay_date is None:
            continue
        rows.append(
            {
                "ticker": ticker,
                "shares": shares,
                "source": "stockanalysis",
                "ex_date": ex_date,
                "record_date": record_date,
                "payable_date": pay_date,
                "per_share": amount,
                "amount": shares * amount,
            }
        )

    rows.sort(key=lambda r: r["payable_date"])
    if not rows:
        raise RuntimeError(f"{ticker}: dividend history table not found on StockAnalysis")

    latest_four = rows[-4:]
    trailing_from_rows = sum(float(r["per_share"]) for r in latest_four)
    if annual_dividend is None or annual_dividend <= 0:
        annual_dividend = trailing_from_rows

    if price is None or price <= 0:
        if yield_pct is not None and yield_pct > 0 and annual_dividend > 0:
            price = annual_dividend / (yield_pct / 100.0)
        else:
            raise RuntimeError(f"{ticker}: current price unavailable")

    if yield_pct is None or yield_pct <= 0:
        yield_pct = annual_dividend / price * 100.0

    return rows, {
        "price": float(price),
        "yield_pct": float(yield_pct),
        "annual_dividend": float(annual_dividend),
    }


def market_metrics(
    positions: dict[str, Any],
    rows_by_ticker: dict[str, list[dict[str, Any]]],
    stockanalysis_metrics: dict[str, dict[str, float]],
    today: date,
) -> dict[str, dict[str, Any]]:
    ishares_products: dict[str, dict[str, Any]] = {}
    if any(info.get("source", "ishares") == "ishares" for info in positions.values()):
        screener = fetch_ishares_screener()
        for raw in screener.values():
            if not isinstance(raw, dict):
                continue
            ticker = str(scalar(raw.get("localExchangeTicker")) or "").strip().upper()
            if ticker:
                ishares_products[ticker] = raw

    trailing_start = today - timedelta(days=365)
    result: dict[str, dict[str, Any]] = {}

    for ticker, info in positions.items():
        source = info.get("source", "ishares")
        shares = float(info["shares"])

        if source == "ishares":
            product = ishares_products.get(ticker.upper())
            if not product:
                raise RuntimeError(f"{ticker}: not found in iShares product screener")
            price = parse_number(product.get("navAmount"))
            sec_yield = parse_number(product.get("thirtyDaySecYield"))
            trailing_yield = parse_number(product.get("twelveMonTrlYield"))
            if price is None or price <= 0:
                raise RuntimeError(f"{ticker}: current NAV unavailable")

            if sec_yield is not None and sec_yield > 0:
                yield_pct = sec_yield
                yield_source = "30日SEC收益率"
            elif trailing_yield is not None and trailing_yield > 0:
                yield_pct = trailing_yield
                yield_source = "12个月股息率"
            else:
                per_share_12m = sum(
                    float(r["per_share"])
                    for r in rows_by_ticker.get(ticker, [])
                    if trailing_start <= r["payable_date"] <= today
                )
                if per_share_12m <= 0:
                    raise RuntimeError(f"{ticker}: no usable yield metric")
                yield_pct = per_share_12m / price * 100.0
                yield_source = "近12个月实际分配率"
            price_label = "NAV"
            frequency = 12
        else:
            raw = stockanalysis_metrics[ticker]
            price = float(raw["price"])
            yield_pct = float(raw["yield_pct"])
            yield_source = "近12个月股息率"
            price_label = "现价"
            frequency = int(info.get("frequency", 4))

        market_value = shares * price
        annual_income = market_value * yield_pct / 100.0
        result[ticker] = {
            "source": source,
            "price": price,
            "price_label": price_label,
            "yield_pct": yield_pct,
            "yield_source": yield_source,
            "market_value": market_value,
            "annual_income": annual_income,
            "frequency": frequency,
            "monthly_amount": annual_income / 12.0,
            "event_amount": annual_income / max(frequency, 1),
        }
    return result


def add_months_month_start(value: date, months: int) -> date:
    absolute = value.year * 12 + (value.month - 1) + months
    year, month0 = divmod(absolute, 12)
    return date(year, month0 + 1, 1)


def calendar_window(today: date) -> tuple[date, date, date]:
    current_month = date(today.year, today.month, 1)
    history_start = add_months_month_start(current_month, -HISTORY_MONTHS)
    future_end = add_months_month_start(current_month, FUTURE_MONTHS + 1)
    return history_start, current_month, future_end


def official_ishares_pay_dates(window_start: date, window_end: date) -> list[date]:
    pdf = request_bytes(ISHARES_DISTRIBUTION_SCHEDULE_URL, "application/pdf")
    reader = PdfReader(io.BytesIO(pdf))
    monthly_text = ""
    for page in reader.pages:
        text = page.extract_text() or ""
        if "IBHF" in text and "IBHG" in text and "IBHK" in text:
            monthly_text = text
            break
    if not monthly_text:
        raise RuntimeError("Could not locate iShares monthly-distribution schedule page")

    pay_dates: set[date] = set()
    normalized = re.sub(r"[ \t]+", " ", monthly_text)
    for match in re.finditer(
        r"PAY DATE:\s*(.*?)(?=(?:DECLARATION DATE:|EX-DATE/RECORD DATE:|PAY DATE:|$))",
        normalized,
        re.S,
    ):
        for token in DATE_TOKEN_RE.findall(match.group(1)):
            parsed = parse_date(token)
            if parsed and window_start <= parsed < window_end:
                pay_dates.add(parsed)

    if not pay_dates:
        for line in monthly_text.splitlines():
            if "PAY DATE:" not in line:
                continue
            for token in DATE_TOKEN_RE.findall(line):
                parsed = parse_date(token)
                if parsed and window_start <= parsed < window_end:
                    pay_dates.add(parsed)

    if not pay_dates:
        raise RuntimeError("No future iShares pay dates parsed from official schedule")
    return sorted(pay_dates)


def safe_date(year: int, month: int, day: int) -> date:
    last_day = monthcalendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def previous_business_day_if_weekend(value: date) -> date:
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def projected_equity_dates(
    rows: list[dict[str, Any]], window_start: date, window_end: date
) -> list[date]:
    """Project future quarterly payment dates from the latest payment-date pattern."""
    historical = sorted({r["payable_date"] for r in rows})
    if len(historical) < 4:
        raise RuntimeError("Not enough dividend history to project quarterly dates")

    patterns = historical[-4:]
    projected: set[date] = set()
    for year in range(window_start.year - 1, window_end.year + 2):
        for pattern in patterns:
            d = previous_business_day_if_weekend(
                safe_date(year, pattern.month, pattern.day)
            )
            if window_start <= d < window_end:
                projected.add(d)
    return sorted(projected)


def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def fold_line(line: str, limit: int = 73) -> list[str]:
    if len(line.encode("utf-8")) <= limit:
        return [line]
    out: list[str] = []
    current = ""
    current_bytes = 0
    first = True
    for ch in line:
        ch_bytes = len(ch.encode("utf-8"))
        effective_limit = limit if first else limit - 1
        if current and current_bytes + ch_bytes > effective_limit:
            out.append(current if first else " " + current)
            first = False
            current = ch
            current_bytes = ch_bytes
        else:
            current += ch
            current_bytes += ch_bytes
    if current:
        out.append(current if first else " " + current)
    return out


def fmt_date(value: date | None) -> str:
    return value.isoformat() if value else "-"


def month_key(value: date) -> tuple[int, int]:
    return value.year, value.month


def active_ishares_tickers(
    positions: dict[str, Any], payable: date
) -> list[str]:
    active: list[str] = []
    for ticker, info in positions.items():
        if info.get("source", "ishares") != "ishares":
            continue
        maturity_year = int(info.get("maturity_year", 9999))
        if payable.year <= maturity_year:
            active.append(ticker)
    return sorted(active)


def build_calendar(
    events_by_date: dict[date, list[dict[str, Any]]],
    positions: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
    scheduled_tickers: dict[date, set[str]],
    history_start: date,
    current_month: date,
    future_end: date,
) -> str:
    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Dividend Calendar//Portfolio//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:ETF 股息",
        "X-WR-CALDESC:过去3个完整日历月+当前月；未来12个月按当前市值与股息率预估；正式公布后自动替换实际金额",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    lines: list[str] = []
    for line in header:
        lines.extend(fold_line(line))

    actual_dates = {d for d in events_by_date if history_start <= d < future_end}
    scheduled_dates = {d for d in scheduled_tickers if current_month <= d < future_end}
    all_dates = sorted(actual_dates | scheduled_dates)

    actual_by_month_ticker: dict[tuple[int, int], dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    actual_tickers_by_date: dict[date, set[str]] = defaultdict(set)
    for payable, rows in events_by_date.items():
        if not (history_start <= payable < future_end):
            continue
        for row in rows:
            actual_tickers_by_date[payable].add(row["ticker"])
            if current_month <= payable < future_end:
                actual_by_month_ticker[month_key(payable)][row["ticker"]] += float(
                    row["amount"]
                )

    unknown_ishares_count: dict[tuple[str, int, int], int] = defaultdict(int)
    for payable, tickers in scheduled_tickers.items():
        if not (current_month <= payable < future_end):
            continue
        for ticker in tickers:
            if metrics[ticker]["source"] != "ishares":
                continue
            if ticker not in actual_tickers_by_date.get(payable, set()):
                unknown_ishares_count[(ticker, payable.year, payable.month)] += 1

    now_stamp = datetime.now(LOCAL_TIMEZONE).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    for event_date in all_dates:
        actual_rows = sorted(events_by_date.get(event_date, []), key=lambda r: r["ticker"])
        actual_tickers = {r["ticker"] for r in actual_rows}
        estimate_tickers = sorted(scheduled_tickers.get(event_date, set()) - actual_tickers)

        estimate_rows: list[tuple[str, float]] = []
        for ticker in estimate_tickers:
            m = metrics[ticker]
            if m["source"] == "ishares":
                key = month_key(event_date)
                announced = actual_by_month_ticker[key].get(ticker, 0.0)
                remaining = max(float(m["monthly_amount"]) - announced, 0.0)
                count = max(
                    1,
                    unknown_ishares_count.get(
                        (ticker, event_date.year, event_date.month), 1
                    ),
                )
                amount = remaining / count
            else:
                amount = float(m["event_amount"])
            if amount > 0:
                estimate_rows.append((ticker, amount))

        actual_total = sum(float(r["amount"]) for r in actual_rows)
        estimate_total = sum(amount for _, amount in estimate_rows)
        total_amount = actual_total + estimate_total
        if total_amount <= 0:
            continue

        title = f"${total_amount:,.2f}"
        if estimate_rows:
            detail_lines = [f"税前股息总额: ${total_amount:,.2f}（含预估）", ""]
        else:
            detail_lines = [f"已公布税前股息总额: ${total_amount:,.2f}", ""]

        for r in actual_rows:
            detail_lines.append(
                f"{r['ticker']}: {r['shares']:,.4f}股 × ${r['per_share']:.6f} = ${r['amount']:,.2f}"
            )
            detail_lines.append(
                f"  除息 {fmt_date(r['ex_date'])} · 登记 {fmt_date(r['record_date'])} · 支付 {fmt_date(r['payable_date'])}"
            )

        if estimate_rows:
            if actual_rows:
                detail_lines.append("")
            detail_lines.append("预估部分:")
            for ticker, amount in estimate_rows:
                shares = float(positions[ticker]["shares"])
                m = metrics[ticker]
                detail_lines.append(
                    f"{ticker}: {shares:,.4f}股 · {m['price_label']} ${m['price']:.2f} · "
                    f"市值 ${m['market_value']:,.2f} · {m['yield_source']} {m['yield_pct']:.2f}% "
                    f"→ 本次 ${amount:,.2f}"
                )
            if any(metrics[t]["source"] == "ishares" for t, _ in estimate_rows):
                detail_lines.append("iShares 债券ETF按年化收益率折算月度预估。")
            if any(metrics[t]["source"] != "ishares" for t, _ in estimate_rows):
                detail_lines.append("QQQ/VOO按近12个月股息率与当前市值估算季度股息；未来支付日按近期季度支付节奏推算。")

        detail_lines.extend(
            [
                "",
                "数据源: iShares / BlackRock 官方数据；QQQ/VOO 股息数据使用 StockAnalysis（S&P Global Market Intelligence 数据）。",
                "预估值会随价格、股息率与正式分配数据自动更新。金额均按当前持股数量计算，未计税费。",
            ]
        )

        next_day = event_date + timedelta(days=1)
        event = [
            "BEGIN:VEVENT",
            f"UID:dividend-{event_date.strftime('%Y%m%d')}@dividend-calendar",
            f"DTSTAMP:{now_stamp}",
            f"DTSTART;VALUE=DATE:{event_date.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{next_day.strftime('%Y%m%d')}",
            f"SUMMARY:{escape_text(title)}",
            f"DESCRIPTION:{escape_text(chr(10).join(detail_lines))}",
            "CATEGORIES:股息,Dividend",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
        for line in event:
            lines.extend(fold_line(line))

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main() -> None:
    config = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    positions: dict[str, Any] = config["positions"]
    today_local = datetime.now(LOCAL_TIMEZONE).date()
    history_start, current_month, future_end = calendar_window(today_local)

    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    rows_by_ticker: dict[str, list[dict[str, Any]]] = {}
    stock_metrics: dict[str, dict[str, float]] = {}
    failures: list[str] = []

    for ticker, info in positions.items():
        try:
            shares = float(info["shares"])
            source = info.get("source", "ishares")
            if source == "ishares":
                rows = ishares_distributions_for(
                    ticker, int(info["portfolio_id"]), shares
                )
            else:
                rows, external_metrics = stockanalysis_data(ticker, shares)
                stock_metrics[ticker] = external_metrics
            rows_by_ticker[ticker] = rows
            for row in rows:
                d = row["payable_date"]
                if history_start <= d < future_end:
                    grouped[d].append(row)
            print(f"{ticker}: {len(rows)} distribution records ({source})")
        except Exception as exc:
            failures.append(f"{ticker}: {exc}")

    if failures:
        raise RuntimeError(
            "One or more funds failed; calendar not replaced. " + " | ".join(failures)
        )

    metrics = market_metrics(positions, rows_by_ticker, stock_metrics, today_local)
    for ticker in sorted(metrics):
        m = metrics[ticker]
        print(
            f"{ticker}: {m['price_label']}=${m['price']:.2f}, market_value=${m['market_value']:,.2f}, "
            f"{m['yield_source']}={m['yield_pct']:.2f}%, annual_est=${m['annual_income']:,.2f}"
        )

    scheduled_tickers: dict[date, set[str]] = defaultdict(set)

    if any(info.get("source", "ishares") == "ishares" for info in positions.values()):
        for d in official_ishares_pay_dates(current_month, future_end):
            for ticker in active_ishares_tickers(positions, d):
                scheduled_tickers[d].add(ticker)

    for ticker, info in positions.items():
        if info.get("source", "ishares") == "ishares":
            continue
        projected = projected_equity_dates(
            rows_by_ticker[ticker], current_month, future_end
        )
        for d in projected:
            scheduled_tickers[d].add(ticker)
        print(f"{ticker}: projected future payment dates = {len(projected)}")

    calendar_text = build_calendar(
        grouped,
        positions,
        metrics,
        scheduled_tickers,
        history_start,
        current_month,
        future_end,
    )
    temp = OUTPUT_FILE.with_suffix(".ics.tmp")
    temp.write_text(calendar_text, encoding="utf-8", newline="")
    temp.replace(OUTPUT_FILE)

    print(
        f"Wrote {OUTPUT_FILE.name}; history_start={history_start.isoformat()}, "
        f"future_end_exclusive={future_end.isoformat()}, actual_dates={len(grouped)}, "
        f"scheduled_dates={len(scheduled_tickers)}, Tokyo date={today_local.isoformat()}"
    )


if __name__ == "__main__":
    main()
