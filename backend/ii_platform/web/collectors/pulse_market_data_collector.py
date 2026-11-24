"""Public market-data adapters for Pulse.

Yahoo Finance chart data is an unofficial, best-effort source. It is not an
exchange-authorized feed and its delivery delay is not contractually verified.
TejHQ is used only as an optional end-of-day fallback; its free terms are for
personal use, so it is disabled unless explicitly enabled by the operator.
"""
from __future__ import annotations

import asyncio
import logging
import os
import statistics
import time
from datetime import datetime, timedelta
from urllib.parse import quote as url_quote
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
TEJHQ_API = "https://api.tejhq.dev/v1"  # Keyless open endpoints: /snapshot/{exchange}?date=YYYY-MM-DD

SECTOR_UNIVERSE: list[dict[str, Any]] = [
    {"symbol": "^NSEBANK", "name": "NIFTY Bank", "key": "BANK", "sector": "Banking", "yahoo": "^NSEBANK"},
    {"symbol": "^CNXIT", "name": "NIFTY IT", "key": "IT", "sector": "Information Technology", "yahoo": "^CNXIT"},
    {"symbol": "^CNXAUTO", "name": "NIFTY Auto", "key": "AUTO", "sector": "Automobile", "yahoo": "^CNXAUTO"},
    {"symbol": "^CNXPHARMA", "name": "NIFTY Pharma", "key": "PHARMA", "sector": "Pharmaceuticals", "yahoo": "^CNXPHARMA"},
    {"symbol": "^CNXFMCG", "name": "NIFTY FMCG", "key": "FMCG", "sector": "Consumer Staples", "yahoo": "^CNXFMCG"},
    {"symbol": "^CNXMETAL", "name": "NIFTY Metal", "key": "METAL", "sector": "Metals", "yahoo": "^CNXMETAL"},
    {"symbol": "^CNXREALTY", "name": "NIFTY Realty", "key": "REALTY", "sector": "Real Estate", "yahoo": "^CNXREALTY"},
    {"symbol": "^CNXENERGY", "name": "NIFTY Energy", "key": "ENERGY", "sector": "Energy", "yahoo": "^CNXENERGY"},
    {"symbol": "^CNXFINANCE", "name": "NIFTY Financial Services", "key": "FINANCE", "sector": "Financial Services", "yahoo": "^CNXFINANCE"},
    {"symbol": "^NSEI", "name": "NIFTY 50", "key": "NIFTY50", "sector": "Market", "yahoo": "^NSEI"},
    {"symbol": "^BSESN", "name": "SENSEX", "key": "SENSEX", "sector": "Market", "yahoo": "^BSESN"},
    {"symbol": "^INDIAVIX", "name": "India VIX", "key": "VIX", "sector": "Volatility", "yahoo": "^INDIAVIX"},
]

