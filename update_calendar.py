#!/usr/bin/env python3
"""Build a validated, after-tax subscribed dividend calendar."""
from __future__ import annotations

import calendar
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from data_sources import DividendSources, SourceError

ROOT = Path(__file__).resolve().parent
POSITIONS_FILE = ROOT / "positions.json"
OUTPUT_FILE = ROOT / "dividends.ics"
CACHE_DIR = ROOT / ".cache" / "dividend-calendar"
HISTORY_MONTHS = 3
FUTURE_MONTHS = 24
LOCAL_TIMEZONE = ZoneInfo("Asia/Tokyo")
NET_FACTOR = Decimal("0.90")


def decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def truncate(value: Decimal, places: str = "0.1") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_DOWN)


def add_months_month_start(value: date, months: int) -> date:
    absolute = value.year * 12 + value.month - 1 + months
    return date(absolute // 12, absolute % 12 + 1, 1)


def calendar_window(today: date) -> tuple[date, date, date]:
    current = date(today.year, today.month, 1)
    return (add_months_month_start(current, -HISTORY_MONTHS), current,
            add_months_month_start(current, FUTURE_MONTHS + 1))


def safe_date(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def previous_business_day_if_weekend(value: date) -> date:
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def escape_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold_line(line: str, limit: int = 73) -> list[str]:
    out = []
    current = ""
    for ch in line:
        if len((current + ch).encode("utf-8")) > limit:
            out.append(current)
            current = " "
        current += ch
    out.append(current)
    return out


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def encode_lines(lines: list[str]) -> str:
    return "\r\n".join(part for line in lines for part in fold_line(line)) + "\r\n"


def stabilize_events(new: str, old: str) -> str:
    """Keep timestamps/sequence for unchanged events; update changed UIDs only."""
    def events(text: str) -> list[list[str]]:
        found, event = [], None
        for line in unfold(text):
            if line == "BEGIN:VEVENT":
                event = []
            if event is not None:
                event.append(line)
            if line == "END:VEVENT" and event is not None:
                found.append(event)
                event = None
        return found

    def uid(event: list[str]) -> str:
        return next(line for line in event if line.startswith("UID:"))

    def semantic(event: list[str]) -> list[str]:
        return [line for line in event if not line.startswith(("DTSTAMP:", "LAST-MODIFIED:", "SEQUENCE:"))]

    previous = {uid(event): event for event in events(old)}
    current = events(new)
    replacements = []
    for event in current:
        prior = previous.get(uid(event))
        if prior and semantic(prior) == semantic(event):
            replacements.append(prior)
        else:
            seq = 0
            if prior:
                raw = next((x.split(":", 1)[1] for x in prior if x.startswith("SEQUENCE:")), "0")
                seq = int(raw) + 1
            replacements.append([f"SEQUENCE:{seq}" if x.startswith("SEQUENCE:") else x for x in event])
    header = unfold(new)
    header = header[:header.index("BEGIN:VEVENT")] if current else header[:-1]
    return encode_lines(header + [line for event in replacements for line in event] + ["END:VCALENDAR"])


def atomic_write_if_changed(path: Path, text: str) -> bool:
    raw = text.encode("utf-8")
    if path.exists() and path.read_bytes() == raw:
        return False
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(raw)
    temp.replace(path)
    return True


def validate_positions(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("currency", "USD") != "USD":
        raise ValueError("Only USD portfolios are supported")
    positions = config["positions"]
    if not isinstance(positions, dict) or not positions:
        raise ValueError("Positions must be a nonempty object")
    for ticker, info in positions.items():
        if not re.fullmatch(r"[A-Z0-9.-]+", ticker):
            raise ValueError(f"Invalid ticker: {ticker}")
        shares = decimal(info["shares"])
        if not shares.is_finite() or shares <= 0:
            raise ValueError(f"{ticker}: shares must be positive and finite")
        source = info.get("source", "ishares")
        if source not in ("ishares", "stockanalysis"):
            raise ValueError(f"{ticker}: unsupported source/cadence")
        if source == "ishares" and int(info["portfolio_id"]) <= 0:
            raise ValueError(f"{ticker}: invalid portfolio ID")
        if source != "ishares" and info.get("frequency", 4) != 4:
            raise ValueError("Equity date projection currently supports quarterly funds only")
        if not 1900 <= int(info.get("maturity_year", 9999)) <= 9999:
            raise ValueError(f"{ticker}: invalid maturity year")
    return positions


def monthly_cycle(payable: date) -> tuple[int, int]:
    # The second December payment is the following January's regular cycle.
    return (payable.year + 1, 1) if payable.month == 12 and payable.day >= 15 else (payable.year, payable.month)


def actual_cycle(row: dict[str, Any], info: dict[str, Any]) -> tuple[int, int] | None:
    ex = row["ex_date"]
    if info.get("source", "ishares") == "ishares":
        if ex.month == 12 and ex.day >= 25:
            return None  # conditional excise distribution: never consumes a regular cycle
        return monthly_cycle(ex)
    return ex.year, (ex.month - 1) // 3 + 1


def projected_monthly_dates(reference: list[date], start: date, end: date) -> list[date]:
    # Preserve the official cadence: Feb-Nov once, December twice, January none.
    regular = sorted(d.day for d in reference if 2 <= d.month <= 11)
    late_dec = sorted(d.day for d in reference if d.month == 12 and 15 <= d.day <= 26)
    if not regular:
        raise SourceError("No validated monthly history to project dates")
    day = regular[len(regular) // 2]
    december_day = late_dec[len(late_dec) // 2] if late_dec else 23
    result = []
    cursor = date(start.year, start.month, 1)
    while cursor < end:
        days = [] if cursor.month == 1 else [day]
        if cursor.month == 12:
            days.append(december_day)
        for target_day in days:
            value = previous_business_day_if_weekend(safe_date(cursor.year, cursor.month, target_day))
            if start <= value < end:
                result.append(value)
        cursor = add_months_month_start(cursor, 1)
    return result


def make_schedule(positions: dict[str, Any], rows: dict[str, list[dict[str, Any]]],
                  official: list[date], official_label: str, start: date, end: date,
                  equity_official: dict[str, dict] | None = None
                  ) -> dict[date, dict[str, dict[str, Any]]]:
    schedule: dict[date, dict[str, dict[str, Any]]] = defaultdict(dict)
    for ticker, info in positions.items():
        if info.get("source", "ishares") == "ishares":
            selected = [(d, official_label) for d in official if start <= d < end]
            after = max(start, add_months_month_start(max(official).replace(day=1), 1)) if official else start
            reference = official or [r["payable_date"] for r in rows[ticker] if r["ex_date"].month != 1]
            if after < end:
                selected += [(d, "推算支付日") for d in projected_monthly_dates(reference, after, end)]
            for d, label in selected:
                if d.year <= int(info.get("maturity_year", 9999)):
                    schedule[d][ticker] = {"cycle": monthly_cycle(d), "date_status": label}
        else:
            # Learn one regular pattern per ex-dividend quarter, retaining the
            # year offset when a December distribution is paid in January.
            patterns = {}
            for row in rows[ticker]:
                ex = row["ex_date"]
                patterns[(ex.month - 1) // 3 + 1] = row
            if len(patterns) != 4:
                raise SourceError(f"{ticker}: missing quarterly payment patterns")
            for year in range(start.year - 1, end.year + 1):
                for quarter, row in patterns.items():
                    pay, ex = row["payable_date"], row["ex_date"]
                    d = previous_business_day_if_weekend(safe_date(year + pay.year - ex.year, pay.month, pay.day))
                    label = "推算支付日"
                    published = (equity_official or {}).get(ticker, {}).get((year, quarter))
                    if published:
                        d, label = published["payable_date"], published["date_status"]
                    if start <= d < end:
                        schedule[d][ticker] = {"cycle": (year, quarter), "date_status": label}
    return schedule


def market_metrics(positions: dict[str, Any], rows: dict[str, list[dict[str, Any]]],
                   screener: dict[str, dict[str, Any]], today: date) -> dict[str, dict[str, Any]]:
    result = {}
    for ticker, info in positions.items():
        shares = decimal(info["shares"])
        source = info.get("source", "ishares")
        frequency = 12 if source == "ishares" else 4
        if ticker in screener:
            product = screener[ticker]
            annual = shares * decimal(product["price"]) * decimal(product["yield_pct"]) / 100
            method, provider = product["method"], product["provider"]
        else:
            # No fabricated price/yield when the quote provider is unavailable.
            # Use cash distributions directly; do not include future announcements.
            history = [r for r in rows[ticker] if today - timedelta(days=365) < r["payable_date"] <= today]
            if not history or sum(decimal(r["per_share"]) for r in history) <= 0:
                raise SourceError(f"{ticker}: no usable trailing cash distribution history")
            annual = shares * sum(decimal(r["per_share"]) for r in history)
            method, provider = "近12个月实际分配", rows[ticker][-1]["provider"]
        result[ticker] = {"annual_income": annual, "event_amount": annual / frequency,
                          "method": method, "provider": provider,
                          "cached": ticker not in screener and any(r.get("cached") for r in rows[ticker])}
    return result


def build_calendar(events_by_date: dict[date, list[dict[str, Any]]], positions: dict[str, Any],
                   metrics: dict[str, dict[str, Any]], scheduled: dict[date, dict[str, dict[str, Any]]],
                   history_start: date, current_month: date, future_end: date,
                   now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Dividend Calendar//Portfolio//CN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:ETF 股息",
             f"X-WR-CALDESC:过去3个完整日历月+当前月；未来{FUTURE_MONTHS}个月；含预估；税后90%；按当前持股计算",
             "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    actual_cycles = defaultdict(set)
    for rows in events_by_date.values():
        for row in rows:
            actual_cycles[row["ticker"]].add(actual_cycle(row, positions[row["ticker"]]))
    all_dates = sorted({d for d in events_by_date if history_start <= d < future_end}
                       | {d for d in scheduled if current_month <= d < future_end})
    for d in all_dates:
        actual = sorted(events_by_date.get(d, []), key=lambda r: (r["ticker"], r["ex_date"]))
        actual_tickers = {r["ticker"] for r in actual}
        estimates = {t: entry for t, entry in sorted(scheduled.get(d, {}).items())
                     if t not in actual_tickers and entry["cycle"] not in actual_cycles[t]}
        gross = sum((decimal(r["amount"]) for r in actual), Decimal(0))
        gross += sum((metrics[t]["event_amount"] for t in estimates), Decimal(0))
        if gross <= 0:
            continue
        amounts: dict[str, Decimal] = defaultdict(Decimal)
        for row in actual:
            amounts[row["ticker"]] += decimal(row["amount"])
        for ticker in estimates:
            amounts[ticker] += metrics[ticker]["event_amount"]
        details = [
            f"{ticker}｜{truncate(decimal(positions[ticker]['shares'])):,.1f}股｜${truncate(amount * NET_FACTOR):,.1f}"
            for ticker, amount in sorted(amounts.items())
        ]
        # Amount estimates can still have an official payment date. Only
        # unresolved date projections need the expected-payment suffix.
        projected_date = any(
            not entry.get("date_status", "").startswith("官方支付日")
            for entry in estimates.values()
        )
        tax_note = f"预扣税{((1 - NET_FACTOR) * 100).normalize():f}%"
        details.append(tax_note + ("｜预计本日发放" if projected_date else ""))
        title = f"{'◇' if projected_date else ''}{int(gross * NET_FACTOR)}"
        lines.extend(["BEGIN:VEVENT", f"UID:dividend-{d.strftime('%Y%m%d')}@dividend-calendar",
                      f"DTSTAMP:{stamp}", f"LAST-MODIFIED:{stamp}", "SEQUENCE:0",
                      f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
                      f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
                      f"SUMMARY:{title}", f"DESCRIPTION:{escape_text(chr(10).join(details))}",
                      "CATEGORIES:股息,Dividend", "TRANSP:TRANSPARENT", "END:VEVENT"])
    return encode_lines(lines + ["END:VCALENDAR"])


def generate_calendar(config: dict[str, Any], sources: DividendSources | None = None) -> str:
    positions = validate_positions(config)
    sources = sources or DividendSources(CACHE_DIR)
    today = sources.now.astimezone(LOCAL_TIMEZONE).date()
    history_start, current_month, future_end = calendar_window(today)
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    rows_by_ticker = {}
    try:
        failures = []
        for ticker, info in positions.items():
            try:
                rows = sources.fetch(ticker, info)
                rows_by_ticker[ticker] = rows
                for row in rows:
                    if history_start <= row["payable_date"] < future_end:
                        grouped[row["payable_date"]].append({**row, "amount": decimal(info["shares"]) * decimal(row["per_share"])})
                print(f"{ticker}: {len(rows)} validated distribution records")
            except SourceError as exc:
                failures.append(str(exc))
        if failures:
            raise SourceError("Calendar not replaced. " + " | ".join(failures))
        monthly_tickers = [t for t, i in positions.items() if i.get("source", "ishares") == "ishares"]
        official, label = sources.schedule() if monthly_tickers else ([], "推算支付日")
        metrics = market_metrics(positions, rows_by_ticker, sources.screener(monthly_tickers), today)
        equity_official = {t: sources.equity_schedule(t) for t in positions if t not in monthly_tickers}
        schedule = make_schedule(positions, rows_by_ticker, official, label, current_month, future_end,
                                 equity_official)
        text = build_calendar(grouped, positions, metrics, schedule, history_start, current_month, future_end, sources.now)
        sources.save()
        sources.status["success"] = True
        return text
    finally:
        sources.save_status()


def write_calendar(text: str) -> bool:
    old = OUTPUT_FILE.read_text(encoding="utf-8") if OUTPUT_FILE.exists() else ""
    return atomic_write_if_changed(OUTPUT_FILE, stabilize_events(text, old))


def main() -> None:
    config = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    changed = write_calendar(generate_calendar(config))
    print("Calendar updated." if changed else "No calendar content changes.")


if __name__ == "__main__":
    main()
