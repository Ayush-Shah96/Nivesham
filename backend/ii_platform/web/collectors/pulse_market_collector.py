from __future__ import annotations

"""NSE/RSS collector for Pulse with a ten-minute shared temporary cache.

The worker warms the market snapshot and movers. The API process reads the same
short-lived OS-temp cache, so a worker refresh can serve the UI without an extra
NSE burst. No application database, Redis, or browser persistent storage is used.
Cache timestamps remain distinct from upstream quote timestamps.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from time import monotonic, time as wall_time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx


logger = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
INDIA_TZ = ZoneInfo("Asia/Kolkata")
INTRADAY_STALE_AFTER_SECONDS = 20 * 60
PULSE_CACHE_TTL_SECONDS = 60  # indices and breadth refresh every minute during the session
PULSE_SHARED_CACHE_DIR = Path(tempfile.gettempdir()) / "c9_equity_pulse_cache_v3"

# Exchange-listed Nifty sectoral index families. Some index names are occasionally
# renamed/retired by NSE; an individual failure is reported rather than faked.
SECTOR_INDICES: tuple[str, ...] = (
    "NIFTY BANK",
    "NIFTY FINANCIAL SERVICES",
    "NIFTY FINANCIAL SERVICES 25/50",
    "NIFTY IT",
    "NIFTY AUTO",
    "NIFTY FMCG",
    "NIFTY PHARMA",
    "NIFTY HEALTHCARE",
    "NIFTY METAL",
    "NIFTY REALTY",
    "NIFTY ENERGY",
    "NIFTY OIL & GAS",
    "NIFTY PSU BANK",
    "NIFTY PRIVATE BANK",
    "NIFTY MEDIA",
    "NIFTY CONSUMER DURABLES",
    "NIFTY CHEMICALS",
    "NIFTY INFRASTRUCTURE",
    "NIFTY PSE",
    "NIFTY COMMODITIES",
    "NIFTY MNC",
    "NIFTY INDIA DEFENCE",
)

ROW_KEYS = {
    "symbol", "index", "indexname", "category", "clienttype", "companyname",
    "marketstate", "seqid", "attchmntfile", "desc", "subject", "buyvalue",
}
CONTAINER_KEYS = (
    "data", "records", "rows", "items", "result", "results", "grapthData",
    "marketStatus", "indices", "filtered", "value", "constituents",
    "constituentData", "indexConstituents", "stockList", "equityData",
)

def _records_from_dataframe_or_payload(payload: Any) -> list[dict[str, Any]]:
    """Convert Jugaad list/DataFrame/dict responses without requiring pandas."""
    if payload is None:
        return []
    to_dict = getattr(payload, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict(orient="records")
            if isinstance(result, list):
                return [row for row in result if isinstance(row, dict)]
        except (TypeError, ValueError):
            pass
    return _items(payload)


def _jugaad_live_constituents(index: str) -> list[dict[str, Any]]:
    """Independent live-index fallback using the already-declared jugaad-data package.

    jugaad-data uses NSE's alternate ``equity-stock-indices`` route and owns its
    own Requests session/cookie bootstrap, so it can work when this app's httpx
    session is challenged. The wrapper is sync; callers run it in a worker thread.
    """
    try:
        payload = _jugaad_call_sync("live_index", index)
        return _records_from_dataframe_or_payload(payload)
    except ImportError:
        return []
    except Exception as exc:
        logger.info("Pulse jugaad-data index fallback unavailable index=%s error_type=%s", index, type(exc).__name__)
        return []


def _jugaad_live_quote(symbol: str) -> dict[str, Any] | None:
    """Fetch a single equity quote via jugaad-data's alternate NSE quote adapter."""
    try:
        payload = _jugaad_call_sync("stock_quote", symbol)
    except ImportError:
        return None
    try:
        if not isinstance(payload, dict):
            return None
        meta = payload.get("metaData") or payload.get("metadata") or payload.get("info") or {}
        price = payload.get("priceInfo") or payload.get("price_info") or {}
        trade = payload.get("tradeInfo") or payload.get("trade_info") or {}
        intraday = price.get("intraDayHighLow") or price.get("intra_day_high_low") or {}
        result = {
            "symbol": symbol.upper(),
            "company": _first(meta, "companyName", "company", "name") or symbol.upper(),
            "last_price": _number(_first(price, "lastPrice", "last", "close")),
            "change": _number(_first(price, "change", "variation")),
            "change_pct": _number(_first(price, "pChange", "percentChange", "changePct")),
            "open": _number(_first(price, "open")),
            "previous_close": _number(_first(price, "previousClose", "prevClose")),
            "day_high": _number(_first(intraday, "max", "high")),
            "day_low": _number(_first(intraday, "min", "low")),
            "volume": _number(_first(trade, "totalTradedVolume", "quantityTraded", "volume")),
            "timestamp": _first(payload, "lastUpdateTime", "timestamp") or _first(meta, "lastUpdateTime", "timestamp"),
            "source": "jugaad-data / NSELive",
        }
        return result if result["last_price"] is not None else None
    except Exception as exc:
        logger.info("Pulse jugaad-data quote fallback unavailable symbol=%s error_type=%s", symbol, type(exc).__name__)
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    if isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").replace("%", "").strip())
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _first(data: dict[str, Any], *keys: str) -> Any:
    """Return the first present value, including support for dotted nested keys."""
    for key in keys:
        current: Any = data
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current not in (None, "", "-"):
            return current
    return None


