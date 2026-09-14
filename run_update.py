#!/usr/bin/env python3
"""Build the calendar and synchronize README only after successful validation."""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import update_calendar

ROOT = Path(__file__).resolve().parent
POSITIONS_FILE = ROOT / "positions.json"
README_FILE = ROOT / "README.md"
POSITIONS_START = "<!-- POSITIONS:START -->"
POSITIONS_END = "<!-- POSITIONS:END -->"


def positions_table(positions: dict[str, Any]) -> str:
    lines = [POSITIONS_START, "| Ticker | Shares |", "|---|---:|"]
    for ticker, info in positions.items():
        lines.append(f"| {ticker} | {Decimal(str(info['shares'])):,.4f} |")
    return "\n".join(lines + [POSITIONS_END])


def updated_readme(positions: dict[str, Any]) -> str:
    text = README_FILE.read_text(encoding="utf-8")
    pattern = re.compile(rf"{re.escape(POSITIONS_START)}.*?{re.escape(POSITIONS_END)}", re.S)
    if len(pattern.findall(text)) != 1:
        raise RuntimeError("README must contain exactly one positions marker pair")
    return pattern.sub(lambda _: positions_table(positions), text, count=1)


def main() -> None:
    config = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    positions = update_calendar.validate_positions(config)
    readme = updated_readme(positions)
    calendar = update_calendar.generate_calendar(config)
    changed = update_calendar.write_calendar(calendar)
    update_calendar.atomic_write_if_changed(README_FILE, readme)
    print("Calendar updated and README synchronized." if changed else "No calendar content changes; README synchronized.")


if __name__ == "__main__":
    main()
