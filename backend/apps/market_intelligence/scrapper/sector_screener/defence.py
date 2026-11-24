from __future__ import annotations
from typing import Any
from .common import capture_report

URL = "https://finintelhub.com/defense-sector/"
SOURCE_NAME = "FININTELHUB_DEFENCE"
SECTOR_NAME = "Defence"
SECTIONS = ("Overview", "Defence Budget", "Production", "Exports", "Imports", "Procurement", "Companies", "Data Sheet", "Full Data")

async def scrape() -> dict[str, Any]:
    return await capture_report(URL, source_name=SOURCE_NAME, sector_name=SECTOR_NAME,
        sections=SECTIONS, filters=("year", "state", "category", "segment", "period"),
        timeout_seconds=20, browser_timeout_seconds=60)
