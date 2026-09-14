#!/usr/bin/env python3
"""Build the dividend calendar and keep generated presentation files in sync."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Match

import update_calendar

ROOT = Path(__file__).resolve().parent
POSITIONS_FILE = ROOT / "positions.json"
README_FILE = ROOT / "README.md"
POSITIONS_START = "<!-- POSITIONS:START -->"
POSITIONS_END = "<!-- POSITIONS:END -->"
AMOUNT_PATTERN = r"\$(?P<amount>\d[\d\\,]*\.\d+)"
DIVIDEND_AMOUNT_PATTERN = r"(?:=|→ 本次)\s*\$(?P<amount>\d[\d\\,]*(?:\.\d+)?)"
ONE_DECIMAL = Decimal("0.1")
WITHHOLDING_RATE = Decimal("0.10")
NET_FACTOR = Decimal("1") - WITHHOLDING_RATE


def after_withholding(value: Any) -> Decimal:
    """Apply the configured withholding tax to a gross dividend amount."""
    return Decimal(str(value)) * NET_FACTOR


def calendar_title_amount(match: Match[str]) -> str:
    """Show the after-tax calendar title as plain integer digits only."""
    raw = match.group("amount").replace("\\,", "").replace(",", "")
    net_amount = after_withholding(Decimal(raw))
    return str(int(net_amount))


def truncate_one_decimal(value: Any) -> Decimal:
    """Truncate a numeric value to one decimal place without rounding."""
    return Decimal(str(value)).quantize(ONE_DECIMAL, rounding=ROUND_DOWN)


def shares_label(value: Any) -> str:
    """Render configured shares with one decimal place and thousands separators."""
    return f"{truncate_one_decimal(value):,.1f}"


def dividend_amount(part: str) -> Decimal | None:
    """Extract the gross cash dividend for one ticker line from the generated memo."""
    match = re.search(DIVIDEND_AMOUNT_PATTERN, part)
    if not match:
        return None
    raw = match.group("amount").replace("\\,", "").replace(",", "")
    return Decimal(raw)


def compact_description(line: str, positions: dict[str, Any]) -> str:
    """Show each ticker, configured shares, and after-tax dividend for this pay date."""
    payload = line.removeprefix("DESCRIPTION:")
    tickers: list[str] = []
    amounts: dict[str, Decimal] = defaultdict(Decimal)

    for part in payload.split("\\n"):
        ticker_match = re.match(r"([A-Za-z0-9.-]+):", part)
        if not ticker_match:
            continue
        ticker = ticker_match.group(1)
        if ticker not in positions:
            continue
        if ticker not in tickers:
            tickers.append(ticker)

        amount = dividend_amount(part)
        if amount is not None:
            amounts[ticker] += amount

    details: list[str] = []
    for ticker in tickers:
        shares = shares_label(positions[ticker]["shares"])
        amount = truncate_one_decimal(after_withholding(amounts[ticker]))
        details.append(f"{ticker}｜持股 {shares} 股｜股息 ${amount:,.1f}")

    return f"DESCRIPTION:{update_calendar.escape_text(chr(10).join(details))}"


def normalize_calendar(calendar_text: str, positions: dict[str, Any]) -> str:
    """Apply after-tax display formatting to titles and compact daily event memos."""
    logical_lines: list[str] = []
    for line in calendar_text.splitlines():
        if line.startswith(" ") and logical_lines:
            logical_lines[-1] += line[1:]
        else:
            logical_lines.append(line)

    output: list[str] = []
    for line in logical_lines:
        if line.startswith("SUMMARY:"):
            line = re.sub(AMOUNT_PATTERN, calendar_title_amount, line, count=1)
        elif line.startswith("DESCRIPTION:"):
            line = compact_description(line, positions)

        output.extend(update_calendar.fold_line(line))

    return "\r\n".join(output) + "\r\n"


def positions_table(positions: dict[str, Any]) -> str:
    lines = [POSITIONS_START, "| Ticker | Shares |", "|---|---:|"]
    for ticker, info in positions.items():
        shares = float(info["shares"])
        lines.append(f"| {ticker} | {shares:,.4f} |")
    lines.append(POSITIONS_END)
    return "\n".join(lines)


def sync_readme(positions: dict[str, Any]) -> None:
    text = README_FILE.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"{re.escape(POSITIONS_START)}.*?{re.escape(POSITIONS_END)}", re.S
    )
    replacement = positions_table(positions)
    if not pattern.search(text):
        raise RuntimeError("README positions markers are missing")
    updated = pattern.sub(replacement, text, count=1)
    if updated != text:
        README_FILE.write_text(updated, encoding="utf-8")


def main() -> None:
    config = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    positions: dict[str, Any] = config["positions"]

    sync_readme(positions)
    update_calendar.main()

    calendar_text = update_calendar.OUTPUT_FILE.read_text(encoding="utf-8")
    normalized = normalize_calendar(calendar_text, positions)
    update_calendar.OUTPUT_FILE.write_text(normalized, encoding="utf-8", newline="")
    print("Applied 10% withholding tax to displayed dividends, normalized calendar titles, merged daily dividend memos, and synced README positions.")


if __name__ == "__main__":
    main()
