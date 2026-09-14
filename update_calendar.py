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

# Calendar window policy:
# - Keep the 3 full calendar months before the current month, plus the current month.
# - Keep the current month through the same calendar month one year later.
# Example for Sep 2026: keep actual history from Jun 1, 2026 onward and scheduled
# future pay dates through Sep 30, 2027. On Oct 1, June rolls off automatically.
HISTORY_MONTHS = 3
FUTURE_MONTHS = 12
LOCAL_TIMEZONE = ZoneInfo("Asia/Tokyo")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36"
)
API_PATH = "/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data"
API_HOSTS = ("https://www.ishares.com", "https://www.blackrock.com")
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
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("$", "").replace(",", "")
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
    """Move a first-of-month date by a whole number of months."""
    absolute = value.year * 12 + (value.month - 1) + months
    year, month0 = divmod(absolute, 12)
    return date(year, month0 + 1, 1)


def calendar_window(today: date) -> tuple[date, date, date]:
    """Return history start, current-month start, and exclusive future end."""
    current_month = date(today.year, today.month, 1)
    history_start = add_months_month_start(current_month, -HISTORY_MONTHS)
    # Exclusive end: first day of the month after the same month next year.
    future_end = add_months_month_start(current_month, FUTURE_MONTHS + 1)
    return history_start, current_month, future_end


def official_monthly_pay_dates(window_start: date, window_end: date) -> list[date]:
    """Read the current iShares distribution-schedule PDF and return monthly pay dates."""
    pdf = request_bytes(DISTRIBUTION_SCHEDULE_URL, "application/pdf")
    reader = PdfReader(io.BytesIO(pdf))

    # The monthly bond-fund page contains all six tracked tickers. Restrict parsing to
    # that page so weekly/quarterly schedules elsewhere in the PDF cannot leak in.
    monthly_text = ""
    for page in reader.pages:
        text = page.extract_text() or ""
        if "IBHF" in text and "IBHG" in text and "IBHK" in text:
            monthly_text = text
            break
    if not monthly_text:
        raise RuntimeError("Could not locate the iShares monthly-distribution page in the schedule PDF")

    pay_dates: set[date] = set()
    # Extract every PAY DATE row. The same page carries the 2026/2027/2028 monthly
    # rows, including any explicitly scheduled year-end/potential-income pay dates.
    normalized = re.sub(r"[ \t]+", " ", monthly_text)
    for match in re.finditer(r"PAY DATE:\s*(.*?)(?=(?:DECLARATION DATE:|EX-DATE/RECORD DATE:|PAY DATE:|$))", normalized, re.S):
        block = match.group(1)
        for token in DATE_TOKEN_RE.findall(block):
            parsed = parse_date(token)
            if parsed and window_start <= parsed < window_end:
                pay_dates.add(parsed)

    if not pay_dates:
        # Some PDF extractors preserve rows more reliably line-by-line.
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


def build_calendar(
    events_by_date: dict[date, list[dict[str, Any]]],
    positions: dict[str, Any],
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
        "X-WR-CALDESC:保留过去3个完整日历月 + 当前月，并显示当前月至未来12个月的iShares官方计划支付日；已公布金额自动更新",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    lines: list[str] = []
    for line in header:
        lines.extend(fold_line(line))

    actual_dates = {d for d in events_by_date if history_start <= d < future_end}
    placeholder_dates = {d for d in scheduled_dates if current_month <= d < future_end}
    all_dates = sorted(actual_dates | placeholder_dates)

    for payable in all_dates:
        rows = sorted(events_by_date.get(payable, []), key=lambda r: r["ticker"])
        if rows:
            total_amount = sum(r["amount"] for r in rows)
            title = f"${total_amount:,.2f}"
            detail_lines = [f"税前股息总额: ${total_amount:,.2f}", ""]
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
                    "金额为税前估算，实际到账可能因税务、券商处理和持仓变动而不同。",
                ]
            )
        else:
            tickers = active_tickers_for_date(positions, payable)
            if not tickers:
                continue
            title = "$待公布"
            detail_lines = [
                "股息金额尚未公布。",
                f"iShares 官方计划支付日: {payable.isoformat()}",
                "",
                "预计仍在存续的当前持仓:",
            ]
            for ticker in tickers:
                shares = float(positions[ticker]["shares"])
                detail_lines.append(f"{ticker}: {shares:,.4f}股 — 每股分配待公布")
            detail_lines.extend(
                [
                    "",
                    "数据源: iShares / BlackRock 官方 Fund Distributions Schedule",
                    "当 iShares 正式宣告分配金额后，本事件会自动更新为 $金额。",
                ]
            )

        next_day = payable + timedelta(days=1)
        event = [
            "BEGIN:VEVENT",
            f"UID:dividend-{payable.strftime('%Y%m%d')}@dividend-calendar",
            f"DTSTAMP:{datetime.now(LOCAL_TIMEZONE).astimezone(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ')}",
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
    failures: list[str] = []
    for ticker, info in positions.items():
        try:
            rows = distributions_for(ticker, int(info["portfolio_id"]), float(info["shares"]))
            for row in rows:
                payable = row["payable_date"]
                if history_start <= payable < future_end:
                    grouped[payable].append(row)
            print(f"{ticker}: {len(rows)} distribution records")
        except Exception as exc:
            failures.append(f"{ticker}: {exc}")

    if failures:
        raise RuntimeError("One or more funds failed; calendar not replaced. " + " | ".join(failures))

    scheduled_dates = official_monthly_pay_dates(current_month, future_end)
    calendar = build_calendar(
        grouped,
        positions,
        scheduled_dates,
        history_start,
        current_month,
        future_end,
    )
    temp = OUTPUT_FILE.with_suffix(".ics.tmp")
    temp.write_text(calendar, encoding="utf-8", newline="")
    temp.replace(OUTPUT_FILE)

    actual_count = sum(1 for d in grouped if history_start <= d < future_end)
    placeholder_count = sum(1 for d in scheduled_dates if d not in grouped)
    print(
        f"Wrote {OUTPUT_FILE.name}; history_start={history_start.isoformat()}, "
        f"future_end_exclusive={future_end.isoformat()}, actual_dates={actual_count}, "
        f"scheduled_placeholders={placeholder_count}, Tokyo date={today_local.isoformat()}"
    )


if __name__ == "__main__":
    main()
