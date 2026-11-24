from __future__ import annotations

"""NISE Solar DCR collector using the portal's direct HTTP POST endpoints."""

import logging
from typing import Any

from infrastructure.config.settings import get_settings
from .nise_http_capture import scrape_product

logger = logging.getLogger(__name__)
_settings = get_settings()
URL = (
    getattr(_settings, "market_solar_url", None)
    or "https://solardcrportal.nise.res.in/Summary/index"
).strip()


async def scrape(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect and normalize the canonical Cell or Module snapshot via HTTP."""
    filters = filters or {}
    raw_product = str(filters.get("product") or "Cell").strip().casefold()
    product = "Module" if raw_product in {"module", "solar module", "panel"} else "Cell"
    report = await scrape_product(product, index_url=URL)
    report["requested_filters"] = {"product": product, "period": "all-years", "source_filters": {}}
    report["source_label"] = "National Institute of Solar Energy — DCR Verification Portal"
    report["source_url"] = URL
    audit = report.get("capture_audit") if isinstance(report.get("capture_audit"), dict) else {}
    logger.info(
        "NISE_SOLAR_DIRECT_HTTP_COMPLETE product=%s manufacturers=%d observations=%d years=%d endpoints=%d complete=%s",
        product,
        len(report.get("manufacturers") or []),
        len(report.get("manufacturing_observations") or []),
        len(report.get("available_years") or []),
        len(audit.get("endpoints") or []),
        audit.get("complete"),
    )
    return report
