from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import and_, case, delete, func, or_, select

from infrastructure.security.permissions import require_role
from apps.coverage_list import sync_coverage_workbook_if_changed
from ii_platform.database.connection import get_db, get_sessionmaker
from ii_platform.database.models import (
    ApprovalStatus,
    CapitalAction,
    CapitalActionAssessment,
    CapitalActionSignal,
    Company,
    InsiderSignal,
    InsiderTransaction,
    RegulatoryCollectorRun,
    RegulatoryFiling,
    RegulatoryMarketPrice,
    TargetCompany,
    TrackMode,
    User,
    UserRole,
)
from ii_platform.web.collectors.pe_warrant_collector import PEWarrantCollector
from ii_platform.web.collectors.nse_corporate_announcements_collector import (
    CAPITAL_EVENT_TYPES,
    classify_capital_event,
    clean_investor_name,
)
from apps.primary_data.store import sync_insider_transactions, sync_capital_actions
from apps.regulatory_activity.sync_state import (
    CAPITAL_ACTION_DESK,
    REGULATORY_DESK,
    get_sync_state,
    mark_finished,
    mark_running,
)
from ii_platform.nse import NSEApiClient
from ii_platform.cache.snapshot import get_snapshot
from ii_platform.web.collectors.nse_insider_collector import (
    NSE_PAGE_URL,
    NSE_TIMEZONE,
    MAX_HISTORY_DAYS as NSE_MAX_HISTORY_DAYS,
    NSEInsiderTradingCollector,
    history_window,
)

router = APIRouter(prefix="/regulatory-signals", tags=["Regulatory Signal Desk"])

logger = logging.getLogger(__name__)

MAX_HISTORY_DAYS = NSE_MAX_HISTORY_DAYS
# User-facing Regulatory priority is deliberately only four buckets. NT is an
# internal Coverage List mode but is presented as Others in the Desk.
PRIORITY_ORDER = {
    TrackMode.PO.value: 1,
    TrackMode.HT.value: 2,
    TrackMode.LT.value: 3,
    "Others": 4,
}

REFERENCE_SOURCES = [
    {
        "name": "NSE India — Insider Trading (Regulation 7(2))",
        "url": NSE_PAGE_URL,
    },
]

CAPITAL_ACTION_SOURCES = [
    {
        "name": "NSE India — Corporate Announcements",
        "url": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
    },
]

RELEVANT_PERSON_CATEGORY_MARKERS = (
    "promoter",
    "director",
    "employee",
    "kmp",
    "designated person",
    "connected person",
)
_REGULATORY_SYNC_LOCK = asyncio.Lock()
_CAPITAL_SYNC_LOCK = asyncio.Lock()


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


_CAPITAL_TYPE_ALIASES = {
    "PE_TAKEOVER": "STRATEGIC_ACQUISITION",
    "STRATEGIC_ACQUISITION": "STRATEGIC_ACQUISITION",
    "ACQUISITION": "STRATEGIC_ACQUISITION",
    "DISPOSAL": "STRATEGIC_ACQUISITION",
    "WARRANT": "WARRANT",
    "WARRANT_ISSUE": "WARRANT",
    "WARRANT_CONVERSION": "WARRANT",
    "PREFERENTIAL": "PREFERENTIAL_ISSUE",
    "PREFERENTIAL_ISSUE": "PREFERENTIAL_ISSUE",
    "PREFERENTIAL_ALLOTMENT": "PREFERENTIAL_ISSUE",
}

def _canonical_capital_type(value: Any) -> str | None:
    key = re.sub(r"[^A-Z0-9_]+", "_", str(value or "").upper()).strip("_")
    return _CAPITAL_TYPE_ALIASES.get(key)






def _allowed_category(category: str | None) -> bool:
    value = _norm(category)
    return bool(value) and any(marker in value for marker in RELEVANT_PERSON_CATEGORY_MARKERS)


def _company_terms(company: Company) -> set[str]:
    values = [
        company.legal_name,
        company.nse_symbol,
        company.bse_code,
        company.isin,
        *(company.aliases or []),
    ]
    return {_norm(value) for value in values if _norm(value)}


def _matches_company(scraped_name: str | None, company: Company) -> bool:
    target = _norm(scraped_name)
    if not target:
        return False
    for exact in (company.nse_symbol, company.bse_code, company.isin):
        normalized = _norm(exact)
        if normalized and target == normalized:
            return True
    terms = _company_terms(company)
    if target in terms:
        return True
    fuzzy = [term for term in terms if len(term) >= 6 and (target in term or term in target)]
    return len(fuzzy) == 1


def _active_master_query():
    return (
        select(Company, TargetCompany, User)
        .join(TargetCompany, TargetCompany.company_id == Company.id)
        .outerjoin(User, and_(User.id == TargetCompany.analyst_id, User.is_active.is_(True)))
        .where(
            TargetCompany.active.is_(True),
            TargetCompany.approval_status == ApprovalStatus.APPROVED,
            TargetCompany.track_mode.in_([TrackMode.PO, TrackMode.HT, TrackMode.LT, TrackMode.NT]),
        )
    )

def _all_nse_master_query():
    """All locally known NSE companies; Coverage List is only presentation metadata."""
    return (
        select(Company, TargetCompany, User)
        .outerjoin(
            TargetCompany,
            and_(
                TargetCompany.company_id == Company.id,
                TargetCompany.active.is_(True),
                TargetCompany.approval_status == ApprovalStatus.APPROVED,
            ),
        )
        .outerjoin(User, and_(User.id == TargetCompany.analyst_id, User.is_active.is_(True)))
        .where(Company.nse_symbol.is_not(None))
    )


def _priority(target: TargetCompany | None) -> int:
    if target is None or target.track_mode == TrackMode.NT:
        return PRIORITY_ORDER["Others"]
    return PRIORITY_ORDER.get(target.track_mode.value, PRIORITY_ORDER["Others"])


def _track(target: TargetCompany | None) -> str:
    if target is None or target.track_mode == TrackMode.NT:
        return "Others"
    return target.track_mode.value


_CAPITAL_PLACEHOLDERS = {
    "", "-", "–", "—", "na", "n a", "nil", "none", "null",
    "not disclosed", "not available", "not provided", "the company", "company",
    "category no", "category no.", "category number", "serial no", "sr no",
}


def _clean_capital_text(value: object) -> str | None:
    text = str(value or "").replace("\x00", "")
    text = "".join(ch for ch in text if ch in "\n\r\t" or ord(ch) >= 32).strip()
    if not text:
        return None
    if text.casefold() in _CAPITAL_PLACEHOLDERS:
        return None
    normalized = _norm(text)
    if any(marker in normalized for marker in (
        "refer annexure", "pre preferential issue", "pre preferential", "post allotment",
        "category maximum no", "serial no",
    )):
        return None
    return text


