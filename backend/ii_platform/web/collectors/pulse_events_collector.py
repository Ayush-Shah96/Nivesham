"""Market-wide NSE event lists for the Dashboard.

Sources: announcements, corporate actions, board meetings, and Reg 30 credit-rating filings.
All requests use the shared jugaad-data NSELive session. Every source is independent, so one
failing route never hides the others. Coverage filtering matches by NSE symbol first, then by
company name, because Coverage List companies are not always stored with an NSE symbol.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .pulse_market_collector import _first, _jugaad_json_sync, _items, _now

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60          # the background refresher keeps this warm every minute
ROW_LIMIT = 2000                # cache enough rows to filter the whole market down to coverage
DISPLAY_LIMIT = 300
CREDIT_RATING_PAGE = "https://www.nseindia.com/companies-listing/corporate-sdd-credit-rating-reg30"
# NSE route names for the Reg 30 credit-rating disclosures, tried in order. The first route that
# returns rows is used, and the reason records each attempt so a wrong name is visible in the API.
CREDIT_RATING_ROUTES = ("corporate-credit-rating", "corporates-creditRating", "corporate-sdd-credit-rating-reg30")

# (route, params, public NSE page for the user to verify against)
SOURCES: dict[str, tuple[str, dict[str, str], str]] = {
    "announcements": ("corporate-announcements", {"index": "equities"},
                      "https://www.nseindia.com/companies-listing/corporate-filings-announcements"),
    "corporate_actions": ("corporates-corporateActions", {"index": "equities"}, "https://www.nseindia.com"),
    "board_meetings": ("corporate-board-meetings", {"index": "equities"},
                       "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"),
}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _name_key(value: Any) -> str:
    """'Acme Industries Ltd.' and 'ACME INDUSTRIES LIMITED' both become 'acme industries'."""
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())
    text = re.sub(r"\b(limited|ltd|private|pvt|india)\b", " ", text)
    return " ".join(text.split())


def _normalize(kind: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows[:ROW_LIMIT]:
        if kind == "announcements":
            item = {
                "symbol": _clean(_first(row, "symbol")),
                "company": _clean(_first(row, "sm_name", "companyName", "company")),
                "subject": _clean(_first(row, "desc", "subject", "category")),
                "detail": _clean(_first(row, "attchmntText")),
                "date": _clean(_first(row, "an_dt", "exchdisstime", "date")),
                "url": _clean(_first(row, "attchmntFile")),
            }
        elif kind == "corporate_actions":
            item = {
                "symbol": _clean(_first(row, "symbol")),
                "company": _clean(_first(row, "comp", "companyName", "company")),
                "purpose": _clean(_first(row, "purpose", "subject")),
                "ex_date": _clean(_first(row, "exDate", "ex_date")),
                "record_date": _clean(_first(row, "recDate", "record_date")),
                "face_value": _clean(_first(row, "faceVal", "face_value")),
            }
        else:
            item = {
                "symbol": _clean(_first(row, "symbol")),
                "company": _clean(_first(row, "company", "companyName", "sm_name", "comp")),
                "purpose": _clean(_first(row, "purpose", "bm_purpose", "desc", "subject", "event")),
                "meeting_date": _clean(_first(row, "bm_date", "meetingDate", "meeting_date", "date", "meetingDateTime")),
                "description": _clean(_first(row, "bm_desc", "description", "remarks", "attchmntText", "outcome")),
            }
        if item["symbol"] or any(value for key, value in item.items() if key != "symbol"):
            output.append(item)
    return output


def _credit_field(row: dict[str, Any], *aliases: str) -> str | None:
    """Read NSE credit-rating fields independent of case, spaces, underscores or punctuation.

    Alias order is intentional: for example, ``ratingAssignedOn`` is the
    instrument description while ``classOfAction`` is only Review/Withdrawal.
    """
    normalized_row = {re.sub(r"[^a-z0-9]", "", str(key).casefold()): value for key, value in row.items()}
    for alias in aliases:
        value = normalized_row.get(re.sub(r"[^a-z0-9]", "", alias.casefold()))
        cleaned = _clean(value)
        if cleaned and cleaned.casefold() not in {"not listed", "notlisted", "-", "—", "–", "na", "n/a", "null", "none", "not available", "not disclosed"}:
            return cleaned
    return None


def _normalize_credit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize real Reg. 30 credit-rating disclosures; reject placeholder-only rows."""
    output: list[dict[str, Any]] = []
    for row in rows[:ROW_LIMIT]:
        item = {
            "symbol": _credit_field(row, "symbol", "tradingSymbol"),
            "company": _credit_field(row, "companyName", "sm_name", "company", "comp", "issuerName", "name"),
            "agency": _credit_field(row, "creditRatingAgency", "ratingAgency", "agency", "rating_agency",
                                     "ratingAgencyName", "agencyName", "creditRatingAgencyName"),
            # NSE's Reg.30 table labels this field "Rating Assigned on
            # (details of instrument)"; the JSON key is commonly ratingAssignedOn.
            "instrument": _credit_field(row, "ratingAssignedOn", "instrument", "instrumentName", "facility",
                                         "debtInstrument", "classOfAction", "typeOfInstrument"),
            "rating": _credit_field(row, "creditRating", "rating", "ratingName", "ratingValue", "currentRating"),
            "outlook": _credit_field(row, "outlook", "ratingOutlook"),
            # Use the filing broadcast timestamp for the date filter; the date
            # on which the rating itself was assigned can be much older.
            "date": _credit_field(row, "broadcastDateTime", "broadcastDate", "date", "an_dt", "dt",
                                  "exchdisstime", "dateOfCurrentCreditRatingOutlook", "ratingDate", "dateOfCurrentRating"),
            "url": _credit_field(row, "detailsOfRatingRationale", "ratingRationale", "attchmntFile", "attachment", "fileUrl", "url"),
        }
        # Symbol alone is not evidence of a credit-rating filing (NSE sometimes returns
        # index/metadata rows with a symbol but no rating attributes).
        if any(item.get(key) for key in ("company", "agency", "instrument", "rating", "outlook", "date")):
            output.append(item)
    return output