def _items(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Extract record arrays across the common NSE JSON envelope variations."""
    if depth > 8:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []

    # Envelopes often also contain metadata keys such as `index` or
    # `marketStatus`; inspect known record containers before deciding this object
    # is itself a data row.
    for key in CONTAINER_KEYS:
        if key in payload and isinstance(payload[key], (dict, list)):
            nested = _items(payload[key], depth + 1)
            # Do not treat an empty first container as definitive: some exchange
            # responses include an empty `data` field alongside populated alternate
            # containers such as `filtered`, `constituents`, or `indexConstituents`.
            if nested:
                return nested

    normalized_keys = {re.sub(r"[^a-z0-9]", "", str(key).casefold()) for key in payload}
    if normalized_keys & ROW_KEYS:
        return [payload]

    # Some NSE snapshots expose several peer arrays (for example separate
    # BULK_DEALS_DATA and BLOCK_DEALS_DATA). Aggregate those arrays rather than
    # silently returning only the first one.
    combined: list[dict[str, Any]] = []
    for value in payload.values():
        if isinstance(value, (dict, list)):
            combined.extend(_items(value, depth + 1))
    return combined


def _parse_source_datetime(value: Any) -> datetime | None:
    """Parse common NSE timestamps. Naive NSE timestamps are India-local."""
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        try:
            # NSE occasionally exposes Unix timestamps in milliseconds.
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None

    text = str(value).strip()
    try:
        rss_parsed = parsedate_to_datetime(text)
        if rss_parsed.tzinfo is None:
            rss_parsed = rss_parsed.replace(tzinfo=timezone.utc)
        return rss_parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=INDIA_TZ)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    formats = (
        "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%B-%Y %H:%M:%S",
        "%d-%B-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            parsed = parsed.replace(tzinfo=INDIA_TZ)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _freshness_for(data: Any, max_age_seconds: int = INTRADAY_STALE_AFTER_SECONDS) -> str:
    """fresh/stale/partial/unverified based only on source-provided timestamps."""
    values: list[Any] = []

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 9:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                if normalized in {"timestamp", "sourcetimestamp", "lastupdatetime", "timeval", "lastupdated"}:
                    if value not in (None, "", "-"):
                        values.append(value)
                elif isinstance(value, (dict, list)):
                    visit(value, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)

    visit(data)
    parsed = [_parse_source_datetime(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    if not parsed:
        return "unverified"
    now = datetime.now(timezone.utc)
    stale = [((now - value).total_seconds() > max_age_seconds) or ((now - value).total_seconds() < -120) for value in parsed]
    if all(stale):
        return "stale"
    if any(stale):
        return "partial"
    return "fresh"


def _source_status(name: str, data: Any, *, intraday: bool, source_url: str | None = None) -> dict[str, Any]:
    freshness = _freshness_for(data) if intraday else "not_applicable"
    status = "ok"
    reason = None
    if freshness == "stale":
        status = "stale"
        reason = "The source-provided market timestamp is older than the 20-minute Pulse freshness window."
    elif freshness == "partial":
        status = "partial"
        reason = "Some source-provided timestamps are stale or inconsistent."
    elif freshness == "unverified":
        reason = "The source did not provide a timestamp that could be verified; do not treat this as confirmed live data."

    if isinstance(data, list) and data and all(isinstance(row, dict) and row.get("status") == "unavailable" for row in data):
        status = "unavailable"
        reason = "All configured sub-sources were unavailable."
    elif isinstance(data, list) and any(isinstance(row, dict) and row.get("status") == "unavailable" for row in data):
        status = "partial"
        reason = "One or more configured sub-sources were unavailable."

    return {
        "name": name,
        "status": status,
        "freshness": freshness,
        "retrieved_at": _now(),
        "source_url": source_url,
        "reason": reason,
        "data": data,
    }


# Shared, cookie-bearing Jugaad Requests session reused for every Pulse NSE feed.
_JUGAAD_LOCK = threading.RLock()
_JUGAAD_LIVE: Any = None


def _get_jugaad_live_unlocked() -> Any:
    global _JUGAAD_LIVE
    if _JUGAAD_LIVE is None:
        from jugaad_data.nse import NSELive  # type: ignore[import-not-found]
        _JUGAAD_LIVE = NSELive()
    return _JUGAAD_LIVE


def _jugaad_call_sync(method_name: str, *args: Any, **kwargs: Any) -> Any:
    # The lock guards only session creation. The network call runs outside it, so one slow
    # NSE request no longer queues every other Pulse request behind it.
    with _JUGAAD_LOCK:
        live = _get_jugaad_live_unlocked()
    method = getattr(live, method_name, None)
    if not callable(method):
        raise RuntimeError(f"jugaad-data does not expose NSELive.{method_name}")
    return method(*args, **kwargs)


def _jugaad_json_sync(route: str, params: dict[str, Any] | None = None) -> Any:
    """Use the same primed Jugaad session for NSE routes without wrappers."""
    with _JUGAAD_LOCK:
        live = _get_jugaad_live_unlocked()
    session = getattr(live, "s", None)
    if session is None:
        raise RuntimeError("jugaad-data NSELive session is unavailable")
    base_url = str(getattr(live, "base_url", "https://www.nseindia.com/api")).rstrip("/")
    response = session.get(f"{base_url}/{route.lstrip('/')}", params=params or {}, timeout=(4, 9))
    response.raise_for_status()
    return response.json()


def _recent_three_day(value: Any, *, now: datetime | None = None) -> bool:
    """True for the last three India-local calendar dates, including today."""
    parsed = _parse_source_datetime(value)
    if parsed is None:
        return False
    local_today = (now or datetime.now(INDIA_TZ)).astimezone(INDIA_TZ).date()
    event_day = parsed.astimezone(INDIA_TZ).date()
    return local_today - timedelta(days=2) <= event_day <= local_today


def _find_nested_number(payload: Any, keys: tuple[str, ...]) -> tuple[float | None, Any]:
    wanted = {key.casefold() for key in keys}
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).casefold() in wanted:
                    number = _number(value)
                    if number is not None:
                        return number, node
            stack.extend(value for value in node.values() if isinstance(value, (dict, list)))
        elif isinstance(node, list):
            stack.extend(value for value in node if isinstance(value, (dict, list)))
    return None, None


def _is_market_cap_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z]", "", key.casefold())
    return "marketcap" in normalized or "capitalisation" in normalized or "capitalization" in normalized


def _find_by_key_predicate(payload: Any, predicate: Any) -> tuple[float | None, Any]:
    """Find the first numeric value whose key satisfies predicate, searching nested payloads."""
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if predicate(str(key)):
                    number = _number(value)
                    if number is not None:
                        return number, node
            stack.extend(value for value in node.values() if isinstance(value, (dict, list)))
        elif isinstance(node, list):
            stack.extend(value for value in node if isinstance(value, (dict, list)))
    return None, None


def _key_names(payload: Any, limit: int = 60) -> list[str]:
    """Key names only (never values) for diagnosing unexpected upstream shapes."""
    names: list[str] = []
    queue = [payload]
    while queue and len(names) < limit:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key) not in names:
                    names.append(str(key))
                if isinstance(value, (dict, list)):
                    queue.append(value)
        elif isinstance(node, list):
            queue.extend(value for value in node[:3] if isinstance(value, (dict, list)))
    return names


def _normalise_market_cap(value: float | None, key: str | None) -> float | None:
    """Return total market cap in ₹ crore, or None when the figure cannot be a plausible India total.

    Upstream keys carry inconsistent units. A small stray number (for example a count) must never be shown
    as market capitalisation, so only values inside the plausible range for the whole Indian market are kept.
    """
    if value is None or value <= 0:
        return None
    name = (key or "").casefold()
    if "lac" in name:
        value = value / 100.0          # 1 crore = 100 lakh
    elif "cr" not in name and "crore" not in name and 1e12 <= value <= 1e16:
        value = value / 1e7            # bare rupees -> crore
    # Accept ₹10 lakh crore to ₹1000 lakh crore (expressed in crore). India's listed market sits near ₹375 lakh crore.
    return value if 1e6 <= value <= 1e9 else None


def _index_name_variants(name: str) -> list[str]:
    """Spellings NSE's constituent routes are known to accept for the same index.

    Overview tiles carry suffixes such as "25/50" that the constituent routes do not use, and some
    names differ only by "&" vs "AND" or singular vs plural.
    """
    base = re.sub(r"\s+\d+/\d+$", "", name).strip()
    variants = [name, base, base.replace("&", "AND"), base.replace("CONSUMER DURABLES", "CONSUMER DURABLE")]
    return [variant for variant in dict.fromkeys(variants) if variant]


# Index membership from NSE's published constituent CSVs, cached per index name.
_MEMBERSHIP_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
MEMBERSHIP_TTL_SECONDS = 12 * 3600
MEMBERSHIP_SOURCE_BASES = ("https://www.niftyindices.com/IndexConstituent", "https://nsearchives.nseindia.com/content/indices", "https://archives.nseindia.com/content/indices")
MEMBERSHIP_SOURCE_BASE = MEMBERSHIP_SOURCE_BASES[0]
# NSE constituent filenames are not generated consistently from display names.
# Keep explicit official filename stems for indices whose names contain omitted
# separators/underscores in the published CSV filename.
MEMBERSHIP_FILE_STEMS = {
    "NIFTY INDIA DEFENCE": ("ind_niftyindiadefence_list.csv",),
    "NIFTY INDIA DEFENSE": ("ind_niftyindiadefence_list.csv",),
    "NIFTY PRIVATE BANK": ("ind_nifty_privatebanklist.csv",),
    # NSE Indices filenames are not consistently derivable from display names.
    "NIFTY MIDSMALL FINANCIAL SERVICES": ("ind_niftymidsmallfinancailservice_list.csv",),
    "NIFTY MIDSMALL FINANCIAL SERVICE": ("ind_niftymidsmallfinancailservice_list.csv",),
    "NIFTY INFRASTRUCTURE": ("ind_niftyinfralist.csv",),
    "NIFTY FINANCIAL SERVICES EX-BANK": ("ind_niftyfinancialservicesexbanklist.csv",),
    "NIFTY TOP 15 EQUAL WEIGHT": ("ind_niftytop15EqualWeight_list.csv",),
    "NIFTY500 LARGEMIDSMALL EQUAL-CAP WEIGHTED": ("ind_nifty500LargeMidSmallEqualCapWeighted_list.csv",),
}


class PulseMarketCollector:
    """Collect Pulse market data with a short-lived, process-memory cache."""

    def __init__(self, *, timeout_seconds: float = 10.0, cache_ttl_seconds: int = PULSE_CACHE_TTL_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = max(0.01, float(cache_ttl_seconds))
        self._cache: dict[str, tuple[float, str, Any]] = {}
        self._cache_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _cacheable_result(value: Any) -> bool:
        """Do not pin total outages in cache for ten minutes."""
        if not isinstance(value, dict):
            return value is not None
        sources = value.get("sources")
        if isinstance(sources, dict) and sources:
            return any(
                isinstance(source, dict)
                and source.get("status") not in {"unavailable", "error"}
                and source.get("data") is not None
                for source in sources.values()
            )
        if value.get("status") in {"unavailable", "error"}:
            return False
        if "stocks" in value:
            return bool(value.get("stocks"))
        return True

    @staticmethod
    def _shared_cache_path(key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return PULSE_SHARED_CACHE_DIR / f"{digest}.json"

    def _read_shared_cache(self, key: str) -> tuple[float, str, Any] | None:
        """Read worker-produced JSON from the OS temp directory, if still fresh."""
        path = self._shared_cache_path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            saved_epoch = float(payload.get("saved_at_epoch"))
            age = wall_time() - saved_epoch
            value = payload.get("value")
            saved_at = str(payload.get("saved_at") or "")
            if age < 0 or age >= self.cache_ttl_seconds or not saved_at or not self._cacheable_result(value):
                return None
            return age, saved_at, value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _write_shared_cache(self, key: str, value: Any, cached_at: str) -> None:
        """Atomically publish a short-lived market snapshot for the API process."""
        path = self._shared_cache_path(key)
        temporary: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"saved_at_epoch": wall_time(), "saved_at": cached_at, "value": value}, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError):
            logger.debug("Pulse shared temporary cache write failed key=%s", key, exc_info=True)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _with_cache_metadata(self, value: Any, cached_at: str, cache_hit: bool) -> Any:
        result = deepcopy(value)
        if isinstance(result, dict):
            created = _parse_source_datetime(cached_at)
            expires = (created.timestamp() if created else wall_time()) + self.cache_ttl_seconds
            result["cache"] = {
                "ttl_seconds": self.cache_ttl_seconds,
                "cached_at": cached_at,
                "cached_until": datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
                "cache_hit": cache_hit,
                "storage": "OS temporary cache only; no application database",
            }
        return result

    async def _cached_result(self, key: str, loader: Any, *, force_refresh: bool = False) -> Any:
        now = monotonic()
        entry = self._cache.get(key)
        if not force_refresh:
            shared = self._read_shared_cache(key)
            # A worker snapshot newer than this process's RAM cache wins. This is
            # the key cross-process handoff for worker.py -> FastAPI.
            if shared and (entry is None or shared[1] > entry[1]):
                age, saved_at, value = shared
                entry = (now - age, saved_at, deepcopy(value))
                self._cache[key] = entry
            if entry and now - entry[0] < self.cache_ttl_seconds:
                return self._with_cache_metadata(entry[2], entry[1], True)

        lock = self._cache_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._cache_locks[key] = lock
        async with lock:
            now = monotonic()
            entry = self._cache.get(key)
            if not force_refresh:
                shared = self._read_shared_cache(key)
                if shared and (entry is None or shared[1] > entry[1]):
                    age, saved_at, value = shared
                    entry = (now - age, saved_at, deepcopy(value))
                    self._cache[key] = entry
                if entry and now - entry[0] < self.cache_ttl_seconds:
                    return self._with_cache_metadata(entry[2], entry[1], True)
            result = await loader()
            if self._cacheable_result(result):
                cached_at = _now()
                entry = (monotonic(), cached_at, deepcopy(result))
                self._cache[key] = entry
                self._write_shared_cache(key, result, cached_at)
                # Bound the number of per-index keys retained by an app process.
                for old_key, old_entry in list(self._cache.items()):
                    if monotonic() - old_entry[0] >= self.cache_ttl_seconds:
                        self._cache.pop(old_key, None)
                return self._with_cache_metadata(result, cached_at, False)
            return self._with_cache_metadata(result, _now(), False)

    def clear_cache(self) -> None:
        """Clear in-memory Pulse data (primarily useful for tests/operations)."""
        self._cache.clear()

    async def aclose(self) -> None:
        """Close the shared Jugaad NSELive Requests session during shutdown."""
        global _JUGAAD_LIVE
        with _JUGAAD_LOCK:
            live = _JUGAAD_LIVE
            _JUGAAD_LIVE = None
            session = getattr(live, "s", None) if live is not None else None
            close = getattr(session, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _unavailable_source(name: str, exc: Exception, source_url: str | None = None) -> dict[str, Any]:
        return {
            "name": name,
            "status": "unavailable",
            "freshness": "unavailable",
            "retrieved_at": _now(),
            "source_url": source_url,
            "reason": f"Source request or parsing failed ({type(exc).__name__}).",
            "data": None,
        }

    async def _source(
        self,
        name: str,
        awaitable: Any,
        *,
        intraday: bool = False,
        source_url: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        try:
            # Bound each source independently so one slow feed cannot hold up all
            # the other Pulse sections indefinitely.
            data = await asyncio.wait_for(awaitable, timeout=self.timeout_seconds + 3.0)
            effective_source_url = source_url
            if isinstance(data, dict) and data.get("source_url"):
                effective_source_url = str(data["source_url"])
            return name, _source_status(name, data, intraday=intraday, source_url=effective_source_url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not log scraped content, response bodies, transcripts, or credentials.
            logger.warning("Pulse source failed name=%s error_type=%s", name, type(exc).__name__)
            return name, self._unavailable_source(name, exc, source_url)

    def _normalize_constituent_rows(
        self, index: str, rows: list[dict[str, Any]], *, timestamp: Any = None, source: str = "NSE"
    ) -> list[dict[str, Any]]:
        stocks: list[dict[str, Any]] = []
        index_symbols = {index.casefold(), index.replace("NIFTY ", "").casefold()}
        for row in rows:
            symbol = _first(row, "symbol", "Symbol", "SYMBOL", "symbolName", "ticker", "Ticker", "tradingSymbol")
            if not symbol or str(symbol).strip().casefold() in index_symbols:
                continue
            pct_change = _number(_first(row, "pChange", "percentChange", "changePercent", "% Chng", "%Chng", "% Change", "Change %"))
            if pct_change is None:
                change_value = _number(_first(row, "change", "Chng", "Change"))
                previous = _number(_first(row, "previousClose", "previous_close", "Prev. Close", "Prev Close"))
                if change_value is not None and previous:
                    pct_change = change_value / previous * 100.0
            industry = _first(row, "industry", "Industry", "meta.industry", "sector", "Sector")
            stocks.append({
                "symbol": str(symbol).strip().upper(),
                "company": _first(row, "companyName", "Company Name", "meta.companyName", "company", "Company", "name", "Name") or str(symbol).strip().upper(),
                "industry": str(industry).strip() if industry else None,
                "last_price": _number(_first(row, "lastPrice", "last", "close", "currentValue", "LTP", "Last Price", "ltp", "Close")),
                "change": _number(_first(row, "change", "variation", "Chng", "Change")),
                "change_pct": pct_change,
                "ffmc": _number(_first(row, "ffmc", "FFMC", "freeFloatMarketCap", "freeFloatMarketCapitalization", "meta.ffmc", "meta.freeFloatMarketCap")),
                "weightage_pct": _number(_first(row, "weightage", "weightagePct", "weight_pct", "indexWeight", "weightPercent", "meta.weightage")),
                "open": _number(_first(row, "open", "Open")),
                "high": _number(_first(row, "dayHigh", "high", "High")),
                "low": _number(_first(row, "dayLow", "low", "Low")),
                "previous_close": _number(_first(row, "previousClose", "previous_close", "Prev. Close", "Prev Close")),
                "volume": _number(_first(row, "totalTradedVolume", "quantityTraded", "volume", "Volume", "Shares Traded")),
                "value_crores": _number(_first(row, "totalTradedValue", "totalTradedValueCr", "Value (₹ Crores)", "Value (Rs. Crores)", "Traded Value")),
                "market_cap": _number(_first(row, "marketCap", "marketCapitalization", "Market Cap")),
                "year_high": _number(_first(row, "yearHigh", "year_high", "52 Week High")),
                "year_low": _number(_first(row, "yearLow", "year_low", "52 Week Low")),
                "change_30d_pct": _number(_first(row, "perChange30d", "change30dPct", "30D Change %")),
                "change_365d_pct": _number(_first(row, "perChange365d", "change365dPct", "1Y Change %")),
                "timestamp": _first(row, "lastUpdateTime", "timestamp", "timeVal", "lastUpdated", "Date") or timestamp,
                "source": source,
            })
        explicit_complete = bool(stocks) and all(row.get("weightage_pct") is not None for row in stocks)
        explicit_total = sum(float(row["weightage_pct"]) for row in stocks if row.get("weightage_pct") is not None)
        if explicit_complete and explicit_total > 0:
            for row in stocks:
                row["weightage_pct"] = float(row["weightage_pct"]) / explicit_total * 100.0
                row["weightage_source"] = "Source-supplied constituent weight normalized to 100%"
        else:
            ffmc_total = sum(float(row["ffmc"]) for row in stocks if row.get("ffmc") is not None and float(row["ffmc"]) > 0)
            if ffmc_total > 0:
                for row in stocks:
                    ffmc = row.get("ffmc")
                    row["weightage_pct"] = float(ffmc) / ffmc_total * 100.0 if ffmc is not None and float(ffmc) > 0 else None
                    row["weightage_source"] = "Calculated from free-float market capitalization (FFMC)"
        return stocks

    async def _index_heatmap(self, index: str) -> dict[str, Any]:
        """Fetch constituents: NSELive live_index first, then NSE's equity-stock-indices route on the same session."""
        attempts: list[str] = []
        payload: Any = None
        rows: list[dict[str, Any]] = []
        for label, call in (
            ("live_index", lambda: _jugaad_call_sync("live_index", index)),
            ("equity-stock-indices", lambda: _jugaad_json_sync("equity-stock-indices", {"index": index})),
        ):
            try:
                payload = await asyncio.wait_for(asyncio.to_thread(call), timeout=14.0)
                rows = _records_from_dataframe_or_payload(payload)
                if rows:
                    break
                attempts.append(f"{label}: empty payload (keys={_key_names(payload)[:10]})")
            except Exception as exc:
                attempts.append(f"{label}: {type(exc).__name__}: {str(exc)[:160]}")
        if not rows:
            raise ValueError(f"no constituent rows for {index} ({'; '.join(attempts)})")
        root_timestamp = _first(payload, "timestamp", "lastUpdateTime", "timeVal", "asOn") if isinstance(payload, dict) else None
        stocks = self._normalize_constituent_rows(index, rows, timestamp=root_timestamp, source="jugaad-data / NSELive")
        if not stocks:
            raise ValueError(f"constituent rows for {index} had no recognisable symbols (rows={len(rows)}, keys={_key_names(rows[0])[:10]})")
        freshness = _freshness_for(payload)
        return {
            "index": index,
            "timestamp": root_timestamp or next((row.get("timestamp") for row in stocks if row.get("timestamp")), None),
            "freshness": freshness,
            "status": "stale" if freshness == "stale" else "ok",
            "retrieved_at": _now(),
            "source_url": f"{NSE_BASE}/market-data/live-market-indices",
            "weight_method": "Source-supplied weights, or FFMC share of total returned free-float market capitalization",
            "ffmc_total": sum(float(row.get("ffmc") or 0) for row in stocks) or None,
            "stocks": stocks,
            "source": "jugaad-data / NSELive",
        }

    async def _breadth(self) -> dict[str, Any]:
        """Calculate broad-market breadth from Jugaad's NIFTY 500 constituents."""
        payload = await asyncio.wait_for(asyncio.to_thread(_jugaad_call_sync, "live_index", "NIFTY 500"), timeout=13.0)
        rows = _records_from_dataframe_or_payload(payload)
        changes = [_number(_first(row, "pChange", "percentChange", "changePercent", "% Chng")) for row in rows]
        changes = [value for value in changes if value is not None]
        if not changes:
            raise ValueError("Jugaad NIFTY 500 response contained no usable constituent changes")
        timestamp = _first(payload, "timestamp", "lastUpdateTime", "timeVal", "asOn") if isinstance(payload, dict) else None
        return {
            "advances": sum(value > 0 for value in changes),
            "declines": sum(value < 0 for value in changes),
            "unchanged": sum(value == 0 for value in changes),
            "method": f"Proxy calculated from {len(changes)} Jugaad NIFTY 500 constituents; not exchange-wide breadth",
            "timestamp": timestamp,
            "source": "jugaad-data / NSELive NIFTY 500",
        }

    async def _market_metrics(self) -> dict[str, Any]:
        """Get market status, source-reported total market capitalization and USD/INR through Jugaad."""
        async def call(method_name: str, *args: Any) -> Any:
            try:
                return await asyncio.wait_for(asyncio.to_thread(_jugaad_call_sync, method_name, *args), timeout=10.5)
            except Exception as exc:
                logger.info("Pulse Jugaad metric unavailable method=%s error_type=%s", method_name, type(exc).__name__)
                return None

        status_payload, turnover_payload, fx_payload = await asyncio.gather(
            call("market_status"), call("market_turnover"), call("currency_option_chain", "USDINR")
        )
        state_payload = _first(status_payload, "marketState", "marketStates", "market_status") if isinstance(status_payload, dict) else None
        status_rows = _items(state_payload) or _items(status_payload)
        market_row = next((row for row in status_rows if "CAPITAL" in str(_first(row, "market", "marketType", "segment") or "").upper()), status_rows[0] if status_rows else {})
        market_status = _first(market_row, "marketStatus", "status", "marketState")
        trade_date = _first(market_row, "tradeDate", "date", "asOn", "marketDate") or (_first(status_payload, "tradeDate", "date", "asOn") if isinstance(status_payload, dict) else None)
        market_cap_keys = (
            "totalMarketCap", "totalMarketCapitalization", "marketCapitalization", "marketCapitalisation",
            "capitalization", "capitalisation", "marketCapInCrores", "marketCapCr", "marketCapinLacs",
            "totalMarketCapCr", "marketCapitalizationCr", "marketCap",
        )
        market_cap, cap_node = _find_nested_number(status_payload, market_cap_keys)
        if market_cap is None:
            market_cap, cap_node = _find_nested_number(turnover_payload, market_cap_keys)
        if market_cap is None:
            market_cap, cap_node = _find_by_key_predicate(turnover_payload, _is_market_cap_key)
        if market_cap is None:
            market_cap, cap_node = _find_by_key_predicate(status_payload, _is_market_cap_key)
        if market_cap is None:
            logger.info(
                "Pulse total market cap not located; status keys=%s turnover keys=%s",
                _key_names(status_payload), _key_names(turnover_payload),
            )
        cap_key = None
        if cap_node is not None and market_cap is not None:
            cap_key = next((str(key) for key, value in cap_node.items() if _number(value) == market_cap), None)
        cap_crore = _normalise_market_cap(market_cap, cap_key)
        if market_cap is not None and cap_crore is None:
            logger.info("Pulse market cap rejected as implausible key=%s", cap_key)
        currency, _ = _find_nested_number(fx_payload, ("underlyingValue", "underlying_value", "underlyingPrice"))
        advances, _ = _find_nested_number(status_payload, ("advances", "advanceCount", "advance"))
        declines, _ = _find_nested_number(status_payload, ("declines", "declineCount", "decline"))
        unchanged, _ = _find_nested_number(status_payload, ("unchanged", "unchangedCount"))
        timestamp = _first(status_payload, "timestamp", "lastUpdateTime", "timeVal", "asOn") if isinstance(status_payload, dict) else None
        if status_payload is None and turnover_payload is None and fx_payload is None:
            raise ValueError("Jugaad NSELive market-status, turnover and currency feeds were unavailable")
        return {
            "market_status": market_status,
            "trade_date": trade_date,
            "market_cap": cap_crore,
            "market_cap_unit": "₹ Cr" if cap_crore is not None else None,
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "usd_inr": currency,
            "usd_inr_basis": "Jugaad NSELive USDINR currency-option-chain underlying value; indicative, not RBI reference rate" if currency is not None else None,
            "timestamp": timestamp,
            "source": "jugaad-data / NSELive",
            "source_url": f"{NSE_BASE}/market-data/live-market-indices",
        }

    async def _institutional_flows(self) -> list[dict[str, Any]]:
        """Retrieve NSE FII/DII flows through the Jugaad-owned Requests session."""
        payload = await asyncio.wait_for(asyncio.to_thread(_jugaad_json_sync, "fiidiiTradeReact"), timeout=12.0)
        rows = _items(payload)
        result: list[dict[str, Any]] = []
        for row in rows:
            event_date = _first(row, "date", "tradeDate", "tradingDate", "asOfDate", "timestamp")
            combined = (
                ("FII/FPI", ("fiibuy", "fiiBuy", "fii_buy", "foreignBuy", "foreignBuyValue"), ("fiisell", "fiiSell", "fii_sell", "foreignSell", "foreignSellValue"), ("fiinet", "fiiNet", "fii_net", "foreignNet", "foreignNetValue")),
                ("DII", ("diibuy", "diiBuy", "dii_buy", "domesticBuy", "domesticBuyValue"), ("diisell", "diiSell", "dii_sell", "domesticSell", "domesticSellValue"), ("diinet", "diiNet", "dii_net", "domesticNet", "domesticNetValue")),
            )
            for participant, buy_keys, sell_keys, net_keys in combined:
                buy = _number(_first(row, *buy_keys)); sell = _number(_first(row, *sell_keys)); net = _number(_first(row, *net_keys))
                if any(value is not None for value in (buy, sell, net)):
                    result.append({"participant": participant, "date": event_date, "buy_value": buy, "sell_value": sell, "net_value": net if net is not None else (buy - sell if buy is not None and sell is not None else None), "unit": "₹ crore", "source": "jugaad-data / NSE session", "source_url": f"{NSE_BASE}/reports/fii-dii"})
            category = str(_first(row, "category", "clientType", "participant", "name", "investorType") or "").strip().upper()
            if category:
                participant = "FII/FPI" if "FII" in category or "FOREIGN" in category else "DII" if "DII" in category or "DOMESTIC" in category else category
                if not any(item.get("participant") == participant and item.get("date") == event_date for item in result):
                    buy = _number(_first(row, "buyValue", "buy_value", "buy", "purchaseValue"))
                    sell = _number(_first(row, "sellValue", "sell_value", "sell", "saleValue"))
                    net = _number(_first(row, "netValue", "net_value", "net"))
                    if any(value is not None for value in (buy, sell, net)):
                        result.append({"participant": participant, "date": event_date, "buy_value": buy, "sell_value": sell, "net_value": net if net is not None else (buy - sell if buy is not None and sell is not None else None), "unit": _first(row, "unit", "units") or "₹ crore", "source": "jugaad-data / NSE session", "source_url": f"{NSE_BASE}/reports/fii-dii"})
        if not result and isinstance(payload, dict):
            event_date = _first(payload, "date", "tradeDate", "timestamp")
            for participant, keys in (("FII/FPI", ("fiibuy", "fiisell", "fiinet")), ("DII", ("diibuy", "diisell", "diinet"))):
                buy, sell, net = (_number(payload.get(keys[0])), _number(payload.get(keys[1])), _number(payload.get(keys[2])))
                if any(value is not None for value in (buy, sell, net)):
                    result.append({"participant": participant, "date": event_date, "buy_value": buy, "sell_value": sell, "net_value": net, "unit": "₹ crore", "source": "jugaad-data / NSE session", "source_url": f"{NSE_BASE}/reports/fii-dii"})
        if not result:
            raise ValueError("Jugaad FII/DII response contained no recognizable participant rows")
        return result

    @staticmethod
    def _market_movers(stock_groups: list[list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
        unique: dict[str, dict[str, Any]] = {}
        for group in stock_groups:
            for stock in group:
                symbol = str(stock.get("symbol") or "").strip().upper()
                if symbol:
                    # Prefer the row with more populated quote/volume fields.
                    previous = unique.get(symbol)
                    score = lambda row: sum(row.get(key) is not None for key in ("change_pct", "last_price", "volume", "market_cap"))
                    if previous is None or score(stock) > score(previous):
                        unique[symbol] = stock
        valid = [stock for stock in unique.values() if stock.get("change_pct") is not None]
        gainers = sorted((stock for stock in valid if stock["change_pct"] > 0), key=lambda row: row["change_pct"], reverse=True)
        losers = sorted((stock for stock in valid if stock["change_pct"] < 0), key=lambda row: row["change_pct"])
        volume_leaders = sorted((stock for stock in valid if stock.get("volume") is not None), key=lambda row: row["volume"], reverse=True)
        # There is no historical average-volume baseline in the supplied NSE
        # constituent feed. These are explicitly intraday move outliers, not a claim
        # that an algorithmic historical-volume anomaly detector ran.
        activity_outliers = sorted(
            (stock for stock in valid if abs(float(stock["change_pct"])) >= 3.0),
            key=lambda row: abs(float(row["change_pct"])), reverse=True,
        )
        return {
            "gainers": gainers[:20],
            "losers": losers[:20],
            "volume_leaders": volume_leaders[:20],
            "activity_outliers": activity_outliers[:30],
            "outlier_definition": "Absolute source-reported intraday percentage move >= 3%; not a historical-volume anomaly score.",
        }

    @staticmethod
    def _sector_movers(sectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = []
        for sector in sectors:
            stocks = [stock for stock in sector.get("stocks", []) if stock.get("change_pct") is not None]
            output.append({
                "sector": sector.get("index"),
                "timestamp": sector.get("timestamp"),
                "freshness": sector.get("freshness", "unverified"),
                "status": sector.get("status", "ok"),
                "reason": sector.get("reason"),
                "retrieved_at": sector.get("retrieved_at"),
                "gainers": sorted((stock for stock in stocks if stock["change_pct"] > 0), key=lambda row: row["change_pct"], reverse=True)[:10],
                "losers": sorted((stock for stock in stocks if stock["change_pct"] < 0), key=lambda row: row["change_pct"])[:10],
                "stocks": stocks,
            })
        return output

    @staticmethod
    def _index_kind(name: str) -> str:
        """Classify known NSE sectoral indices without matching unrelated words."""
        normalized = re.sub(r"\s+", " ", str(name or "").strip().upper())
        known_sector_names = {re.sub(r"\s+", " ", value.upper().strip()) for value in SECTOR_INDICES}
        if normalized in known_sector_names:
            return "sector"
        # Add common sectoral indices not always included in SECTOR_INDICES, but
        # deliberately avoid broad substring markers such as IT or SELECT.
        if re.fullmatch(
            r"NIFTY (?:BANK|IT|AUTO|FMCG|PHARMA|HEALTHCARE|METAL|REALTY|ENERGY|INFRA|OIL & GAS|"
            r"PSU BANK|PRIVATE BANK|MEDIA|CONSUMER DURABLES|CHEMICALS?|INFRASTRUCTURE|PSE|"
            r"COMMODITIES|MNC|INDIA DEFENCE|INDIA DEFENSE|FINANCIAL SERVICES(?: 25/50)?)",
            normalized,
        ):
            return "sector"
        return "index"

    async def _indices_payload(self) -> tuple[list[dict[str, Any]], Any]:
        """Normalize all index returns through Jugaad NSELive.all_indices()."""
        payload = await asyncio.wait_for(asyncio.to_thread(_jugaad_call_sync, "all_indices"), timeout=12.0)
        rows = _records_from_dataframe_or_payload(payload)
        root_timestamp = _first(payload, "timestamp", "lastUpdateTime", "timeVal", "asOn") if isinstance(payload, dict) else None
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            name = _first(row, "index", "indexName", "name", "symbol")
            if not name:
                continue
            clean_name = re.sub(r"\s+", " ", str(name).strip().upper())
            if clean_name in seen:
                continue
            seen.add(clean_name)
            output.append({
                "name": clean_name, "kind": self._index_kind(clean_name),
                "value": _number(_first(row, "last", "lastPrice", "close", "currentValue")),
                "change": _number(_first(row, "variation", "change", "netChange")),
                "change_pct": _number(_first(row, "percentChange", "pChange", "changePercent", "percChange")),
                "open": _number(_first(row, "open")), "high": _number(_first(row, "high", "dayHigh")),
                "low": _number(_first(row, "low", "dayLow")),
                "previous_close": _number(_first(row, "previousClose", "previous_close", "prevClose")),
                "year_high": _number(_first(row, "yearHigh", "year_high")), "year_low": _number(_first(row, "yearLow", "year_low")),
                "change_1w_pct": _number(_first(row, "perChange1w", "change1wPct", "change_1w_pct")),
                "change_30d_pct": _number(_first(row, "perChange30d", "change30dPct", "change_30d_pct")),
                "change_90d_pct": _number(_first(row, "perChange90d", "change90dPct", "change_90d_pct")),
                "change_365d_pct": _number(_first(row, "perChange365d", "change365dPct", "change_365d_pct")),
                "advances": _number(_first(row, "advances", "advance")), "declines": _number(_first(row, "declines", "decline")),
                "unchanged": _number(_first(row, "unchanged", "unchangedCount")),
                "timestamp": _first(row, "timeVal", "timestamp", "lastUpdateTime", "lastUpdated") or root_timestamp,
                "pe": _number(_first(row, "pe", "PE", "priceToEarnings")),
                "pb": _number(_first(row, "pb", "PB", "priceToBook")),
                "dividend_yield": _number(_first(row, "dy", "DY", "dividendYield")),
                "source": "jugaad-data / NSELive", "source_url": f"{NSE_BASE}/market-data/live-market-indices",
            })
        if not output:
            raise ValueError("Jugaad all_indices response contained no recognizable index rows")
        return output, payload

    async def heatmap_for_index(self, index: str, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return a cached constituent heatmap or fetch it on first use."""
        clean_index = re.sub(r"\s+", " ", str(index or "").strip().upper())
        if not clean_index or len(clean_index) > 80 or not re.fullmatch(r"[A-Z0-9 &()/.-]+", clean_index):
            raise ValueError("A valid NSE index name is required")
        return await self._cached_result(
            f"heatmap:jugaad-v3:{clean_index}",
            lambda: self._fetch_heatmap_for_index(clean_index),
            force_refresh=force_refresh,
        )

    async def _index_membership(self, index: str) -> list[dict[str, Any]]:
        """Fetch NSE Indices constituent CSVs using a bootstrapped browser session.

        The site can return its HTML application shell with HTTP 200 when a CSV
        request has no same-origin cookies/referer. A successful status alone is
        therefore not treated as a successful constituent download.
        """
        import csv
        import io
        import time as _time

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/csv,application/octet-stream,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.niftyindices.com/",
            "Origin": "https://www.niftyindices.com",
        }
        for candidate in _index_name_variants(index):
            slug = re.sub(r"[^a-z0-9]", "", candidate.casefold())
            explicit = MEMBERSHIP_FILE_STEMS.get(candidate.upper(), ())
            filenames = tuple(dict.fromkeys((*explicit, f"ind_{slug}list.csv", f"ind_{slug}_list.csv")))
            # Keep one client for the origin so cookies set by the landing page
            # are sent with subsequent constituent downloads.
            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
                    try:
                        await client.get("https://www.niftyindices.com/")
                    except Exception as exc:
                        logger.debug("Pulse Nifty Indices session bootstrap failed error_type=%s", type(exc).__name__)
                    for base in MEMBERSHIP_SOURCE_BASES:
                        for filename in filenames:
                            url = f"{base}/{filename}"
                            cached = _MEMBERSHIP_CACHE.get(url)
                            if cached and _time.monotonic() - cached[0] < MEMBERSHIP_TTL_SECONDS:
                                if cached[1]:
                                    return cached[1]
                                continue
                            try:
                                response = await client.get(url, headers={"Referer": "https://www.niftyindices.com/"})
                            except Exception as exc:
                                logger.warning("Pulse index list request failed url=%s error_type=%s", url, type(exc).__name__)
                                continue
                            body = response.text.lstrip("\ufeff\r\n \t")
                            content_type = response.headers.get("content-type", "").casefold()
                            looks_html = body[:300].casefold().startswith(("<!doctype html", "<html", "<head")) or "text/html" in content_type
                            members: list[dict[str, Any]] = []
                            if response.status_code == 200 and not looks_html:
                                try:
                                    reader = csv.DictReader(io.StringIO(body))
                                    for row in reader:
                                        clean = {str(k).strip().casefold(): (v or "").strip() for k, v in row.items() if k}
                                        symbol = clean.get("symbol", "").upper()
                                        if symbol and re.fullmatch(r"[A-Z0-9&._-]{1,32}", symbol):
                                            members.append({"symbol": symbol, "company": clean.get("company name") or clean.get("companyname") or symbol,
                                                            "industry": clean.get("industry") or None, "url": url})
                                except (csv.Error, UnicodeError):
                                    members = []
                            if not members:
                                logger.warning("Pulse index list unusable url=%s status=%s content_type=%s html=%s first_bytes=%r",
                                               url, response.status_code, content_type[:50], looks_html, body[:80])
                                _MEMBERSHIP_CACHE[url] = (_time.monotonic() - MEMBERSHIP_TTL_SECONDS + 300, [])
                                continue
                            _MEMBERSHIP_CACHE[url] = (_time.monotonic(), members)
                            return members
            except Exception as exc:
                logger.warning("Pulse index list session failed index=%s error_type=%s", candidate, type(exc).__name__)
        return []

    async def _membership_heatmap(self, clean_index: str) -> dict[str, Any] | None:
        """Membership from NSE's index list, with live moves joined from the NIFTY 500 quotes."""
        members = await self._index_membership(clean_index)
        if not members:
            return None
        live_by_symbol: dict[str, dict[str, Any]] = {}
        if clean_index != "NIFTY 500":
            try:
                nifty500 = await self.heatmap_for_index("NIFTY 500")
                live_by_symbol = {str(row.get("symbol")).upper(): row
                                  for row in (nifty500.get("data", {}).get("stocks") or []) if row.get("symbol")}
            except Exception as exc:
                logger.info("Pulse NIFTY 500 live quotes unavailable for membership join error_type=%s", type(exc).__name__)
        stocks: list[dict[str, Any]] = []
        for member in members:
            live = live_by_symbol.get(member["symbol"], {})
            stocks.append({
                "symbol": member["symbol"],
                "company": live.get("company") or member["company"],
                "industry": live.get("industry") or member["industry"],
                "last_price": live.get("last_price"), "change": live.get("change"), "change_pct": live.get("change_pct"),
                "ffmc": live.get("ffmc"), "market_cap": live.get("market_cap"),
                "weightage_pct": None, "weightage_source": None,
                "timestamp": live.get("timestamp"), "source": "NSE index list + NIFTY 500 live quotes",
            })
        ffmc_total = sum(float(row["ffmc"]) for row in stocks if row.get("ffmc"))
        if ffmc_total > 0:
            for row in stocks:
                if row.get("ffmc"):
                    row["weightage_pct"] = float(row["ffmc"]) / ffmc_total * 100.0
                    row["weightage_source"] = "Calculated from free-float market capitalization (FFMC)"
        live_count = sum(1 for row in stocks if row.get("change_pct") is not None)
        data = {
            "index": clean_index, "timestamp": None, "freshness": "fresh",
            "status": "ok" if live_count else "partial", "retrieved_at": _now(),
            "source_url": f"{MEMBERSHIP_SOURCE_BASE}/", "membership_source_url": members[0].get("url"),
            "weight_method": "Calculated from free-float market capitalization (FFMC) of stocks with a live quote",
            "ffmc_total": ffmc_total or None, "stocks": stocks,
            "source": "NSE index constituent list; live moves from NIFTY 500 quotes",
        }
        source = _source_status("constituents", stocks, intraday=True, source_url=data["source_url"])
        source["data"] = data
        source["reason"] = (f"Membership from NSE's published index list. Live moves are available for "
                            f"{live_count} of {len(stocks)} stocks (only NIFTY 500 members have a live quote here).")
        return source

    async def _fetch_heatmap_for_index(self, index: str) -> dict[str, Any]:
        """Fetch constituents exclusively through the shared Jugaad NSELive session."""
        clean_index = re.sub(r"\s+", " ", str(index or "").strip().upper())
        if not clean_index or len(clean_index) > 80 or not re.fullmatch(r"[A-Z0-9 &()/.-]+", clean_index):
            raise ValueError("A valid NSE index name is required")
        if "BHARAT BOND" in clean_index:
            raise ValueError(
                "This is a fixed-income bond index, not an equity-stock index. "
                "The Pulse equity heatmap cannot show live stock constituents for it."
            )
        aliases = {
            "NIFTY INFRASTRUCTURE": ("NIFTY INFRASTRUCTURE", "NIFTY INFRA"),
            "NIFTY INFRA": ("NIFTY INFRA", "NIFTY INFRASTRUCTURE"),
            "NIFTY INDIA DEFENCE": ("NIFTY INDIA DEFENCE", "NIFTY INDIA DEFENSE"),
            "NIFTY INDIA DEFENSE": ("NIFTY INDIA DEFENSE", "NIFTY INDIA DEFENCE"),
        }
        source_url = f"{NSE_BASE}/market-data/live-market-indices"
        errors: list[Exception] = []
        for candidate in dict.fromkeys((*aliases.get(clean_index, (clean_index,)), *_index_name_variants(clean_index))):
            try:
                data = await self._index_heatmap(candidate)
                data["index"] = clean_index
                source = _source_status("constituents", data["stocks"], intraday=True, source_url=source_url)
                source["data"] = data
                source["reason"] = None if source.get("status") != "stale" else "Jugaad constituent data is older than the configured freshness window."
                return source
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(exc)
                logger.warning("Pulse Jugaad constituent request failed index=%s candidate=%s error_type=%s", clean_index, candidate, type(exc).__name__)
        membership = await self._membership_heatmap(clean_index)
        if membership is not None:
            return membership
        last_error = errors[-1] if errors else RuntimeError("No Jugaad index alias was attempted")
        raise RuntimeError(f"Jugaad could not load constituents for {clean_index}: {type(last_error).__name__}: {str(last_error)[:240]}") from last_error

    @staticmethod
    def _headline(indices: list[dict[str, Any]], metrics: dict[str, Any]) -> dict[str, Any]:
        """Points-based headline KPIs for the Pulse landing page, from the same NSE payloads."""
        by_name = {row.get("name"): row for row in indices if row.get("name")}

        def pick(name: str) -> dict[str, Any] | None:
            row = by_name.get(name)
            if not row:
                return None
            return {key: row.get(key) for key in (
                "name", "value", "change", "change_pct", "open", "high", "low", "previous_close",
                "year_high", "year_low", "change_1w_pct", "change_30d_pct", "change_365d_pct",
                "pe", "pb", "dividend_yield", "timestamp", "source",
            )}

        return {
            "nifty50": pick("NIFTY 50"),
            "nifty_bank": pick("NIFTY BANK"),
            "nifty_next50": pick("NIFTY NEXT 50"),
            "nifty_midcap100": pick("NIFTY MIDCAP 100"),
            "nifty_smallcap100": pick("NIFTY SMALLCAP 100"),
            "india_vix": pick("INDIA VIX"),
            "market_cap": metrics.get("market_cap"),
            "market_cap_unit": metrics.get("market_cap_unit"),
            "market_status": metrics.get("market_status"),
            "trade_date": metrics.get("trade_date"),
            "advances": metrics.get("advances"),
            "declines": metrics.get("declines"),
            "unchanged": metrics.get("unchanged"),
            "breadth_basis": metrics.get("breadth_basis"),
            "usd_inr": metrics.get("usd_inr"),
            "usd_inr_basis": metrics.get("usd_inr_basis"),
        }

    async def market_snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return a ten-minute in-memory snapshot unless an explicit refresh is requested."""
        return await self._cached_result(
            "market_snapshot:jugaad-v3", self._fetch_market_snapshot, force_refresh=force_refresh
        )

    async def _fetch_market_snapshot(self) -> dict[str, Any]:
        """Fetch Pulse's landing snapshot from Jugaad and publish independent sections."""
        source_urls = {
            "indices": f"{NSE_BASE}/market-data/live-market-indices",
            "institutional_flows": f"{NSE_BASE}/reports/fii-dii",
            "market_metrics": f"{NSE_BASE}/market-data/live-market-indices",
        }
        results = await asyncio.gather(
            self._source("indices", self._indices_payload(), intraday=True, source_url=source_urls["indices"]),
            self._source("institutional_flows", self._institutional_flows(), intraday=False, source_url=source_urls["institutional_flows"]),
            self._source("market_metrics", self._market_metrics(), intraday=True, source_url=source_urls["market_metrics"]),
        )
        indices_result, flow_result, metrics_result = results
        indices: list[dict[str, Any]] = []
        if indices_result[1].get("status") != "unavailable" and isinstance(indices_result[1].get("data"), tuple):
            normalized, raw_payload = indices_result[1]["data"]
            indices_result[1]["data"] = normalized
            indices = normalized
            root_ts = _first(raw_payload, "timestamp", "lastUpdateTime", "timeVal", "asOn") if isinstance(raw_payload, dict) else None
            if root_ts:
                for row in normalized:
                    row["timestamp"] = row.get("timestamp") or root_ts
            indices_result[1]["freshness"] = _freshness_for(raw_payload)
            if indices_result[1]["freshness"] == "stale":
                indices_result[1]["status"] = "stale"
                indices_result[1]["reason"] = "Jugaad all_indices timestamp is older than the Pulse freshness window."
            elif indices_result[1]["freshness"] == "unverified":
                indices_result[1]["reason"] = "Jugaad did not expose a verifiable index timestamp."

        sources = {name: source for name, source in results}
        metric_data = sources.get("market_metrics", {}).get("data")
        if isinstance(metric_data, dict):
            broad_row = next((row for row in indices if row.get("name") == "NIFTY 500"), None)
            if broad_row:
                for key in ("advances", "declines", "unchanged"):
                    if metric_data.get(key) is None and broad_row.get(key) is not None:
                        metric_data[key] = broad_row[key]
                        metric_data["breadth_basis"] = "Jugaad NSELive all_indices · NIFTY 500"
            if metric_data.get("advances") is None or metric_data.get("declines") is None:
                try:
                    breadth = await asyncio.wait_for(self._breadth(), timeout=14.0)
                    for key in ("advances", "declines", "unchanged"):
                        if metric_data.get(key) is None:
                            metric_data[key] = breadth.get(key)
                    metric_data["breadth_method"] = breadth.get("method")
                    metric_data["breadth_basis"] = "Jugaad NSELive NIFTY 500 constituents"
                    metric_data["timestamp"] = metric_data.get("timestamp") or breadth.get("timestamp")
                except Exception as exc:
                    logger.info("Pulse Jugaad breadth proxy unavailable error_type=%s", type(exc).__name__)

        headline = self._headline(indices, metric_data if isinstance(metric_data, dict) else {})
        sector_rows = [row for row in indices if row.get("kind") == "sector"]
        broad_rows = [row for row in indices if row.get("kind") == "index"]
        rotation_ready = [row for row in sector_rows if row.get("change_pct") is not None and row.get("change_30d_pct") is not None]
        return {
            "as_of": _now(),
            "headline": headline,
            "mode": "jugaad-data NSELive; ten-minute in-memory and shared temporary cache",
            "source_policy": "All Pulse market/index/constituent/quote/flow/filing data uses one reusable Jugaad NSELive session. Google News RSS is used only for news headlines.",
            "sources": {
                **sources,
                "sector_heatmap": {"name": "sector_heatmap", "status": sources.get("indices", {}).get("status", "unavailable"), "freshness": sources.get("indices", {}).get("freshness", "unavailable"), "retrieved_at": sources.get("indices", {}).get("retrieved_at", _now()), "source_url": source_urls["indices"], "reason": sources.get("indices", {}).get("reason"), "data": sector_rows},
                "index_heatmap": {"name": "index_heatmap", "status": sources.get("indices", {}).get("status", "unavailable"), "freshness": sources.get("indices", {}).get("freshness", "unavailable"), "retrieved_at": sources.get("indices", {}).get("retrieved_at", _now()), "source_url": source_urls["indices"], "reason": sources.get("indices", {}).get("reason"), "data": broad_rows},
                "sector_rotation": {"name": "sector_rotation", "status": "ok" if rotation_ready else "unavailable", "freshness": sources.get("indices", {}).get("freshness", "unverified"), "retrieved_at": sources.get("indices", {}).get("retrieved_at", _now()), "source_url": source_urls["indices"], "reason": None if rotation_ready else "Jugaad all_indices did not provide both 1D and 30D changes for sector indices.", "data": rotation_ready},
            },
            "market_movers": {"gainers": [], "losers": [], "volume_leaders": [], "activity_outliers": [], "universe": "Jugaad NSELive top_stocks feed", "source_status": "loading-separately", "outlier_definition": "Moves are source-reported by NSE; historical-volume anomalies are not inferred."},
            "sector_movers": [],
        }

    async def market_movers_snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return broad-universe gainers/losers independently of the main snapshot."""
        return await self._cached_result(
            "market_movers_snapshot:jugaad-v3", self._fetch_market_movers_snapshot, force_refresh=force_refresh
        )

    async def _fetch_market_movers_snapshot(self) -> dict[str, Any]:
        source_url = f"{NSE_BASE}/market-data/top-10-stocks"
        try:
            payload = await asyncio.wait_for(asyncio.to_thread(_jugaad_call_sync, "top_stocks"), timeout=13.0)
            groups = payload if isinstance(payload, dict) else {}
            def rows_for(*keys: str) -> list[dict[str, Any]]:
                for key in keys:
                    value = groups.get(key)
                    if value is not None:
                        return _records_from_dataframe_or_payload(value)
                return []
            gainers_raw = rows_for("topGainers", "top_gainers")
            losers_raw = rows_for("topLoosers", "topLosers", "top_losers")
            volume_raw = rows_for("mostActiveVolume", "most_active_volume")
            stocks_gainers = self._normalize_constituent_rows("JUGAAD GAINERS", gainers_raw, source="jugaad-data / NSELive top_stocks")
            stocks_losers = self._normalize_constituent_rows("JUGAAD LOSERS", losers_raw, source="jugaad-data / NSELive top_stocks")
            volume = self._normalize_constituent_rows("JUGAAD VOLUME", volume_raw, source="jugaad-data / NSELive top_stocks")
            valid = [row for row in stocks_gainers + stocks_losers if row.get("change_pct") is not None]
            gainers_present = any(row["change_pct"] > 0 for row in valid)
            losers_present = any(row["change_pct"] < 0 for row in valid)
            if not valid or not gainers_present or not losers_present:
                first_keys = list(gainers_raw[0].keys())[:12] if gainers_raw else None
                raise ValueError(
                    "Jugaad top_stocks returned no usable price movers "
                    f"(payload={type(payload).__name__} keys={_key_names(payload)[:12]} "
                    f"gainers={len(gainers_raw)} losers={len(losers_raw)} gainer_row_keys={first_keys})"
                )
            market_movers = {
                "gainers": sorted((row for row in valid if row["change_pct"] > 0), key=lambda row: row["change_pct"], reverse=True)[:20],
                "losers": sorted((row for row in valid if row["change_pct"] < 0), key=lambda row: row["change_pct"])[:20],
                "volume_leaders": volume[:20], "activity_outliers": [],
                "universe": "Jugaad NSELive top_stocks", "source_url": source_url,
                "source_status": "ok", "retrieved_at": _now(),
                "quote_coverage": {"quoted": len(valid), "total": len(stocks_gainers) + len(stocks_losers)},
                "outlier_definition": "Source-reported NSE top gainer/loser lists; no historical-volume anomaly score is inferred.",
            }
            source = _source_status("market_universe", {"timestamp": _first(groups, "timestamp", "timeVal"), "data": valid}, intraday=True, source_url=source_url)
            source["data"] = market_movers
            source["status"] = "ok"
            return {"market_movers": market_movers, "sources": {"market_universe": source}, "mode": "jugaad-data NSELive; ten-minute cache"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Pulse Jugaad top_stocks failed error_type=%s reason=%s; trying NIFTY 500 constituents", type(exc).__name__, str(exc)[:400])
            try:
                data = await asyncio.wait_for(self._index_heatmap("NIFTY 500"), timeout=16.0)
                stocks = data.get("stocks", [])
                market_movers = self._market_movers([stocks])
                source = _source_status("market_universe", stocks, intraday=True, source_url=source_url)
                source["data"] = market_movers
                source["status"] = "partial" if stocks else "unavailable"
                market_movers.update({"universe": "Jugaad NSELive NIFTY 500 constituents", "source_url": source_url, "source_status": source["status"], "retrieved_at": _now()})
                return {"market_movers": market_movers, "sources": {"market_universe": source}, "mode": "jugaad-data NSELive; ten-minute cache"}
            except Exception as fallback_exc:
                source = self._unavailable_source("market_universe", fallback_exc, source_url)
                return {"market_movers": {"gainers": [], "losers": [], "volume_leaders": [], "activity_outliers": [], "universe": "Jugaad NSELive", "source_url": source_url, "source_status": "unavailable", "retrieved_at": source["retrieved_at"], "reason": source["reason"], "outlier_definition": "Source unavailable; no ranking is inferred."}, "sources": {"market_universe": source}, "mode": "jugaad-data NSELive; ten-minute cache"}

    async def _quote(self, symbol: str) -> dict[str, Any]:
        source_url = f"{NSE_BASE}/get-quotes/equity?symbol={quote(symbol)}"
        quote_row = await asyncio.wait_for(asyncio.to_thread(_jugaad_live_quote, symbol), timeout=12.0)
        if not quote_row or quote_row.get("last_price") is None:
            raise ValueError(f"Jugaad NSELive returned no usable quote for {symbol}")
        quote_row["source_note"] = "Retrieved through the shared Jugaad NSELive session; no direct NSE or Yahoo quote fallback is used."
        quote_row["source_url"] = source_url
        return quote_row

    async def _announcements(self, symbol: str) -> list[dict[str, Any]]:
        today = datetime.now(INDIA_TZ).date()
        start_date = today - timedelta(days=2)
        payload = await asyncio.wait_for(asyncio.to_thread(
            _jugaad_call_sync, "corporate_announcements", segment="equities", from_date=start_date, to_date=today, symbol=symbol
        ), timeout=12.0)
        results = []
        for row in _records_from_dataframe_or_payload(payload):
            row_symbol = str(_first(row, "symbol", "symbolName") or symbol).upper().strip()
            event_date = _first(row, "an_dt", "dt", "sort_date", "date", "broadcastDateTime", "timestamp")
            if row_symbol != symbol or not _recent_three_day(event_date):
                continue
            results.append({"symbol": row_symbol, "title": _first(row, "desc", "subject", "headline", "attchmntText") or "Corporate announcement", "date": event_date, "source": "jugaad-data / NSE", "url": _first(row, "attchmntFile", "attachmentUrl", "url") or f"{NSE_BASE}/companies-listing/corporate-filings-announcements"})
        return results

    async def _insider_disclosures(self, symbol: str) -> list[dict[str, Any]]:
        today = datetime.now(INDIA_TZ).date()
        start_date = today - timedelta(days=2)
        payload = await asyncio.wait_for(asyncio.to_thread(
            _jugaad_json_sync, "corporate-officials-insider-trading",
            {"index": "equities", "symbol": symbol, "from_date": start_date.strftime("%d-%m-%Y"), "to_date": today.strftime("%d-%m-%Y")},
        ), timeout=12.0)
        results = []
        for row in _items(payload):
            row_symbol = str(_first(row, "symbol", "symbolName") or "").upper().strip()
            event_date = _first(row, "date", "an_dt", "disclosureDate", "dt", "timestamp")
            if row_symbol and row_symbol != symbol:
                continue
            if not _recent_three_day(event_date):
                continue
            results.append({"symbol": row_symbol or symbol, "title": _first(row, "desc", "subject", "nameOfAcquirer", "personName", "acquirerName") or "Insider disclosure", "date": event_date, "transaction": _first(row, "acqMode", "transactionType", "type", "transactionTypeDescription"), "quantity": _number(_first(row, "secAcq", "quantity", "shares", "noOfSecurities")), "price": _number(_first(row, "price", "secAcqPrice")), "source": "jugaad-data / NSE", "url": _first(row, "attchmntFile", "url") or f"{NSE_BASE}/companies-listing/corporate-filings-insider-trading"})
        return results

    async def _large_deals(self, symbol: str) -> list[dict[str, Any]]:
        payload = await asyncio.wait_for(asyncio.to_thread(_jugaad_json_sync, "snapshot-capital-market-largedeal", {"index": "equities"}), timeout=12.0)
        categorized: list[tuple[dict[str, Any], str | None]] = []
        if isinstance(payload, dict):
            for key, category in (("BULK_DEALS_DATA", "BULK"), ("BLOCK_DEALS_DATA", "BLOCK")):
                categorized.extend((row, category) for row in _items(payload.get(key)))
        if not categorized:
            categorized = [(row, None) for row in _items(payload)]
        results = []
        for row, category in categorized:
            row_symbol = str(_first(row, "symbol", "symbolName", "ticker") or "").upper().strip()
            event_date = _first(row, "date", "tradeDate", "tradingDate", "timestamp")
            if row_symbol != symbol or not _recent_three_day(event_date):
                continue
            results.append({"symbol": row_symbol, "company": _first(row, "companyName", "company", "name"), "client": _first(row, "clientName", "client", "acquirerName"), "transaction": _first(row, "buySell", "buySellIndicator", "transaction"), "deal_type": _first(row, "dealType", "type", "transactionType") or category or "Bulk/block deal", "quantity": _number(_first(row, "quantityTraded", "quantity", "qty", "noOfShares")), "price": _number(_first(row, "tradePrice", "price", "avgPrice", "watp")), "date": event_date, "source": "jugaad-data / NSE", "url": f"{NSE_BASE}/market-data/large-deals"})
        return results

    async def _news(self, symbol: str) -> list[dict[str, Any]]:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; C9EquityPulse/1.0)", "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8"}
        params = {"q": f'"{symbol}" stock India when:3d', "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds), follow_redirects=True, headers=headers) as client:
            response = await client.get(GOOGLE_NEWS_RSS, params=params)
            response.raise_for_status()
            try:
                root = ET.fromstring(response.text)
            except ET.ParseError as exc:
                raise ValueError("Google News returned an invalid RSS document") from exc
        results = []
        for item in root.findall(".//item"):
            published_at = item.findtext("pubDate")
            if not _recent_three_day(published_at):
                continue
            source_node = item.find("source")
            results.append({"title": (item.findtext("title") or "News item").strip(), "url": (item.findtext("link") or "").strip() or None, "published_at": published_at, "source": (source_node.text or "Google News RSS").strip() if source_node is not None else "Google News RSS"})
        return results[:30]

    async def stock_context(self, symbol: str, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return the selected company's research context from the ten-minute cache."""
        normalized = re.sub(r"[^A-Za-z0-9&._-]", "", str(symbol or "")).upper()
        if not normalized or len(normalized) > 32:
            raise ValueError("A valid NSE symbol is required")
        return await self._cached_result(
            f"stock:jugaad-v3:{normalized}", lambda: self._fetch_stock_context(normalized), force_refresh=force_refresh
        )

    async def _fetch_stock_context(self, symbol: str) -> dict[str, Any]:
        """Fetch selected-stock research sources; enforce a strict three-day window."""
        symbol = re.sub(r"[^A-Za-z0-9&._-]", "", str(symbol or "")).upper()
        if not symbol or len(symbol) > 32:
            raise ValueError("A valid NSE symbol is required")
        source_specs = [
            ("quote", self._quote(symbol), True, f"{NSE_BASE}/get-quotes/equity?symbol={quote(symbol)}"),
            ("insider_deals", self._insider_disclosures(symbol), False, f"{NSE_BASE}/companies-listing/corporate-filings-insider-trading"),
            ("bulk_block_deals", self._large_deals(symbol), False, f"{NSE_BASE}/market-data/large-deals"),
            ("corporate_announcements", self._announcements(symbol), False, f"{NSE_BASE}/companies-listing/corporate-filings-announcements"),
            ("news", self._news(symbol), False, f"{GOOGLE_NEWS_RSS}?q={quote(symbol + ' stock India when:3d')}"),
        ]
        pairs = await asyncio.gather(*(self._source(name, task, intraday=intraday, source_url=url) for name, task, intraday, url in source_specs))
        sources = {name: source for name, source in pairs}
        date_fields = ("date", "published_at", "timestamp", "tradeDate", "broadcastDateTime")
        for key in ("news", "insider_deals", "bulk_block_deals", "corporate_announcements"):
            source = sources.get(key)
            if not source or not isinstance(source.get("data"), list):
                continue
            filtered = [item for item in source["data"] if _recent_three_day(_first(item, *date_fields))]
            source["data"] = filtered[:30]
            if not filtered and source.get("status") not in {"unavailable", "error"}:
                source["reason"] = "No matching records were published in the last three calendar days."
        return {
            "symbol": symbol, "as_of": _now(),
            "mode": "Jugaad NSELive/session for market and filing data; Google News RSS for news; last-three-calendar-days filter",
            "recent_window": {"days": 3, "start_date": (datetime.now(INDIA_TZ).date() - timedelta(days=2)).isoformat(), "end_date": datetime.now(INDIA_TZ).date().isoformat(), "timezone": "Asia/Kolkata"},
            "sources": sources,
        }



collector = PulseMarketCollector()