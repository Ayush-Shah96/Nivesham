from __future__ import annotations

"""HTTP/session client for NSE public web APIs.

NSE's public JSON endpoints sit behind ordinary web-session protections. This
client establishes and refreshes an httpx cookie session, then talks directly to
the JSON/API endpoints. It deliberately does not launch a browser or parse NSE
HTML pages for data extraction.
"""

import asyncio
import csv
import io
import logging
import math
import random
import time
import zipfile
from datetime import date, datetime, timedelta
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote as url_quote, urljoin

import httpx

from infrastructure.config.settings import get_settings

logger = logging.getLogger(__name__)

NSE_BASE_URL = "https://www.nseindia.com"
NSE_ORIGIN_URL = f"{NSE_BASE_URL}/"
NSE_LATEST_ANNOUNCEMENTS_URL = f"{NSE_BASE_URL}/companies-listing/corporate-filings-application?id=allAnnouncements"
NSE_SESSION_PRIME_URLS = (
    f"{NSE_BASE_URL}/",
    # The equity quote endpoint uses cookies established by the public quote page.
    # Priming this page is materially more reliable than only priming filing pages.
    f"{NSE_BASE_URL}/get-quotes/equity",
    f"{NSE_BASE_URL}/companies-listing/corporate-filings-insider-trading",
    f"{NSE_BASE_URL}/companies-listing/corporate-filings-announcements",
    NSE_LATEST_ANNOUNCEMENTS_URL,
)
NSE_ALLOWED_HOSTS = {
    "www.nseindia.com",
    "nseindia.com",
    "archives.nseindia.com",
    "nsearchives.nseindia.com",
    # Newer corporate filing/Insider detail documents may use the .in archive.
    "www.nseindia.in",
    "nseindia.in",
    "archives.nseindia.in",
    "nsearchives.nseindia.in",
}

DEFAULT_TIMEOUT = httpx.Timeout(connect=20.0, read=90.0, write=30.0, pool=60.0)
DEFAULT_LIMITS = httpx.Limits(max_connections=24, max_keepalive_connections=12)
DEFAULT_RETRIES = 3
DEFAULT_SESSION_TTL_SECONDS = 6 * 60
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 0.35

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-IN,en;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Connection": "keep-alive",
}

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
REAUTH_STATUS = {401, 403}


class NSEApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
        response_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.response_text = response_text