def _safe_capital_numeric_values(
    quantity: object, price: object, amount: object,
) -> tuple[float | None, float | None, float | None]:
    """Hide invalid legacy capital metrics until the attachment parser repairs them.

    Older parser versions stored negative quantities and unrelated document amounts.
    Never expose a price/value pair attached to a non-positive quantity, or an
    aggregate value that materially contradicts quantity × unit price.
    """
    def positive(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        import math
        return number if math.isfinite(number) and number > 0 else None

    q = positive(quantity)
    p = positive(price)
    a = positive(amount)
    # A negative/non-numeric quantity is a strong signal that the extracted row was
    # misaligned. Suppress all figures from that row rather than leaving a random
    # price or issue size visible on its own.
    try:
        raw_q = float(quantity) if quantity not in (None, "") else None
    except (TypeError, ValueError):
        raw_q = None
    if raw_q is not None and raw_q <= 0:
        return None, None, None
    if q is not None and p is not None and a is not None:
        expected = q * p
        if expected > 0 and not (expected * 0.5 <= a <= expected * 2.0):
            a = None
    return q, p, a


def _threshold_status_text(value: object) -> str | None:
    """Convert the collector's threshold assessment object to a DB-safe label.

    Threshold collectors return metadata plus a list of states, while the
    relational model stores a VARCHAR. Persist the state labels, never ``str(dict)``.
    """
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        states = value.get("states")
        if isinstance(states, (list, tuple)):
            labels = [str(item).strip() for item in states if item not in (None, "")]
            if labels:
                return ", ".join(labels)[:80] or None
        state = value.get("state")
        if isinstance(state, (list, tuple)):
            labels = [str(item).strip() for item in state if item not in (None, "")]
            return ", ".join(labels)[:80] or None
        if state not in (None, "") and isinstance(state, (str, int, float)):
            return str(state).strip()[:80] or None
        return None
    if isinstance(value, (list, tuple)):
        labels = [str(item).strip() for item in value if item not in (None, "")]
        return ", ".join(labels)[:80] or None
    if isinstance(value, (str, int, float)):
        return str(value).strip()[:80] or None
    return None


def _capital_payload_value(payload: dict[str, Any], key: str):
    parsed = payload.get("parsed") if isinstance(payload.get("parsed"), dict) else {}
    value = parsed.get(key)
    if key == "investors":
        values = value if isinstance(value, list) else [value] if value else []
        cleaned = [item for item in (_clean_capital_text(v) for v in values) if item]
        return cleaned
    value = _clean_capital_text(value)
    return value


def _capital_evidence_text(signal: CapitalActionSignal, payload: dict[str, Any]) -> str:
    parsed = payload.get("parsed") if isinstance(payload.get("parsed"), dict) else {}
    investors = parsed.get("investors") or []
    if isinstance(investors, str):
        investors = [investors]
    parts = [
        signal.title,
        signal.action,
        payload.get("announcement_subject"),
        payload.get("announcement_details"),
        payload.get("document_summary"),
        payload.get("investor_category"),
        parsed.get("investor_category"),
        " ".join(str(value) for value in investors),
        payload.get("document_text"),
    ]
    stake = parsed.get("stake_percent")
    if stake not in (None, ""):
        parts.append(f"stake {stake}%")
    return " ".join(str(part or "") for part in parts if str(part or "").strip())[:240_000]


def _is_valid_stored_capital_event(signal: CapitalActionSignal, payload: dict[str, Any]) -> bool:
    """Reject stale pre-classifier NSE rows that no longer meet the 3-event contract."""
    event_type = str(signal.event_type or "").strip().upper()
    if event_type == "REVIEW" or str(getattr(signal, "classification_status", "")).upper() == "REVIEW":
        return True
    if event_type not in CAPITAL_EVENT_TYPES:
        return False
    evidence = _capital_evidence_text(signal, payload)
    classified = classify_capital_event(evidence)
    return classified == event_type


def _action_key(action: str | None) -> str:
    value = _norm(action)
    if "pledge invocation" in value:
        return "pledge invocation"
    if "release" in value or "revok" in value:
        return "pledge released"
    if "pledge" in value:
        return "pledge"
    if any(token in value for token in ("sale", "sell", "disposal", "disposed")):
        return "sale"
    if any(token in value for token in ("buy", "purchase", "acquisition", "acquired")):
        return "buy"
    return value


def _signal_key(
    company_id: int,
    person_name: str | None,
    transaction_date: datetime | None,
    action: str | None,
    quantity: float | None,
    amount_inr: float | None,
) -> tuple[Any, ...]:
    return (
        company_id,
        _norm(person_name),
        transaction_date.date() if transaction_date else None,
        _action_key(action),
        round(float(quantity), 4) if quantity is not None else None,
        round(float(amount_inr), 2) if amount_inr is not None else None,
    )


def _capital_display_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Collapse duplicate NSE filings for the same capital action in the desk view.

    NSE often publishes an initial board-meeting/result notice and a follow-up
    outcome notice for the same issue. The source rows can have different
    announcement IDs, so source-id based dedupe does not catch them.
    """
    signal = row["signal"]
    event_day = signal.event_date.date() if signal.event_date else None

    def number(value: Any):
        if value in (None, ""):
            return None
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            return _norm(value)

    investor = _norm(row.get("investor"))
    return (
        signal.company_id,
        str(signal.event_type or "").upper(),
        event_day,
        number(row.get("quantity") if row.get("quantity") is not None else signal.quantity),
        number(row.get("price") if row.get("price") is not None else signal.price),
        number(row.get("value") if row.get("value") is not None else signal.amount_inr),
        number(row.get("stake") if row.get("stake") is not None else None),
        investor,
    )


def _capital_key(row: dict[str, Any], company_id: int | None = None) -> str:
    """Build a source-independent id for the same capital-action disclosure.

    Exchange filings and Trendlyne often describe the same event with different URLs
    and upstream ids. Using those source ids as the DB key creates duplicate rows.
    This identity deliberately excludes the source and uses stable event attributes.
    """
    event_type = _norm(row.get("event_type"))
    company_identity = str(company_id) if company_id is not None else _norm(
        row.get("company_name") or row.get("stock_name") or row.get("nse_symbol") or row.get("bse_scrip_code")
    )
    investor = _norm(row.get("investor_name"))
    action = _norm(row.get("action"))
    event_date = row.get("event_date") or row.get("transaction_date")
    if hasattr(event_date, "date"):
        event_date = event_date.date()
    quantity = row.get("quantity")
    if quantity not in (None, ""):
        try:
            quantity = round(float(quantity), 4)
        except (TypeError, ValueError):
            quantity = _norm(quantity)
    else:
        quantity = ""
    price = row.get("price")
    if price not in (None, ""):
        try:
            price = round(float(price), 6)
        except (TypeError, ValueError):
            price = _norm(price)
    else:
        price = ""
    basis = "|".join(
        [str(event_type), company_identity, str(investor), str(event_date or ""), str(action), str(quantity), str(price)]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class SignalOut(BaseModel):
    id: int
    stock: str
    client_name: str
    client_category: str | None
    action: str | None
    date: datetime
    quantity: float | None
    post_transaction_holding: float | None
    holding_before: float | None
    traded_percent: float | None
    percent_change: float | None
    avg_price: float | None
    current_price: float | None
    value: float | None
    period: str | None
    mode: str | None
    company: str
    lt: str
    ht: str
    track: str
    priority: int
    analyst: str | None
    market_cap: float | None
    market_cap_eligible: bool
    category_eligible: bool
    history_eligible: bool
    source: str | None = None
    source_url: str | None = None
    regulation: str | None = None
    submission_type: str | None = None
    revision_remarks: str | None = None
    broadcast_at: datetime | None = None
    nse_symbol: str | None = None
    stock_price_change_percent: float | None = None
    cmp_vs_avg_transaction_price_pct: float | None = None


class CapitalActionOut(BaseModel):
    id: int
    event_type: str
    stock: str
    investor: str | None
    investor_category: str | None
    investor_type: str | None = None
    promoter_pac: str | None = None
    action: str | None
    date: datetime
    quantity: float | None
    price: float | None
    issue_price: float | None = None
    value: float | None
    issue_size: float | None = None
    transaction_value: float | None = None
    current_price: float | None = None
    current_price_change_percent: float | None = None
    quote_source: str | None = None
    quote_trade_date: str | None = None
    source: str
    stake_percent: float | None = None
    pre_holding_pct: float | None = None
    post_holding_pct: float | None = None
    holding_change_pct: float | None = None
    threshold_state: str | None = None
    severity: str | None = None
    importance_score: float | None = None
    confidence: float | str | None = None
    purpose: str | None = None
    event_sub_type: str | None = None
    shares_acquired: float | None = None
    shares_disposed: float | None = None
    shares_issued: float | None = None
    warrant_count: float | None = None
    isin: str | None = None
    event_chain_key: str | None = None
    source_url: str | None
    title: str | None
    details: str | None
    document_url: str | None = None
    nse_symbol: str | None = None
    document_status: str | None = None
    document_available: bool = False
    transaction_status: str | None = None
    classification_status: str | None = None
    review_reason: str | None = None
    company: str
    track: str
    priority: int
    analyst: str | None = None
    assessment: str | None = None
    assessment_provider: str | None = None
    assessment_confidence: int | None = None
    assessment_review_required: bool | None = None
    assessment_rationale: str | None = None
    missing_information: str | None = None


class CapitalActionDetailOut(BaseModel):
    id: int
    event_type: str
    stock: str
    company: str
    nse_symbol: str | None
    investor: str | None
    investor_category: str | None
    action: str | None
    date: datetime
    quantity: float | None
    price: float | None
    value: float | None
    source: str
    source_url: str | None
    document_url: str | None
    document_type: str | None
    document_status: str | None
    document_sha256: str | None
    document_pages: int | None
    document_bytes: int | None
    announcement_subject: str | None
    announcement_details: str | None
    parsed: dict[str, Any]
    document_summary: str | None
    document_text: str | None
    raw_nse_row: dict[str, Any] | None
    transaction_status: str = "REVIEW"
    classification_status: str = "CLASSIFIED"
    review_reason: str | None = None
    track: str
    priority: int
    analyst: str | None


async def _load_company_indexes(db):
    all_companies = (await db.execute(select(Company))).scalars().all()
    by_id = {company.id: company for company in all_companies}
    by_symbol = {_norm(company.nse_symbol): company for company in all_companies if _norm(company.nse_symbol)}
    by_name = {_norm(company.legal_name): company for company in all_companies if _norm(company.legal_name)}
    return all_companies, by_id, by_symbol, by_name


def _find_existing_company(
    item: dict[str, Any],
    companies: list[Company],
    by_symbol: dict[str, Company],
    by_name: dict[str, Company],
) -> Company | None:
    symbol = _norm(item.get("nse_symbol"))
    if symbol and symbol in by_symbol:
        return by_symbol[symbol]
    bse_code = _norm(item.get("bse_scrip_code") or item.get("bse_code"))
    if bse_code:
        exact_bse = [
            company for company in companies
            if _norm(getattr(company, "bse_code", None)) == bse_code
        ]
        if len(exact_bse) == 1:
            return exact_bse[0]
    name = _norm(item.get("company_name") or item.get("stock_name"))
    if not name:
        return None
    if name in by_name:
        return by_name[name]
    exact = [company for company in companies if name in _company_terms(company)]
    if len(exact) == 1:
        return exact[0]
    fuzzy = [
        company
        for company in companies
        if any(len(term) >= 6 and (name in term or term in name) for term in _company_terms(company))
    ]
    return fuzzy[0] if len(fuzzy) == 1 else None


def _new_company_from_source(
    db,
    item: dict[str, Any],
    companies: list[Company],
    by_symbol: dict[str, Company],
    by_name: dict[str, Company],
) -> Company | None:
    name = str(item.get("company_name") or item.get("stock_name") or "").strip()
    if not name:
        name = str(item.get("nse_symbol") or "").strip()
    if not name:
        return None

    existing = _find_existing_company(item, companies, by_symbol, by_name)
    if existing is not None:
        return existing

    symbol = str(item.get("nse_symbol") or "").strip().upper() or None
    if symbol and symbol in by_symbol:
        symbol = None

    # Company.legal_name is unique; use the source name and preserve the source
    # symbol when it is not already claimed by another company.
    company = Company(
        legal_name=name[:255],
        nse_symbol=symbol[:40] if symbol else None,
        aliases=[],
    )
    db.add(company)
    companies.append(company)
    by_name[_norm(company.legal_name)] = company
    if company.nse_symbol:
        by_symbol[_norm(company.nse_symbol)] = company
    return company


def _transaction_direction(action: str | None) -> str:
    value = _norm(action)
    if any(token in value for token in ("sale", "sell", "disposal", "disposed")):
        return "sold"
    if any(token in value for token in ("buy", "purchase", "acquisition", "acquired")):
        return "bought"
    return "unknown"


def _derive_holding_before(action: str | None, quantity: float | None, post_holding: float | None) -> float | None:
    if quantity is None or post_holding is None:
        return None
    direction = _transaction_direction(action)
    if direction == "bought":
        return max(0.0, float(post_holding) - float(quantity))
    if direction == "sold":
        return float(post_holding) + float(quantity)
    return None


def _derive_percent_change(action: str | None, traded_percent: float | None) -> float | None:
    if traded_percent is None:
        return None
    direction = _transaction_direction(action)
    value = abs(float(traded_percent))
    if direction == "sold":
        return -value
    if direction == "bought":
        return value
    return value


def _fill_numeric_pair(quantity: float | None, price: float | None, value: float | None) -> tuple[float | None, float | None]:
    if price is None and value is not None and quantity not in (None, 0):
        price = float(value) / float(quantity)
    if value is None and price is not None and quantity is not None:
        value = float(price) * float(quantity)
    return price, value


def _signal_identity(
    company_id: int,
    person_name: str | None,
    transaction_date: datetime | None,
    action: str | None,
    quantity: float | None,
    period: str | None = None,
    transaction_mode: str | None = None,
    regulation: str | None = None,
    client_category: str | None = None,
    security_type: str | None = None,
    report_date: datetime | None = None,
) -> tuple[Any, ...]:
    # Preserve distinct source rows (e.g. Insider Trading vs SAST) while
    # remaining stable across repeated refreshes of the same row.
    return (
        company_id,
        _norm(person_name),
        transaction_date.date() if transaction_date else None,
        _action_key(action),
        round(float(quantity), 4) if quantity is not None else None,
        _norm(period),
        _norm(transaction_mode),
        _norm(regulation),
        _norm(client_category),
        _norm(security_type),
        report_date.date() if report_date else None,
    )


def _canonical_company_name(value: Any) -> str:
    text = _norm(value)
    # Remove legal suffixes without collapsing meaningful company names.
    tokens = [token for token in text.split() if token not in {
        "limited", "ltd", "private", "pvt", "company", "co", "corporation", "corp"
    }]
    return " ".join(tokens)


def _match_coverage_spec(item: dict[str, Any], master_specs: list[dict[str, Any]]) -> dict[str, Any] | None:
    symbol = _norm(item.get("nse_symbol"))
    if symbol:
        exact = [m for m in master_specs if _norm(m.get("nse_symbol")) == symbol]
        if len(exact) == 1:
            return exact[0]

    stock_name = _canonical_company_name(item.get("company_name") or item.get("stock_name"))
    if not stock_name:
        return None

    exact = [m for m in master_specs if _canonical_company_name(m.get("legal_name")) == stock_name]
    if len(exact) == 1:
        return exact[0]

    fuzzy = []
    for master in master_specs:
        candidate = _canonical_company_name(master.get("legal_name"))
        if len(candidate) >= 6 and (stock_name in candidate or candidate in stock_name):
            fuzzy.append(master)
    return fuzzy[0] if len(fuzzy) == 1 else None


def _regulatory_date_window(history_days: int) -> tuple[date, date]:
    """Return the same inclusive date range used by the NSE collector."""
    return history_window(history_days)


def _date_floor(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=NSE_TIMEZONE)


def _parse_source_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_recent_insider_disclosure(
    transaction_date: datetime | None, broadcast_date: Any, cutoff: datetime, end_exclusive: datetime,
) -> bool:
    """Window PIT records by disclosure date, even when the trade date is missing.

    The trade date describes the underlying transaction; the collection window is
    about when NSE broadcast the disclosure. Legacy rows without a broadcast date
    fall back to their trade date. The caller may use the disclosure date as a
    documented last-resort display date if a source row omitted the trade date.
    """
    disclosure_date = _parse_source_datetime(broadcast_date) or transaction_date
    return disclosure_date is not None and cutoff <= disclosure_date < end_exclusive


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        return number if number == number and number not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _canonical_json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalise_filing_url(value: Any) -> str | None:
    text = str(value or "").strip()
    return text.rstrip("/") if text else None


async def _persist_regulatory_observations(
    db,
    raw_filings: list[dict[str, Any]] | None,
    window_stats: list[dict[str, Any]] | None,
    company_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist auditable raw NSE payloads plus one outcome row per requested date window."""
    raw_filings = raw_filings or []
    window_stats = window_stats or []
    company_by_symbol = company_by_symbol or {}
    now = datetime.now(timezone.utc)
    prepared: list[tuple[dict[str, Any], str, str, tuple[str, str, str] | None]] = []
    window_counts: dict[tuple[str, str, str], dict[str, int]] = {}

    for original in raw_filings:
        item = dict(original)
        source = str(item.get("source") or "NSE_UNKNOWN")[:40]
        symbol = str(item.get("nse_symbol") or "").strip().upper() or None
        raw_payload = item.get("raw_payload") if isinstance(item.get("raw_payload"), dict) else {"payload": item.get("raw_payload")}
        source_key = str(item.get("source_key") or "").strip()
        if not source_key:
            source_key = _canonical_json_hash({"source": source, "symbol": symbol, "payload": raw_payload})
        content_hash = _canonical_json_hash(raw_payload)
        ws = str(item.get("window_start") or "")[:10]
        we = str(item.get("window_end") or "")[:10]
        window_key = (source, ws, we) if ws and we else None
        if window_key:
            window_counts.setdefault(window_key, {"inserted": 0, "updated": 0})
        prepared.append((item, source_key[:64], content_hash, window_key))

    keys = list(dict.fromkeys(row[1] for row in prepared))
    existing_by_key: dict[str, RegulatoryFiling] = {}
    # Keep IN clauses bounded for SQLite and PostgreSQL alike.
    for offset in range(0, len(keys), 400):
        batch = keys[offset:offset + 400]
        if batch:
            existing = (await db.execute(select(RegulatoryFiling).where(RegulatoryFiling.source_key.in_(batch)))).scalars().all()
            existing_by_key.update({str(row.source_key): row for row in existing})

    inserted = updated = unchanged = 0
    for item, source_key, content_hash, window_key in prepared:
        source = str(item.get("source") or "NSE_UNKNOWN")[:40]
        symbol = str(item.get("nse_symbol") or "").strip().upper() or None
        raw_payload = item.get("raw_payload") if isinstance(item.get("raw_payload"), dict) else {"payload": item.get("raw_payload")}
        company = company_by_symbol.get(_norm(symbol)) if symbol else None
        filing_date = _parse_source_datetime(item.get("filing_date"))
        window_start = _parse_source_datetime(item.get("window_start"))
        window_end = _parse_source_datetime(item.get("window_end"))
        row = existing_by_key.get(source_key)
        if row is None:
            row = RegulatoryFiling(
                source=source, source_key=source_key,
                source_filing_id=str(item.get("source_filing_id") or "")[:255] or None,
                company_id=int(company["company_id"]) if company and company.get("company_id") is not None else None,
                nse_symbol=symbol, company_name=str(item.get("company_name") or "")[:255] or None,
                filing_date=filing_date, source_url=str(item.get("source_url") or "")[:1000] or None,
                raw_payload=raw_payload, content_hash=content_hash, first_seen_at=now, last_seen_at=now,
                processing_status="RECEIVED", classification_status="UNCLASSIFIED",
                window_start=window_start, window_end=window_end,
            )
            db.add(row)
            existing_by_key[source_key] = row
            inserted += 1
            if window_key: window_counts[window_key]["inserted"] += 1
        else:
            changed = str(row.content_hash or "") != content_hash
            row.last_seen_at = now
            row.source_filing_id = str(item.get("source_filing_id") or "")[:255] or row.source_filing_id
            row.company_id = int(company["company_id"]) if company and company.get("company_id") is not None else row.company_id
            row.nse_symbol = symbol or row.nse_symbol
            row.company_name = str(item.get("company_name") or "")[:255] or row.company_name
            row.filing_date = filing_date or row.filing_date
            row.source_url = str(item.get("source_url") or "")[:1000] or row.source_url
            row.window_start, row.window_end = window_start or row.window_start, window_end or row.window_end
            if changed:
                row.raw_payload = raw_payload
                row.content_hash = content_hash
                row.changed_at = now
                row.processing_status = "UPDATED"
                updated += 1
                if window_key: window_counts[window_key]["updated"] += 1
            else:
                unchanged += 1

    await db.flush()

    by_url: dict[tuple[str, str, str], int] = {}
    by_filing_id: dict[tuple[str, str, str], int] = {}
    by_symbol_day: dict[tuple[str, str, str], list[int]] = {}
    for item, source_key, _content_hash, _window_key in prepared:
        row = existing_by_key[source_key]
        symbol = str(item.get("nse_symbol") or "").strip().upper()
        source = str(item.get("source") or "NSE_UNKNOWN")[:40]
        source_url = _normalise_filing_url(item.get("source_url"))
        filing_id = str(item.get("source_filing_id") or "").strip()
        if symbol and source_url:
            by_url[(source, symbol, source_url)] = int(row.id)
        if symbol and filing_id:
            by_filing_id[(source, symbol, filing_id)] = int(row.id)
        filing_dt = _parse_source_datetime(item.get("filing_date"))
        if symbol and filing_dt:
            key = (source, symbol, filing_dt.date().isoformat())
            by_symbol_day.setdefault(key, []).append(int(row.id))

    for stat in window_stats:
        source = str(stat.get("source") or "NSE_UNKNOWN")[:40]
        ws = str(stat.get("from_date") or "")[:10]
        we = str(stat.get("to_date") or "")[:10]
        window_key = (source, ws, we)
        duration_ms = max(0, int(stat.get("duration_ms") or 0))
        finished_at = datetime.now(timezone.utc)
        started_at = finished_at - timedelta(milliseconds=duration_ms)
        status = str(stat.get("status") or "FAILED").upper()
        db.add(RegulatoryCollectorRun(
            source=source, window_start=_parse_source_datetime(ws), window_end=_parse_source_datetime(we),
            started_at=started_at, finished_at=finished_at, status=status,
            http_status=(int(stat.get("http_status")) if stat.get("http_status") is not None else (200 if status in {"SUCCESS", "OK", "EMPTY", "EMPTY_UNVERIFIED"} else None)),
            records_received=max(0, int(stat.get("records_received") or 0)),
            records_inserted=window_counts.get(window_key, {}).get("inserted", 0),
            records_updated=window_counts.get(window_key, {}).get("updated", 0),
            content_hash=str(stat.get("content_hash") or "")[:64] or None, duration_ms=duration_ms,
            error_message=str(stat.get("error") or "")[:10000] or None, created_at=finished_at,
        ))

    await db.flush()
    logger.info(
        "REGULATORY_RAW_FILINGS_PERSISTED source_count=%s received=%s inserted=%s updated=%s unchanged=%s windows=%s failed_windows=%s",
        len({str(i.get("source") or "NSE_UNKNOWN") for i in raw_filings}), len(raw_filings), inserted, updated, unchanged,
        len(window_stats), sum(1 for s in window_stats if str(s.get("status") or "").upper() == "FAILED"),
    )
    return {
        "inserted": inserted, "updated": updated, "unchanged": unchanged,
        "by_url": by_url, "by_filing_id": by_filing_id, "by_symbol_day": by_symbol_day,
    }


def _linked_filing_id(
    mapping: dict[str, Any], source: str, symbol: Any, source_url: Any = None, filing_ids: list[Any] | None = None, filing_date: Any = None,
) -> int | None:
    symbol_key = str(symbol or "").strip().upper()
    if not symbol_key:
        return None
    for candidate_url in [source_url, *(filing_ids or [])]:
        normalized = _normalise_filing_url(candidate_url)
        if normalized:
            found = mapping.get("by_url", {}).get((source, symbol_key, normalized))
            if found:
                return found
    for candidate_id in filing_ids or []:
        value = str(candidate_id or "").strip()
        if value:
            found = mapping.get("by_filing_id", {}).get((source, symbol_key, value))
            if found:
                return found
    parsed_date = _parse_source_datetime(filing_date)
    if parsed_date:
        choices = mapping.get("by_symbol_day", {}).get((source, symbol_key, parsed_date.date().isoformat()), [])
        if len(set(choices)) == 1:
            return choices[0]
    return None


async def _latest_market_prices_by_symbol(db, symbols: list[str] | set[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Fallback quote enrichment from the durable market_prices history table."""
    cleaned = list(dict.fromkeys(str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()))
    if not cleaned:
        return {}
    latest = (
        select(
            RegulatoryMarketPrice.nse_symbol.label("nse_symbol"),
            func.max(RegulatoryMarketPrice.trade_date).label("latest_trade_date"),
        )
        .where(RegulatoryMarketPrice.nse_symbol.in_(cleaned))
        .group_by(RegulatoryMarketPrice.nse_symbol)
        .subquery()
    )
    rows = (await db.execute(
        select(RegulatoryMarketPrice).join(
            latest, and_(
                RegulatoryMarketPrice.nse_symbol == latest.c.nse_symbol,
                RegulatoryMarketPrice.trade_date == latest.c.latest_trade_date,
            )
        )
    )).scalars().all()
    return {
        str(row.nse_symbol).upper(): {
            "last_price": row.current_price, "previous_close": row.previous_close,
            "change_percent": row.change_percent,
            "trade_date": row.trade_date.isoformat() if row.trade_date else None,
            "source": row.source, "source_url": row.source_url,
        }
        for row in rows
    }


def _nse_source_data(signal: InsiderSignal) -> dict[str, Any]:
    """Return a view-model built from normalized relational columns (not a stored blob)."""
    return {
        "holding_before": signal.holding_before,
        "holding_before_percent": signal.holding_before_percent,
        "current_price": signal.current_price,
        "current_price_change_percent": signal.current_price_change_percent,
        "current_price_fetched_at": signal.current_price_fetched_at.isoformat() if signal.current_price_fetched_at else None,
    }


def _partition_nse_coverage(master_specs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Separate NSE-addressable Coverage rows from valid non-NSE/unconfigured rows.

    A single BSE-only or unconfigured Coverage row must never prevent the NSE desk
    from scanning the other Coverage companies. Only an empty NSE-eligible set is
    a configuration failure.
    """
    nse_specs: list[dict[str, Any]] = []
    skipped: list[str] = []
    for master in master_specs:
        symbol = _norm(master.get("nse_symbol"))
        if symbol:
            nse_specs.append(master)
        else:
            skipped.append(str(master.get("legal_name") or master.get("company_id") or "Unknown"))
    skipped.sort(key=str.casefold)
    return nse_specs, skipped


async def sync_regulatory_data(db, history_days: int = MAX_HISTORY_DAYS) -> dict[str, Any]:
    """Refresh Regulation 7(2) data with incremental durable commits.

    The collector works in small NSE-symbol batches. Each completed batch is
    committed before the next batch starts, so a seven-year bootstrap does not hold
    the entire desk in one uncommitted transaction. Successful rows are visible to
    the FE immediately while failed company windows remain retryable.
    """
    history_days = max(1, min(int(history_days), MAX_HISTORY_DAYS))
    history_start, history_end = _regulatory_date_window(history_days)
    cutoff = _date_floor(history_start)
    end_exclusive = _date_floor(history_end + timedelta(days=1))

    master_rows = (await db.execute(_all_nse_master_query())).all()
    master_specs = [
        {
            "company_id": int(company.id),
            "legal_name": company.legal_name,
            "nse_symbol": company.nse_symbol,
            "bse_code": company.bse_code,
            "isin": company.isin,
            "aliases": list(company.aliases or []),
            "track_mode": target.track_mode.value if target else TrackMode.NT.value,
            "analyst": analyst.username if analyst else None,
        }
        for company, target, analyst in master_rows
    ]
    nse_master_specs, skipped_nse_companies = _partition_nse_coverage(master_specs)
    if skipped_nse_companies:
        logger.warning(
            "NSE Regulatory company-master rows skipped (no NSE symbol): %s",
            ", ".join(skipped_nse_companies[:50]),
        )

    if not nse_master_specs:
        source_errors = {"NSE Universe": "No NSE-listed companies are available in the local company master."}
        await mark_running(db, REGULATORY_DESK, history_days=history_days, from_date=history_start.isoformat(), to_date=history_end.isoformat())
        await mark_finished(
            db, REGULATORY_DESK, status="configuration_required", history_days=history_days,
            from_date=history_start.isoformat(), to_date=history_end.isoformat(),
            scanned=0, stored=0, complete=False, errors=source_errors,
        )
        await db.commit()
        return {
            "status": "configuration_required", "source": "NSE", "sources": REFERENCE_SOURCES,
            "history_days": history_days, "from_date": history_start.isoformat(), "to_date": history_end.isoformat(),
            "date_range_inclusive": True, "master_companies": len(master_specs), "nse_companies": 0,
            "missing_nse_symbols": skipped_nse_companies, "skipped_nse_companies": skipped_nse_companies,
            "source_scanned": {"NSE": 0}, "source_created": {"NSE": 0}, "source_errors": source_errors,
            "scanned": 0, "stored": 0, "created": 0, "updated": 0, "skipped": 0, "filtered": 0,
            "unresolved": 0, "coverage_companies_with_rows": 0, "coverage_companies_without_rows": skipped_nse_companies,
            "scrape_status": "configuration_required", "complete": False,
            "priority_order": ["PO", "HT", "LT", "Others"],
        }

    symbols = [str(m["nse_symbol"]).strip().upper() for m in nse_master_specs]
    master_by_symbol = {_norm(m["nse_symbol"]): m for m in nse_master_specs}
    source_errors: dict[str, str] = {}
    symbol_stats: list[dict[str, Any]] = []
    coverage_counts = {m["company_id"]: 0 for m in nse_master_specs}
    totals = {"scanned": 0, "stored": 0, "created": 0, "updated": 0, "filtered": 0, "unresolved": 0}
    batch_commit_count = 0
    raw_window_commit_count = 0
    observation_map: dict[str, Any] = {"by_url": {}, "by_filing_id": {}, "by_symbol_day": {}}

    async with _REGULATORY_SYNC_LOCK:
        await mark_running(
            db, REGULATORY_DESK, history_days=history_days,
            from_date=history_start.isoformat(), to_date=history_end.isoformat(),
        )
        await db.commit()

        async def on_batch(batch_payload, batch_start: int, total_symbols: int) -> None:
            nonlocal batch_commit_count
            batch_matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
            batch_completed_symbols: list[str] = []
            batch_seen: set[str] = set()
            batch_filtered = 0
            batch_unresolved = 0
            batch_received_rows = 0

            for symbol, rows, stats in batch_payload:
                symbol_key = _norm(symbol)
                errors = list(stats.get("errors") or [])
                if errors:
                    source_errors.setdefault(symbol, "; ".join(str(item) for item in errors))
                else:
                    batch_completed_symbols.append(symbol_key)
                symbol_stats.append(stats)
                totals["scanned"] += int(stats.get("raw_rows") or 0)

                for item in rows:
                    batch_received_rows += 1
                    tx_date = item.get("transaction_date")
                    broadcast_date = item.get("broadcast_date")
                    # Do not reject a recent disclosure solely because the attachment
                    # omitted its trade date. Prefer the actual trade date; if absent,
                    # store the broadcast date as an explicitly recorded fallback so
                    # the existing non-null transaction_date column can retain the row.
                    if not _is_recent_insider_disclosure(
                        tx_date, broadcast_date, cutoff, end_exclusive,
                    ):
                        totals["filtered"] += 1
                        batch_filtered += 1
                        continue
                    effective_tx_date = tx_date or _parse_source_datetime(broadcast_date)
                    if effective_tx_date is None:
                        totals["filtered"] += 1
                        batch_filtered += 1
                        logger.warning("NSE PIT row omitted both transaction date and broadcast date symbol=%s source_key=%s", symbol, item.get("source_key"))
                        continue
                    if tx_date is None:
                        source_data = item.get("source_data") if isinstance(item.get("source_data"), dict) else {}
                        source_data["transaction_date_inferred_from_broadcast"] = True
                        item["source_data"] = source_data
                    master = master_by_symbol.get(symbol_key)
                    if master is None:
                        totals["unresolved"] += 1
                        batch_unresolved += 1
                        continue
                    source_key = str(item.get("source_key") or "").strip()
                    if not source_key:
                        source_key = hashlib.sha256(
                            "|".join([
                                str(master["company_id"]), _norm(item.get("person_name")), str(tx_date),
                                _norm(item.get("action")), str(item.get("quantity") or ""), _norm(item.get("source_url")),
                            ]).encode("utf-8")
                        ).hexdigest()
                        item["source_key"] = source_key
                    if source_key in batch_seen:
                        continue
                    batch_seen.add(source_key)
                    batch_matched.append((item, master))
                    coverage_counts[master["company_id"]] += 1

            # Use stable source keys for upserts. Do not wholesale delete/reinsert a
            # company's history: that changes row IDs and can orphan linked notifications.
            completed_symbols = set(batch_completed_symbols)
            completed_company_ids = [
                int(master_by_symbol[s]["company_id"])
                for s in completed_symbols if s in master_by_symbol
            ]
            seen_keys_by_company: dict[int, set[str]] = {}
            source_keys = []
            for item, master in batch_matched:
                company_id = int(master["company_id"])
                key = str(item.get("source_key") or "")[:64]
                if key:
                    source_keys.append(key)
                    seen_keys_by_company.setdefault(company_id, set()).add(key)
            existing_by_key: dict[str, InsiderSignal] = {}
            if source_keys:
                for existing_row in (await db.execute(
                    select(InsiderSignal).where(InsiderSignal.source_key.in_(list(dict.fromkeys(source_keys))))
                )).scalars().all():
                    if existing_row.source_key:
                        existing_by_key[str(existing_row.source_key)] = existing_row

            # Refresh the symbol-level NSE quote on previously stored PIT rows. This
            # keeps "Current Price" current across four-hour runs even for companies
            # that did not file a new Regulation 7(2) transaction in this window.
            current_price_updates: dict[int, tuple[float, str]] = {}
            fetched_at = datetime.now(NSE_TIMEZONE).isoformat()
            for stat in symbol_stats:
                symbol_key = _norm(stat.get("symbol"))
                current_price = stat.get("current_price")
                if symbol_key in master_by_symbol and current_price not in (None, ""):
                    try:
                        current_price_updates[master_by_symbol[symbol_key]["company_id"]] = (float(current_price), fetched_at)
                    except (TypeError, ValueError):
                        pass
            if current_price_updates:
                existing_signal_rows = (await db.execute(
                    select(InsiderSignal).where(
                        InsiderSignal.source == "NSE",
                        InsiderSignal.company_id.in_(list(current_price_updates)),
                    )
                )).scalars().all()
                for existing_signal in existing_signal_rows:
                    update = current_price_updates.get(existing_signal.company_id)
                    if not update:
                        continue
                    existing_signal.current_price = update[0]
                    existing_signal.current_price_fetched_at = datetime.fromisoformat(update[1])

            inserted = 0
            updated = 0
            for item, master in batch_matched:
                key = str(item.get("source_key") or "")[:64]
                if not key:
                    continue
                symbol_key = _norm(master.get("nse_symbol"))
                row = existing_by_key.get(key)
                # On an incomplete company fetch, do not overwrite facts already stored;
                # a previously unseen filing can still be recorded and will reconcile later.
                if row is not None and symbol_key not in completed_symbols:
                    continue
                quantity = item.get("quantity")
                price, amount = _fill_numeric_pair(quantity, item.get("price"), item.get("security_value"))
                if amount is None:
                    amount = item.get("buy_value") or item.get("sell_value")
                if price is None and amount is not None and quantity not in (None, 0):
                    price = float(amount) / float(quantity)
                source_data = item.get("source_data") if isinstance(item.get("source_data"), dict) else {}
                quote_update = current_price_updates.get(int(master["company_id"]))
                stored_current_price = _as_float(source_data.get("current_price"))
                if stored_current_price is None and quote_update:
                    stored_current_price = quote_update[0]
                stored_change_percent = _as_float(source_data.get("current_price_change_percent"))
                if stored_change_percent is None and row is not None:
                    stored_change_percent = row.current_price_change_percent
                fetched_at = _parse_source_datetime(source_data.get("current_price_fetched_at"))
                if fetched_at is None and quote_update:
                    fetched_at = _parse_source_datetime(quote_update[1])
                values = {
                    "company_id": int(master["company_id"]),
                    "stock_name": str(item.get("company_name") or master["legal_name"])[:255],
                    "person_name": str(item.get("person_name") or "N/D")[:255],
                    "client_category": str(item.get("client_category") or "N/D")[:120],
                    "action": str(item.get("action") or "N/D")[:120],
                    "transaction_date": effective_tx_date,
                    "quantity": quantity,
                    "post_transaction_holding": item.get("post_transaction_holding"),
                    "traded_percent": item.get("percent_change"),
                    "price": price,
                    "amount_inr": amount,
                    "period": str(item.get("period") or "N/D")[:120],
                    "transaction_mode": str(item.get("transaction_mode") or "N/D")[:120],
                    "market_cap": float(master.get("market_cap") or 0.0),
                    "source": "NSE",
                    "source_key": key,
                    "source_url": item.get("source_url"),
                    "holding_before": item.get("holding_before"),
                    "holding_before_percent": item.get("holding_before_percent"),
                    "current_price": stored_current_price,
                    "current_price_change_percent": stored_change_percent,
                    "current_price_fetched_at": fetched_at,
                    "regulation": str(item.get("regulation") or "")[:80] or None,
                    "submission_type": str(item.get("submission_type") or "")[:80] or None,
                    "revision_remarks": str(item.get("revision_remarks") or "")[:255] or None,
                    "broadcast_at": _parse_source_datetime(item.get("broadcast_date")),
                    "nse_symbol": str(item.get("nse_symbol") or master["nse_symbol"])[:40],
                    "nse_pid": str(item.get("nse_pid") or "")[:80] or None,
                    "nse_did": str(item.get("nse_did") or "")[:80] or None,
                }
                if row is None:
                    row = InsiderSignal(**values)
                    db.add(row)
                    existing_by_key[key] = row
                    inserted += 1
                else:
                    changed = any(getattr(row, field) != value for field, value in values.items() if field != "source_key")
                    for field, value in values.items():
                        setattr(row, field, value)
                    if changed:
                        updated += 1

            # Only a fully successful symbol capture can remove rows missing from that
            # source response. Scope cleanup to the requested seven-year/incremental window.
            if completed_company_ids:
                stale_rows = (await db.execute(
                    select(InsiderSignal).where(
                        InsiderSignal.source == "NSE",
                        InsiderSignal.company_id.in_(completed_company_ids),
                        or_(
                            and_(InsiderSignal.broadcast_at.is_not(None),
                                 InsiderSignal.broadcast_at >= cutoff,
                                 InsiderSignal.broadcast_at < end_exclusive),
                            and_(InsiderSignal.broadcast_at.is_(None),
                                 InsiderSignal.transaction_date >= cutoff,
                                 InsiderSignal.transaction_date < end_exclusive),
                        ),
                    )
                )).scalars().all()
                stale_ids = [
                    row.id for row in stale_rows
                    if str(row.source_key or "") not in seen_keys_by_company.get(int(row.company_id), set())
                ]
                if stale_ids:
                    await db.execute(delete(InsiderSignal).where(InsiderSignal.id.in_(stale_ids)))

            if inserted or updated or completed_company_ids:
                await db.flush()
            totals["created"] = totals.get("created", 0) + inserted
            totals["updated"] = totals.get("updated", 0) + updated
            totals["stored"] += inserted + updated
            batch_commit_count += 1
            await db.commit()
            logger.info(
                "NSE Regulatory batch persisted batch_start=%s batch_size=%s total_symbols=%s received_rows=%s filtered=%s unresolved=%s matched=%s created_batch=%s updated_batch=%s stored_total=%s completed_symbols=%s errors=%s",
                batch_start, len(batch_payload), total_symbols, batch_received_rows, batch_filtered, batch_unresolved,
                len(batch_matched), inserted, updated, totals["stored"], batch_completed_symbols, list(source_errors),
            )

        async def on_raw_window(raw_filings: list[dict[str, Any]], window_stat: dict[str, Any]) -> None:
            """Commit each NSE PIT raw window before potentially slow detail parsing."""
            nonlocal raw_window_commit_count
            mapping = await _persist_regulatory_observations(
                db, raw_filings, [window_stat], master_by_symbol
            )
            await db.commit()
            raw_window_commit_count += 1
            for mapping_key in ("by_url", "by_filing_id"):
                observation_map[mapping_key].update(mapping.get(mapping_key) or {})
            for key, ids in (mapping.get("by_symbol_day") or {}).items():
                target_ids = observation_map["by_symbol_day"].setdefault(key, [])
                for filing_id in ids:
                    if filing_id not in target_ids:
                        target_ids.append(filing_id)

        try:
            collection = await NSEInsiderTradingCollector().collect(
                history_days=history_days,
                symbols=symbols,
                on_batch=on_batch,
                on_raw_window=on_raw_window,
            )
            if batch_commit_count == 0 and collection.get("rows"):
                stats_by_symbol = {
                    _norm(item.get("symbol")): item
                    for item in (collection.get("symbol_stats") or [])
                    if _norm(item.get("symbol"))
                }
                fallback_payload = []
                for symbol in symbols:
                    symbol_rows = [
                        row for row in (collection.get("rows") or [])
                        if _norm(row.get("nse_symbol")) == _norm(symbol)
                    ]
                    fallback_payload.append((symbol, symbol_rows, stats_by_symbol.get(_norm(symbol), {"symbol": symbol, "errors": []})))
                await on_batch(fallback_payload, 0, len(symbols))
        except Exception as exc:
            logger.exception("NSE Regulatory PIT collection failed")
            source_errors["NSE PIT"] = str(exc)
            failed_at = datetime.now(timezone.utc)
            db.add(RegulatoryCollectorRun(
                source="NSE_PIT", window_start=_date_floor(history_start), window_end=_date_floor(history_end),
                started_at=failed_at, finished_at=failed_at, status="FAILED", http_status=None,
                records_received=0, records_inserted=0, records_updated=0, duration_ms=None,
                error_message=f"Collector aborted before returning window diagnostics: {type(exc).__name__}: {exc}"[:10000],
                created_at=failed_at,
            ))
            await mark_finished(
                db, REGULATORY_DESK, status="source_error", history_days=history_days,
                from_date=history_start.isoformat(), to_date=history_end.isoformat(),
                scanned=totals["scanned"], stored=totals["stored"], complete=False, errors=source_errors,
            )
            await db.commit()
            return {
                "status": "source_error", "source": "NSE", "sources": REFERENCE_SOURCES,
                "history_days": history_days, "from_date": history_start.isoformat(), "to_date": history_end.isoformat(),
                "master_companies": len(master_specs), "nse_companies": len(nse_master_specs),
                "missing_nse_symbols": skipped_nse_companies, "skipped_nse_companies": skipped_nse_companies,
                "scanned": totals["scanned"], "stored": totals["stored"], "created": totals["created"], "updated": totals["updated"],
                "source_errors": source_errors, "complete": False, "symbol_stats": symbol_stats,
            }

    # Preserve the exact NSE filing payloads and per-window outcomes separately from
    # the normalized transaction table. Stable source IDs let the same row be updated
    # in place when NSE revises a filing.
    if raw_window_commit_count == 0:
        # Defensive fallback for empty company-universe/legacy collector paths.
        observation_map = await _persist_regulatory_observations(
            db, collection.get("raw_filings"), collection.get("window_stats"), master_by_symbol
        )
        await db.commit()
    transaction_items = {str(item.get("source_key") or ""): item for item in (collection.get("rows") or []) if item.get("source_key")}
    transaction_keys = list(transaction_items)
    for offset in range(0, len(transaction_keys), 400):
        key_batch = transaction_keys[offset:offset + 400]
        if not key_batch:
            continue
        signals = (await db.execute(select(InsiderSignal).where(InsiderSignal.source_key.in_(key_batch)))).scalars().all()
        for signal in signals:
            item = transaction_items.get(str(signal.source_key)) or {}
            source_data = item.get("source_data") if isinstance(item.get("source_data"), dict) else {}
            listing_row = source_data.get("listing_row") if isinstance(source_data.get("listing_row"), dict) else {}
            filing_id = _linked_filing_id(
                observation_map, "NSE_PIT", item.get("nse_symbol") or signal.nse_symbol,
                source_url=item.get("source_url") or source_data.get("details_url") or source_data.get("source_url"),
                filing_ids=[item.get("nse_pid"), item.get("nse_did"), listing_row.get("pid"), listing_row.get("did"), listing_row.get("PID"), listing_row.get("DID")],
                filing_date=item.get("broadcast_date") or item.get("transaction_date"),
            )
            if filing_id is not None:
                signal.regulatory_filing_id = filing_id
    await db.commit()

    source_errors.update(collection.get("source_errors") or {})
    complete = bool(collection.get("complete")) and not source_errors
    completed_symbols = len([s for s in collection.get("symbol_stats") or [] if not (s.get("errors") or [])])
    failed_name_set = {
        master_by_symbol[_norm(key)]["legal_name"]
        for key in source_errors
        if _norm(key) in master_by_symbol
    }
    failed_names = sorted(failed_name_set, key=str.casefold)
    coverage_with_rows = sum(1 for count in coverage_counts.values() if count > 0)
    status = "ok" if complete else ("partial" if totals["stored"] > 0 else "error")
    await mark_finished(
        db, REGULATORY_DESK, status=status, history_days=history_days,
        from_date=history_start.isoformat(), to_date=history_end.isoformat(),
        scanned=totals["scanned"], stored=totals["stored"], complete=complete, errors=source_errors,
    )
    await db.commit()
    primary_projection = await sync_insider_transactions(db, days=history_days)
    await db.commit()
    logger.info("REGULATORY_PRIMARY_PROJECTION rows=%s", primary_projection)
    return {
        "status": status, "source": "NSE", "sources": REFERENCE_SOURCES, "history_days": history_days,
        "from_date": history_start.isoformat(), "to_date": history_end.isoformat(), "date_range_inclusive": True,
        "master_companies": len(master_specs), "nse_companies": len(nse_master_specs),
        "successful_nse_companies": completed_symbols, "failed_nse_companies": failed_names,
        "missing_nse_symbols": skipped_nse_companies, "skipped_nse_companies": skipped_nse_companies,
        "scanned": totals["scanned"], "stored": totals["stored"], "created": totals["created"], "updated": totals["updated"],
        "skipped": max(0, int(collection.get("deduped") or 0) - totals["stored"]), "filtered": totals["filtered"],
        "unresolved": totals["unresolved"], "source_scanned": {"NSE": totals["scanned"]},
        "source_created": {"NSE": totals["created"]}, "source_updated": {"NSE": totals["updated"]},
        "source_errors": source_errors, "coverage_companies_with_rows": coverage_with_rows,
        "coverage_companies_without_rows": skipped_nse_companies + [
            master["legal_name"] for master in nse_master_specs
            if _norm(master["nse_symbol"]) in {_norm(x.get("symbol")) for x in (collection.get("symbol_stats") or []) if not (x.get("errors") or [])}
            and coverage_counts[master["company_id"]] == 0
        ],
        "scrape_status": status, "complete": complete, "symbol_stats": collection.get("symbol_stats") or symbol_stats,
        "regulation_7_2": int(collection.get("regulation_7_2") or 0),
        "xml_errors": int(collection.get("xml_errors") or 0),
        "details_ok": int(collection.get("details_ok") or 0), "detail_errors": int(collection.get("detail_errors") or 0),
        "priority_order": ["PO", "HT", "LT", "Others"],
    }


@router.get("", response_model=list[SignalOut])
async def signals(
    analyst: str | None = None,
    track_mode: str | None = None,
    company: str | None = None,
    sort_by: str = "date",
    sort_dir: str = "desc",
    limit: int = 100,
    offset: int = 0,
    days: int = 20,
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    """Serve canonical Insider Buying rows from the requested recent disclosure window."""
    # Backfills can populate more than the old six-day UI window. Keep the default
    # aligned with the 20-day backfill, while bounding requests to supported history.
    history_days = min(max(int(days), 1), 2557)
    start_day, end_day = history_window(history_days)
    start_at, end_at = _date_floor(start_day), _date_floor(end_day + timedelta(days=1))
    stmt = (
        select(InsiderTransaction, Company, TargetCompany, User)
        .outerjoin(Company, Company.id == InsiderTransaction.company_id)
        .outerjoin(TargetCompany, and_(TargetCompany.company_id == Company.id,
            TargetCompany.active.is_(True), TargetCompany.approval_status == ApprovalStatus.APPROVED))
        .outerjoin(User, and_(User.id == TargetCompany.analyst_id, User.is_active.is_(True)))
        # Filter disclosures by their NSE broadcast date, not the underlying trade
        # date. Legacy records without broadcast_at still use transaction_date.
        .where(func.coalesce(InsiderTransaction.broadcast_at, InsiderTransaction.transaction_date) >= start_at,
               func.coalesce(InsiderTransaction.broadcast_at, InsiderTransaction.transaction_date) < end_at)
        .where(or_(
            func.lower(func.coalesce(InsiderTransaction.action, "")).like("%acquisit%"),
            func.lower(func.coalesce(InsiderTransaction.action, "")).like("%buy%"),
            func.lower(func.coalesce(InsiderTransaction.action, "")).like("%purchas%"),
            func.lower(func.coalesce(InsiderTransaction.action, "")).like("%acquired%"),
            func.lower(func.coalesce(InsiderTransaction.action, "")).like("%sell%"),
            func.lower(func.coalesce(InsiderTransaction.action, "")).like("%sold%"),
            func.lower(func.coalesce(InsiderTransaction.action, "")).like("%sale%"),
            func.lower(func.coalesce(InsiderTransaction.action, "")).like("%dispos%"),
        ))
    )
    if analyst:
        stmt = stmt.where(func.lower(func.coalesce(InsiderTransaction.analyst, "")).contains(analyst.strip().casefold()))
    if track_mode:
        wanted = track_mode.strip().upper()
        if wanted == "OTHERS":
            stmt = stmt.where(or_(TargetCompany.id.is_(None), TargetCompany.track_mode == TrackMode.NT))
        else:
            try:
                stmt = stmt.where(TargetCompany.track_mode == TrackMode(wanted))
            except ValueError:
                return []
    if company:
        needle = f"%{company.strip()}%"
        stmt = stmt.where(or_(
            InsiderTransaction.stock_name.ilike(needle),
            func.coalesce(InsiderTransaction.nse_symbol, "").ilike(needle),
            func.coalesce(InsiderTransaction.client_name, "").ilike(needle),
            func.coalesce(Company.legal_name, "").ilike(needle),
        ))
    sort_columns = {
        "date": InsiderTransaction.transaction_date,
        "quantity": InsiderTransaction.quantity,
        "percent_change": InsiderTransaction.holding_change_pct,
        "holding_change": InsiderTransaction.holding_change_pct,
        "avg_price": InsiderTransaction.average_price,
        "current_price": InsiderTransaction.current_market_price,
        "stock_price_change_percent": InsiderTransaction.daily_stock_change_pct,
        "value": InsiderTransaction.total_transaction_value,
        "market_cap": Company.market_cap,
    }
    sort_col = sort_columns.get(sort_by, InsiderTransaction.transaction_date)
    order = sort_col.asc().nullslast() if sort_dir.lower() == "asc" else sort_col.desc().nullslast()
    rows = (await db.execute(stmt.order_by(order, InsiderTransaction.id.desc())
        .offset(max(int(offset), 0)).limit(min(max(int(limit), 1), 200)))).all()
    output = []
    for tx, company_row, target, analyst_row in rows:
        meta = tx.metadata_json if isinstance(tx.metadata_json, dict) else {}
        track = _track(target) if target else str(meta.get("track_mode") or "Others")
        if track not in {"PO", "HT", "LT", "NT", "Others"}:
            track = "Others"
        output.append({
            "id": int(tx.id), "stock": tx.stock_name or (company_row.legal_name if company_row else "Unknown"),
            "client_name": tx.client_name, "client_category": tx.category, "action": tx.action,
            "date": tx.transaction_date, "quantity": tx.quantity,
            "post_transaction_holding": tx.holding_after, "holding_before": tx.holding_before,
            "traded_percent": tx.holding_change_pct, "percent_change": tx.holding_change_pct,
            "avg_price": tx.average_price, "current_price": tx.current_market_price,
            "value": tx.total_transaction_value, "period": meta.get("period"),
            "mode": meta.get("transaction_mode"),
            "company": company_row.legal_name if company_row else tx.stock_name,
            "lt": "", "ht": "", "track": track, "priority": _priority(target) if target else 5,
            "analyst": tx.analyst or (analyst_row.username if analyst_row else None),
            "market_cap": (company_row.market_cap if company_row else meta.get("market_cap")),
            "market_cap_eligible": bool(company_row and company_row.market_cap and company_row.market_cap > 0),
            "category_eligible": True, "history_eligible": True, "source": tx.source,
            "source_url": tx.source_url, "regulation": meta.get("regulation"),
            "submission_type": meta.get("submission_type"), "revision_remarks": meta.get("revision_remarks"),
            "broadcast_at": tx.broadcast_at or meta.get("broadcast_at"), "nse_symbol": tx.nse_symbol,
            "stock_price_change_percent": tx.daily_stock_change_pct,
            "cmp_vs_avg_transaction_price_pct": tx.cmp_vs_avg_transaction_price_pct,
        })
    return output


@router.get("/sources")
async def sources(
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT))
):
    return REFERENCE_SOURCES


@router.get("/source-status")
async def source_status(
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT))
):
    return [
        {**source, "configured": True}
        for source in REFERENCE_SOURCES
    ]


