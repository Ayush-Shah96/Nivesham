from __future__ import annotations

"""Cached paginated public Screener.in community-screen snapshots for Pulse."""

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from time import time as wall_time
from typing import Any
from urllib.parse import quote, unquote, urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
CACHE_TTL_SECONDS = 600
MAX_STALE_SECONDS = 24 * 60 * 60
CACHE_FILE = Path(tempfile.gettempdir()) / "c9_equity_pulse_screener_lists_v1.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SCREENS = {
    "quarterly_growers": {
        "title": "Quarterly Growing Stocks",
        "url": "https://www.screener.in/screens/86/quarterly-growers/",
    },
    "promoters_confidence": {
        "title": "Promoters’ Confidence",
        "url": "https://www.screener.in/screens/263229/promoters-confidence/",
    },
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _page_count(html: str) -> int:
    text = _clean(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    matches = [int(x) for x in re.findall(r"Showing\s+page\s+\d+\s+of\s+(\d+)", text, re.I)]
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        match = re.search(r"[?&]page=(\d+)", href)
        if match:
            matches.append(int(match.group(1)))
    return max(1, min(max(matches or [1]), 100))


def _parse_screen_page(html: str, base_url: str) -> tuple[list[str], list[dict[str, Any]]]:
    soup = BeautifulSoup(html, "lxml")
    tables = soup.select("table.data-table, table#results, table")
    selected = None
    # Prefer a results table with at least one row linking to a company page.
    for table in tables:
        if table.select_one('a[href*="/company/"]') and table.find("tr"):
            selected = table
            break
    if selected is None:
        return [], []

    header_row = selected.find("thead")
    header_cells = header_row.find_all(["th", "td"]) if header_row else []
    if not header_cells:
        first_row = selected.find("tr")
        header_cells = first_row.find_all(["th", "td"]) if first_row else []
    headers = [_clean(cell.get_text(" ", strip=True)) or f"Column {index + 1}" for index, cell in enumerate(header_cells)]
    if not headers:
        return [], []

    parsed: list[dict[str, Any]] = []
    tbody = selected.find("tbody")
    tr_rows = (tbody.find_all("tr", recursive=False) if tbody else selected.find_all("tr"))
    for tr in tr_rows:
        cells = tr.find_all(["th", "td"], recursive=False)
        values = [_clean(cell.get_text(" ", strip=True)) for cell in cells]
        if not values or not any(values):
            continue
        link = tr.select_one('a[href*="/company/"]')
        if link is None:
            # Skip repeated header rows and any explanatory/footer rows.
            if any(value.casefold() == "company" or value.casefold() == "name" for value in values):
                continue
            continue
        href = urljoin(base_url, str(link.get("href") or ""))
        match = re.search(r"/company/([^/?#]+)/", href, re.I)
        symbol = quote(unquote(match.group(1)), safe="._-&") if match else ""
        name = _clean(link.get_text(" ", strip=True)) or (values[1] if len(values) > 1 else values[0])
        # Deduplicate table row cells when data tables repeat headers or hidden mobile labels.
        if len(values) < len(headers):
            values = values + [""] * (len(headers) - len(values))
        parsed.append({
            "symbol": symbol,
            "name": name,
            "url": href,
            "cells": values[:len(headers)],
        })
    # Screener pages can contain a sticky duplicate header/results table; dedupe by symbol.
    unique: dict[str, dict[str, Any]] = {}
    for row in parsed:
        if row["symbol"]:
            unique.setdefault(row["symbol"].casefold(), row)
    return headers, list(unique.values())


class ScreenerListsCollector:
    def __init__(self, *, timeout_seconds: float = 15.0, cache_ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._warm_lock = asyncio.Lock()
        self._memory: tuple[float, dict[str, Any]] | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout_seconds, connect=6.0),
                    follow_redirects=True,
                    headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                )
            return self._client

    async def _fetch_page(self, url: str) -> str:
        client = await self._get_client()
        response = await client.get(url)
        response.raise_for_status()
        if "html" not in response.headers.get("content-type", "").casefold():
            raise ValueError("Screener.in did not return HTML")
        return response.text

    @staticmethod
    def _read_shared(*, allow_stale: bool = False) -> dict[str, Any] | None:
        try:
            payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            age = wall_time() - float(payload.get("saved_at_epoch", 0))
            max_age = MAX_STALE_SECONDS if allow_stale else CACHE_TTL_SECONDS
            if age < 0 or age >= max_age or not isinstance(payload.get("screens"), dict):
                return None
            payload["stale"] = age >= CACHE_TTL_SECONDS
            payload["age_seconds"] = age
            return payload
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_shared(payload: dict[str, Any]) -> None:
        temporary = None
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=CACHE_FILE.name + ".", suffix=".tmp", dir=str(CACHE_FILE.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, CACHE_FILE)
        except (OSError, TypeError, ValueError):
            logger.warning("PULSE_SCREENER_LIST_CACHE_WRITE_FAILED", exc_info=True)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    async def _load_screen(self, title: str, url: str) -> dict[str, Any]:
        first_html = await self._fetch_page(url)
        headers, first_rows = _parse_screen_page(first_html, url)
        pages = _page_count(first_html)
        if not headers or not first_rows:
            raise ValueError(f"No company rows found on {url}")
        # Fetch one complete screen independently; one blocked screen must not
        # make the other screen disappear.
        semaphore = asyncio.Semaphore(3)
        async def load_page(page_no: int) -> tuple[int, list[dict[str, Any]]]:
            page_url = f"{url}?page={page_no}" if "?" not in url else f"{url}&page={page_no}"
            last_error: Exception | None = None
            async with semaphore:
                for attempt in range(2):
                    try:
                        if attempt:
                            await asyncio.sleep(0.35)
                        html = await self._fetch_page(page_url)
                        page_headers, rows = _parse_screen_page(html, page_url)
                        if rows:
                            return page_no, rows
                        last_error = ValueError("No company rows found on paginated screen")
                    except Exception as exc:
                        last_error = exc
            logger.warning("PULSE_SCREENER_LIST_PAGE_FAILED screen=%s page=%s error_type=%s", title, page_no, type(last_error).__name__ if last_error else "NoRows")
            return page_no, []
        results = await asyncio.gather(*(load_page(page) for page in range(2, pages + 1))) if pages > 1 else []
        merged: dict[str, dict[str, Any]] = {row["symbol"].casefold(): row for row in first_rows}
        failed_pages: list[int] = []
        for page_no, rows in results:
            if not rows:
                failed_pages.append(page_no)
            for row in rows:
                merged.setdefault(row["symbol"].casefold(), row)
        rows = list(merged.values())
        logger.info("PULSE_SCREENER_LIST_WARMED title=%s pages=%d rows=%d failed_pages=%s", title, pages, len(rows), failed_pages)
        return {"title": title, "url": url, "headers": headers, "rows": rows, "row_count": len(rows), "page_count": pages, "failed_pages": failed_pages, "available": bool(rows), "reason": f"Some pages could not be fetched: {', '.join(map(str, failed_pages))}." if failed_pages else None}

    async def warm_cache(self, *, force_refresh: bool = False) -> dict[str, Any]:
        current = self._read_shared()
        if current and not force_refresh:
            self._memory = (wall_time(), current)
            return {"status": "ok", "count": sum(v.get("row_count", 0) for v in current["screens"].values()), "cache_hit": True}
        async with self._warm_lock:
            current = self._read_shared()
            if current and not force_refresh:
                self._memory = (wall_time(), current)
                return {"status": "ok", "count": sum(v.get("row_count", 0) for v in current["screens"].values()), "cache_hit": True}
            semaphore = asyncio.Semaphore(2)
            async def load_one(key: str, item: dict[str, str]) -> tuple[str, dict[str, Any]]:
                async with semaphore:
                    try:
                        return key, await self._load_screen(item["title"], item["url"])
                    except Exception as exc:
                        logger.warning("PULSE_SCREENER_LIST_FAILED title=%s error_type=%s", item["title"], type(exc).__name__)
                        return key, {"title": item["title"], "url": item["url"], "headers": [], "rows": [], "row_count": 0, "page_count": 0, "available": False, "reason": f"Source unavailable ({type(exc).__name__})."}
            loaded = await asyncio.gather(*(load_one(key, item) for key, item in SCREENS.items()))
            screens = dict(loaded)
            if not any(item.get("rows") for item in screens.values()):
                stale = self._read_shared(allow_stale=True)
                if stale:
                    logger.warning("PULSE_SCREENER_LIST_REFRESH_FAILED_USING_STALE")
                    self._memory = (wall_time(), stale)
                    return {"status": "stale", "count": sum(v.get("row_count", 0) for v in stale["screens"].values()), "reason": "Refresh failed; prior snapshot retained."}
                return {"status": "unavailable", "count": 0, "reason": "Screener.in did not return readable company tables."}
            timestamp = datetime.now(timezone.utc).isoformat()
            incomplete = any((not item.get("available")) or item.get("failed_pages") for item in screens.values())
            payload = {"status": "partial" if incomplete else "ok", "saved_at": timestamp, "saved_at_epoch": wall_time(), "ttl_seconds": self.cache_ttl_seconds, "source": "Screener.in", "screens": screens}
            self._write_shared(payload)
            self._memory = (wall_time(), payload)
            return {"status": payload["status"], "count": sum(v.get("row_count", 0) for v in screens.values()), "screens": screens, "saved_at": timestamp, "cache_hit": False}

    async def get_lists(self, *, force_refresh: bool = False) -> dict[str, Any]:
        snapshot = self._read_shared() or self._read_shared(allow_stale=True)
        if snapshot:
            return {**snapshot, "status": "stale" if snapshot.get("stale") else snapshot.get("status", "ok")}
        if self._memory and wall_time() - self._memory[0] < self.cache_ttl_seconds:
            return self._memory[1]
        return {"status": "warming", "source": "Screener.in", "screens": {}, "reason": "The two paginated Screener.in lists are warming in the worker; try again shortly."}

    async def aclose(self) -> None:
        async with self._client_lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None


collector = ScreenerListsCollector()