def filter_rows(rows: list[dict[str, Any]], symbols: frozenset[str], names: frozenset[str]) -> list[dict[str, Any]]:
    """Keep rows whose NSE symbol is a Coverage List symbol, or whose company name matches a Coverage List name."""
    covered_symbols = {str(symbol).strip().upper() for symbol in symbols if symbol}
    name_keys = {_name_key(name) for name in names if name}
    name_keys.discard("")
    kept: list[dict[str, Any]] = []
    for row in rows:
        if (row.get("symbol") or "").strip().upper() in covered_symbols:
            kept.append(row)
            continue
        key = _name_key(row.get("company"))
        if key and (key in name_keys or any(len(k) >= 10 and (k in key or key in k) for k in name_keys)):
            kept.append(row)
    return kept


IST = ZoneInfo("Asia/Kolkata")
EVENT_DATE_FIELD = {"announcements": "date", "corporate_actions": "ex_date", "board_meetings": "meeting_date"}
_DATE_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y",
                 "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y")


def today_ist() -> date:
    return datetime.now(IST).date()


def parse_day(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text[:19], text[:11]):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def row_is_today(value: Any, today: date) -> bool:
    """Rows whose date cannot be read are kept, so an unfamiliar NSE format never hides data."""
    day = parse_day(value)
    return True if day is None else day == today