# The Coverage List workbook does not contain a sector/industry column.
# These classifications are maintained as Pulse metadata so each workbook
# instrument can be grouped without adding symbols from a separate universe.
# They are industry classifications, not a claim that every stock is an
# official constituent of the corresponding NIFTY sector index.
COVERAGE_SECTOR_BY_SYMBOL: dict[str, str] = {
    # Banks and financial services
    "UJJIVANSFB": "Banking", "CSBBANK": "Banking", "HDFCBANK": "Banking",
    "ICICIBANK": "Banking", "KOTAKBANK": "Banking",
    "ARMANFIN": "Financial Services", "MUTHOOTMF": "Financial Services",
    "FEDFINA": "Financial Services", "MUTHOOTFIN": "Financial Services",
    "MANAPPURAM": "Financial Services", "AAVAS": "Financial Services",
    "APTUS": "Financial Services", "INDIASHLTR": "Financial Services",
    # Pharmaceuticals and healthcare
    "BLUEJET": "Pharmaceuticals", "NEULANDLAB": "Pharmaceuticals",
    "AARTIPHARM": "Pharmaceuticals", "ACUTAAS": "Pharmaceuticals",
    "DIVISLAB": "Pharmaceuticals", "LAURUSLABS": "Pharmaceuticals",
    "GRANULES": "Pharmaceuticals", "ONESOURCE": "Pharmaceuticals",
    "GLAND": "Pharmaceuticals", "ORCHPHARMA": "Pharmaceuticals",
    "ALIVUS": "Pharmaceuticals", "ENTERO": "Healthcare Services",
    "NH": "Healthcare Services", "MAXHEALTH": "Healthcare Services",
    "APOLLOHOSP": "Healthcare Services", "HCG": "Healthcare Services",
    "KIMS": "Healthcare Services", "RAINBOW": "Healthcare Services",
    # Auto and auto components
    "SUPRAJIT": "Automobile", "RACLGEAR": "Automobile",
    "SANSERA": "Automobile", "SJS": "Automobile", "SHIVALIK": "Automobile",
    "SHRIPISTON": "Automobile", "SONACOMS": "Automobile", "AEQUS": "Automobile",
    # Technology, electronics and electrical equipment
    "ROSSTECH": "Technology & Electronics", "ASMS": "Technology & Electronics",
    "VOEPL": "Technology & Electronics", "CGRAPHICS": "Technology & Electronics",
    "AIMTRON": "Technology & Electronics", "AXISCADES": "Technology & Electronics",
    "AVANTEL": "Technology & Electronics", "CENTUM": "Technology & Electronics",
    "VINYAS": "Technology & Electronics", "RRKABEL": "Electricals & Cables",
    "PRIMECAB": "Electricals & Cables", "POLYCAB": "Electricals & Cables",
    "JAYBEE": "Electricals & Cables", "VILAS": "Electricals & Cables",
    "QPOWER": "Electricals & Cables", "PGEL": "Technology & Electronics",
    # Metals, materials, chemicals and recycling
    "GOODLUCK": "Metals", "SAMBHV": "Metals", "JSL": "Metals",
    "HITECH": "Metals", "APLAPOLLO": "Metals", "VENUSPIPES": "Metals",
    "GRAVITA": "Metals & Recycling", "JAINREC": "Metals & Recycling",
    "CLEAN": "Chemicals & Materials", "TIRUMALCHM": "Chemicals & Materials",
    "MANORG": "Chemicals & Materials", "POCL": "Chemicals & Materials",
    "IGPL": "Chemicals & Materials", "NEOGEN": "Chemicals & Materials",
    "FLUOROCHEM": "Chemicals & Materials", "YASHO": "Chemicals & Materials",
    "AGI": "Materials & Packaging", "GRWRHITECH": "Materials & Packaging",
    "XPROINDIA": "Materials & Packaging", "ALLTIME": "Materials & Packaging",
    # Industrial engineering, water, machinery, construction and defence supply chain
    "HBLENGINE": "Industrial & Engineering", "PENIND": "Industrial & Engineering",
    "TIIL": "Industrial & Engineering", "WABAG": "Industrial & Engineering",
    "INTERARCH": "Industrial & Engineering", "EPACKPEB": "Industrial & Engineering",
    "KRN": "Industrial & Engineering", "544458": "Industrial & Engineering",
    "TRITURBINE": "Industrial & Engineering", "JASH": "Industrial & Engineering",
    "DYNAMATECH": "Industrial & Engineering", "ICEMAKE": "Industrial & Engineering",
    "DEEDEV": "Industrial & Engineering", "AEROFLEX": "Industrial & Engineering",
    "MARINE": "Industrial & Engineering",
    # Energy and renewables
    "WAAREEENER": "Energy", "SOLEX": "Energy", "WEBELSOLAR": "Energy",
    "BORORENEW": "Energy", "PREMIERENE": "Energy", "EMMVEE": "Energy",
    # Consumer products, beverages, textiles and agriculture
    "PICCADIL": "Beverages & Consumer", "ABDL": "Beverages & Consumer",
    "RADICO": "Beverages & Consumer", "UNITDSPR": "Beverages & Consumer",
    "CCL": "Beverages & Consumer", "VINCOFE": "Beverages & Consumer",
    "VBL": "Beverages & Consumer", "VADILALIND": "Beverages & Consumer",
    "DHARMAJ": "Agriculture & Agri Inputs", "DHANUKA": "Agriculture & Agri Inputs",
    "GANECOS": "Textiles & Apparel", "RAYMOND": "Textiles & Apparel",
    "PRECOT": "Textiles & Apparel",
    # Hospitality and logistics
    "SAMHI": "Hospitality", "ADVENTHTL": "Hospitality",
    "AEGISVOPAK": "Ports & Logistics", "ADANIPORTS": "Ports & Logistics",
}


