from __future__ import annotations
from typing import Any
from .common import capture_report

URL = "https://finintelhub.com/india-chemical-overview-chemical-sector/"
SOURCE_NAME = "FININTELHUB_CHEMICAL"
SECTOR_NAME = "Chemical"
SECTIONS = ("Overview", "Group Performance", "Product-Wise", "State Consumption", "Projections", "View All Data")

async def scrape() -> dict[str, Any]:
    return await capture_report(URL, source_name=SOURCE_NAME, sector_name=SECTOR_NAME,
        sections=SECTIONS, filters=("year", "state", "group", "product", "period"),
        timeout_seconds=20, browser_timeout_seconds=60)
