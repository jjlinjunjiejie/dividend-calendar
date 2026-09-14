#!/usr/bin/env python3
"""Build an Apple-compatible subscribed dividend calendar from iShares distributions."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
POSITIONS_FILE = ROOT / "positions.json"
OUTPUT_FILE = ROOT / "dividends.ics"

# Calendar retention policy:
# - Never show anything before 2026-07-01.
# - Keep a rolling 3 calendar-month window. For example, during Sep 2026 the
#   calendar starts at Jul 1; during Oct it starts at Aug 1, so July disappears.
INITIAL_START_DATE = date(2026, 7, 1)
RETENTION_MONTHS = 3
LOCAL_TIMEZONE = ZoneInfo("Asia/Tokyo")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36"
)
API_PATH = "/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data"
API_HOSTS = ("https://www.ishares.com", "https://www.blackrock.com")


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


def fetch_json(portfolio_id: int) -> dict[str, Any]:
    errors: list[str] = []
    for host in API_HOSTS:
        url = api_url(host, portfolio_id)
        for attempt in range(2):
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8-sig"))
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
                errors.append(f"{host} attempt {attempt + 1}: {exc}")
                if attempt == 0:
                    time.sleep(3)
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
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
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


def calendar_cutoff(today: date) -> date:
    """Return the first date retained in the rolling 3-calendar-month window."""
    current_month = date(today.year, today.month, 1)
    rolling_start = add_months_month_start(current_month, -(RETENTION_MONTHS - 1))
    return max(INITIAL_START_DATE, rolling_start)


def build_calendar(events_by_date: dict[date, list[dict[str, Any]]], cutoff: date) -> str:
    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Dividend Calendar//iShares Portfolio//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:ETF 股息",
        "X-WR-CALDESC:IBHF/IBHG/IBHH/IBHI/IBHJ/IBHK 持仓税前股息；按 iShares 官方数据自动更新；仅保留最近3个日历月",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    lines: list[str] = []
    for line in header:
        lines.extend(fold_line(line))

    for payable in sorted(events_by_date):
        if payable < cutoff:
            continue
        rows = sorted(events_by_date[payable], key=lambda r: r["ticker"])
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
        next_day = payable + timedelta(days=1)
        event = [
            "BEGIN:VEVENT",
            f"UID:dividend-{payable.strftime('%Y%m%d')}@dividend-calendar",
            f"DTSTAMP:{payable.strftime('%Y%m%d')}T000000Z",
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

    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    failures: list[str] = []
    for ticker, info in positions.items():
        try:
            rows = distributions_for(ticker, int(info["portfolio_id"]), float(info["shares"]))
            for row in rows:
                grouped[row["payable_date"]].append(row)
            print(f"{ticker}: {len(rows)} distribution records")
        except Exception as exc:
            failures.append(f"{ticker}: {exc}")

    if failures:
        raise RuntimeError("One or more funds failed; calendar not replaced. " + " | ".join(failures))
    if not grouped:
        raise RuntimeError("No distribution records returned; calendar not replaced.")

    today_local = datetime.now(LOCAL_TIMEZONE).date()
    cutoff = calendar_cutoff(today_local)
    retained_dates = [d for d in grouped if d >= cutoff]
    calendar = build_calendar(grouped, cutoff)
    temp = OUTPUT_FILE.with_suffix(".ics.tmp")
    temp.write_text(calendar, encoding="utf-8", newline="")
    temp.replace(OUTPUT_FILE)
    print(
        f"Wrote {OUTPUT_FILE.name} with {len(retained_dates)} payment-date events; "
        f"cutoff={cutoff.isoformat()} (Tokyo date {today_local.isoformat()})"
    )


if __name__ == "__main__":
    main()