@router.get("/collector-runs")
async def collector_runs(
    source: str | None = None,
    limit: int = 100,
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    """Expose auditable success/failure outcomes for NSE date-window collections."""
    stmt = select(RegulatoryCollectorRun)
    if source:
        stmt = stmt.where(RegulatoryCollectorRun.source == source.strip().upper())
    rows = (await db.execute(
        stmt.order_by(RegulatoryCollectorRun.started_at.desc(), RegulatoryCollectorRun.id.desc())
        .limit(min(max(int(limit), 1), 500))
    )).scalars().all()
    return [{
        "id": row.id, "source": row.source,
        "window_start": row.window_start.isoformat() if row.window_start else None,
        "window_end": row.window_end.isoformat() if row.window_end else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "status": row.status, "http_status": row.http_status,
        "records_received": row.records_received, "records_inserted": row.records_inserted,
        "records_updated": row.records_updated, "content_hash": row.content_hash,
        "duration_ms": row.duration_ms, "error_message": row.error_message,
    } for row in rows]


@router.get("/coverage-list")
async def master_list(
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    priority_case = case(
        (TargetCompany.track_mode == TrackMode.PO, 1),
        (TargetCompany.track_mode == TrackMode.HT, 2),
        (TargetCompany.track_mode == TrackMode.LT, 3),
        else_=4,
    )
    rows = (await db.execute(_active_master_query().order_by(priority_case, Company.legal_name))).all()
    return [
        {
            "company_name": company.legal_name,
            "nse_symbol": company.nse_symbol,
            "bse_code": company.bse_code,
            "isin": company.isin,
            "market_cap": company.market_cap,
            "market_cap_bucket": company.market_cap_bucket,
            "track_mode": target.track_mode.value if target else TrackMode.NT.value,
            "analyst": analyst.username if analyst else None,
            "date_of_start": target.start_date,
            "date_of_end": target.end_date,
            "approval_status": target.approval_status.value,
            "priority": _priority(target),
        }
        for company, target, analyst in rows
    ]


@router.post("/ingest")
async def ingest(
    history_days: int = 6,
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST)),
):
    # Return sync diagnostics. A blocked/empty NSE source is represented as a
    # source condition and never triggers deletion of previously persisted valid rows.
    return await sync_regulatory_data(db, history_days=max(1, min(history_days, MAX_HISTORY_DAYS)))


