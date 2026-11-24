"""Free public news/RSS discovery used by Pulse event investigations.

Google News RSS is an aggregation/search surface, not an authoritative filing
repository. Results are leads for an analyst to inspect. Exchange filings are
labeled separately when the RSS result points to NSE/BSE or explicitly names an
exchange; absence of a result is not proof that no filing exists.
"""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
RSS_URL = "https://news.google.com/rss/search"


def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    value = node.findtext(path)
    return value.strip() if value else None


class PulseNewsCollector:
    def __init__(self) -> None:
        self.timeout = float(__import__("os").getenv("PULSE_NEWS_TIMEOUT_SECONDS", "12"))
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=5.0),
            headers={"User-Agent": "Mozilla/5.0 (compatible; PulseMarketResearch/1.0)", "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8"},
            follow_redirects=True,
        )
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _classify(title: str, url: str) -> str:
        text = f"{title} {url}".casefold()
        if any(term in text for term in ("nseindia.com", "bseindia.com", "exchange filing", "corporate announcement", "stock exchange")):
            return "exchange_filing"
        return "news"

    async def search(self, query: str, *, kind: str = "news", limit: int = 18) -> dict[str, Any]:
        query = " ".join(str(query or "").split())[:240]
        if not query:
            return {"status": "unavailable", "items": [], "reason": "Empty search query"}
        cache_key = f"{kind}:{query.casefold()}"
        cached = self._cache.get(cache_key)
        if cached and datetime.now().timestamp() - cached[0] < 180:
            return {"status": "ok" if cached[1] else "empty", "items": cached[1], "cached": True, "source": "Google News RSS"}
        params = {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
        try:
            response = await self._client.get(RSS_URL, params=params)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            items: list[dict[str, Any]] = []
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            for node in root.findall(".//item"):
                title = _text(node, "title") or "Untitled article"
                url = _text(node, "link")
                published = _text(node, "pubDate")
                publisher = _text(node, "source") or "Google News"
                parsed = None
                if published:
                    try:
                        from email.utils import parsedate_to_datetime
                        parsed = parsedate_to_datetime(published)
                    except (TypeError, ValueError, OverflowError):
                        parsed = None
                if parsed and parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed and parsed < cutoff:
                    continue
                items.append({
                    "title": title,
                    "url": url,
                    "published_at": parsed.astimezone(IST).isoformat() if parsed else published,
                    "publisher": publisher,
                    "kind": self._classify(title, url or ""),
                    "query_type": kind,
                    "source": "Google News RSS",
                    "source_url": "https://news.google.com/",
                })
                if len(items) >= max(1, min(limit, 30)):
                    break
            # Deduplicate by URL/title while preserving relevance order.
            seen: set[str] = set()
            unique = []
            for item in items:
                key = (item.get("url") or item["title"]).casefold()
                if key not in seen:
                    seen.add(key)
                    unique.append(item)
            self._cache[cache_key] = (datetime.now().timestamp(), unique)
            return {"status": "ok" if unique else "empty", "items": unique, "cached": False, "source": "Google News RSS", "query": query}
        except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
            logger.info("Pulse news RSS search failed query=%s error=%s", query[:80], type(exc).__name__)
            return {"status": "unavailable", "items": [], "source": "Google News RSS", "query": query, "reason": f"News search unavailable: {type(exc).__name__}: {str(exc)[:140]}"}

    async def investigation_sources(self, *, symbol: str, company: str, sector: str | None = None) -> dict[str, Any]:
        company_query = f'"{company}" stock India when:7d' if company else f'{symbol} stock India when:7d'
        filing_query = f'"{symbol}" (NSE OR BSE) (announcement OR filing OR exchange) when:7d'
        sector_query = f'"{sector}" India stocks market when:7d' if sector else f'{symbol} sector India market when:7d'
        editorial_query = f'("{company}" OR "{symbol}") (analysis OR blog OR outlook OR commentary OR research) India when:7d'
        news, filings, sector_news, editorial = await asyncio.gather(
            self.search(company_query, kind="company_news", limit=15),
            self.search(filing_query, kind="exchange_filings", limit=15),
            self.search(sector_query, kind="sector_news", limit=10),
            self.search(editorial_query, kind="market_commentary", limit=12),
            return_exceptions=True,
        )
        def safe(result: Any) -> dict[str, Any]:
            return result if isinstance(result, dict) else {"status": "unavailable", "items": [], "reason": type(result).__name__}
        news_data, filing_data, sector_data, editorial_data = safe(news), safe(filings), safe(sector_news), safe(editorial)
        merged: list[dict[str, Any]] = []
        for group in (filing_data.get("items", []), news_data.get("items", []), sector_data.get("items", []), editorial_data.get("items", [])):
            merged.extend(group)
        seen: set[str] = set()
        unique = []
        for item in merged:
            key = str(item.get("url") or item.get("title", "")).casefold()
            if key and key not in seen:
                seen.add(key)
                unique.append(item)
        unique.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
        return {
            "items": unique[:35],
            "news_status": news_data.get("status"),
            "filings_search_status": filing_data.get("status"),
            "sector_news_status": sector_data.get("status"),
            "editorial_status": editorial_data.get("status"),
            "disclaimer": "Search results, blog posts and articles are investigation leads, not verified causes or a complete official filing feed. Check the original source and publication time.",
        }


pulse_news_collector = PulseNewsCollector()
