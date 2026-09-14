#!/usr/bin/env python3
"""Build an Apple-compatible subscribed dividend calendar from iShares data."""

from __future__ import annotations

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

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
POSITIONS_FILE = ROOT / "positions.json"
OUTPUT_FILE = ROOT / "dividends.ics"

# Calendar window:
# - Keep the 3 full calendar months before the current month, plus current month.
# - Show current month through the same calendar month one year later.
HISTORY_MONTHS = 3
FUTURE_MONTHS = 12
LOCAL_TIMEZONE = ZoneInfo("Asia/Tokyo")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36"
)
API_PATH = "/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data"
API_HOSTS = ("https://www.ishares.com", "https://www.blackrock.com")
SCREENER_URL = (
    "https://www.ishares.com/us/product-screener/product-screener-v3.1.jsn"
    "?dcrPath=/templatedata/config/product-screener-v3/data/en/us-ishares/"
    "ishares-product-screener-backend-config&siteEntryPassthrough=true"
)
DISTRIBUTION_SCHEDULE_URL = (
    "https://www.ishares.com/us/literature/shareholder-letters/"
    "isharesandblackrocketfsdistributionschedule.pdf"
)
DATE_TOKEN_RE = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{2}\b")


def api_url(host: str, portfolio_id: int) -> str:
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
    return f"{host}{API_PATH}?{query}"


def request_bytes(url: str, accept: str, attempts: int = 3) -> bytes:
    errors: list[str] = []
    for attempt in range(attempts):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": accept},
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            errors.append(f"attempt {attempt + 1}: {exc}")
            if attempt + 1 < attempts:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Unable to fetch {url}: {'; '.join(errors)}")


def fetch_json(portfolio_id: int) -> dict[str, Any]:
    errors: list[str] = []
    for host in API_HOSTS:
        url = api_url(host, portfolio_id)
        try:
            return json.loads(request_bytes(url, "application/json", attempts=2).decode("utf-8-sig"))
        except (RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"{host}: {exc}")
    raise RuntimeError(f"Unable to fetch iShares data for portfolio {portfolio_id}: {'; '.join(errors)}")


def fetch_screener() -> dict[str, Any]:
    return json.loads(request_bytes(SCREENER_URL, "application/json", attempts=3).decode("utf-8-sig"))


def data_points(payload: dict[str, Any]) -> dict[str, Any]:
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


def scalar(value: Any) -> Any:
    """Read an iShares screener scalar, preferring raw (.r) over display (.d)."""
    if isinstance(value, dict):
        if value.get("r") not in (None, "", "-"):
            return value.get("r")
        return value.get("d")
    return value


def parse_date(value: Any) -> date | None:
    if value in (None, "", "-"):
        return None
    text = str(int(value)) if isinstance(value, (int, float)) else str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


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


def distributions_for(ticker: str, portfolio_id: int, shares: float) -> list[dict[str, Any]]:
    points = data_points(fetch_json(portfolio_id))
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
                "portfolio_id": portfolio_id,
                "ex_date": ex_date,
                "record_date": record,
                "payable_date": payable,
                "per_share": total,
                "amount": shares * total,
                "income": parse_number(cell("incomeAmount")),
                "stcg": parse_number(cell("shortTermCapitalGain")),
                "ltcg": parse_number(cell("longTermCapitalGain")),
                "roc": parse_number(cell("returnOnCapital")),
            }
        )
    return rows


def market_metrics(
    positions: dict[str, Any],
    rows_by_ticker: dict[str, list[dict[str, Any]]],
    today: date,
) -> dict[str, dict[str, Any]]:
    """
    Estimate a monthly cash distribution from current position market value and yield.

    Preferred yield = 30-day SEC yield (forward-looking for bond ETFs).
    Fallback = 12-month trailing yield from the iShares screener.
    Final fallback = realized distributions over the last 365 days / current NAV.
    Position market value is approximated as shares × current NAV.
    """
    screener = fetch_screener()
    by_ticker: dict[str, dict[str, Any]] = {}
    for raw in screener.values():
        if not isinstance(raw, dict):
            continue
        ticker = str(scalar(raw.get("localExchangeTicker")) or "").strip().upper()
        if ticker:
            by_ticker[ticker] = raw

    result: dict[str, dict[str, Any]] = {}
    for ticker, info in positions.items():
        product = by_ticker.get(ticker.upper())
        if not product:
            raise RuntimeError(f"{ticker}: not found in iShares product screener")

        nav = parse_number(product.get("navAmount"))
        sec_yield = parse_number(product.get("thirtyDaySecYield"))
        trailing_yield = parse_number(product.get("twelveMonTrlYield"))
        if nav is None or nav <= 0:
            raise RuntimeError(f"{ticker}: current NAV unavailable in iShares product screener")

        yield_pct: float | None = None
        yield_source = ""
        if sec_yield is not None and sec_yield > 0:
            yield_pct = sec_yield
            yield_source = "30日SEC收益率"
        elif trailing_yield is not None and trailing_yield > 0:
            yield_pct = trailing_yield
            yield_source = "12个月股息率"
        else:
            trailing_start = today - timedelta(days=365)
            per_share_12m = sum(
                float(r["per_share"])
                for r in rows_by_ticker.get(ticker, [])
                if trailing_start <= r["payable_date"] <= today
            )
            if per_share_12m > 0:
                yield_pct = per_share_12m / nav * 100.0
                yield_source = "近12个月实际分配率"

        if yield_pct is None or yield_pct <= 0:
            raise RuntimeError(f"{ticker}: no usable dividend/yield metric available")

        shares = float(info["shares"])
        market_value = shares * nav
        annual_income = market_value * yield_pct / 100.0
        monthly_amount = annual_income / 12.0
        monthly_per_share = nav * yield_pct / 100.0 / 12.0

        result[ticker] = {
            "nav": nav,
            "yield_pct": yield_pct,
            "yield_source": yield_source,
            "market_value": market_value,
            "monthly_amount": monthly_amount,
            "monthly_per_share": monthly_per_share,
        }
    return result