class RegulatoryDetailOut(BaseModel):
    id: int
    stock: str
    client_name: str
    client_category: str | None
    action: str | None
    date: datetime
    quantity: float | None
    post_transaction_holding: float | None
    holding_before: float | None
    percent_change: float | None
    avg_price: float | None
    value: float | None
    period: str | None
    mode: str | None
    company: str
    track: str
    priority: int
    analyst: str | None
    source: str
    source_url: str | None
    regulation: str | None
    submission_type: str | None
    revision_remarks: str | None
    broadcast_at: datetime | None
    nse_symbol: str | None
    nse_pid: str | None
    nse_did: str | None
    source_data: dict[str, Any]


@router.get("/detail/{signal_id}", response_model=RegulatoryDetailOut)
async def regulatory_detail(
    signal_id: int,
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    """Return the full NSE filing payload, including parsed Details/XBRL XML facts."""
    target_rows = (await db.execute(_active_master_query())).all()
    coverage: dict[int, dict[str, Any]] = {}
    for company_row, target, analyst_row in target_rows:
        priority = _priority(target)
        current = coverage.get(company_row.id)
        if current is None or priority < current["priority"]:
            coverage[company_row.id] = {
                "track": _track(target),
                "priority": priority,
                "analyst": analyst_row.username if analyst_row else None,
            }

    # The list endpoint returns canonical InsiderTransaction IDs. Resolve those first;
    # falling directly through to the legacy InsiderSignal table can return 404 or the
    # wrong transaction when the two tables have different ID sequences.
    canonical_result = (
        await db.execute(
            select(InsiderTransaction, Company)
            .outerjoin(Company, Company.id == InsiderTransaction.company_id)
            .where(InsiderTransaction.id == signal_id)
        )
    ).first()
    if canonical_result:
        transaction, company_row = canonical_result
        meta = transaction.metadata_json if isinstance(transaction.metadata_json, dict) else {}
        info = coverage.get(
            company_row.id if company_row else transaction.company_id,
            {"track": "Others", "priority": PRIORITY_ORDER["Others"], "analyst": None},
        )
        source_data = meta.get("source_data") if isinstance(meta.get("source_data"), dict) else {}
        return RegulatoryDetailOut(
            id=transaction.id,
            stock=transaction.stock_name,
            client_name=transaction.client_name,
            client_category=transaction.category,
            action=transaction.action,
            date=transaction.transaction_date,
            quantity=transaction.quantity,
            post_transaction_holding=transaction.holding_after,
            holding_before=transaction.holding_before,
            percent_change=transaction.holding_change_pct,
            avg_price=transaction.average_price,
            value=transaction.total_transaction_value,
            period=meta.get("period"),
            mode=meta.get("transaction_mode"),
            company=company_row.legal_name if company_row else transaction.stock_name,
            track=info["track"],
            priority=info["priority"],
            analyst=transaction.analyst or info["analyst"],
            source=transaction.source or "NSE_PIT",
            source_url=transaction.source_url,
            regulation=meta.get("regulation"),
            submission_type=meta.get("submission_type"),
            revision_remarks=meta.get("revision_remarks"),
            broadcast_at=_parse_source_datetime(meta.get("broadcast_at")),
            nse_symbol=transaction.nse_symbol,
            nse_pid=meta.get("nse_pid"),
            nse_did=meta.get("nse_did"),
            source_data=source_data,
        )

    result = (
        await db.execute(
            select(InsiderSignal, Company)
            .join(Company, Company.id == InsiderSignal.company_id)
            .where(InsiderSignal.id == signal_id, InsiderSignal.source == "NSE")
        )
    ).first()
    if not result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="NSE regulatory filing not found")

    signal, company_row = result
    info = coverage.get(
        company_row.id,
        {"track": "Others", "priority": PRIORITY_ORDER["Others"], "analyst": None},
    )
    normalized = _nse_source_data(signal)
    holding_before = normalized.get("holding_before")
    percent_change = normalized.get("percent_change")
    if holding_before is not None:
        holding_before = float(holding_before)
    else:
        holding_before = _derive_holding_before(signal.action, signal.quantity, signal.post_transaction_holding)
    if percent_change is not None:
        percent_change = float(percent_change)
    else:
        percent_change = _derive_percent_change(signal.action, signal.traded_percent)
    avg_price, total_value = _fill_numeric_pair(signal.quantity, signal.price, signal.amount_inr)
    return RegulatoryDetailOut(
        id=signal.id,
        stock=signal.stock_name,
        client_name=signal.person_name,
        client_category=signal.client_category,
        action=signal.action,
        date=signal.transaction_date,
        quantity=signal.quantity,
        post_transaction_holding=signal.post_transaction_holding,
        holding_before=holding_before,
        percent_change=percent_change,
        avg_price=avg_price,
        value=total_value,
        period=signal.period,
        mode=signal.transaction_mode,
        company=company_row.legal_name,
        track=info["track"],
        priority=info["priority"],
        analyst=info["analyst"],
        source=signal.source or "NSE",
        source_url=signal.source_url,
        regulation=signal.regulation,
        submission_type=signal.submission_type,
        revision_remarks=signal.revision_remarks,
        broadcast_at=signal.broadcast_at,
        nse_symbol=signal.nse_symbol,
        nse_pid=signal.nse_pid,
        nse_did=signal.nse_did,
        source_data=_nse_source_data(signal),
    )