def coverage_sector_for(symbol: str | None, company_name: str | None = None) -> str:
    """Resolve a Coverage List instrument to its curated business sector."""
    normalized = str(symbol or "").strip().upper()
    if normalized in COVERAGE_SECTOR_BY_SYMBOL:
        return COVERAGE_SECTOR_BY_SYMBOL[normalized]
    name = str(company_name or "").casefold()
    keyword_groups = (
        (("refrigerat", "engineering", "turbine", "water", "pipes", "machinery"), "Industrial & Engineering"),
        (("pharma", "laborator", "healthcare", "hospital", "life science"), "Pharmaceuticals"),
        (("finance", "bank", "microfin", "housing"), "Financial Services"),
        (("chemical", "fluoro", "organics"), "Chemicals & Materials"),
        (("energy", "solar", "photovoltaic", "renewable"), "Energy"),
        (("hotel", "hospitality"), "Hospitality"),
        (("port", "terminal", "logistics"), "Ports & Logistics"),
    )
    for keywords, sector in keyword_groups:
        if any(word in name for word in keywords):
            return sector
    return "Other Coverage"


MACRO_KPI_INSTRUMENTS: list[dict[str, str]] = [
    {"key": "usd_inr", "name": "USD / INR", "symbol": "USDINR=X", "yahoo": "USDINR=X", "unit": "₹ per USD", "category": "currency"},
    {"key": "crude_oil", "name": "WTI Crude Oil", "symbol": "CL=F", "yahoo": "CL=F", "unit": "USD / barrel", "category": "energy"},
]

COMMODITY_INSTRUMENTS: list[dict[str, str]] = [
    {"key": "wti_crude", "name": "WTI Crude Oil", "symbol": "CL=F", "yahoo": "CL=F", "unit": "USD / barrel", "category": "energy"},
    {"key": "brent_crude", "name": "Brent Crude Oil", "symbol": "BZ=F", "yahoo": "BZ=F", "unit": "USD / barrel", "category": "energy"},
    {"key": "natural_gas", "name": "Natural Gas", "symbol": "NG=F", "yahoo": "NG=F", "unit": "USD / MMBtu", "category": "energy"},
    {"key": "gold", "name": "Gold", "symbol": "GC=F", "yahoo": "GC=F", "unit": "USD / troy oz", "category": "metals"},
    {"key": "silver", "name": "Silver", "symbol": "SI=F", "yahoo": "SI=F", "unit": "USD / troy oz", "category": "metals"},
    {"key": "copper", "name": "Copper", "symbol": "HG=F", "yahoo": "HG=F", "unit": "USD / lb", "category": "metals"},
]


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return round((current / previous - 1.0) * 100.0, 4)


