from __future__ import annotations
"""Unified sector screener catalogue and source-specific collectors."""
from typing import Any
from . import automotive, battery, chemical, commodities, defence, gpu, lpg, pharma_healthcare, semiconductor, solar, tradestat

SECTORS = {
    "pharma": {"name": "Pharma & Healthcare", "source": "FinIntelHub", "source_url": pharma_healthcare.URL,
        "description": "Industry indicators, production, trade, pricing and healthcare market activity.",
        "reports": {"overview": {"label": "Overview", "url": pharma_healthcare.URL}}},
    "chemical": {"name": "Chemical", "source": "FinIntelHub", "source_url": chemical.URL,
        "description": "Capacity, production, exports, imports, product groups and state consumption.",
        "reports": {"overview": {"label": "Overview", "url": chemical.URL}}},
    "semiconductor": {"name": "Semiconductors", "source": "FinIntelHub", "source_url": semiconductor.URL,
        "description": "Market size, manufacturing, trade, investment and technology trends.",
        "reports": {"overview": {"label": "Overview", "url": semiconductor.URL}}},
    "defence": {"name": "Defence", "source": "FinIntelHub", "source_url": defence.URL,
        "description": "Defence budget, production, exports, procurement and industry indicators.",
        "reports": {"overview": {"label": "Overview", "url": defence.URL}}},
    "automotive": {"name": "Automotive", "source": "Ministry of Road Transport & Highways · Parivahan VAHAN", "source_url": automotive.REPORTS["overview"][1],
        "description": "Vehicle registrations, EV, 2W, 4W, commercial vehicle and state-level trends.",
        "reports": {key: {"label": value[0], "url": value[1]} for key, value in automotive.REPORTS.items()}},
    "solar": {"name": "Solar", "source": "NISE DCR Portal", "source_url": solar.URL,
        "description": "Solar cell and module manufacturer counts and manufactured output (MW for cells; MWp for modules).",
        "reports": {"overview": {"label": "Overview", "url": solar.URL}}},
    "tradestat": {"name": "TradeStat", "source": "Department of Commerce · MEIDB", "source_url": tradestat.EXPORT_URL,
        "description": "HS-code watchlists, monthly imports, exports, financial-year comparisons and historical trade trends.",
        "reports": {"overview": {"label": "Commodity Trade Monitor", "url": tradestat.IMPORT_URL}}},
    "commodities": {"name": "Commodities", "source": "Trading Economics", "source_url": commodities.URL,
        "description": "Energy, metals, agriculture and industrial commodity prices and performance.",
        "reports": {"overview": {"label": "Overview", "url": commodities.URL}}},
    "lpg": {"name": "Petroleum & Natural Gas", "source": "PPAC", "source_url": lpg.URL,
        "description": "PPAC petroleum imports/exports, LNG imports, production and domestic consumption.",
        "reports": {"overview": {"label": "Overview", "url": lpg.URL}}},
    "gpu": {"name": "GPU / Compute", "source": "Silicon Data", "source_url": gpu.URL,
        "description": "GPU rental pricing, compute benchmarks and AI infrastructure indices.",
        "reports": {"overview": {"label": "Overview", "url": gpu.URL}}},
    "battery": {"name": "Battery Installation", "source": "Mercom India", "source_url": battery.URL,
        "description": "Battery storage deployments, BESS projects, capacity, tenders and manufacturing.",
        "reports": {"overview": {"label": "Overview", "url": battery.URL}}},
}

async def scrape(sector_key: str, report_key: str = "overview", *, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    if sector_key not in SECTORS:
        raise KeyError(f"Unknown Sector Screener sector: {sector_key}")
    if report_key not in SECTORS[sector_key]["reports"]:
        raise KeyError(f"Unknown report {report_key} for sector {sector_key}")
    if sector_key == "automotive":
        return await automotive.scrape(report_key, filters=filters)
    if sector_key == "solar":
        return await solar.scrape(filters=filters)
    if sector_key == "tradestat":
        return await tradestat.scrape(filters=filters)
    scraper = {
        "pharma": pharma_healthcare.scrape,
        "chemical": chemical.scrape,
        "semiconductor": semiconductor.scrape,
        "defence": defence.scrape,
        "solar": solar.scrape,
        "commodities": commodities.scrape,
        "tradestat": tradestat.scrape,
        "lpg": lpg.scrape,
        "gpu": gpu.scrape,
        "battery": battery.scrape,
    }[sector_key]
    return await scraper()

__all__ = ["SECTORS", "scrape"]
