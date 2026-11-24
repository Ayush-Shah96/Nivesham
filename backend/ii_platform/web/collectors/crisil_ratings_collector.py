"""Credit ratings scraped from CRISIL Ratings' public credit-ratings list page.

Parsing is generic: the page's rating table is located by its header keywords, so a layout
change degrades to an explicit "unavailable" reason instead of invented rows.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .pulse_market_collector import _now

logger = logging.getLogger(__name__)

CRISIL_URL = "https://www.crisilratings.com/en/home/our-business/ratings/credit-ratings-list.html"
CACHE_TTL_SECONDS = 3600
# Full URL of the JSON request the CRISIL page makes for its rating results (copy it from DevTools → Network → Copy URL).
# While this is None the collector reports that the data endpoint is not configured instead of guessing.
CRISIL_RESULTS_URL: str | None = (
    "https://www.crisilratings.com/content/crisilratings/en/home/our-business/ratings/credit-ratings-list"
    "/_jcr_content/wrapper_100_par/columncontrol_copy/container-100-1/ratingresultlisting.results.json"
)
RESULT_PAGE_SIZE = 50
RESULT_MAX_PAGES = 60
RESULT_COLUMNS = ("Company Name", "Industry", "Instrument", "Rating", "Outlook")
COLUMN_ALIASES = {
    "Company Name": ("companyname", "company", "companyName", "company_name", "name", "entity", "issuername"),
    "Industry": ("industry", "industryname", "sector"),
    "Instrument": ("instrument", "instrumentname", "instrument_name", "facility", "instrumenttype"),
    "Rating": ("rating", "ratingname", "rating_text", "ratingvalue"),
    "Outlook": ("outlook", "ratingoutlook"),
}
ROW_LIMIT = 5000
RATING_KEYWORDS = ("rating", "instrument", "company", "entity", "issuer", "outlook")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.crisilratings.com/",
}


def _text(node: Any) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def parse_ratings_html(html: str, base_url: str = CRISIL_URL) -> tuple[list[str], list[dict[str, Any]]]:
    """Return (columns, rows) from the table whose headers look most like a credit-rating list."""
    soup = BeautifulSoup(html, "lxml")
    best_columns: list[str] = []
    best_rows: list[dict[str, Any]] = []
    best_score = 0
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if len(trs) < 2:
            continue
        columns = [_text(cell) for cell in trs[0].find_all(["th", "td"])]
        columns = [column for column in columns if column]
        if len(columns) < 2:
            continue
        score = sum(any(keyword in column.casefold() for keyword in RATING_KEYWORDS) for column in columns)
        if score == 0:
            continue
        rows: list[dict[str, Any]] = []
        for tr in trs[1:]:
            cells = [_text(cell) for cell in tr.find_all(["td", "th"])]
            if not any(cells):
                continue
            link = tr.find("a", href=True)
            rows.append({
                "cells": dict(zip(columns, cells)),
                "values": cells,
                "url": urljoin(base_url, link["href"]) if link else None,
            })
            if len(rows) >= ROW_LIMIT:
                break
        if rows and (score, len(rows)) > (best_score, len(best_rows)):
            best_columns, best_rows, best_score = columns, rows, score
    return best_columns, best_rows


def _squash(value: str) -> str:
    """Lower-case and drop spaces, underscores and hyphens: 'Company Name' -> 'companyname'."""
    return re.sub(r"[\s_\-]", "", value.casefold())


def _canonical_key(key: str) -> str | None:
    lowered = _squash(key)
    for column, aliases in COLUMN_ALIASES.items():
        if lowered in {_squash(alias) for alias in aliases}:
            return column
    return None


def _shape_hint(payload: Any) -> str:
    """Describe the JSON shape (keys only, no values) so a failed parse is diagnosable from the API reason."""
    if isinstance(payload, dict):
        keys = list(payload.keys())[:12]
        nested = {k: type(v).__name__ for k, v in list(payload.items())[:12] if isinstance(v, (dict, list))}
        return f"object keys={keys} nested={nested}"
    if isinstance(payload, list):
        first = payload[0] if payload else None
        return f"list length={len(payload)} first item keys={list(first.keys())[:12] if isinstance(first, dict) else type(first).__name__}"
    return type(payload).__name__


def _column_arrays(node: Any, depth: int = 0) -> dict[str, list[Any]] | None:
    """Find an object whose values are parallel arrays, e.g. {"companyName": [...], "rating": [...]}."""
    if depth > 6:
        return None
    if isinstance(node, dict):
        mapped = {column: node[key] for key in node if isinstance(node[key], list) and (column := _canonical_key(str(key)))}
        if len(mapped) >= 2:
            return mapped
        for value in node.values():
            found = _column_arrays(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _column_arrays(value, depth + 1)
            if found:
                return found
    return None


def records_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Return canonical {column: value} records from column-wise arrays or record lists."""
    arrays = _column_arrays(payload)
    if arrays:
        length = min(len(values) for values in arrays.values())
        return [{column: values[index] for column, values in arrays.items()} for index in range(length)]

    def find_records(node: Any, depth: int = 0) -> list[dict[str, Any]]:
        if depth > 6:
            return []
        if isinstance(node, list) and node and all(isinstance(item, dict) for item in node):
            if any(_canonical_key(str(key)) for key in node[0]):
                return node
        if isinstance(node, dict):
            for value in node.values():
                found = find_records(value, depth + 1)
                if found:
                    return found
        return []

    return [
        {_canonical_key(str(key)): value for key, value in item.items() if _canonical_key(str(key))}
        for item in find_records(payload)
    ]