async def sync_pe_warrant_data(
    db,
    history_days: int = MAX_HISTORY_DAYS,
) -> dict[str, Any]:
    """Refresh NSE corporate-announcement capital actions with batch commits."""
    history_days = max(1, min(int(history_days), MAX_HISTORY_DAYS))
    history_start, history_end = history_window(history_days)
    master_rows = (await db.execute(_all_nse_master_query())).all()
    coverage = [
        {
            "company_id": int(company.id), "legal_name": company.legal_name, "nse_symbol": company.nse_symbol,
            "bse_code": company.bse_code, "isin": company.isin, "aliases": list(company.aliases or []),
            "track_mode": target.track_mode.value if target else TrackMode.NT.value, "analyst": analyst.username if analyst else None,
        }
        for company, target, analyst in master_rows
    ]
    nse_coverage, skipped_nse_companies = _partition_nse_coverage(coverage)
    if skipped_nse_companies:
        logger.warning(
            "NSE capital-action company-master rows skipped (no NSE symbol): %s",
            ", ".join(skipped_nse_companies[:50]),
        )
    if not nse_coverage:
        source_errors = {"NSE Universe": "No NSE-listed companies are available in the local company master."}
        await mark_running(db, CAPITAL_ACTION_DESK, history_days=history_days, from_date=history_start.isoformat(), to_date=history_end.isoformat())
        await mark_finished(
            db, CAPITAL_ACTION_DESK, status="configuration_required", history_days=history_days,
            from_date=history_start.isoformat(), to_date=history_end.isoformat(), scanned=0, stored=0, complete=False, errors=source_errors,
        )
        await db.commit()
        return {
            "status": "configuration_required", "source": "NSE", "sources": CAPITAL_ACTION_SOURCES, "history_days": history_days,
            "created": 0, "stored": 0, "skipped": 0, "unresolved": 0, "scanned": 0, "parsed_announcements": 0,
            "target_candidates": 0, "source_errors": source_errors, "missing_nse_symbols": skipped_nse_companies,
            "skipped_nse_companies": skipped_nse_companies, "complete": False, "event_types": list(CAPITAL_EVENT_TYPES),
        }

    by_symbol = {_norm(row["nse_symbol"]): row for row in nse_coverage}
    totals = {"scanned": 0, "created": 0, "updated": 0, "unresolved": 0, "filtered": 0}
    source_errors: dict[str, str] = {}
    symbol_stats: list[dict[str, Any]] = []
    matched_count = 0
    batch_commit_count = 0
    # The NSE all-company feed is fetched in bounded date windows, then each
    # returned symbol is reconciled against the local master. This avoids thousands
    # of per-symbol API calls on both daily and full-history refreshes. Destructive
    # cleanup for symbols absent from the feed is deferred until every feed window
    # succeeds, and failed symbol captures keep their previous DB rows.
    use_broad_feed = True

    async with _CAPITAL_SYNC_LOCK:
        await mark_running(
            db, CAPITAL_ACTION_DESK, history_days=history_days,
            from_date=history_start.isoformat(), to_date=history_end.isoformat(),
        )
        await db.commit()

        async def on_batch(batch_payload, batch_start: int, total_symbols: int) -> None:
            nonlocal batch_commit_count
            batch_matched: list[tuple[dict[str, Any], dict[str, Any], str]] = []
            completed_symbols: set[str] = set()
            batch_seen: set[str] = set()
            for discovered_master, rows, stats in batch_payload:
                symbol = _norm(discovered_master.get("nse_symbol"))
                symbol_stats.append(stats)
                totals["scanned"] += int(stats.get("raw_rows") or 0)
                master = by_symbol.get(symbol)
                if master is None:
                    totals["unresolved"] += max(1, len(rows))
                    source_errors.setdefault(f"unmapped:{symbol or 'unknown'}", "NSE announcement symbol is not present in the local company master")
                    logger.warning(
                        "NSE_CAPITAL_UNMAPPED_SYMBOL symbol=%s source_company=%s rows=%s",
                        symbol or "<missing>", discovered_master.get("legal_name"), len(rows),
                    )
                    continue
                errors = list(stats.get("errors") or [])
                if errors:
                    # Canonical symbol keys prevent the same failure being counted
                    # separately once by company name and again by symbol.
                    source_errors.setdefault(symbol, "; ".join(str(item) for item in errors))
                elif not (use_broad_feed and stats.get("feed_complete") is False):
                    # In broad-feed mode, a symbol can have raw NSE announcements but
                    # produce zero classified capital-action rows. Do not delete its
                    # existing events solely because this parser run returned no
                    # candidates: a classifier false-negative must not erase data.
                    # When candidate rows do exist, replace that symbol's window only
                    # after its documents were fetched/parsed without errors. Symbols
                    # absent from a complete broad feed are handled by the guarded
                    # stale-row cleanup after all windows finish.
                    if not use_broad_feed or rows:
                        completed_symbols.add(symbol)
                    else:
                        logger.info(
                            "NSE_CAPITAL_PRESERVE_EXISTING_NO_CANDIDATES symbol=%s raw_rows=%s",
                            symbol, stats.get("raw_rows"),
                        )
                for item in rows:
                    event_date = item.get("event_date") or item.get("transaction_date")
                    if event_date is None or event_date.date() < history_start:
                        totals["filtered"] += 1
                        continue
                    if symbol not in by_symbol:
                        totals["unresolved"] += 1
                        continue
                    key = str(item.get("source_key") or "").strip()
                    if not key:
                        key = _capital_key(item, master["company_id"])
                        item["source_key"] = key
                    if key in batch_seen:
                        continue
                    batch_seen.add(key)
                    batch_matched.append((item, master, key))

            # Upsert by stable source_key so a full reconciliation does not replace
            # unchanged event IDs (and therefore does not destroy their review history).
            keys = sorted({key for _item, _master, key in batch_matched})
            existing_by_key: dict[str, CapitalActionSignal] = {}
            if keys:
                existing_rows = (await db.execute(
                    select(CapitalActionSignal).where(CapitalActionSignal.source_key.in_(keys))
                )).scalars().all()
                existing_by_key = {str(row.source_key): row for row in existing_rows}

            def capital_values(item: dict[str, Any], master: dict[str, Any], key: str) -> dict[str, Any]:
                investor = clean_investor_name(item.get("investor_name"))
                return {
                    "company_id": master["company_id"],
                    "event_type": str(item.get("event_type") or "WARRANT")[:40],
                    "stock_name": str(master.get("legal_name") or item.get("stock_name") or "")[:255],
                    "investor_name": str(investor).strip()[:255] if investor else None,
                    "investor_category": str(item.get("investor_category"))[:120] if item.get("investor_category") else None,
                    "action": str(item.get("action"))[:160] if item.get("action") else None,
                    "event_date": item.get("event_date") or item.get("transaction_date"),
                    "quantity": item.get("quantity"),
                    "price": item.get("price"),
                    "amount_inr": item.get("amount_inr"),
                    "source": "NSE",
                    "source_url": str(item.get("source_url"))[:1000] if item.get("source_url") else None,
                    "title": str(item.get("title"))[:1000] if item.get("title") else None,
                    "details": str(item.get("details"))[:10000] if item.get("details") else None,
                    "source_key": key,
                    "event_sub_type": str(item.get("event_sub_type"))[:80] if item.get("event_sub_type") else None,
                    "pre_holding_pct": item.get("pre_holding_pct"),
                    "post_holding_pct": item.get("post_holding_pct"),
                    "holding_change_pct": item.get("holding_change_pct"),
                    "promoter_or_pac": item.get("promoter_or_pac"),
                    "severity": str(item.get("severity"))[:40] if item.get("severity") else None,
                    "importance_score": item.get("importance_score"),
                    "threshold_status": _threshold_status_text(item.get("threshold_status")),
                    "confidence": item.get("confidence"),
                    "purpose": item.get("purpose"),
                    "event_chain_key": str(item.get("event_chain_key"))[:255] if item.get("event_chain_key") else None,
                    "investor_type": str(item.get("investor_type"))[:120] if item.get("investor_type") else None,
                    "isin": str(item.get("isin"))[:30] if item.get("isin") else None,
                    "document_url": str(item.get("document_url"))[:1000] if item.get("document_url") else None,
                    "document_type": str(item.get("document_type"))[:120] if item.get("document_type") else None,
                    "document_status": str(item.get("document_status"))[:80] if item.get("document_status") else None,
                    "document_sha256": str(item.get("document_sha256"))[:64] if item.get("document_sha256") else None,
                    "document_pages": item.get("document_pages"),
                    "document_bytes": item.get("document_bytes"),
                    "announcement_subject": str(item.get("announcement_subject"))[:1000] if item.get("announcement_subject") else None,
                    "announcement_details": str(item.get("announcement_details"))[:10000] if item.get("announcement_details") else None,
                    "document_summary": str(item.get("document_summary"))[:10000] if item.get("document_summary") else None,
                    "document_text": str(item.get("document_text"))[:100000] if item.get("document_text") else None,
                    "transaction_status": str(item.get("transaction_status") or "REVIEW")[:24].upper(),
                    "classification_status": str(item.get("classification_status") or ("REVIEW" if item.get("event_type") == "REVIEW" else "CLASSIFIED"))[:24].upper(),
                    "review_reason": str(item.get("review_reason") or "")[:4000] or None,
                }

            inserted = 0
            updated = 0
            changed_event_ids: list[int] = []
            for item, master, key in batch_matched:
                values = capital_values(item, master, key)
                existing = existing_by_key.get(key)
                if existing is None:
                    db.add(CapitalActionSignal(**values))
                    inserted += 1
                    continue
                fields_to_compare = tuple(values.keys())
                changed = any(getattr(existing, field) != value for field, value in values.items())
                if changed:
                    # Any changed facts invalidate the prior AI assessment; facts and
                    # AI conclusions must never silently drift apart.
                    changed_event_ids.append(existing.id)
                    for field in fields_to_compare:
                        setattr(existing, field, values[field])
                    updated += 1

            if changed_event_ids:
                await db.execute(delete(CapitalActionAssessment).where(
                    CapitalActionAssessment.capital_action_id.in_(changed_event_ids)
                ))

            # Only a successfully captured company window may remove stale records.
            # Rows returned by the source retain their IDs above; missing rows from a
            # complete window are deleted, while failed/partial symbols are preserved.
            for symbol in completed_symbols:
                master = by_symbol.get(symbol)
                if master is None:
                    continue
                seen_for_symbol = [
                    key for _item, item_master, key in batch_matched
                    if _norm(item_master.get("nse_symbol")) == symbol
                ]
                stale_stmt = delete(CapitalActionSignal).where(
                    CapitalActionSignal.source == "NSE",
                    CapitalActionSignal.company_id == master["company_id"],
                )
                if history_days < MAX_HISTORY_DAYS:
                    stale_stmt = stale_stmt.where(
                        CapitalActionSignal.event_date >= _date_floor(history_start),
                        CapitalActionSignal.event_date < _date_floor(history_end + timedelta(days=1)),
                    )
                if seen_for_symbol:
                    stale_stmt = stale_stmt.where(CapitalActionSignal.source_key.not_in(seen_for_symbol))
                deleted_result = await db.execute(stale_stmt)
                if int(getattr(deleted_result, "rowcount", 0) or 0) > 0:
                    logger.info("NSE_CAPITAL_STALE_ROWS_CLEARED symbol=%s deleted=%s", symbol, deleted_result.rowcount)

            await db.flush()
            totals["created"] += inserted
            totals["updated"] += updated
            batch_commit_count += 1
            await db.commit()
            logger.info(
                "NSE_CAPITAL_BATCH_PERSISTED batch_start=%s batch_size=%s symbols_in_scope=%s inserted_batch=%s updated_batch=%s inserted_total=%s updated_total=%s batch_error_count=%s total_error_count=%s",
                batch_start, len(batch_payload), total_symbols, inserted, updated,
                totals["created"], totals["updated"],
                len({_norm(item[2].get("symbol")) for item in batch_payload if item[2].get("errors")}), len(source_errors),
            )
            batch_commit_count += 1
            await db.commit()
            logger.info(
                "NSE_CAPITAL_BATCH_PERSISTED batch_start=%s batch_size=%s symbols_in_scope=%s stored_batch=%s stored_total=%s batch_error_count=%s total_error_count=%s",
                batch_start, len(batch_payload), total_symbols, inserted, totals["created"],
                len({ _norm(item[2].get("symbol")) for item in batch_payload if item[2].get("errors") }), len(source_errors),
            )

        try:
            result = await PEWarrantCollector().collect(
                history_days=history_days,
                allowed_companies=nse_coverage,
                on_batch=on_batch,
                use_broad_feed=use_broad_feed,
            )
        except Exception as exc:
            # A failed flush leaves AsyncSession in PendingRollback state. Roll back
            # before writing collector diagnostics or updating sync-state rows.
            try:
                await db.rollback()
            except Exception:
                logger.exception("NSE capital-action session rollback failed")
            logger.exception("NSE capital-action collection failed")
            source_errors["NSE Corporate Announcements"] = str(exc)
            failed_at = datetime.now(timezone.utc)
            db.add(RegulatoryCollectorRun(
                source="NSE_CA", window_start=_date_floor(history_start), window_end=_date_floor(history_end),
                started_at=failed_at, finished_at=failed_at, status="FAILED", http_status=None,
                records_received=0, records_inserted=0, records_updated=0, duration_ms=None,
                error_message=f"Collector aborted before returning window diagnostics: {type(exc).__name__}: {exc}"[:10000],
                created_at=failed_at,
            ))
            await mark_finished(
                db, CAPITAL_ACTION_DESK, status="source_error", history_days=history_days,
                from_date=history_start.isoformat(), to_date=history_end.isoformat(), scanned=totals["scanned"], stored=totals["created"] + totals["updated"], complete=False, errors=source_errors,
            )
            await db.commit()
            return {
                "status": "source_error", "source": "NSE", "sources": CAPITAL_ACTION_SOURCES, "history_days": history_days,
                "scanned": totals["scanned"], "created": totals["created"], "updated": totals["updated"], "stored": totals["created"] + totals["updated"],
                "source_errors": source_errors, "complete": False, "event_types": list(CAPITAL_EVENT_TYPES),
            }

    # Raw announcement payloads and per-date-window results remain auditable, even
    # when a candidate is filtered from the normalized desk. Link normalized events
    # only when source URL / stable filing ID / unique same-day filing identify it.
    capital_observation_map = await _persist_regulatory_observations(
        db, result.get("raw_filings"), result.get("window_stats"), by_symbol
    )
    capital_items_by_key = {str(item.get("source_key") or ""): item for item in (result.get("rows") or []) if item.get("source_key")}
    capital_keys = list(capital_items_by_key)
    for offset in range(0, len(capital_keys), 400):
        key_batch = capital_keys[offset:offset + 400]
        if not key_batch:
            continue
        signals = (await db.execute(select(CapitalActionSignal).where(CapitalActionSignal.source_key.in_(key_batch)))).scalars().all()
        for signal in signals:
            item = capital_items_by_key.get(str(signal.source_key)) or {}
            filing_id = _linked_filing_id(
                capital_observation_map, "NSE_CA", item.get("nse_symbol"),
                source_url=item.get("source_url") or item.get("document_url"),
                filing_ids=[item.get("nse_announcement_id"), item.get("announcement_id"), item.get("seq_id"), item.get("id")],
                filing_date=item.get("event_date") or item.get("transaction_date"),
            )
            if filing_id is not None:
                signal.regulatory_filing_id = filing_id
    await db.commit()

    # If every all-company date window was fetched successfully, companies omitted
    # from the feed had no announcements in that interval. Remove stale rows for
    # those companies only after the feed is known complete. A failed feed window
    # never triggers destructive cleanup.
    if use_broad_feed and result.get("feed_complete") and int(result.get("scanned") or 0) > 0:
        returned_symbols = {
            _norm(item.get("symbol")) for item in (result.get("symbol_stats") or [])
            if _norm(item.get("symbol"))
        }
        absent_symbols = set(by_symbol) - returned_symbols
        absent_ids = [by_symbol[symbol]["company_id"] for symbol in absent_symbols if symbol in by_symbol]
        if absent_ids:
            delete_stmt = delete(CapitalActionSignal).where(
                CapitalActionSignal.source == "NSE",
                CapitalActionSignal.company_id.in_(absent_ids),
            )
            if history_days < MAX_HISTORY_DAYS:
                delete_stmt = delete_stmt.where(CapitalActionSignal.event_date >= _date_floor(history_start))
            deleted_result = await db.execute(delete_stmt)
            await db.commit()
            logger.info(
                "NSE_CAPITAL_STALE_ROWS_CLEARED mode=broad-feed absent_symbols=%s companies=%s deleted_rows=%s feed_complete=true",
                len(absent_symbols), len(absent_ids), getattr(deleted_result, "rowcount", None),
            )

    # Merge collector errors using one canonical symbol key. The collector may
    # originate an error keyed by uppercase NSE symbol, while the persistence
    # callback keys the same error by normalized symbol; avoid storing it twice.
    for error_key, error_message in (result.get("source_errors") or {}).items():
        raw_key = str(error_key)
        if raw_key.startswith("feed-window:"):
            canonical_key = raw_key
        else:
            normalized_key = _norm(raw_key)
            canonical_key = normalized_key if normalized_key in by_symbol else raw_key
        source_errors.setdefault(canonical_key, str(error_message))
    complete = bool(result.get("complete")) and not source_errors
    status = "ok" if complete else ("partial" if totals["created"] + totals["updated"] > 0 else "error")
    await mark_finished(
        db, CAPITAL_ACTION_DESK, status=status, history_days=history_days,
        from_date=history_start.isoformat(), to_date=history_end.isoformat(), scanned=totals["scanned"], stored=totals["created"] + totals["updated"], complete=complete, errors=source_errors,
    )
    await db.commit()
    primary_projection = await sync_capital_actions(db, days=history_days)
    await db.commit()
    logger.info("CAPITAL_ACTION_PRIMARY_PROJECTION rows=%s", primary_projection)
    return {
        "status": status, "source": "NSE", "sources": CAPITAL_ACTION_SOURCES, "history_days": history_days,
        "coverage_companies": len(coverage), "nse_companies": len(nse_coverage),
        "collection_mode": result.get("collection_mode") or ("nse_broad_feed" if use_broad_feed else "company_scoped"),
        "feed_complete": result.get("feed_complete") if use_broad_feed else None,
        "symbols_with_announcements": int(result.get("symbols_with_announcements") or len(result.get("symbol_stats") or [])),
        "companies_scanned": int(result.get("companies_scanned") or len(result.get("symbol_stats") or [])),
        "successful_nse_companies": len([s for s in result.get("symbol_stats") or [] if not (s.get("errors") or [])]),
        "failed_nse_companies": sorted(
            {
                by_symbol[_norm(key)]["legal_name"]
                for key in source_errors
                if _norm(key) in by_symbol
            },
            key=str.casefold,
        ),
        "scanned": totals["scanned"], "created": totals["created"], "updated": totals["updated"], "stored": totals["created"] + totals["updated"],
        "skipped": max(0, len(list(result.get("rows") or [])) - totals["created"]), "unresolved": totals["unresolved"],
        "filtered": totals["filtered"], "source_created": {"NSE": totals["created"]},
        "source_scanned": {"NSE": totals["scanned"]}, "source_counts": {"nse": totals["created"]},
        "pages": int(result.get("pages") or 0), "parsed_announcements": int(result.get("candidate_rows") or 0),
        "target_candidates": int(result.get("target_candidates") or len(list(result.get("rows") or []))),
        "coverage_rows": len(coverage), "missing_nse_symbols": skipped_nse_companies, "skipped_nse_companies": skipped_nse_companies,
        "source_errors": source_errors, "symbol_stats": result.get("symbol_stats") or symbol_stats,
        "review_needed_count": int(result.get("review_needed_count") or 0),
        "pre_screen_rejected": int(result.get("pre_screen_rejected") or 0),
        "candidates_without_document": int(result.get("candidate_without_document") or 0),
        "candidates_skipped_document_failure": int(result.get("candidates_skipped_document_failure") or 0),
        "classification_rejected_after_document": int(result.get("classification_rejected_after_document") or 0),
        "documents_ok": int(result.get("documents_ok") or 0), "documents_failed": int(result.get("documents_failed") or 0),
        "complete": complete, "event_types": list(CAPITAL_EVENT_TYPES),
    }