def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def fold_line(line: str, limit: int = 73) -> list[str]:
    """Fold an iCalendar line without splitting UTF-8 code points."""
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


def add_months_month_start(value: date, months: int) -> date:
    absolute = value.year * 12 + (value.month - 1) + months
    year, month0 = divmod(absolute, 12)
    return date(year, month0 + 1, 1)


def calendar_window(today: date) -> tuple[date, date, date]:
    current_month = date(today.year, today.month, 1)
    history_start = add_months_month_start(current_month, -HISTORY_MONTHS)
    future_end = add_months_month_start(current_month, FUTURE_MONTHS + 1)
    return history_start, current_month, future_end


def official_monthly_pay_dates(window_start: date, window_end: date) -> list[date]:
    """Read the current iShares distribution-schedule PDF and return monthly pay dates."""
    pdf = request_bytes(DISTRIBUTION_SCHEDULE_URL, "application/pdf")
    reader = PdfReader(io.BytesIO(pdf))

    monthly_text = ""
    for page in reader.pages:
        text = page.extract_text() or ""
        if "IBHF" in text and "IBHG" in text and "IBHK" in text:
            monthly_text = text
            break
    if not monthly_text:
        raise RuntimeError("Could not locate the iShares monthly-distribution page in the schedule PDF")

    pay_dates: set[date] = set()
    normalized = re.sub(r"[ \t]+", " ", monthly_text)
    for match in re.finditer(
        r"PAY DATE:\s*(.*?)(?=(?:DECLARATION DATE:|EX-DATE/RECORD DATE:|PAY DATE:|$))",
        normalized,
        re.S,
    ):
        block = match.group(1)
        for token in DATE_TOKEN_RE.findall(block):
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
        raise RuntimeError("No future monthly pay dates parsed from the iShares schedule PDF")
    return sorted(pay_dates)


def active_tickers_for_date(positions: dict[str, Any], payable: date) -> list[str]:
    active: list[str] = []
    for ticker, info in positions.items():
        maturity_year = int(info.get("maturity_year", 9999))
        if payable.year <= maturity_year:
            active.append(ticker)
    return sorted(active)


def month_key(value: date) -> tuple[int, int]:
    return value.year, value.month


