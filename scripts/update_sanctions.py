"""Download the OFAC Specially Designated Nationals (SDN) list.

Fetches the official U.S. Treasury consolidated SDN CSV into
``data/sanctions/sdn.csv`` so ``app.services.sanctions`` screens against the
real list. Re-run periodically (the list changes frequently); in production
this would be a scheduled job with the World-Check / consolidated-lists API.

Usage:
    python scripts/update_sanctions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"
TARGET = Path("data") / "sanctions" / "sdn.csv"


def main() -> int:
    print(f"Downloading OFAC SDN list from {SDN_URL} ...")
    response = requests.get(SDN_URL, timeout=120)
    response.raise_for_status()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(response.content)
    line_count = response.text.count("\n")
    print(f"Saved {TARGET} ({len(response.content):,} bytes, ~{line_count:,} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
