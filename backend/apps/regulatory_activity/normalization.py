from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, date, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", " ")
    text = "".join(ch for ch in text if ch in "\n\r\t" or ord(ch) >= 32)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def json_safe(value: Any) -> Any:
    """Convert nested collector values into values accepted by SQLAlchemy JSON.

    NSE API rows and parsed XBRL facts can contain ``datetime`` objects after
    normalization.  PostgreSQL JSON columns only accept JSON primitives, so
    sanitize recursively at the normalization boundary instead of allowing one
    QIP/PREF record to abort an entire source batch during ``flush``.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    # Keep unexpected scalar types traceable without making JSON serialization
    # fail; this is metadata, not a numeric serving column.
    return str(value)


def parse_number(value: Any) -> float | None:
    text = clean_text(value)
    if not text or norm(text) in {"-", "na", "n a", "nil", "none", "null", "not disclosed"}:
        return None
    multiplier = 1.0
    lowered = text.casefold()
    if re.search(r"\b(crore|crores|cr)\b", lowered):
        multiplier = 10_000_000.0
    elif re.search(r"\b(lakh|lakhs|lac|lacs)\b", lowered):
        multiplier = 100_000.0
    match = re.search(r"[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)", text)
    if not match:
        return None
    try:
        number = float(match.group(0).replace(",", "")) * multiplier
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in (
        "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y",
        "%d-%B-%Y %H:%M:%S", "%d-%B-%Y %H:%M", "%d-%B-%Y",
        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    found = re.search(r"\b\d{1,2}[-/][A-Za-z0-9]{2,9}[-/]\d{4}\b", text)
    if found and found.group(0) != text:
        return parse_datetime(found.group(0))
    return None


def stable_key(exchange: str, activity_type: str, subtype: str | None, identity: Any) -> str:
    payload = json.dumps(
        [exchange.upper(), activity_type.upper(), (subtype or "").upper(), identity],
        sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def canonical_activity_type(value: Any) -> tuple[str, str | None]:
    text = norm(value)
    if "insider" in text or "promoter" in text or "regulation 7" in text:
        return "INSIDER_TRADING", None
    if "qip" in text or "qualified institutional" in text:
        return "QIP", None
    if "bulk" in text or "block" in text:
        return "BULK_DEAL", "BLOCK" if "block" in text else "BULK"
    if "warrant" in text:
        return "PREFERENTIAL_WARRANT", "WARRANT"
    if "preferential" in text:
        return "PREFERENTIAL_WARRANT", "PREFERENTIAL"
    return str(value or "OTHER").upper(), None


def validate_event(event: dict[str, Any]) -> tuple[bool, str]:
    if not clean_text(event.get("company_name")):
        return False, "missing_company"
    if parse_datetime(event.get("event_date")) is None:
        return False, "missing_event_date"
    if not clean_text(event.get("source_url")):
        return False, "missing_source_url"
    if event.get("activity_type") not in {
        "INSIDER_TRADING", "BULK_DEAL", "QIP", "PREFERENTIAL_WARRANT",
    }:
        return False, "unsupported_activity_type"
    details = event.get("details_json")
    if not isinstance(details, dict) or not details:
        # Bulk CSV rows carry normalized table values and do not require popup JSON.
        if event.get("activity_type") != "BULK_DEAL":
            return False, "missing_detail_facts"
    # Preserve a discovered filing even when its detail document could not be
    # parsed.  These rows are explicitly FAILED and contain a failure_reason;
    # dropping them hides coverage gaps from the UI and source-health counters.
    if (
        str(event.get("status") or "").upper() == "FAILED"
        and isinstance(details, dict)
        and clean_text(details.get("failure_reason"))
    ):
        return True, "ok"
    activity = event.get("activity_type")
    if activity == "INSIDER_TRADING" and not any((
        clean_text(event.get("person_allottee_client")), clean_text(event.get("action")), event.get("quantity") is not None,
    )):
        return False, "missing_insider_transaction_facts"
    if activity in {"QIP", "PREFERENTIAL_WARRANT"}:
        if str(event.get("status") or "").upper() != "LEGACY":
            metadata = event.get("source_metadata") if isinstance(event.get("source_metadata"), dict) else {}
            event_status = str(event.get("status") or "").upper()
            if (
                event_status == "DETAILED"
                and str(metadata.get("event_date_basis") or "").casefold() in {
                    "filing_date_fallback", "board_resolution_fallback", "board_resolution_date"
                }
            ):
                return False, "detailed_issue_date_not_allotment_or_issue_date"
            facts = details.get("company_popup_facts") if isinstance(details, dict) else None
            popup_text = clean_text(details.get("popup_text")) if isinstance(details, dict) else None
            if event_status == "DETAILED" and (not isinstance(facts, dict) or len(facts) < 3 or not popup_text):
                return False, "missing_issue_popup_facts"
            if event_status == "DETAILED":
                quantity = parse_number(event.get("quantity"))
                price = parse_number(event.get("price"))
                value = parse_number(event.get("value_inr"))
                if quantity is None or quantity <= 0:
                    return False, "detailed_issue_missing_positive_quantity"
                if not ((price is not None and price > 0) or (value is not None and value > 0)):
                    return False, "detailed_issue_missing_positive_price_or_value"
    if activity == "BULK_DEAL":
        if not clean_text(event.get("person_allottee_client")) or not clean_text(event.get("action")) or event.get("quantity") is None or event.get("price") is None:
            return False, "missing_bulk_block_table_facts"
    return True, "ok"


def normalized_event(
    *, exchange: str, activity_type: str, activity_sub_type: str | None,
    company_name: Any, symbol: Any, isin: Any, event_date: Any, disclosure_date: Any = None,
    person: Any = None, action: Any = None, quantity: Any = None, price: Any = None,
    value: Any = None, holding_before: Any = None, holding_after: Any = None,
    current_market_price: Any = None, market_price_change_percent: Any = None,
    status: str = "PARTIAL", source_url: Any = None, details: dict[str, Any] | None = None,
    identity: Any = None, source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    company = clean_text(company_name)
    event_dt = parse_datetime(event_date)
    url = clean_text(source_url)
    if not company or not event_dt or not url:
        return None
    quantity_n, price_n, value_n = parse_number(quantity), parse_number(price), parse_number(value)
    if value_n is None and quantity_n is not None and price_n is not None:
        value_n = quantity_n * price_n
    details_obj = json_safe(details if isinstance(details, dict) else {})
    candidate = {
        "source_exchange": (exchange or "NSE").upper(),
        "activity_type": activity_type,
        "activity_sub_type": activity_sub_type,
        "company_name": company[:255],
        "symbol": (clean_text(symbol) or "").upper()[:40] or None,
        "isin": clean_text(isin),
        "event_date": event_dt,
        "disclosure_date": parse_datetime(disclosure_date),
        "person_allottee_client": clean_text(person),
        "action": clean_text(action),
        "quantity": quantity_n,
        "price": price_n,
        "value_inr": value_n,
        "holding_before": parse_number(holding_before),
        "holding_after": parse_number(holding_after),
        "current_market_price": parse_number(current_market_price),
        "market_price_change_percent": parse_number(market_price_change_percent),
        "status": status.upper(),
        "source_url": url[:1500],
        "details_json": details_obj,
        "source_metadata": json_safe(source_metadata if isinstance(source_metadata, dict) else {}),
    }
    candidate["source_key"] = stable_key(
        candidate["source_exchange"], activity_type, activity_sub_type,
        identity if identity is not None else [candidate["symbol"], event_dt.isoformat(), url, candidate["person_allottee_client"], candidate["action"]],
    )
    valid, reason = validate_event(candidate)
    if not valid:
        candidate["status"] = "PARTIAL"
        candidate["source_metadata"] = {**candidate["source_metadata"], "validation_warning": reason}
    return candidate


def normalize_buy_sell(value: Any) -> str | None:
    """Normalize NSE transaction wording to the common Buy/Sell actions."""
    text = norm(value)
    if not text:
        return None
    if any(token in text for token in ("buy", "purchase", "acquisition", "acquired", "bought")):
        return "Buy"
    if any(token in text for token in ("sell", "sale", "disposal", "disposed", "sold")):
        return "Sell"
    return None


def normalize_issue_action(value: Any, default: str | None = None) -> str | None:
    """Normalize QIP/preferential lifecycle stage without implying allotment.

    Instrument/category labels (for example ``Warrant`` or ``Promoter``) are
    not lifecycle actions. Return the caller's fallback for those values rather
    than surfacing them in the dashboard's Action column.
    """
    text = norm(value)
    # Test the lifecycle stages before the generic word "allotment" so that
    # "Pre-Allotment" is not accidentally labelled as completed.
    if "in principle" in text or "pre allotment" in text or "preallotment" in text:
        return "In-Principle"
    if "listing stage" in text or "post allotment" in text or "postallotment" in text:
        return "Listing Stage"
    if re.search(r"\ballotted\b|\ballotment\b", text):
        return "Allotted"
    if any(token in text for token in ("issued", "issue", "issuance", "placement", "preferential")):
        return "Issued"
    return default
