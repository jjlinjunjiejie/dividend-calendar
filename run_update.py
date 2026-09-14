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


def whole_dollar(match: Match[str]) -> str:
    """Format a positive dollar amount by truncating, never rounding."""
    raw = match.group("amount").replace("\\,", "")
    value = int(Decimal(raw))
    return f"${value:,}".replace(",", "\\,")


def truncate_dividend_amounts(calendar_text: str) -> str:
    """Truncate displayed dividend cash amounts while preserving price precision."""
    logical_lines: list[str] = []
    for line in calendar_text.splitlines():
        if line.startswith(" ") and logical_lines:
            logical_lines[-1] += line[1:]
        else:
            logical_lines.append(line)

    output: list[str] = []
    for line in logical_lines:
        if line.startswith("SUMMARY:"):
            line = re.sub(AMOUNT_PATTERN, whole_dollar, line, count=1)
        elif line.startswith("DESCRIPTION:"):
            line = re.sub(
                rf"(?<=税前股息总额: ){AMOUNT_PATTERN}", whole_dollar, line
            )
            line = re.sub(
                rf"(?<=已公布税前股息总额: ){AMOUNT_PATTERN}", whole_dollar, line
            )
            line = re.sub(rf"(?<== ){AMOUNT_PATTERN}", whole_dollar, line)
            line = re.sub(rf"(?<=→ 本次 ){AMOUNT_PATTERN}", whole_dollar, line)

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
    normalized = truncate_dividend_amounts(calendar_text)
    update_calendar.OUTPUT_FILE.write_text(normalized, encoding="utf-8", newline="")
    print("Truncated displayed dividend amounts to whole dollars and synced README positions.")


if __name__ == "__main__":
    main()