class NSEApiClient:
    """Reusable NSE public-API client with cookie-session renewal and retries."""

    def __init__(
        self,
        *,
        retries: int | None = None,
        session_ttl_seconds: float | None = None,
        min_request_interval_seconds: float | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        limits: httpx.Limits = DEFAULT_LIMITS,
        session_prime_urls: tuple[str, ...] | None = None,
        session_prime_timeout_seconds: float = 12.0,
    ) -> None:
        app_settings = get_settings()
        self.retries = max(1, int(retries if retries is not None else app_settings.nse_api_request_retries))
        self.session_ttl_seconds = max(30.0, float(session_ttl_seconds if session_ttl_seconds is not None else app_settings.nse_api_session_ttl_seconds))
        self.min_request_interval_seconds = max(0.0, float(min_request_interval_seconds if min_request_interval_seconds is not None else app_settings.nse_api_min_request_interval_seconds))
        self.session_prime_urls = tuple(session_prime_urls or NSE_SESSION_PRIME_URLS)
        self.session_prime_timeout_seconds = max(1.0, float(session_prime_timeout_seconds))
        if timeout is DEFAULT_TIMEOUT:
            timeout = httpx.Timeout(
                connect=float(app_settings.nse_api_connect_timeout_seconds),
                read=float(app_settings.nse_api_read_timeout_seconds),
                write=float(app_settings.nse_api_write_timeout_seconds),
                pool=float(app_settings.nse_api_pool_timeout_seconds),
            )
        self._client = httpx.AsyncClient(
            base_url=NSE_BASE_URL,
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            headers=BASE_HEADERS,
        )
        self._session_lock = asyncio.Lock()
        self._reauth_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_request_monotonic = 0.0
        self._session_ready_monotonic = 0.0
        self._blocked_until_monotonic = 0.0
        self._blocked_status: int | None = None
        self._blocked_url: str | None = None
        self._last_reauth_monotonic = 0.0
        self.last_corporate_announcement_diagnostics: dict[str, Any] = {}
        self._closed = False

    async def __aenter__(self) -> "NSEApiClient":
        await self.ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    @staticmethod
    def _absolute_url(url_or_path: str) -> str:
        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            return url_or_path
        return urljoin(NSE_BASE_URL, url_or_path)

    @staticmethod
    def _validate_nse_host(url: str) -> None:
        parsed = httpx.URL(url)
        host = (parsed.host or "").casefold()
        if host not in NSE_ALLOWED_HOSTS:
            raise NSEApiError(f"Refusing non-NSE URL: {url}", url=url)

    async def _paced(self) -> None:
        async with self._request_lock:
            now = time.monotonic()
            wait_for = self.min_request_interval_seconds - (now - self._last_request_monotonic)
            if wait_for > 0:
                await asyncio.sleep(wait_for + random.uniform(0.0, 0.08))
            self._last_request_monotonic = time.monotonic()

    async def _raw_get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        await self._paced()
        request_kwargs = {"params": params, "headers": dict(headers or {})}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        response = await self._client.get(url, **request_kwargs)
        return response

    def _is_temporarily_blocked(self) -> bool:
        return self._blocked_until_monotonic > time.monotonic()

    def _mark_blocked(self, status_code: int, url: str) -> None:
        # A 401/403 from NSE is an anti-bot/session condition, not a transient
        # server error. Retrying hundreds of symbols only multiplies the block and
        # can turn a page load into several minutes. Fail fast for a short cooldown.
        self._blocked_status = status_code
        self._blocked_url = url
        self._blocked_until_monotonic = time.monotonic() + 300.0

    def _clear_block(self) -> None:
        self._blocked_status = None
        self._blocked_url = None
        self._blocked_until_monotonic = 0.0

    async def ensure_session(self, *, force: bool = False) -> None:
        if self._closed:
            raise NSEApiError("NSE API client is closed")
        if not force and self._session_ready_monotonic:
            if time.monotonic() - self._session_ready_monotonic < self.session_ttl_seconds:
                return

        async with self._session_lock:
            if not force and self._session_ready_monotonic:
                if time.monotonic() - self._session_ready_monotonic < self.session_ttl_seconds:
                    return

            logger.info("NSE API session bootstrap starting")
            last_error: Exception | None = None
            for attempt in range(self.retries):
                try:
                    successful_primes = 0
                    prime_errors: list[str] = []
                    previous_prime = None
                    for prime_url in self.session_prime_urls:
                        prime_headers = {
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                            "Sec-Fetch-Dest": "document",
                            "Sec-Fetch-Mode": "navigate",
                            "Sec-Fetch-Site": "none" if previous_prime is None else "same-origin",
                            "Upgrade-Insecure-Requests": "1",
                        }
                        if previous_prime:
                            prime_headers["Referer"] = previous_prime
                        # Session-bootstrap HTML is only used to establish cookies.
                        # One slow/blocked prime page must not stall all news ingestion
                        # for the normal 60-90 second API read timeout.
                        prime_budget = self.session_prime_timeout_seconds
                        prime_timeout = httpx.Timeout(
                            connect=min(5.0, prime_budget),
                            read=prime_budget,
                            write=min(5.0, prime_budget),
                            pool=min(5.0, prime_budget),
                        )
                        try:
                            prime = await self._raw_get(
                                prime_url, headers=prime_headers, timeout=prime_timeout
                            )
                        except httpx.HTTPError as prime_exc:
                            # Prime pages are independent; a slow quote page should not
                            # prevent the corporate-filings pages from establishing cookies.
                            prime_errors.append(
                                f"{prime_url}: {type(prime_exc).__name__}: {str(prime_exc)[:250]}"
                            )
                            logger.warning(
                                "NSE_SESSION_PRIME_FAILED url=%s error=%s: %s",
                                prime_url, type(prime_exc).__name__, str(prime_exc)[:250],
                            )
                            continue
                        previous_prime = prime_url
                        if prime.status_code >= 400:
                            prime_errors.append(f"{prime_url}: HTTP {prime.status_code}")
                            logger.warning(
                                "NSE_SESSION_PRIME_HTTP_ERROR url=%s status=%s",
                                prime_url, prime.status_code,
                            )
                            continue
                        successful_primes += 1

                    if successful_primes == 0:
                        raise NSEApiError(
                            "NSE session prime failed for all configured pages",
                            url=NSE_BASE_URL,
                            response_text="; ".join(prime_errors)[:2000],
                        )

                    self._session_ready_monotonic = time.monotonic()
                    logger.info("NSE API session bootstrap complete cookies=%s", len(self._client.cookies))
                    return
                except (httpx.HTTPError, NSEApiError) as exc:
                    last_error = exc
                    if attempt + 1 < self.retries:
                        delay = min(8.0, 0.6 * (2**attempt)) + random.uniform(0.0, 0.25)
                        await asyncio.sleep(delay)

            raise NSEApiError(
                f"NSE API session bootstrap failed: {type(last_error).__name__}: {last_error}",
                url=NSE_ORIGIN_URL,
            ) from last_error

    async def _request(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        binary: bool = False,
    ) -> httpx.Response:
        absolute = self._absolute_url(url)
        self._validate_nse_host(absolute)
        parsed_host = (httpx.URL(absolute).host or "").casefold()
        is_archive = parsed_host in {
            "archives.nseindia.com", "nsearchives.nseindia.com",
            "archives.nseindia.in", "nsearchives.nseindia.in",
        }
        # The live JSON API and public archive endpoints have different
        # availability characteristics. A live API 401/403 cooldown must not
        # block downloads of public CSV/ZIP files used as independent fallbacks.
        if not is_archive and self._is_temporarily_blocked():
            raise NSEApiError(
                f"NSE API temporarily blocked after HTTP {self._blocked_status or 403}; retry after cooldown",
                status_code=self._blocked_status or 403,
                url=self._blocked_url or absolute,
            )
        request_headers = {
            "Referer": NSE_ORIGIN_URL,
            **dict(headers or {}),
        }

        # Static NSE archive files (such as EQUITY_L.csv) are public assets and
        # do not require the expensive browser-style cookie/session prime. Skipping
        # that prime avoids ~4 extra network calls before the universe refresh.
        requires_session = not is_archive
        if requires_session:
            await self.ensure_session()
        last_error: Exception | None = None
        reauthenticated = False

        for attempt in range(self.retries):
            try:
                response = await self._raw_get(
                    absolute,
                    params=params,
                    headers=request_headers,
                )
                if response.status_code in REAUTH_STATUS:
                    if is_archive:
                        raise NSEApiError(
                            f"NSE archive returned HTTP {response.status_code}",
                            status_code=response.status_code,
                            url=str(response.url),
                            response_text=response.text[:1000],
                        )
                    # Serialize the one allowed session refresh. Once the refreshed
                    # session is also rejected, trip the circuit breaker so parallel
                    # symbol requests fail immediately instead of re-priming NSE
                    # dozens of times.
                    async with self._reauth_lock:
                        if self._is_temporarily_blocked():
                            raise NSEApiError(
                                f"NSE API temporarily blocked after HTTP {self._blocked_status or response.status_code}; retry after cooldown",
                                status_code=self._blocked_status or response.status_code,
                                url=absolute,
                                response_text=response.text[:1000],
                            )
                        now = time.monotonic()
                        if reauthenticated or (now - self._last_reauth_monotonic) < 5.0:
                            self._mark_blocked(response.status_code, absolute)
                            raise NSEApiError(
                                f"NSE API returned HTTP {response.status_code}; cookie refresh did not clear the block",
                                status_code=response.status_code,
                                url=absolute,
                                response_text=response.text[:1000],
                            )
                        logger.warning(
                            "NSE API returned HTTP %s; refreshing cookie session url=%s",
                            response.status_code,
                            absolute,
                        )
                        self._last_reauth_monotonic = now
                        reauthenticated = True
                        self._session_ready_monotonic = 0.0
                        try:
                            await self.ensure_session(force=True)
                        except Exception:
                            self._mark_blocked(response.status_code, absolute)
                            raise NSEApiError(
                                f"NSE API session refresh rejected after HTTP {response.status_code}",
                                status_code=response.status_code,
                                url=absolute,
                                response_text=response.text[:1000],
                            )
                    continue
                if response.status_code in RETRYABLE_STATUS:
                    raise NSEApiError(
                        f"NSE API returned retryable HTTP {response.status_code}",
                        status_code=response.status_code,
                        url=str(response.url),
                        response_text=response.text[:1000],
                    )
                if response.status_code >= 400:
                    raise NSEApiError(
                        f"NSE API returned HTTP {response.status_code}",
                        status_code=response.status_code,
                        url=str(response.url),
                        response_text=response.text[:2000],
                    )
                # A successful public archive download does not prove the live
                # JSON API's cooldown should be cleared.
                if not is_archive:
                    self._clear_block()
                return response
            except (httpx.HTTPError, NSEApiError) as exc:
                last_error = exc
                if isinstance(exc, NSEApiError) and exc.status_code in REAUTH_STATUS:
                    break
                if attempt + 1 >= self.retries:
                    break
                delay = min(10.0, 0.75 * (2**attempt)) + random.uniform(0.0, 0.3)
                await asyncio.sleep(delay)

        raise NSEApiError(
            f"NSE API request failed: {type(last_error).__name__}: {last_error}",
            status_code=last_error.status_code if isinstance(last_error, NSEApiError) else None,
            url=absolute,
        ) from last_error

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        referer: str | None = None,
    ) -> Any:
        request_headers = {
            "Referer": referer or NSE_ORIGIN_URL,
            "Origin": NSE_BASE_URL,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        last_response: httpx.Response | None = None
        for json_attempt in range(2):
            if json_attempt:
                self._session_ready_monotonic = 0.0
                await self.ensure_session(force=True)
            response = await self._request(
                path,
                params=params,
                headers=request_headers,
            )
            last_response = response
            try:
                return response.json()
            except ValueError as exc:
                content_type = response.headers.get("content-type", "")
                prefix = response.text[:500].replace("\n", " ").replace("\r", " ")
                logger.warning(
                    "NSE JSON decode failed attempt=%s status=%s content_type=%s url=%s body_prefix=%r",
                    json_attempt + 1, response.status_code, content_type, response.url, prefix,
                )
                if json_attempt == 0:
                    continue
                raise NSEApiError(
                    f"NSE API returned non-JSON content (HTTP {response.status_code}, content-type={content_type or 'unknown'})",
                    status_code=response.status_code,
                    url=str(response.url),
                    response_text=response.text[:5000],
                ) from exc
        raise NSEApiError("NSE API JSON request failed", url=str(last_response.url) if last_response else None)

    async def get_pit_disclosures(
        self,
        from_date: str,
        to_date: str,
        *,
        symbol: str | None = None,
        index: str = "equities",
    ) -> Any:
        """Fetch NSE Regulation 7(2) / Prohibition of Insider Trading disclosures.

        The insider collector calls this method for bounded date windows. NSE's
        public PIT endpoint is ``/api/corporates-pit`` (not the corporate
        announcements endpoint). Dates are passed in NSE's ``DD-MM-YYYY`` format.
        If ``symbol`` is omitted the method returns the exchange-wide feed so the
        collector can group the filings locally instead of issuing thousands of
        per-company requests.

        A JSON object with no recognized collection field is treated as a source
        schema error. This is important: an NSE error payload must not masquerade
        as a successful empty filing list and trigger an empty snapshot rebuild.
        """
        from_text = str(from_date or "").strip()
        to_text = str(to_date or "").strip()
        if not from_text or not to_text:
            raise ValueError("get_pit_disclosures requires both from_date and to_date")

        params: dict[str, Any] = {
            "index": str(index or "equities").strip().lower(),
            "from_date": from_text,
            "to_date": to_text,
        }
        clean_symbol = str(symbol or "").strip().upper()
        if clean_symbol:
            params["symbol"] = clean_symbol

        referer = f"{NSE_BASE_URL}/companies-listing/corporate-filings-insider-trading"
        payload = await self.get_json(
            "/api/corporates-pit",
            params=params,
            referer=referer,
        )

        # Known NSE responses place rows in a data/items/records/results-like
        # list. A row-bearing nested list is also accepted for schema variations.
        collection_keys = {
            "data", "items", "records", "results", "aadata", "rows",
            "pitdata", "disclosures", "corporatefilings", "corporatefilingsdata",
        }
        row_keys = {
            "symbol", "company", "anex", "acqname", "personcategory",
            "tdptransactiontype", "xbrl", "pid", "did",
        }

        def has_expected_shape(value: Any, parent_key: str = "") -> bool:
            if isinstance(value, list):
                if parent_key.casefold() in collection_keys:
                    # The endpoint may legitimately return zero filings for a
                    # date window, provided the response has the expected shape.
                    return True
                for item in value[:30]:
                    if isinstance(item, dict) and ({str(k).casefold() for k in item} & row_keys):
                        return True
                    if isinstance(item, (dict, list)) and has_expected_shape(item):
                        return True
                return False
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized_key = str(key).replace("_", "").casefold()
                    normalized_collections = {item.replace("_", "") for item in collection_keys}
                    if normalized_key in normalized_collections and isinstance(child, list):
                        return True
                    if isinstance(child, (dict, list)) and has_expected_shape(child, str(key)):
                        return True
                return False
            return False

        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict) or not has_expected_shape(payload):
            preview = repr(payload)[:1200]
            raise NSEApiError(
                "Unexpected NSE PIT disclosure payload schema; refusing to treat it as an empty feed",
                url=f"{NSE_BASE_URL}/api/corporates-pit",
                response_text=preview,
            )
        return payload

    @staticmethod
    def _extract_announcement_rows(payload: Any, *, depth: int = 0) -> list[dict[str, Any]]:
        """Extract raw announcement records from known/variant NSE JSON shapes."""
        import re
        if depth > 7 or payload is None:
            return []
        if isinstance(payload, str):
            raw = payload.strip()
            if raw[:1] in {"[", "{"} and len(raw) <= 25_000_000:
                try:
                    import json
                    return NSEApiClient._extract_announcement_rows(json.loads(raw), depth=depth + 1)
                except (ValueError, TypeError):
                    return []
            return []
        if isinstance(payload, list):
            found: list[dict[str, Any]] = []
            for child in payload:
                if isinstance(child, dict):
                    found.extend(NSEApiClient._extract_announcement_rows(child, depth=depth + 1))
                elif isinstance(child, (list, str)):
                    found.extend(NSEApiClient._extract_announcement_rows(child, depth=depth + 1))
            return found
        if not isinstance(payload, dict):
            return []

        normalize = lambda value: re.sub(r"[^a-z0-9]", "", str(value).casefold())
        keys = {normalize(key) for key in payload}
        row_markers = {
            "symbol", "smname", "seqid", "attchmntfile", "attchmnttext", "sortdate",
            "andt", "dt", "exchdisstime", "broadcastdatetime", "announcementdate", "subject",
        }
        if keys & row_markers and ("symbol" in keys or "seqid" in keys or "attchmntfile" in keys):
            return [payload]
        found: list[dict[str, Any]] = []
        for child in payload.values():
            if isinstance(child, (dict, list, str)):
                found.extend(NSEApiClient._extract_announcement_rows(child, depth=depth + 1))
        return found

    async def get_corporate_announcements(
        self,
        from_date: date,
        to_date: date,
        *,
        segment: str = "equities",
        query_each_day: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch NSE corporate announcements using the same cookie-aware client as other NSE APIs.

        NSE's public endpoint accepts DD-MM-YYYY date parameters. We first request
        the whole window, then optionally query individual dates to avoid a stale or
        truncated multi-day response omitting current announcements. This is a normal
        public endpoint request; explicit 401/403/429 denials are respected and not
        retried through alternate transports by this client.
        """
        from datetime import timedelta
        from urllib.parse import urlparse

        if not isinstance(from_date, date) or not isinstance(to_date, date):
            raise TypeError("from_date and to_date must be datetime.date or datetime.datetime values")
        start = from_date.date() if isinstance(from_date, datetime) else from_date
        end = to_date.date() if isinstance(to_date, datetime) else to_date
        if start > end:
            raise ValueError("from_date must be <= to_date")
        if (end - start).days > 30:
            start = end - timedelta(days=30)

        stats: dict[str, Any] = {
            "endpoint": f"{NSE_BASE_URL}/api/corporate-announcements",
            "segment": str(segment or "equities"),
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "requests": [],
            "daily_queries_parallel": bool(query_each_day),
            "daily_query_concurrency": 4 if query_each_day else 1,
            "raw_records": 0,
            "unique_records": 0,
            "errors": [],
        }
        merged: dict[str, dict[str, Any]] = {}

        def merge_payload(payload: Any, request_label: str) -> int:
            rows = self._extract_announcement_rows(payload)
            stats["raw_records"] += len(rows)
            added = 0
            for row in rows:
                seq_id = str(row.get("seq_id") or row.get("seqId") or row.get("id") or "").strip()
                symbol = str(row.get("symbol") or "").strip().upper()
                dt = str(row.get("dt") or row.get("an_dt") or row.get("sort_date") or "").strip()
                attachment = str(row.get("attchmntFile") or row.get("attachment_url") or "").strip()
                key = seq_id or attachment or "|".join((symbol, dt, str(row.get("desc") or row.get("subject") or "")))
                if not key.strip("|"):
                    # Stable last-resort identity; preserves otherwise valid records.
                    import hashlib, json
                    key = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()
                previous = merged.get(key)
                if previous is None:
                    merged[key] = dict(row)
                    added += 1
                else:
                    # Prefer the richer representation if the range and date-specific
                    # requests both return the same filing.
                    if len(str(row.get("attchmntText") or row.get("desc") or "")) > len(
                        str(previous.get("attchmntText") or previous.get("desc") or "")
                    ):
                        merged[key] = {**previous, **row}
            stats["requests"].append({"query": request_label, "rows": len(rows), "new": added})
            return len(rows)

        async def fetch(params: dict[str, Any], label: str) -> int:
            try:
                payload = await self.get_json(
                    "/api/corporate-announcements",
                    params=params,
                    referer=NSE_LATEST_ANNOUNCEMENTS_URL,
                )
                return merge_payload(payload, label)
            except NSEApiError as exc:
                detail = f"{label}: {type(exc).__name__}: {str(exc)[:240]}"
                stats["errors"].append(detail)
                logger.warning("NSE_CORPORATE_ANNOUNCEMENTS_QUERY_FAILED query=%s status=%s error=%s", label, exc.status_code, str(exc)[:220])
                if exc.status_code in {401, 403, 429}:
                    raise
                return 0
            except Exception as exc:
                detail = f"{label}: {type(exc).__name__}: {str(exc)[:240]}"
                stats["errors"].append(detail)
                logger.warning("NSE_CORPORATE_ANNOUNCEMENTS_QUERY_FAILED query=%s error=%s", label, str(exc)[:220])
                return 0

        range_params = {
            "index": str(segment or "equities"),
            "from_date": start.strftime("%d-%m-%Y"),
            "to_date": end.strftime("%d-%m-%Y"),
        }
        try:
            await fetch(range_params, "date-range")
            if query_each_day and not self._is_temporarily_blocked():
                # Date-scoped requests are independent. Run a small bounded batch in
                # parallel so a seven-day refresh does not wait on seven full network
                # round trips. NSEApiClient._paced() still spaces request starts and
                # the shared session/block guards remain authoritative.
                days: list[date] = []
                day = start
                while day <= end:
                    days.append(day)
                    day += timedelta(days=1)
                daily_semaphore = asyncio.Semaphore(4)

                async def fetch_day(day_value: date) -> int:
                    params = {
                        "index": str(segment or "equities"),
                        "from_date": day_value.strftime("%d-%m-%Y"),
                        "to_date": day_value.strftime("%d-%m-%Y"),
                    }
                    async with daily_semaphore:
                        return await fetch(params, f"day:{day_value.isoformat()}")

                daily_results = await asyncio.gather(
                    *(fetch_day(day_value) for day_value in days),
                    return_exceptions=True,
                )
                hard_errors = [
                    value for value in daily_results
                    if isinstance(value, NSEApiError) and value.status_code in REAUTH_STATUS | {429}
                ]
                if hard_errors:
                    raise hard_errors[0]
            if not merged and not self._is_temporarily_blocked():
                # NSE's default current-list request is a final schema/parameter
                # fallback. It can expose the current list when custom dates are empty.
                await fetch({"index": str(segment or "equities")}, "latest-default")
        except NSEApiError as exc:
            if exc.status_code not in {401, 403, 429} and not merged:
                stats["errors"].append(f"fatal: {str(exc)[:220]}")
            elif exc.status_code in {401, 403, 429}:
                stats["hard_denial"] = {"status": exc.status_code, "url": exc.url}
                if not merged:
                    stats["unique_records"] = 0
                    self.last_corporate_announcement_diagnostics = stats
                    raise

        stats["unique_records"] = len(merged)
        stats["latest_an_dt"] = max(
            (str(row.get("an_dt") or row.get("sort_date") or row.get("dt") or "") for row in merged.values()),
            default="",
        )
        self.last_corporate_announcement_diagnostics = stats
        logger.info(
            "NSE_CORPORATE_ANNOUNCEMENTS_API_RESULT unique=%s raw=%s requests=%s errors=%s latest=%s",
            stats["unique_records"], stats["raw_records"], len(stats["requests"]),
            len(stats["errors"]), stats["latest_an_dt"],
        )
        return list(merged.values())

    async def get_equity_master(self) -> list[dict[str, str | None]]:
        """Fetch the NSE-listed equity master in one request.

        This is the worker's company-universe source. It is intentionally separate
        from Coverage List so Regulatory/Capital ingestion can discover every NSE
        company while Coverage List remains only analyst/priority metadata.
        """
        import csv
        import io

        url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
        response = await self._archive_get(url)
        data = response.content
        text = data.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows: list[dict[str, str | None]] = []
        for raw in reader:
            normalized = {str(k or "").strip().casefold(): str(v or "").strip() for k, v in raw.items()}
            symbol = normalized.get("symbol") or normalized.get("﻿symbol")
            series = normalized.get("series")
            name = normalized.get("name of company") or normalized.get("company name")
            isin = normalized.get("isin number") or normalized.get("isin")
            if not symbol or not name:
                continue
            if series and series.upper() not in {"EQ", "BE", "BZ", "SM", "ST", "SZ"}:
                continue
            rows.append({
                "nse_symbol": symbol.upper(),
                "legal_name": name[:255],
                "isin": isin[:20] if isin else None,
                "series": series.upper() if series else None,
            })
        dedup: dict[str, dict[str, str | None]] = {}
        for row in rows:
            dedup[str(row["nse_symbol"])] = row
        return list(dedup.values())

    async def _archive_get(self, url: str) -> httpx.Response:
        """Fetch a public NSE archive asset without the API-session circuit breaker.

        NSE can temporarily reject the JSON endpoints with 401/403 while its public
        daily archive files remain available. Archive reads therefore use a separate
        transport path and never inherit the JSON API cooldown state.
        """
        absolute = self._absolute_url(url)
        self._validate_nse_host(absolute)
        await self._paced()
        response = await self._client.get(
            absolute,
            headers={
                "Referer": f"{NSE_BASE_URL}/all-reports",
                "Accept": "application/zip,application/octet-stream,text/csv,*/*;q=0.8",
            },
        )
        if response.status_code >= 400:
            raise NSEApiError(
                f"NSE archive returned HTTP {response.status_code}",
                status_code=response.status_code,
                url=str(response.url),
                response_text=response.text[:1000],
            )
        return response

    async def get_latest_equity_prices(
        self,
        symbols: list[str] | tuple[str, ...] | set[str],
        *,
        lookback_days: int = 7,
    ) -> dict[str, dict[str, float | str | None]]:
        """Return the newest usable NSE EOD CMP for each requested equity symbol.

        NSE's CM-UDiFF bhavcopy is exchange-wide. A symbol may be absent from the
        newest file because it did not trade that session, so continue through the
        whole lookback and fill only symbols not found on a newer date. If multiple
        series share a symbol, prefer the regular EQ series (then BE/BZ/SM/ST/SZ).
        Official close price is the CMP for this daily snapshot; Last Price is a
        fallback only when the close field is absent.
        """
        wanted = {str(symbol or '').strip().upper() for symbol in symbols if str(symbol or '').strip()}
        if not wanted:
            return {}

        today = date.today()
        last_error: Exception | None = None
        prices: dict[str, dict[str, float | str | None]] = {}
        series_priority = {"EQ": 6, "BE": 5, "BZ": 4, "SM": 3, "ST": 2, "SZ": 1}
        selected_priority: dict[str, int] = {}
        selected_day: dict[str, date] = {}
        for delta in range(max(1, int(lookback_days))):
            trading_date = today - timedelta(days=delta)
            stamp = trading_date.strftime('%Y%m%d')
            url = (
                'https://nsearchives.nseindia.com/content/cm/'
                f'BhavCopy_NSE_CM_0_0_0_{stamp}_F_0000.csv.zip'
            )
            try:
                response = await self._archive_get(url)
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    csv_members = [name for name in archive.namelist() if name.lower().endswith('.csv')]
                    if not csv_members:
                        raise NSEApiError('NSE CM UDiFF archive contains no CSV', url=url)
                    with archive.open(csv_members[0], 'r') as raw_file:
                        text = io.TextIOWrapper(raw_file, encoding='utf-8-sig', errors='replace', newline='')
                        reader = csv.DictReader(text)
                        day_matches = 0
                        for raw in reader:
                            row = {str(k or '').strip().casefold(): str(v or '').strip() for k, v in raw.items()}
                            symbol = (row.get('tckrsymb') or row.get('symbol') or row.get('symboll') or '').upper().strip()
                            if symbol not in wanted:
                                continue
                            # Keep newer trading-day observations; within the same
                            # archive day, allow a higher-priority equity series to
                            # replace a lower-priority duplicate symbol row.
                            if symbol in prices and selected_day.get(symbol) != trading_date:
                                continue

                            series = (row.get('sctysrs') or row.get('series') or '').upper().strip()
                            # Ignore non-equity instruments when the archive provides
                            # an explicit series. A company may still be listed in a
                            # secondary equity series (BE/BZ/SM/ST/SZ).
                            if series and series not in series_priority:
                                continue
                            priority = series_priority.get(series, 0)

                            def num(*keys):
                                for key in keys:
                                    value = row.get(key.casefold())
                                    if value in (None, '', '0', '0.0'):
                                        continue
                                    try:
                                        parsed = float(value.replace(',', ''))
                                    except (TypeError, ValueError):
                                        continue
                                    if math.isfinite(parsed) and parsed > 0:
                                        return parsed
                                return None

                            # The official daily close is more stable than Last Price,
                            # which may be an intraday/last-trade observation.
                            close_price = num('clspric', 'closeprice', 'close_price')
                            last_price = num('lastpric', 'lastprice', 'last_price')
                            cmp = close_price if close_price is not None else last_price
                            previous_close = num('prvsclsgpric', 'prevclosingprice', 'previous_close', 'prvsclspric')
                            if cmp is None:
                                continue
                            change = ((cmp - previous_close) / previous_close) * 100.0 if previous_close else None
                            if change is None and row.get('pnlpct'):
                                try:
                                    candidate_change = float(row['pnlpct'].replace('%', '').replace(',', ''))
                                    change = candidate_change if math.isfinite(candidate_change) else None
                                except (TypeError, ValueError):
                                    change = None
                            trade_date = row.get('trddt') or row.get('trad_dt') or trading_date.isoformat()
                            candidate = {
                                'last_price': cmp,
                                'change_percent': change,
                                'previous_close': previous_close,
                                'trade_date': trade_date,
                                'source': 'NSE-CM-UDIFF-BHAVCOPY',
                                'source_url': url,
                            }
                            existing_priority = selected_priority.get(symbol, -1)
                            if symbol not in prices or priority > existing_priority:
                                prices[symbol] = candidate
                                selected_priority[symbol] = priority
                                selected_day[symbol] = trading_date
                            day_matches += 1
                        if day_matches == 0:
                            last_error = NSEApiError('NSE CM archive loaded but none of the unresolved requested symbols were present', url=url)
                if len(prices) == len(wanted):
                    break
            except Exception as exc:
                last_error = exc
                continue

        if prices:
            logger.info(
                'NSE_BULK_EQUITY_PRICES matched=%s requested=%s lookback_days=%s oldest_quote=%s',
                len(prices), len(wanted), max(1, int(lookback_days)),
                min((str(value.get('trade_date') or '') for value in prices.values()), default=None),
            )
            return prices
        if last_error:
            raise NSEApiError(
                f'NSE bulk equity price archive unavailable: {type(last_error).__name__}: {last_error}'
            ) from last_error
        return {}

    async def get_equity_quote(self, symbol: str) -> dict[str, float | None]:
        """Fetch the latest NSE equity quote used by Insider Buying.

        The quote is deliberately best-effort. A quote failure must never make an
        otherwise valid Regulation 7(2) filing disappear from the desk.
        """
        clean_symbol = str(symbol or "").strip().upper()
        if not clean_symbol:
            return {"last_price": None, "change_percent": None}

        referer = (
            f"{NSE_BASE_URL}/get-quotes/equity?symbol="
            f"{url_quote(clean_symbol)}"
        )
        payload = await self.get_json(
            "/api/quote-equity",
            params={"symbol": clean_symbol},
            referer=referer,
        )
        price_info = payload.get("priceInfo") if isinstance(payload, dict) else None
        if not isinstance(price_info, dict):
            return {"last_price": None, "change_percent": None}

        def number(value: Any) -> float | None:
            if value in (None, "", "-"):
                return None
            try:
                return float(str(value).replace(",", "").replace("%", "").strip())
            except (TypeError, ValueError):
                return None

        return {
            "last_price": number(price_info.get("lastPrice")),
            "change_percent": number(price_info.get("pChange") or price_info.get("changePct")),
        }

    async def get_bytes(
        self,
        url: str,
        *,
        referer: str | None = None,
        accept: str = "application/pdf,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    ) -> tuple[bytes, str | None]:
        response = await self._request(
            url,
            headers={
                "Referer": referer or NSE_ORIGIN_URL,
                "Accept": accept,
            },
        )
        return response.content, response.headers.get("content-type")