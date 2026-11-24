from __future__ import annotations
from typing import Any
from .common import capture_report

URL = "https://finintelhub.com/semiconductor/"
SOURCE_NAME = "FININTELHUB_SEMICONDUCTOR"
SECTOR_NAME = "Semiconductors"
SECTIONS = ("Overview", "Manufacturing", "Trade", "Investment", "Companies", "Technology", "Full Data Sheet")

async def scrape() -> dict[str, Any]:
    return await capture_report(URL, source_name=SOURCE_NAME, sector_name=SECTOR_NAME,
        sections=SECTIONS, filters=("quarter", "year", "metric", "technology", "period"),
        timeout_seconds=20, browser_timeout_seconds=60)
