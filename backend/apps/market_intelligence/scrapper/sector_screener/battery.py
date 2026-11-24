from __future__ import annotations
from typing import Any
from infrastructure.config.settings import get_settings
from .common import capture_report

URL = get_settings().market_battery_url or "https://www.mercomindia.com/research/report"
async def scrape() -> dict[str, Any]:
    return await capture_report(URL, source_name="MERCOM_BATTERY_STORAGE", sector_name="Battery Installation",
        sections=("Battery Storage", "Energy Storage", "BESS", "Installations", "Capacity", "Manufacturing", "Projects", "Tender", "Market Outlook", "Data"),
        filters=("year", "quarter", "state", "technology", "project", "period"),
        timeout_seconds=25, browser_timeout_seconds=70)