def build_calendar(
    events_by_date: dict[date, list[dict[str, Any]]],
    positions: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
    scheduled_dates: list[date],
    history_start: date,
    current_month: date,
    future_end: date,
) -> str:
    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Dividend Calendar//iShares Portfolio//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:ETF 股息",
        "X-WR-CALDESC:保留过去3个完整日历月 + 当前月；未来12个月按当前市值与股息率估算，正式公布后自动替换为实际金额",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    lines: list[str] = []
    for line in header:
        lines.extend(fold_line(line))

    actual_dates = {d for d in events_by_date if history_start <= d < future_end}
    scheduled_set = {d for d in scheduled_dates if current_month <= d < future_end}
    all_dates = sorted(actual_dates | scheduled_set)

    actual_by_month_ticker: dict[tuple[int, int], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for payable, rows in events_by_date.items():
        if not (current_month <= payable < future_end):
            continue
        key = month_key(payable)
        for row in rows:
            actual_by_month_ticker[key][row["ticker"]] += float(row["amount"])

    unknown_dates_by_month: dict[tuple[int, int], list[date]] = defaultdict(list)
    for payable in scheduled_set:
        if not events_by_date.get(payable):
            unknown_dates_by_month[month_key(payable)].append(payable)
    for dates in unknown_dates_by_month.values():
        dates.sort()

    now_stamp = datetime.now(LOCAL_TIMEZONE).astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")

    for payable in all_dates:
        rows = sorted(events_by_date.get(payable, []), key=lambda r: r["ticker"])
        if rows:
            total_amount = sum(r["amount"] for r in rows)
            title = f"${total_amount:,.2f}"
            detail_lines = [f"已公布税前股息总额: ${total_amount:,.2f}", ""]
            for r in rows:
                detail_lines.append(
                    f"{r['ticker']}: {r['shares']:,.4f}股 × ${r['per_share']:.6f} = ${r['amount']:,.2f}"
                )
                detail_lines.append(
                    f"  除息 {fmt_date(r['ex_date'])} · 登记 {fmt_date(r['record_date'])} · 支付 {fmt_date(r['payable_date'])}"
                )
            detail_lines.extend(
                [
                    "",
                    "数据源: iShares / BlackRock 官方 fundDownload API",
                    "金额按当前持股数量计算；实际到账可能因税务、券商处理和持仓变动而不同。",
                ]
            )
        else:
            tickers = active_tickers_for_date(positions, payable)
            if not tickers:
                continue
            key = month_key(payable)
            unknown_count = max(1, len(unknown_dates_by_month.get(key, [payable])))

            estimate_rows: list[tuple[str, float]] = []
            total_estimate = 0.0
            for ticker in tickers:
                m = metrics[ticker]
                monthly_target = float(m["monthly_amount"])
                announced_this_month = actual_by_month_ticker[key].get(ticker, 0.0)
                remaining = max(monthly_target - announced_this_month, 0.0)
                event_estimate = remaining / unknown_count
                estimate_rows.append((ticker, event_estimate))
                total_estimate += event_estimate

            title = f"≈${total_estimate:,.2f}"
            detail_lines = [
                f"预估税前股息总额: ≈${total_estimate:,.2f}",
                f"iShares 官方计划支付日: {payable.isoformat()}",
                "",
                "估算方法: 当前持仓市值(NAV×股数) × 当前股息率 ÷ 12。",
                "债券ETF优先使用30日SEC收益率；缺失时使用12个月股息率。",
                "同一月份若有多个尚未公布的支付日，则将该月剩余预估金额平均分配。",
                "",
            ]
            for ticker, amount in estimate_rows:
                shares = float(positions[ticker]["shares"])
                m = metrics[ticker]
                detail_lines.append(
                    f"{ticker}: {shares:,.4f}股 · NAV ${m['nav']:.2f} · "
                    f"市值≈${m['market_value']:,.2f} · {m['yield_source']} {m['yield_pct']:.2f}% "
                    f"→ 本次≈${amount:,.2f}"
                )
            detail_lines.extend(
                [
                    "",
                    "数据源: iShares / BlackRock 官方产品数据与 Fund Distributions Schedule",
                    "这是预估值；当 iShares 正式公布每股分配后，会自动替换为实际金额。",
                ]
            )

        next_day = payable + timedelta(days=1)
        event = [
            "BEGIN:VEVENT",
            f"UID:dividend-{payable.strftime('%Y%m%d')}@dividend-calendar",
            f"DTSTAMP:{now_stamp}",
            f"DTSTART;VALUE=DATE:{payable.strftime('%Y%m%d')}",
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
    positions = config["positions"]
    today_local = datetime.now(LOCAL_TIMEZONE).date()
    history_start, current_month, future_end = calendar_window(today_local)

    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    rows_by_ticker: dict[str, list[dict[str, Any]]] = {}
    failures: list[str] = []
    for ticker, info in positions.items():
        try:
            rows = distributions_for(ticker, int(info["portfolio_id"]), float(info["shares"]))
            rows_by_ticker[ticker] = rows
            for row in rows:
                payable = row["payable_date"]
                if history_start <= payable < future_end:
                    grouped[payable].append(row)
            print(f"{ticker}: {len(rows)} distribution records")
        except Exception as exc:
            failures.append(f"{ticker}: {exc}")

    if failures:
        raise RuntimeError("One or more funds failed; calendar not replaced. " + " | ".join(failures))

    metrics = market_metrics(positions, rows_by_ticker, today_local)
    for ticker in sorted(metrics):
        m = metrics[ticker]
        print(
            f"{ticker}: NAV=${m['nav']:.2f}, market_value≈${m['market_value']:,.2f}, "
            f"{m['yield_source']}={m['yield_pct']:.2f}%, monthly_est≈${m['monthly_amount']:,.2f}"
        )

    scheduled_dates = official_monthly_pay_dates(current_month, future_end)
    calendar = build_calendar(
        grouped,
        positions,
        metrics,
        scheduled_dates,
        history_start,
        current_month,
        future_end,
    )
    temp = OUTPUT_FILE.with_suffix(".ics.tmp")
    temp.write_text(calendar, encoding="utf-8", newline="")
    temp.replace(OUTPUT_FILE)

    actual_count = sum(1 for d in grouped if history_start <= d < future_end)
    estimate_count = sum(1 for d in scheduled_dates if d not in grouped)
    print(
        f"Wrote {OUTPUT_FILE.name}; history_start={history_start.isoformat()}, "
        f"future_end_exclusive={future_end.isoformat()}, actual_dates={actual_count}, "
        f"estimated_future_dates={estimate_count}, Tokyo date={today_local.isoformat()}"
    )


if __name__ == "__main__":
    main()
