"""Public exchange filing collectors for Pulse.

Primary feed inputs are NSE-published RSS feeds. A best-effort BSE public
announcements request is also attempted. These are public web surfaces, not a
licensed low-latency data feed; they may be delayed, incomplete or unavailable.
"""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

NSE_FEEDS = {
    "announcements": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
    "corporate_actions": "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml",
    "board_meetings": "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml",
    "financial_results": "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml",
    "insider_trading": "https://nsearchives.nseindia.com/content/RSS/InsiderTrading.xml",
}
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_NOTICES_XML = "https://www.bseindia.com/data/xml/notices.xml"


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(IST).isoformat()
    except (TypeError, ValueError, OverflowError):
        return value


class PulseExchangeFilingsCollector:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=5.0),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "application/rss+xml, application/xml, text/xml, application/json, */*",
                "Accept-Language": "en-IN,en;q=0.9",
                "Referer": "https://www.bseindia.com/",
                "Origin": "https://www.bseindia.com",
            },
            follow_redirects=True,
        )
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _nse_feed(self, feed_name: str, url: str) -> dict[str, Any]:
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            rows = []
            for node in root.findall(".//item"):
                title = (node.findtext("title") or "NSE filing").strip()
                link = (node.findtext("link") or "").strip()
                description = (node.findtext("description") or "").strip()
                pub_date = (node.findtext("pubDate") or "").strip()
                source_kind = {
                    "announcements": "exchange_filing",
                    "corporate_actions": "corporate_action",
                    "board_meetings": "board_meeting",
                    "financial_results": "financial_result",
                    "insider_trading": "insider_trading",
                }.get(feed_name, "exchange_filing")
                rows.append({
                    "title": title,
                    "url": link or None,
                    "description": re.sub(r"<[^>]+>", " ", description)[:1000],
                    "published_at": _date(pub_date),
                    "publisher": "NSE India",
                    "exchange": "NSE",
                    "kind": "exchange_filing",
                    "filing_type": source_kind,
                    "source": "NSE official RSS",
                    "source_url": url,
                })
            return {"status": "available", "rows": rows[:500], "count": len(rows), "source_url": url}
        except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
            logger.info("Pulse NSE RSS failed feed=%s error=%s", feed_name, type(exc).__name__)
            return {"status": "unavailable", "rows": [], "count": 0, "source_url": url, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}

    async def _bse_announcements(self) -> dict[str, Any]:
        now = datetime.now(IST).date()
        params = {
            "pageno": "1",
            "strCat": "-1",
            "subcategory": "-1",
            "strPrevDate": (now - timedelta(days=2)).strftime("%Y%m%d"),
            "strToDate": now.strftime("%Y%m%d"),
            "strSearch": "P",
            "strScrip": "",
            "strType": "C",
        }
        try:
            response = await self._client.get(BSE_API, params=params)
            response.raise_for_status()
            payload = response.json()
            table = payload.get("Table") or payload.get("table") or payload.get("Data") or []
            rows = []
            for row in table if isinstance(table, list) else []:
                if not isinstance(row, dict):
                    continue
                title = next((str(row[key]).strip() for key in ("HEADLINE", "NEWS_SUB", "NEWSSUB", "SUBJECT", "NEWS_TITLE", "CATEGORYNAME") if row.get(key)), "BSE announcement")
                company = next((str(row[key]).strip() for key in ("SLONGNAME", "SCRIP_NAME", "COMPANY", "Company_Name") if row.get(key)), None)
                attachment = next((str(row[key]).strip() for key in ("ATTACHMENTNAME", "ATTACHMENT", "NEWSURL", "URL") if row.get(key)), None)
                published = next((str(row[key]).strip() for key in ("NEWS_DT", "NEWS_DATE", "DT_TM", "DATE") if row.get(key)), None)
                code = next((str(row[key]).strip() for key in ("SCRIP_CD", "SCRIPCODE", "SC_CODE") if row.get(key)), None)
                if attachment and not attachment.startswith(("http://", "https://")):
                    attachment = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/" + attachment.lstrip("/")
                rows.append({
                    "title": f"{company}: {title}" if company else title,
                    "company": company,
                    "symbol": code,
                    "url": attachment,
                    "published_at": published,
                    "publisher": "BSE India",
                    "exchange": "BSE",
                    "kind": "exchange_filing",
                    "filing_type": "announcement",
                    "source": "BSE public announcements endpoint",
                    "source_url": "https://www.bseindia.com/corporates/ann.html",
                })
            return {"status": "available", "rows": rows, "count": len(rows), "source_url": "https://www.bseindia.com/corporates/ann.html"}
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.info("Pulse BSE announcement request failed error=%s", type(exc).__name__)
            return {"status": "unavailable", "rows": [], "count": 0, "source_url": "https://www.bseindia.com/corporates/ann.html", "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}

    async def all_filings(self, *, force_refresh: bool = False) -> dict[str, Any]:
        if self._cache and not force_refresh and __import__("time").monotonic() - self._cache[0] < 300:
            return self._cache[1]
        async with self._lock:
            if self._cache and not force_refresh and __import__("time").monotonic() - self._cache[0] < 300:
                return self._cache[1]
            names, urls = list(NSE_FEEDS.items()), list(NSE_FEEDS.values())
            tasks = [self._nse_feed(name, url) for name, url in names]
            tasks.append(self._bse_announcements())
            results = await asyncio.gather(*tasks, return_exceptions=True)
            sources: dict[str, Any] = {}
            all_rows: list[dict[str, Any]] = []
            for (name, _), result in zip(names, results[:len(names)]):
                source = result if isinstance(result, dict) else {"status": "unavailable", "rows": [], "reason": type(result).__name__}
                sources[f"nse_{name}"] = source
                all_rows.extend(source.get("rows", []))
            bse = results[-1] if isinstance(results[-1], dict) else {"status": "unavailable", "rows": [], "reason": type(results[-1]).__name__}
            sources["bse_announcements"] = bse
            all_rows.extend(bse.get("rows", []))
            result = {"as_of": datetime.now(IST).isoformat(), "sources": sources, "rows": all_rows, "count": len(all_rows)}
            self._cache = (__import__("time").monotonic(), result)
            return result

    async def search(self, *, symbol: str, company: str | None = None, limit: int = 25) -> dict[str, Any]:
        payload = await self.all_filings()
        symbol_key = _norm(symbol)
        company_tokens = [token for token in _norm(company).split() if len(token) >= 4]
        matches = []
        for row in payload.get("rows", []):
            haystack = _norm(" ".join(str(row.get(key) or "") for key in ("title", "description", "url", "company", "symbol")))
            matched = bool(symbol_key and symbol_key in haystack)
            if not matched and company_tokens:
                matched = sum(1 for token in company_tokens if token in haystack) >= min(2, len(company_tokens))
            if matched:
                matches.append(row)
        matches.sort(key=lambda row: str(row.get("published_at") or ""), reverse=True)
        return {
            "status": "ok" if matches else "empty",
            "items": matches[:max(1, min(limit, 100))],
            "sources": {key: {"status": value.get("status"), "count": value.get("count"), "reason": value.get("reason")} for key, value in payload.get("sources", {}).items()},
            "source": "NSE official RSS + best-effort BSE public announcements",
            "note": "NSE RSS and BSE public announcement pages can be delayed or incomplete; follow links to verify the original disclosure.",
        }


pulse_exchange_filings_collector = PulseExchangeFilingsCollector()
