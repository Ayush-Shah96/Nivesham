from __future__ import annotations
from typing import Any
from .common import capture_report

URL = "https://finintelhub.com/pharma-healthcare/"
SOURCE_NAME = "FININTELHUB_PHARMA_HEALTHCARE"
SECTOR_NAME = "Pharma & Healthcare"
SECTIONS = (
    "Executive Overview", "Export & Import Dynamics", "Pricing & NPPA Regulation",
    "Manufacturing & State Footprint", "Government PLI & Bulk Drug Parks",
    "Consolidated Master Repository",
)

async def scrape() -> dict[str, Any]:
    return await capture_report(URL, source_name=SOURCE_NAME, sector_name=SECTOR_NAME,
        sections=SECTIONS, filters=("year", "state", "category", "period"),
        timeout_seconds=20, browser_timeout_seconds=55)
