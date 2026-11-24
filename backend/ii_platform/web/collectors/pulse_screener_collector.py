from __future__ import annotations

"""Screener.in public-screen collector for the Pulse dashboard.

Each screen is crawled page-by-page using the pagination advertised by
Screener.in. Results are shared between the worker and API process through a
short-lived JSON file in the OS temp directory; no market/screen data is written
to the application database. The cache TTL intentionally matches Pulse's
10-minute market-data cache.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, time as wall_time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

PULSE_SCREENER_CACHE_TTL_SECONDS = 10 * 60
PULSE_SCREENER_SHARED_CACHE_DIR = Path(os.getenv("C9_SHARED_CACHE_DIR") or (Path(__file__).resolve().parents[3] / ".cache")) / "pulse_screens_v2"
MAX_PAGES_PER_SCREEN = 40
PAGE_CONCURRENCY = 4

SCREEN_DEFINITIONS: dict[str, dict[str, str]] = {
    "quarterly_growers": {
        "title": "Quarterly Growers",
        "url": "https://www.screener.in/screens/86/quarterly-grower",
    },
    "down_30_from_high": {
        "title": "Stocks Down by 30% or More from 52 Week High",
        "url": "https://www.screener.in/screens/1554787/stocks-down-by-30-or-more-from-52-week-high/",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _extract_page_count(soup: BeautifulSoup, page_text: str) -> int:
    """Read pagination from the Screener result summary and/or page links."""
    page_count = 1
    match = re.search(r"Showing\s+page\s+\d+\s+of\s+(\d+)", page_text, flags=re.IGNORECASE)
    if match:
        page_count = max(page_count, int(match.group(1)))

    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "")
        query = parse_qs(urlsplit(href).query)
        for value in query.get("page", []):
            try:
                page_count = max(page_count, int(value))
            except (TypeError, ValueError):
                continue
    return max(page_count, 1)


def _parse_screen_page(html: str, page_number: int, response_url: str) -> dict[str, Any]:
    """Parse one public Screener result page without assuming fixed columns."""
    soup = BeautifulSoup(html, "lxml")
    page_text = _clean_text(soup.get_text(" ", strip=True))
    title_node = soup.select_one("h1") or soup.select_one(".page-title")
    title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
    description_node = soup.select_one(".sub") or soup.select_one(".screen-description")
    description = _clean_text(description_node.get_text(" ", strip=True)) if description_node else ""

    result_match = re.search(r"([\d,]+)\s+results?\s+found", page_text, flags=re.IGNORECASE)
    result_count = int(result_match.group(1).replace(",", "")) if result_match else None

    table = soup.select_one("table.data-table") or soup.select_one("#results-table") or soup.select_one("table")
    if table is None:
        raise ValueError("Screener page did not contain a results table")

    header_row = table.select_one("thead tr")
    if header_row is None:
        header_row = table.find("tr")
    columns = [
        _clean_text(cell.get_text(" ", strip=True))
        for cell in (header_row.find_all(["th", "td"], recursive=False) if header_row else [])
    ]
    if not columns:
        raise ValueError("Screener results table did not contain column headers")

    body = table.select_one("tbody") or table
    rows: list[dict[str, Any]] = []
    for tr in body.find_all("tr", recursive=False):
        if header_row is tr:
            continue
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        values = [_clean_text(cell.get_text(" ", strip=True)) for cell in cells]
        if not any(values):
            continue
        # The company link is the most stable unique identifier across pages.
        company_link = None
        company_index = next((i for i, name in enumerate(columns) if "company" in name.lower()), None)
        if company_index is not None and company_index < len(cells):
            company_link = cells[company_index].find("a", href=True)
        if company_link is None:
            for cell in cells:
                candidate = cell.find("a", href=True)
                if candidate and "/company/" in str(candidate.get("href")):
                    company_link = candidate
                    break

        company = _clean_text(company_link.get_text(" ", strip=True)) if company_link else ""
        company_url = urljoin(response_url, str(company_link.get("href"))) if company_link else None
        mapped = {columns[i]: values[i] for i in range(min(len(columns), len(values)))}
        if not company and company_index is not None and company_index < len(values):
            company = values[company_index]
        rows.append({
            "page": page_number,
            "company": company,
            "company_url": company_url,
            "serial_number": values[0] if values else None,
            "cells": mapped,
            "values": values,
        })

    return {
        "title": title,
        "description": description,
        "columns": columns,
        "rows": rows,
        "page_count": _extract_page_count(soup, page_text),
        "result_count": result_count,
        "source_url": response_url,
    }


class PulseScreenerCollector:
    """Fetch and cache every row from the configured public Screener.in screens."""

    def __init__(self, *, timeout_seconds: float = 18.0, cache_ttl_seconds: int = PULSE_SCREENER_CACHE_TTL_SECONDS) -> None:
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.cache_ttl_seconds = max(1.0, float(cache_ttl_seconds))
        self._cache: tuple[float, str, dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _cache_path() -> Path:
        key = "pulse_screener_screens_v1"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return PULSE_SCREENER_SHARED_CACHE_DIR / f"{digest}.json"

    @staticmethod
    def _has_data(value: Any) -> bool:
        screens = value.get("screens") if isinstance(value, dict) else None
        return isinstance(screens, dict) and any(
            isinstance(screen, dict) and bool(screen.get("rows"))
            for screen in screens.values()
        )

    def _read_shared_cache(self) -> tuple[float, str, dict[str, Any]] | None:
        try:
            payload = json.loads(self._cache_path().read_text(encoding="utf-8"))
            saved_epoch = float(payload.get("saved_at_epoch"))
            age = wall_time() - saved_epoch
            saved_at = str(payload.get("saved_at") or "")
            value = payload.get("value")
            if age < 0 or age >= self.cache_ttl_seconds or not saved_at or not self._has_data(value):
                return None
            return age, saved_at, value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _write_shared_cache(self, value: dict[str, Any], saved_at: str) -> None:
        path = self._cache_path()
        temporary: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            import os
            fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"saved_at_epoch": wall_time(), "saved_at": saved_at, "value": value}, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError):
            logger.debug("Pulse Screener shared cache write failed", exc_info=True)
            if temporary:
                try:
                    import os
                    os.unlink(temporary)
                except OSError:
                    pass

    def _with_cache_metadata(self, value: dict[str, Any], saved_at: str, cache_hit: bool) -> dict[str, Any]:
        result = deepcopy(value)
        expires_epoch = (datetime.fromisoformat(saved_at.replace("Z", "+00:00")).timestamp() if saved_at else wall_time()) + self.cache_ttl_seconds
        result["cache"] = {
            "ttl_seconds": self.cache_ttl_seconds,
            "cached_at": saved_at,
            "cached_until": datetime.fromtimestamp(expires_epoch, tz=timezone.utc).isoformat(),
            "cache_hit": cache_hit,
            "storage": "OS temporary cache only; no application database",
        }
        return result

    async def _get_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        page_number: int,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:
        async with semaphore:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    response = await client.get(url, params={"page": page_number} if page_number > 1 else None)
                    if response.status_code in {429, 500, 502, 503, 504}:
                        raise httpx.HTTPStatusError(
                            f"Screener returned HTTP {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    parsed = _parse_screen_page(response.text, page_number, str(response.url))
                    if not parsed["rows"]:
                        raise ValueError(f"Screener page {page_number} contained no result rows")
                    return parsed
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep page-level failures isolated
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(0.4 * (attempt + 1))
            raise RuntimeError(f"Could not retrieve Screener page {page_number}: {type(last_error).__name__ if last_error else 'unknown error'}") from last_error

    async def _scrape_screen(
        self,
        client: httpx.AsyncClient,
        screen_key: str,
        definition: dict[str, str],
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:
        initial = await self._get_page(client, definition["url"], 1, semaphore)
        advertised_page_count = max(int(initial.get("page_count") or 1), 1)
        page_count = min(advertised_page_count, MAX_PAGES_PER_SCREEN)
        parsed_pages: dict[int, dict[str, Any]] = {1: initial}

        async def fetch_page(page_number: int) -> tuple[int, dict[str, Any] | Exception]:
            try:
                return page_number, await self._get_page(client, initial["source_url"].split("?", 1)[0], page_number, semaphore)
            except Exception as exc:  # one page failing should not discard the rest
                return page_number, exc

        if page_count > 1:
            results = await asyncio.gather(*(fetch_page(page) for page in range(2, page_count + 1)))
            failed_pages: list[int] = []
            for page_number, result in results:
                if isinstance(result, Exception):
                    failed_pages.append(page_number)
                    logger.warning("Pulse Screener page failed screen=%s page=%d error=%s", screen_key, page_number, type(result).__name__)
                else:
                    parsed_pages[page_number] = result
        else:
            failed_pages = []

        # Preserve source order and deduplicate accidental overlap if the screen
        # membership changes while the paginated crawl is in progress.
        columns = initial["columns"]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page_number in sorted(parsed_pages):
            page = parsed_pages[page_number]
            if len(page.get("columns") or []) > len(columns):
                columns = page["columns"]
            for row in page["rows"]:
                identity = row.get("company_url") or row.get("company") or json.dumps(row.get("cells", {}), sort_keys=True)
                identity = str(identity).strip().lower()
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(row)

        canonical_url = initial["source_url"].split("?", 1)[0]
        expected_count = initial.get("result_count")
        status = "ok" if not failed_pages and len(parsed_pages) == page_count else "partial"
        reason = None
        if failed_pages:
            reason = f"Could not retrieve page(s): {', '.join(map(str, failed_pages))}. Showing all successfully retrieved pages."
        elif advertised_page_count > MAX_PAGES_PER_SCREEN:
            status = "partial"
            reason = f"The source advertises more than {MAX_PAGES_PER_SCREEN} pages; crawling was capped to protect the source."

        return {
            "key": screen_key,
            "title": initial.get("title") or definition["title"],
            "description": initial.get("description") or None,
            "status": status,
            "reason": reason,
            "source_url": canonical_url,
            "retrieved_at": _now(),
            "result_count": expected_count,
            "page_count": page_count,
            "pages_scraped": len(parsed_pages),
            "failed_pages": failed_pages,
            "row_count": len(rows),
            "columns": columns,
            "rows": rows,
        }

    async def _fetch_screens(self) -> dict[str, Any]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": "https://www.screener.in/",
        }
        timeout = httpx.Timeout(self.timeout_seconds, connect=8.0)
        semaphore = asyncio.Semaphore(PAGE_CONCURRENCY)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
            names = list(SCREEN_DEFINITIONS)
            results = await asyncio.gather(
                *(
                    self._scrape_screen(client, key, SCREEN_DEFINITIONS[key], semaphore)
                    for key in names
                ),
                return_exceptions=True,
            )

        screens: dict[str, Any] = {}
        for key, result in zip(names, results):
            if isinstance(result, Exception):
                logger.warning("Pulse Screener screen failed key=%s error=%s", key, type(result).__name__)
                screens[key] = {
                    "key": key,
                    "title": SCREEN_DEFINITIONS[key]["title"],
                    "status": "unavailable",
                    "reason": f"Screener.in could not be retrieved ({type(result).__name__}).",
                    "source_url": SCREEN_DEFINITIONS[key]["url"],
                    "retrieved_at": _now(),
                    "result_count": None,
                    "page_count": 0,
                    "pages_scraped": 0,
                    "failed_pages": [],
                    "row_count": 0,
                    "columns": [],
                    "rows": [],
                }
            else:
                screens[key] = result

        usable = any(screen.get("rows") for screen in screens.values())
        return {
            "as_of": _now(),
            "mode": "Screener.in public-screen scrape; all advertised pages are attempted and cached for ten minutes",
            "status": "ok" if usable and all(screen.get("status") == "ok" for screen in screens.values()) else "partial" if usable else "unavailable",
            "screens": screens,
        }

    async def get_screens(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return cached screens or scrape all configured screen pages."""
        now = monotonic()
        if not force_refresh:
            shared = self._read_shared_cache()
            if shared:
                age, saved_at, value = shared
                if self._cache is None or saved_at > self._cache[1]:
                    self._cache = (now - age, saved_at, deepcopy(value))
            if self._cache and now - self._cache[0] < self.cache_ttl_seconds:
                return self._with_cache_metadata(self._cache[2], self._cache[1], True)

        async with self._lock:
            now = monotonic()
            if not force_refresh:
                shared = self._read_shared_cache()
                if shared:
                    age, saved_at, value = shared
                    if self._cache is None or saved_at > self._cache[1]:
                        self._cache = (now - age, saved_at, deepcopy(value))
                if self._cache and now - self._cache[0] < self.cache_ttl_seconds:
                    return self._with_cache_metadata(self._cache[2], self._cache[1], True)

            result = await self._fetch_screens()
            if self._has_data(result):
                saved_at = _now()
                self._cache = (monotonic(), saved_at, deepcopy(result))
                self._write_shared_cache(result, saved_at)
                return self._with_cache_metadata(result, saved_at, False)
            return self._with_cache_metadata(result, _now(), False)

    def clear_cache(self) -> None:
        self._cache = None


pulse_screener_collector = PulseScreenerCollector()