class PulseEventsCollector:
    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._credit_cache: dict[str, Any] | None = None
        self._credit_cached_at = 0.0
        self._lock = asyncio.Lock()
        self._credit_lock = asyncio.Lock()

    async def _fetch_source(self, kind: str) -> dict[str, Any]:
        route, params, source_url = SOURCES[kind]
        try:
            request_params = dict(params)
            if kind in {"announcements", "corporate_actions", "board_meetings"}:
                day = today_ist()
                request_params.update({"from_date": day.strftime("%d-%m-%Y"), "to_date": day.strftime("%d-%m-%Y")})
            payload = await asyncio.wait_for(asyncio.to_thread(_jugaad_json_sync, route, request_params), timeout=18.0)
            data = _normalize(kind, [row for row in _items(payload) if isinstance(row, dict)])
            if not data:
                return {"status": "unavailable", "rows": [], "source_url": source_url, "retrieved_at": _now(),
                        "reason": f"NSE route '{route}' returned no rows."}
            return {"status": "ok", "rows": data, "count": len(data), "source_url": source_url,
                    "retrieved_at": _now(), "reason": None}
        except Exception as exc:
            logger.info("Pulse NSE event source failed kind=%s error_type=%s", kind, type(exc).__name__)
            return {"status": "unavailable", "rows": [], "source_url": source_url, "retrieved_at": _now(),
                    "reason": f"NSE route '{route}' failed: {type(exc).__name__}: {str(exc)[:160]}"}

    async def _market_events(self, *, force_refresh: bool) -> dict[str, Any]:
        """Market-wide lists, cached unfiltered so one cache serves every coverage set."""
        fresh = self._cache is not None and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS
        if fresh and not force_refresh:
            return self._cache  # type: ignore[return-value]
        async with self._lock:
            if self._cache is not None and not force_refresh and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS:
                return self._cache  # type: ignore[return-value]
            results = await asyncio.gather(*(self._fetch_source(kind) for kind in SOURCES))
            payload = {"as_of": _now(), "sources": dict(zip(SOURCES.keys(), results)),
                       "cache": {"cached_at": _now(), "ttl_seconds": CACHE_TTL_SECONDS}}
            if any(source["status"] == "ok" for source in payload["sources"].values()):
                self._cache = payload
                self._cached_at = time.monotonic()
            return payload

    async def corporate_events(self, *, force_refresh: bool = False, symbols: frozenset[str] = frozenset(),
                               names: frozenset[str] = frozenset(), coverage_only: bool = False,
                               today_only: bool = True) -> dict[str, Any]:
        raw = await self._market_events(force_refresh=force_refresh)
        today = today_ist()
        sources: dict[str, Any] = {}
        for kind, source in raw["sources"].items():
            market_rows = source.get("rows", [])
            rows = filter_rows(market_rows, symbols, names) if coverage_only else list(market_rows)
            if today_only:
                field = EVENT_DATE_FIELD[kind]
                rows = [row for row in rows if row_is_today(row.get(field), today)]
            if rows:
                reason = None
            elif source.get("status") != "ok":
                reason = source.get("reason")
            else:
                reason = "No rows for today in this NSE list." if today_only else "No rows in this NSE list."
            sources[kind] = {**source, "rows": rows[:DISPLAY_LIMIT], "count": len(rows),
                             "market_row_count": len(market_rows),
                             "status": "ok" if rows else "unavailable", "reason": reason}
        return {"as_of": raw["as_of"], "today": today.isoformat(), "coverage_symbol_count": len(symbols),
                "coverage_name_count": len(names), "sources": sources, "cache": raw["cache"]}

    async def _fetch_credit_raw(self) -> dict[str, Any]:
        attempts: list[str] = []
        for route in CREDIT_RATING_ROUTES:
            try:
                today = today_ist()
                params = {"index": "equities", "from_date": (today - timedelta(days=7)).strftime("%d-%m-%Y"),
                          "to_date": today.strftime("%d-%m-%Y")}
                payload = await asyncio.wait_for(asyncio.to_thread(_jugaad_json_sync, route, params), timeout=18.0)
                rows = _normalize_credit([row for row in _items(payload) if isinstance(row, dict)])
                if rows:
                    return {"status": "ok", "rows": rows, "route": route, "source_url": CREDIT_RATING_PAGE,
                            "retrieved_at": _now(), "reason": None}
                attempts.append(f"{route}: no valid credit-rating records (raw={len(_items(payload))})")
            except Exception as exc:
                attempts.append(f"{route}: {type(exc).__name__}")
        return {"status": "unavailable", "rows": [], "route": None, "source_url": CREDIT_RATING_PAGE,
                "retrieved_at": _now(),
                "reason": "NSE Reg 30 credit-rating routes returned no rows (" + "; ".join(attempts) + ")."}

    async def credit_ratings(self, *, force_refresh: bool = False, symbols: frozenset[str] = frozenset(),
                             names: frozenset[str] = frozenset(), coverage_only: bool = False,
                             today_only: bool = True) -> dict[str, Any]:
        fresh = self._credit_cache is not None and time.monotonic() - self._credit_cached_at < CACHE_TTL_SECONDS
        if not (fresh and not force_refresh):
            async with self._credit_lock:
                if not (self._credit_cache is not None and not force_refresh
                        and time.monotonic() - self._credit_cached_at < CACHE_TTL_SECONDS):
                    raw = await self._fetch_credit_raw()
                    self._credit_cache = raw
                    # Successful results are cached for the full TTL; failures are retried on the next cycle.
                    self._credit_cached_at = time.monotonic() if raw["status"] == "ok" else time.monotonic() - CACHE_TTL_SECONDS + 15
        raw = self._credit_cache or await self._fetch_credit_raw()
        market_rows = raw.get("rows", [])
        rows = filter_rows(market_rows, symbols, names) if coverage_only else list(market_rows)
        if today_only:
            today = today_ist()
            rows = [row for row in rows if row_is_today(row.get("date"), today)]
        if raw["status"] != "ok":
            reason = raw.get("reason")
        elif rows:
            reason = None
        elif coverage_only and not (symbols or names):
            reason = "No active Coverage List companies were found."
        elif coverage_only:
            reason = "No Coverage List company has a Reg 30 credit-rating filing in this list."
        else:
            reason = "No Reg 30 credit-rating filings for today." if today_only else "No Reg 30 credit-rating filings."
        return {**raw, "rows": rows[:DISPLAY_LIMIT], "row_count": len(rows), "total_row_count": len(market_rows),
                "status": "ok" if rows else "unavailable", "reason": reason,
                "cache": {"cached_at": _now(), "ttl_seconds": CACHE_TTL_SECONDS}}

    async def refresh_all(self) -> None:
        """Used by the background refresher to keep every source warm without any visitor."""
        await self._market_events(force_refresh=True)
        await self.credit_ratings(force_refresh=True, coverage_only=False)


pulse_events_collector = PulseEventsCollector()