class PulseMarketDataCollector:
    """Fetch minute bars and optional end-of-day fallback data."""

    def __init__(self) -> None:
        self.timeout = float(os.getenv("PULSE_MARKET_TIMEOUT_SECONDS", "6"))
        self.concurrency = max(1, int(os.getenv("PULSE_MARKET_CONCURRENCY", "8")))
        self._sem = asyncio.Semaphore(self.concurrency)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache_ttl = max(10, int(os.getenv("PULSE_MARKET_CACHE_SECONDS", "30")))
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=5.0),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "en-IN,en;q=0.9",
            },
            follow_redirects=True,
        )
        self._tejhq_enabled = os.getenv("PULSE_TEJHQ_ENABLE", "false").strip().lower() in {"1", "true", "yes"}
        self._tejhq_personal_ack = os.getenv("PULSE_TEJHQ_PERSONAL_USE", "false").strip().lower() in {"1", "true", "yes"}
        # TejHQ's open snapshot endpoints are keyless, but its data terms limit
        # free use to personal/private research. Require an explicit opt-in.
        self._eod_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._tejhq_last_status: dict[str, Any] = {
            "status": "disabled", "rows": 0,
            "reason": "Enable PULSE_TEJHQ_ENABLE and acknowledge personal/private use to query the free snapshot API.",
        }

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _yahoo_chart(self, yahoo_symbol: str) -> dict[str, Any]:
        cache_key = f"yahoo:{yahoo_symbol}"
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < self._cache_ttl:
            return cached[1]
        async with self._sem:
            url = YAHOO_CHART.format(url_quote(yahoo_symbol, safe=""))
            try:
                response = await self._client.get(url, params={"range": "1d", "interval": "1m", "includePrePost": "false", "events": "div,splits"})
                if response.status_code != 200:
                    raise RuntimeError(f"Yahoo chart HTTP {response.status_code}")
                payload = response.json()
                chart = payload.get("chart", {})
                if chart.get("error"):
                    raise RuntimeError(str(chart["error"])[:180])
                result_rows = chart.get("result") or []
                if not result_rows:
                    raise RuntimeError("Yahoo returned no chart rows")
                result = result_rows[0]
                meta = result.get("meta") or {}
                timestamps = result.get("timestamp") or []
                quote = (((result.get("indicators") or {}).get("quote") or [{}])[0])
                closes = quote.get("close") or []
                volumes = quote.get("volume") or []
                points = []
                for index, stamp in enumerate(timestamps):
                    close = _safe_float(closes[index] if index < len(closes) else None)
                    if close is None:
                        continue
                    volume = _safe_float(volumes[index] if index < len(volumes) else None) or 0.0
                    points.append({"timestamp": datetime.fromtimestamp(int(stamp), IST).isoformat(), "epoch": int(stamp), "price": close, "volume": volume})
                if not points:
                    raise RuntimeError("Yahoo returned no valid one-minute closes")
                last = points[-1]
                previous_close = _safe_float(meta.get("chartPreviousClose") or meta.get("previousClose"))
                day_pct = _pct(last["price"], previous_close)
                def window_pct(minutes: int) -> float | None:
                    target_epoch = last["epoch"] - minutes * 60
                    baseline = next((point for point in reversed(points[:-1]) if point["epoch"] <= target_epoch), None)
                    if baseline is None or (target_epoch - baseline["epoch"]) > 120:
                        return None
                    return _pct(last["price"], baseline["price"])
                recent_volumes = [max(0.0, float(p["volume"])) for p in points[-5:]]
                baseline_volumes = [max(0.0, float(p["volume"])) for p in points[-35:-5]]
                recent_avg = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 0.0
                baseline = statistics.median(baseline_volumes) if baseline_volumes else 0.0
                volume_ratio = round(recent_avg / baseline, 2) if baseline > 0 else None
                retrieved_at = datetime.now(IST).isoformat()
                age_seconds = max(0, int(time.time()) - int(last["epoch"]))
                data = {
                    "status": "available",
                    "symbol": yahoo_symbol,
                    "price": last["price"],
                    "previous_close": previous_close,
                    "change_pct": day_pct,
                    "change_5m_pct": window_pct(5),
                    "change_15m_pct": window_pct(15),
                    "volume_ratio": volume_ratio,
                    "timestamp": last["timestamp"],
                    "age_seconds": age_seconds,
                    "freshness": "intraday_unverified" if age_seconds <= 600 else "stale",
                    "data_type": "intraday_indicative",
                    "delay_minutes": None,
                    "delay_verified": False,
                    "source": "Yahoo Finance chart endpoint (unofficial)",
                    "source_url": f"https://finance.yahoo.com/quote/{yahoo_symbol}",
                    "series": points[-120:],
                    "retrieved_at": retrieved_at,
                }
                self._cache[cache_key] = (time.monotonic(), data)
                return data
            except Exception as exc:
                logger.info("Pulse Yahoo chart failed symbol=%s error=%s", yahoo_symbol, type(exc).__name__)
                return {"status": "unavailable", "symbol": yahoo_symbol, "reason": f"Yahoo chart unavailable: {type(exc).__name__}: {str(exc)[:160]}", "source": "Yahoo Finance chart endpoint (unofficial)", "retrieved_at": datetime.now(IST).isoformat(), "series": []}

    @staticmethod
    def _quote_card(meta: dict[str, str], quote: dict[str, Any]) -> dict[str, Any]:
        return {
            **meta,
            "status": quote.get("status", "unavailable"),
            "price": quote.get("price"),
            "previous_close": quote.get("previous_close"),
            "change_pct": quote.get("change_pct"),
            "change_5m_pct": quote.get("change_5m_pct"),
            "change_15m_pct": quote.get("change_15m_pct"),
            "timestamp": quote.get("timestamp"),
            "freshness": quote.get("freshness"),
            "data_type": quote.get("data_type"),
            "delay_verified": False,
            "source": quote.get("source", "Yahoo Finance chart endpoint (unofficial)"),
            "source_url": quote.get("source_url"),
            "reason": quote.get("reason"),
        }

    async def market_kpis(self) -> dict[str, dict[str, Any]]:
        """Return the two macro quotes used in the Pulse blue banner."""
        results = await asyncio.gather(
            *(self._yahoo_chart(row["yahoo"]) for row in MACRO_KPI_INSTRUMENTS),
            return_exceptions=True,
        )
        output: dict[str, dict[str, Any]] = {}
        for meta, result in zip(MACRO_KPI_INSTRUMENTS, results):
            quote = result if isinstance(result, dict) else {"status": "unavailable", "reason": type(result).__name__}
            output[meta["key"]] = self._quote_card(meta, quote)
        return output

    async def commodity_prices(self) -> dict[str, Any]:
        """Fetch an on-demand commodity board; quotes are indicative futures prices."""
        results = await asyncio.gather(
            *(self._yahoo_chart(row["yahoo"]) for row in COMMODITY_INSTRUMENTS),
            return_exceptions=True,
        )
        items = []
        for meta, result in zip(COMMODITY_INSTRUMENTS, results):
            quote = result if isinstance(result, dict) else {"status": "unavailable", "reason": type(result).__name__}
            items.append(self._quote_card(meta, quote))
        return {
            "as_of": datetime.now(IST).isoformat(),
            "items": items,
            "source_note": "Indicative Yahoo Finance futures quotes. Contract prices are not the same as local spot/MCX prices; actual delivery delay is not guaranteed.",
        }

    async def market_universe(self, extra_stocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Collect index data and only the instruments in the Coverage List.

        The Coverage List workbook is the sole stock universe for the Pulse
        stock heatmap. The old hard-coded Pulse stock universe is deliberately
        not merged into this result. Coverage rows without a valid quote remain
        in the payload with an unavailable status so the UI still reflects the
        workbook rather than silently dropping companies.
        """
        instruments: list[dict[str, Any]] = []
        tasks = []
        meta: list[dict[str, Any]] = []
        for row in SECTOR_UNIVERSE:
            meta.append({
                **row,
                "kind": "sector" if row["key"] not in {"NIFTY50", "SENSEX", "VIX"} else "index",
                "yahoo": row["yahoo"],
                "watchlist_source": "index_universe",
            })
            tasks.append(self._yahoo_chart(row["yahoo"]))

        merged_stocks: dict[tuple[str, str], dict[str, Any]] = {}
        for row in extra_stocks or []:
            symbol = str(row.get("symbol") or "").strip().upper()
            yahoo_symbol = str(row.get("yahoo") or "").strip()
            exchange = str(row.get("exchange") or "NSE").upper()
            if not symbol or not yahoo_symbol or exchange not in {"NSE", "BSE"}:
                continue
            identity = (exchange, symbol)
            merged_stocks[identity] = {
                **row,
                "symbol": symbol,
                "kind": "stock",
                "exchange": exchange,
                "watchlist_source": "coverage_list",
                "yahoo": yahoo_symbol,
                "sector": row.get("sector") or coverage_sector_for(symbol, row.get("name")),
            }

        max_stocks = max(10, min(300, int(os.getenv("PULSE_MAX_WATCHLIST_STOCKS", "250"))))
        selected_stocks = list(merged_stocks.values())[:max_stocks]
        for row in selected_stocks:
            meta.append(row)
            tasks.append(self._yahoo_chart(str(row["yahoo"])))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = 0
        for item, result in zip(meta, results):
            market = result if isinstance(result, dict) else {
                "status": "unavailable", "reason": type(result).__name__, "series": [],
                "data_type": "unavailable", "freshness": "unavailable",
            }
            normalized = {**market, **item}
            instruments.append(normalized)
            if market.get("status") == "available":
                success += 1

        # EOD data can fill a missing quote, but it remains explicitly EOD and
        # is never fed into the intraday movement detector.
        eod_rows = await self._get_tejhq_snapshot()
        for row in instruments:
            if row.get("kind") != "stock" or row.get("status") == "available":
                continue
            exchange = str(row.get("exchange") or "NSE").upper()
            symbol = str(row.get("symbol") or "").upper()
            eod = eod_rows.get(f"{exchange}:{symbol}") or (eod_rows.get(symbol) if exchange == "NSE" else None)
            if eod:
                row.update(eod)
                row["status"] = "eod_fallback"
                row["data_type"] = "end_of_day"
                row["freshness"] = "eod"
                row["delay_verified"] = False
                row["source"] = f"TejHQ {exchange} keyless EOD snapshot"
                row["source_url"] = "https://tejhq.dev/docs"

        # Add industry baskets for covered companies that do not have a direct
        # NIFTY sector index. Basket changes are equal-weighted averages of
        # available Coverage List members, clearly distinguished from official
        # exchange indices.
        official_sectors = {
            row.get("sector") for row in SECTOR_UNIVERSE
            if row.get("key") not in {"NIFTY50", "SENSEX", "VIX"}
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in instruments:
            if row.get("kind") == "stock" and row.get("watchlist_source") == "coverage_list":
                grouped.setdefault(str(row.get("sector") or "Other Coverage"), []).append(row)

        for sector_name, members in sorted(grouped.items()):
            if sector_name in official_sectors:
                continue
            valid = [row for row in members if row.get("status") in {"available", "eod_fallback"}]
            intraday = [row for row in valid if row.get("status") == "available" and row.get("data_type") != "end_of_day"]
            def mean_value(key: str) -> float | None:
                values = [_safe_float(row.get(key)) for row in valid]
                values = [value for value in values if value is not None]
                return round(sum(values) / len(values), 4) if values else None
            slug = "-".join(part for part in "".join(ch.lower() if ch.isalnum() else " " for ch in sector_name).split())
            instruments.append({
                "kind": "sector", "key": f"COVERAGE-{slug.upper().replace('-', '_')}",
                "symbol": f"PULSE-SECTOR-{slug.upper().replace('-', '_')}",
                "name": sector_name, "sector": sector_name,
                "is_synthetic": True, "source": "Equal-weighted Coverage List industry basket",
                "status": "available" if intraday else "eod_fallback" if valid else "unavailable",
                "data_type": "intraday" if intraday else "end_of_day" if valid else "unavailable",
                "freshness": "fresh" if intraday else "eod" if valid else "unavailable",
                "price": None, "change_pct": mean_value("change_pct"),
                "change_5m_pct": mean_value("change_5m_pct"),
                "change_15m_pct": mean_value("change_15m_pct"),
                "volume_ratio": mean_value("volume_ratio"),
                "constituent_count": len(members),
                "priced_constituent_count": len(valid),
                "timestamp": max((str(row.get("timestamp") or "") for row in members), default=None) or None,
                "age_seconds": max((_safe_float(row.get("age_seconds")) or 0.0 for row in intraday), default=None),
                "series": [], "watchlist_source": "coverage_list_basket",
            })

        macro = await self.market_kpis()
        coverage_count = len(selected_stocks)
        stock_success = sum(1 for row in instruments if row.get("kind") == "stock" and row.get("status") == "available")
        return {
            "as_of": datetime.now(IST).isoformat(),
            "macro": macro,
            "instruments": instruments,
            "provider_status": {
                "yahoo": {"status": "available" if success else "unavailable", "successful_instruments": success, "requested_instruments": len(meta), "delay_verified": False, "note": "Unofficial chart endpoint; actual delivery delay is not guaranteed."},
                "tejhq": {**self._tejhq_last_status, "status": "available" if eod_rows else self._tejhq_last_status.get("status", "unavailable"), "note": "NSE/BSE end-of-day snapshots only; not intraday quotes. Free data terms limit use to personal/private research."},
            },
            "market_data_count": success,
            "stock_data_count": stock_success,
            "watchlist": {
                "requested_stocks": coverage_count,
                "coverage_stocks_requested": coverage_count,
                "core_stocks_requested": 0,
                "stock_cap": max_stocks,
                "source": "Coverage List workbook only",
                "note": "Stock instruments come exclusively from the Coverage List workbook; unavailable quotes remain visible as unavailable.",
            },
        }

    async def _get_tejhq_snapshot(self) -> dict[str, dict[str, Any]]:
        """Fetch the current available NSE+BSE EOD snapshot from TejHQ.

        Current TejHQ keyless API contract:
        GET /v1/snapshot/{exchange}?date=YYYY-MM-DD
        Response envelope: {"data": [...], "meta": {...}}

        No intraday prices are inferred from these daily rows. The caller only
        uses them as visibly-labelled EOD context when an intraday quote is
        unavailable. Free data terms require personal/private use.
        """
        if not (self._tejhq_enabled and self._tejhq_personal_ack):
            self._tejhq_last_status = {
                "status": "disabled", "rows": 0,
                "reason": "Opt-in required: PULSE_TEJHQ_ENABLE=true and PULSE_TEJHQ_PERSONAL_USE=true.",
            }
            return {}
        if self._eod_cache and time.monotonic() - self._eod_cache[0] < 12 * 3600:
            cached_rows = self._eod_cache[1]
            self._tejhq_last_status = {"status": "available" if cached_rows else "empty", "rows": len(cached_rows), "cached": True}
            return cached_rows

        output: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        dates_checked: list[str] = []
        stop_for_provider_failure = False
        today = datetime.now(IST).date()
        for exchange in ("nse", "bse"):
            exchange_rows = 0
            # TejHQ is updated on trading days, normally after the close. Check
            # recent dates rather than assuming today's bhavcopy already exists.
            for offset in range(0, 11):
                target = today - timedelta(days=offset)
                date_text = target.isoformat()
                dates_checked.append(date_text)
                url = f"{TEJHQ_API}/snapshot/{exchange}"
                try:
                    response = await self._client.get(
                        url,
                        params={"date": date_text},
                        timeout=12.0,
                        headers={"Accept": "application/json"},
                    )
                    if response.status_code == 404:
                        continue
                    if response.status_code != 200:
                        detail = response.text[:120].replace("\n", " ")
                        errors.append(f"{exchange.upper()} {date_text}: HTTP {response.status_code} {detail}")
                        if response.status_code in (401, 403, 429) or response.status_code >= 500:
                            # Authentication, policy, rate-limit and upstream
                            # server failures won't improve by changing dates.
                            stop_for_provider_failure = True
                            break
                        # A bad date can be a 400; try the preceding trading day.
                        continue
                    try:
                        payload = response.json()
                    except ValueError:
                        errors.append(f"{exchange.upper()} {date_text}: response was not JSON")
                        stop_for_provider_failure = True
                        break
                    rows = payload.get("data", []) if isinstance(payload, dict) else []
                    if not isinstance(rows, list) or not rows:
                        # Keyless snapshot is an open endpoint. A structured
                        # error envelope is more useful than silently failing.
                        error_text = payload.get("error") or payload.get("message") if isinstance(payload, dict) else None
                        if error_text:
                            errors.append(f"{exchange.upper()} {date_text}: {str(error_text)[:120]}")
                        continue
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        raw_symbol = str(row.get("symbol") or row.get("scrip_code") or row.get("code") or "").strip().upper()
                        close = _safe_float(row.get("close") or row.get("last"))
                        previous = _safe_float(row.get("prev_close"))
                        if not raw_symbol or close is None:
                            continue
                        normalized = {
                            "price": close,
                            "previous_close": previous,
                            "change_pct": _pct(close, previous),
                            "change_5m_pct": None,
                            "change_15m_pct": None,
                            "volume_ratio": None,
                            "timestamp": f"{date_text}T15:30:00+05:30",
                            "age_seconds": None,
                            "freshness": "eod",
                            "data_type": "end_of_day",
                            "delay_verified": False,
                            "source": f"TejHQ {exchange.upper()} EOD snapshot",
                            "source_url": "https://tejhq.dev/docs",
                            "exchange": exchange.upper(),
                            "series": [],
                        }
                        output[f"{exchange.upper()}:{raw_symbol}"] = normalized
                        # Keep the unqualified NSE symbol for older callers and
                        # instruments where exchange metadata is not populated.
                        if exchange == "nse":
                            output.setdefault(raw_symbol, normalized)
                        exchange_rows += 1
                    if exchange_rows:
                        break
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    errors.append(f"{exchange.upper()} {date_text}: {type(exc).__name__}: {str(exc)[:100]}")
                    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                        # DNS/TLS/connectivity failures are host-wide; don't
                        # repeat the same failing request for every date.
                        stop_for_provider_failure = True
                        break
                    continue
            logger.info("Pulse TejHQ snapshot exchange=%s rows=%s", exchange.upper(), exchange_rows)
            if stop_for_provider_failure:
                break

        # Do not cache an empty/failed response for 12 hours. A transient DNS,
        # TLS or provider error should be retried on the next worker cycle.
        self._eod_cache = (time.monotonic(), output) if output else None
        self._tejhq_last_status = {
            "status": "available" if output else "unavailable",
            "rows": len([key for key in output if ":" in key]),
            "exchanges": [exchange for exchange in ("NSE", "BSE") if any(key.startswith(f"{exchange}:") for key in output)],
            "dates_checked": list(dict.fromkeys(dates_checked))[-12:],
            "reason": "; ".join(errors[-4:]) if errors and not output else None,
            "cached": False,
        }
        if not output:
            logger.warning("Pulse TejHQ EOD snapshot unavailable details=%s", self._tejhq_last_status.get("reason") or "No rows returned in recent dates")
        return output


pulse_market_data_collector = PulseMarketDataCollector()
