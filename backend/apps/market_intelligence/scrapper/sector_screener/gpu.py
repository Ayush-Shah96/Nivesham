from __future__ import annotations
from typing import Any
from infrastructure.config.settings import get_settings
from .common import capture_report

URL = get_settings().market_gpu_url or "https://www.silicondata.com/products/silicon-index"
async def scrape() -> dict[str, Any]:
    return await capture_report(URL, source_name="SILICON_DATA_GPU_COMPUTE", sector_name="GPU / Compute",
        sections=("Silicon Index", "GPU Pricing", "GPU Cloud", "GPU Rental", "Compute", "AI Infrastructure", "LLM Token Index", "Historical Data", "Benchmarks"),
        filters=("gpu", "model", "provider", "region", "period", "date"),
        timeout_seconds=25, browser_timeout_seconds=65)