def _match_coverage_company_for_capital_item(
    item: dict[str, Any],
    coverage: list[dict[str, Any]],
    by_id: dict[int, Company],
) -> Company | None:
    symbol = _norm(item.get("nse_symbol"))
    bse_code = _norm(item.get("bse_scrip_code") or item.get("bse_code"))
    name = _canonical_company_name(item.get("company_name") or item.get("stock_name"))

    for master in coverage:
        if symbol and symbol == _norm(master.get("nse_symbol")):
            return by_id.get(master["company_id"])
        if bse_code and bse_code == _norm(master.get("bse_code")):
            return by_id.get(master["company_id"])

    if name:
        matches = []
        for master in coverage:
            master_name = _canonical_company_name(master.get("legal_name"))
            aliases = [_canonical_company_name(alias) for alias in (master.get("aliases") or [])]
            terms = [master_name, *[a for a in aliases if a]]
            if any(name == term or (len(term) >= 6 and (name in term or term in name)) for term in terms if term):
                matches.append(master)
        if len(matches) == 1:
            return by_id.get(matches[0]["company_id"])
    return None


@router.get("/pe-warrant", response_model=list[CapitalActionOut])
async def pe_warrant(
    event_type: str | None = None,
    company: str | None = None,
    track_mode: str | None = None,
    sort_by: str = "date",
    sort_dir: str = "desc",
    limit: int = 100,
    offset: int = 0,
    include_quotes: bool = False,
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    """Serve Capital Ownership exclusively from the canonical capital_actions table."""
    start_day, end_day = history_window(6)
    start_at, end_at = _date_floor(start_day), _date_floor(end_day + timedelta(days=1))
    stmt = (
        select(CapitalAction, Company, TargetCompany, User)
        .outerjoin(Company, Company.id == CapitalAction.company_id)
        .outerjoin(TargetCompany, and_(TargetCompany.company_id == Company.id,
            TargetCompany.active.is_(True), TargetCompany.approval_status == ApprovalStatus.APPROVED))
        .outerjoin(User, and_(User.id == TargetCompany.analyst_id, User.is_active.is_(True)))
        .where(CapitalAction.event_date >= start_at, CapitalAction.event_date < end_at)
    )
    if event_type:
        wanted = _canonical_capital_type(event_type) or event_type.strip().upper()
        if wanted not in (*CAPITAL_EVENT_TYPES, "REVIEW"):
            return []
        stmt = stmt.where(CapitalAction.event == wanted)
    if track_mode:
        wanted = track_mode.strip().upper()
        if wanted == "OTHERS":
            stmt = stmt.where(or_(TargetCompany.id.is_(None), TargetCompany.track_mode == TrackMode.NT))
        else:
            try:
                stmt = stmt.where(TargetCompany.track_mode == TrackMode(wanted))
            except ValueError:
                return []
    if company:
        needle = f"%{company.strip()}%"
        stmt = stmt.where(or_(CapitalAction.company_name.ilike(needle),
            func.coalesce(CapitalAction.nse_symbol, "").ilike(needle),
            func.coalesce(CapitalAction.investor_allottee, "").ilike(needle),
            func.coalesce(CapitalAction.details, "").ilike(needle)))
    sort_cols = {
        "date": CapitalAction.event_date, "quantity": CapitalAction.shares_warrants,
        "price": CapitalAction.issue_transaction_price, "value": CapitalAction.value_issue_size,
        "current_price": CapitalAction.cmp, "stake_percent": CapitalAction.ownership_pct,
        "holding_change_pct": CapitalAction.ownership_pct, "importance_score": CapitalAction.importance_score,
    }
    sort_col = sort_cols.get(sort_by, CapitalAction.event_date)
    order = sort_col.asc().nullslast() if sort_dir.lower() == "asc" else sort_col.desc().nullslast()
    rows = (await db.execute(stmt.order_by(order, CapitalAction.id.desc())
        .offset(max(int(offset), 0)).limit(min(max(int(limit), 1), 200)))).all()
    output = []
    for event, company_row, target, analyst_row in rows:
        meta = event.metadata_json if isinstance(event.metadata_json, dict) else {}
        stock = event.company_name or (company_row.legal_name if company_row else "Unknown")
        event_type_value = _canonical_capital_type(event.event) or event.event
        quantity, price, value = _safe_capital_numeric_values(
            event.shares_warrants, event.issue_transaction_price, event.value_issue_size,
        )
        output.append({
            "id": event.id, "event_type": event_type_value, "stock": stock,
            "investor": clean_investor_name(event.investor_allottee), "investor_category": meta.get("investor_category"),
            "investor_type": None, "promoter_pac": "YES" if meta.get("promoter_or_pac") else ("NO" if meta.get("promoter_or_pac") is False else None),
            "action": meta.get("action"), "date": event.event_date,
            "quantity": quantity, "price": price,
            "issue_price": price, "value": value,
            "issue_size": value, "transaction_value": value,
            "current_price": event.cmp, "current_price_change_percent": None,
            "quote_source": "NSE market-price history" if event.cmp is not None else None,
            "quote_trade_date": None, "source": event.source or "NSE",
            "stake_percent": event.ownership_pct, "pre_holding_pct": meta.get("pre_holding_pct"),
            "post_holding_pct": meta.get("post_holding_pct"), "holding_change_pct": event.ownership_pct,
            "threshold_state": meta.get("threshold_status"), "severity": event.importance,
            "importance_score": event.importance_score, "confidence": None, "purpose": meta.get("purpose"),
            "event_sub_type": event.subtype,
            "shares_acquired": quantity if event_type_value == "STRATEGIC_ACQUISITION" else None,
            "shares_disposed": None,
            "shares_issued": quantity if event_type_value == "PREFERENTIAL_ISSUE" else None,
            "warrant_count": quantity if event_type_value == "WARRANT" else None,
            "isin": meta.get("isin"), "event_chain_key": meta.get("event_chain_key"),
            "source_url": event.source_url, "title": event.subtype or event.event,
            "details": event.details, "document_url": meta.get("document_url") or event.source_url,
            "nse_symbol": event.nse_symbol or (company_row.nse_symbol if company_row else None),
            "document_status": meta.get("document_status"), "document_available": bool(event.source_url),
            "transaction_status": event.transaction_status or "REVIEW",
            "classification_status": event.classification_status or "REVIEW", "review_reason": event.review_reason,
            "company": company_row.legal_name if company_row else stock,
            "track": _track(target) if target else "Others", "priority": _priority(target) if target else 5,
            "analyst": event.analyst or (analyst_row.username if analyst_row else None),
            "assessment": "REVIEW" if event.classification_status == "REVIEW" else None,
            "assessment_provider": None, "assessment_confidence": None,
            "assessment_review_required": event.classification_status == "REVIEW",
            "assessment_rationale": event.review_reason, "missing_information": None,
        })
    return output


@router.get("/pe-warrant/quotes")
async def pe_warrant_quotes(
    symbols: str = "",
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    """Return quotes from the shared market-price service; no module snapshot involved."""
    requested = list(dict.fromkeys(
        value.strip().upper() for value in str(symbols or "").split(",") if value.strip()
    ))[:50]
    if not requested:
        return {}
    snapshot, _meta = await get_snapshot("nse:equity-prices", allow_stale=True)
    items = snapshot.get("items") if isinstance(snapshot, dict) else {}
    if not isinstance(items, dict):
        return {}
    output = {}
    for symbol in requested:
        quote = items.get(symbol)
        if isinstance(quote, dict) and (quote.get("last_price") is not None or quote.get("change_percent") is not None):
            output[symbol] = {"last_price": quote.get("last_price"), "change_percent": quote.get("change_percent")}
    return output


@router.get("/pe-warrant/detail/{signal_id}", response_model=CapitalActionDetailOut)
async def capital_action_detail(
    signal_id: int,
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    """Return the stored source metadata for one canonical capital action."""
    row = await db.get(CapitalAction, signal_id)
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Capital action not found")
    meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    company = await db.get(Company, row.company_id) if row.company_id else None
    track = await db.scalar(select(TargetCompany.track_mode).where(
        TargetCompany.company_id == row.company_id,
        TargetCompany.active.is_(True),
        TargetCompany.approval_status == ApprovalStatus.APPROVED,
    ).limit(1)) if row.company_id else None
    analyst = row.analyst
    investors = [
        clean_name for item in str(row.investor_allottee or "").split(",")
        if (clean_name := clean_investor_name(item))
    ]
    quantity, price, amount = _safe_capital_numeric_values(
        row.shares_warrants, row.issue_transaction_price, row.value_issue_size,
    )
    parsed = {
        "quantity": quantity, "price": price,
        "amount_inr": amount, "stake_percent": row.ownership_pct,
        "investor_category": meta.get("investor_category"), "event_type": row.event,
        "event_sub_type": row.subtype, "transaction_status": row.transaction_status,
        "classification_status": row.classification_status, "review_reason": row.review_reason,
        "pre_holding_pct": meta.get("pre_holding_pct"), "post_holding_pct": meta.get("post_holding_pct"),
        "holding_change_pct": meta.get("holding_change_pct"), "promoter_or_pac": meta.get("promoter_or_pac"),
        "purpose": meta.get("purpose"), "severity": row.importance,
        "importance_score": row.importance_score, "investors": investors,
    }
    return CapitalActionDetailOut(
        id=row.id, event_type=row.event, stock=row.company_name,
        company=company.legal_name if company else row.company_name, nse_symbol=row.nse_symbol,
        investor=investors[0] if investors else None, investor_category=meta.get("investor_category"),
        action=meta.get("action"), date=row.event_date or row.first_seen_at,
        quantity=quantity, price=price, value=amount,
        source=row.source, source_url=row.source_url, document_url=meta.get("document_url") or row.source_url,
        document_type=meta.get("document_type"), document_status=meta.get("document_status"),
        document_sha256=meta.get("document_sha256"), document_pages=meta.get("document_pages"),
        document_bytes=meta.get("document_bytes"), announcement_subject=meta.get("announcement_subject") or row.subtype,
        announcement_details=meta.get("announcement_details") or row.details, parsed=parsed,
        document_summary=meta.get("document_summary") or row.details, document_text=meta.get("document_text"),
        raw_nse_row=meta.get("raw_nse_row") if isinstance(meta.get("raw_nse_row"), dict) else None,
        transaction_status=row.transaction_status or "REVIEW",
        classification_status=row.classification_status or "REVIEW", review_reason=row.review_reason,
        track=getattr(track, "value", str(track)) if track else "Others",
        priority=1 if str(getattr(track, "value", track)) == "PO" else 2 if str(getattr(track, "value", track)) == "HT" else 3 if str(getattr(track, "value", track)) == "LT" else 4,
        analyst=analyst,
    )


@router.post("/pe-warrant/ingest")
async def ingest_pe_warrant(
    history_days: int = 6,
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST)),
):
    return await sync_pe_warrant_data(db, history_days=max(1, min(history_days, MAX_HISTORY_DAYS)))