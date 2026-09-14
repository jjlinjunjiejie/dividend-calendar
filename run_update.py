#!/usr/bin/env python3
"""Build the dividend calendar and keep generated presentation files in sync."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Match

import update_calendar

ROOT = Path(__file__).resolve().parent
POSITIONS_FILE = ROOT / "positions.json"
README_FILE = ROOT / "README.md"
POSITIONS_START = "<!-- POSITIONS:START -->"
POSITIONS_END = "<!-- POSITIONS:END -->"
AMOUNT_PATTERN = r"\$(?P<amount>\d[\d\\,]*\.\d+)"


def calendar_title_amount(match: Match[str]) -> str:
    """Format a calendar title amount as plain integer digits only."""
    raw = match.group("amount").replace("\\,", "").replace(",", "")
    return str(int(Decimal(raw)))


def shares_label(value: Any) -> str:
    """Render configured shares compactly without adding thousands separators."""
    return format(Decimal(str(value)), "f").rstrip("0").rstrip(".")


def compact_description(line: str, positions: dict[str, Any]) -> str:
    """Keep only the tickers included in this dividend event and their share counts."""
    payload = line.removeprefix("DESCRIPTION:")
    tickers: list[str] = []
    for part in payload.split("\\n"):
        match = re.match(r"([A-Za-z0-9.-]+):", part)
        if not match:
            continue
        ticker = match.group(1)
        if ticker in positions and ticker not in tickers:
            tickers.append(ticker)

    details = [f"{ticker} {shares_label(positions[ticker]['shares'])}股" for ticker in tickers]
    return f"DESCRIPTION:{update_calendar.escape_text(chr(10).join(details))}"


def normalize_calendar(calendar_text: str, positions: dict[str, Any]) -> str:
    """Normalize calendar titles and reduce event memo text to holdings only."""
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
    print("Normalized calendar titles, simplified event memos, and synced README positions.")


if __name__ == "__main__":
    main()