def rows_from_records(records: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Build table rows. Company and industry appear once per company, so blanks inherit the row above."""
    rows: list[dict[str, Any]] = []
    last_company = last_industry = ""
    for record in records[:ROW_LIMIT]:
        company = str(record.get("Company Name") or "").strip() or last_company
        industry = str(record.get("Industry") or "").strip() or last_industry
        instrument = str(record.get("Instrument") or "").strip()
        rating = str(record.get("Rating") or "").strip()
        outlook = str(record.get("Outlook") or "").strip()
        if not (company or instrument or rating):
            continue
        last_company, last_industry = company, industry
        cells = {"Company Name": company, "Industry": industry, "Instrument": instrument, "Rating": rating, "Outlook": outlook}
        rows.append({"cells": cells, "values": list(cells.values()), "url": None})
    return list(RESULT_COLUMNS), rows


def ratings_from_json(payload: Any) -> tuple[list[str], list[dict[str, Any]]]:
    return rows_from_records(records_from_payload(payload))


class CrisilRatingsCollector:
    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None
        self._cached_at = 0.0

    async def credit_ratings(self, *, force_refresh: bool = False) -> dict[str, Any]:
        if self._cache is not None and not force_refresh and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS:
            return self._cache
        result = await self._scrape()
        if result["status"] == "ok":
            self._cache = result
            self._cached_at = time.monotonic()
        return result

    async def _scrape(self) -> dict[str, Any]:
        base = {"source_url": CRISIL_URL, "retrieved_at": _now(), "cache": {"cached_at": _now(), "ttl_seconds": CACHE_TTL_SECONDS}}
        if CRISIL_RESULTS_URL:
            return await self._scrape_results_json(base)
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=15.0, follow_redirects=True) as client:
                response = await client.get(CRISIL_URL)
        except Exception as exc:
            logger.warning("CRISIL ratings request failed error_type=%s", type(exc).__name__)
            return {**base, "status": "unavailable", "columns": [], "rows": [], "row_count": 0,
                    "reason": f"Request to CRISIL failed: {type(exc).__name__}"}
        if response.status_code != 200:
            return {**base, "status": "unavailable", "columns": [], "rows": [], "row_count": 0,
                    "reason": f"CRISIL returned HTTP {response.status_code}; the page may block automated access."}
        columns, rows = parse_ratings_html(response.text, CRISIL_URL)
        if not rows:
            return {**base, "status": "unavailable", "columns": [], "rows": [], "row_count": 0,
                    "reason": "CRISIL builds this list with JavaScript (filters and Load More), so the server HTML has no ratings table. A data endpoint is needed."}
        return {**base, "status": "ok", "columns": columns, "rows": rows, "row_count": len(rows), "reason": None}

    async def _scrape_results_json(self, base: dict[str, Any]) -> dict[str, Any]:
        """Page through CRISIL's results endpoint (start/limit) and join the pages before building rows."""
        records: list[dict[str, Any]] = []
        headers = {"Accept": "application/json", "Referer": CRISIL_URL, "X-Requested-With": "XMLHttpRequest"}
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=20.0, follow_redirects=True) as client:
                for page in range(RESULT_MAX_PAGES):
                    response = await client.get(
                        CRISIL_RESULTS_URL,
                        params={"cmd": "CR", "start": page * RESULT_PAGE_SIZE, "limit": RESULT_PAGE_SIZE, "filters": "{}"},
                        headers=headers,
                    )
                    if response.status_code != 200:
                        return {**base, "status": "unavailable", "columns": [], "rows": [], "row_count": 0,
                                "reason": f"CRISIL results endpoint returned HTTP {response.status_code} on page {page + 1}."}
                    try:
                        payload = response.json()
                    except ValueError:
                        return {**base, "status": "unavailable", "columns": [], "rows": [], "row_count": 0,
                                "reason": "CRISIL results endpoint did not return JSON."}
                    page_records = records_from_payload(payload)
                    records.extend(page_records)
                    if len(page_records) < RESULT_PAGE_SIZE:
                        break
        except Exception as exc:
            logger.warning("CRISIL results request failed error_type=%s", type(exc).__name__)
            return {**base, "status": "unavailable", "columns": [], "rows": [], "row_count": 0,
                    "reason": f"Request to the CRISIL results endpoint failed: {type(exc).__name__}"}

        columns, rows = rows_from_records(records)
        if not rows:
            return {**base, "status": "unavailable", "columns": [], "rows": [], "row_count": 0,
                    "reason": f"CRISIL results JSON had no recognisable rating fields. Shape: {_shape_hint(payload)}"}
        return {**base, "status": "ok", "columns": columns, "rows": rows, "row_count": len(rows), "reason": None}


crisil_ratings_collector = CrisilRatingsCollector()