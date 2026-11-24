from __future__ import annotations
from typing import Any
from infrastructure.config.settings import get_settings
from .common import capture_report

URL = get_settings().market_commodities_url or "https://tradingeconomics.com/commodities"
async def scrape() -> dict[str, Any]:
    return await capture_report(URL, source_name="TRADING_ECONOMICS_COMMODITIES", sector_name="Commodities",
        sections=("Commodities", "Energy", "Metals", "Agriculture", "Industrial", "Historical Data", "Prices", "Performance"),
        filters=("commodity", "category", "period", "date", "country"),
        timeout_seconds=25, browser_timeout_seconds=65)
