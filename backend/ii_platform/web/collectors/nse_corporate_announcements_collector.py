from __future__ import annotations

"""NSE Corporate Announcements collector used by the PE/Warrant desk.

The desk is intentionally NSE-only.  It reproduces the company-scoped NSE filing
workflow, then follows the filing's Details / Attachment links so the structured
fields come from the filed document rather than from a third-party summary.

Flow per Coverage List NSE symbol:
    company/symbol -> NSE corporate-announcements JSON endpoint -> candidate announcements
    -> Attachment/XBRL URL -> HTML/PDF/XBRL evidence -> structured fields
    -> event classification limited to PE_TAKEOVER / PREFERENTIAL_ISSUE / WARRANT.

A full refresh covers the exact seven-year calendar window. Every symbol is audited
for request errors and document extraction errors; callers must not replace a
previous complete database snapshot when any requested symbol failed.
"""

import asyncio
import hashlib
import html as html_lib
import io
import json
import logging
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from pypdf import PdfReader

from ii_platform.nse import NSEApiClient, NSEApiError

logger = logging.getLogger(__name__)

NSE_PAGE_URL = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
NSE_ALLOWED_HOSTS = {
    "www.nseindia.com",
    "nseindia.com",
    "nsearchives.nseindia.com",
}

MAX_HISTORY_DAYS = 2557
DEFAULT_WINDOW_DAYS = 90
RESPONSE_SPLIT_THRESHOLD = 90
REQUEST_RETRIES = 5
SYMBOL_CONCURRENCY = 8
DOCUMENT_CONCURRENCY = 6
CALLBACK_BATCH_SIZE = 25
DOCUMENT_TEXT_LIMIT = 200_000
LIST_DETAILS_LIMIT = 6_000
OCR_MAX_PAGES = 12
OCR_DPI = 180
OCR_MIN_TEXT_CHARS = 80

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9,en-US;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Referer": NSE_PAGE_URL,
}

DATE_FORMATS = (
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%d-%B-%Y %H:%M:%S",
    "%d-%B-%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y-%m-%d",
)

PREFERENTIAL_MARKERS = (
    "preferential issue",
    "preferential allotment",
    "preferential basis",
    "issue of equity shares on preferential basis",
    "allotment of equity shares on preferential basis",
    "issue of shares on preferential basis",
    "allotment pursuant to preferential issue",
)

CAPITAL_EVENT_TYPES = ("STRATEGIC_ACQUISITION", "WARRANT", "PREFERENTIAL_ISSUE")

WARRANT_MARKERS = (
    "allotment of warrants",
    "allotment of convertible warrants",
    "warrants allotted",
    "allotment pursuant to conversion of warrants",
)
CONTROL_MARKERS = (
    "takeover",
    "open offer",
    "acquire control",
    "acquisition of control",
    "change in control",
    "change of control",
    "acquisition of majority",
    "majority stake",
    "controlling stake",
    "acquisition of shares",
    "acquisition of stake",
    "acquisition of equity stake",
    "purchase of stake",
    "strategic acquisition",
)

PE_MARKERS = (
    "private equity",
    "private equity fund",
    "private equity investor",
    "pe fund",
    "growth capital",
    "growth equity",
    "venture capital",
    "venture fund",
    "alternative investment fund",
    "aif",
    "capital partners",
    "blackstone",
    "bain capital",
    "kkr",
    "carlyle",
    "warburg pincus",
    "advent international",
    "apollo global",
    "tpg",
    "cvc capital",
    "baring private equity",
    "general atlantic",
    "eqt",
    "temasek",
    "gic",
)


def exact_seven_year_window(today: date | None = None) -> tuple[date, date]:
    end = today or datetime.now(timezone.utc).date()
    try:
        start = end.replace(year=end.year - 7)
    except ValueError:
        start = end.replace(year=end.year - 7, month=2, day=28)
    return start, end


def exact_two_year_window(today: date | None = None) -> tuple[date, date]:
    """Deprecated compatibility alias; capital ownership now uses seven years."""
    return exact_seven_year_window(today)


def history_window(history_days: int) -> tuple[date, date]:
    history_days = max(1, int(history_days))
    if history_days >= MAX_HISTORY_DAYS:
        return exact_seven_year_window()
    end = datetime.now(timezone.utc).date()
    return end - timedelta(days=history_days - 1), end


def _clean(value: Any) -> str:
    if value is None:
        return ""
    value = html_lib.unescape(str(value)).replace("\x00", "")
    # Never allow binary/control bytes to leak into PostgreSQL text columns.
    value = "".join(ch for ch in value if ch in "\n\r\t" or ord(ch) >= 32)
    return re.sub(r"\s+", " ", value).strip()


def _clean_multiline(value: Any) -> str:
    """Normalize document text without destroying line/row boundaries.

    PDF parsers often expose labels and values on adjacent lines. Collapsing all
    whitespace into one line makes regex extraction lose those boundaries.
    """
    if value is None:
        return ""
    value = html_lib.unescape(str(value)).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(ch for ch in value if ch in "\n\r\t" or ord(ch) >= 32)
    lines = []
    for raw_line in value.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _date_windows(start: date, end: date, window_days: int):
    """Yield inclusive date windows for bounded NSE API requests."""
    days = max(1, int(window_days))
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=days - 1))
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)



def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).casefold()).strip()


def _parse_number(value: Any, *, crore_to_inr: bool = False, lakh_to_inr: bool = False) -> float | None:
    text = _clean(value).replace(",", "")
    if not text or _norm(text) in {"-", "na", "n a", "n d", "null", "none"}:
        return None
    multiplier = 1.0
    lower = text.casefold()
    if crore_to_inr and re.search(r"\b(?:crore|cr)\b", lower):
        multiplier = 10_000_000.0
    elif lakh_to_inr and re.search(r"\b(?:lakh|lac|lacs)\b", lower):
        multiplier = 100_000.0
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?", text, flags=re.I)
    if not match:
        return None
    try:
        return float(match.group(0)) * multiplier
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = _clean(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    match = re.search(
        r"(\d{1,2}-[A-Za-z]{3,9}-\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
        text,
    )
    if match:
        return _parse_datetime(match.group(1))
    return None


def _field(row: dict[str, Any], *names: str) -> Any:
    lowered = {str(k).casefold(): v for k, v in row.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if value not in (None, ""):
            return value
    return None


def _valid_url(value: Any) -> str | None:
    raw = _clean(value).replace("\\/", "/")
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = urljoin("https://www.nseindia.com", raw)
    parsed = urlparse(raw)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"}:
        return None
    if host not in NSE_ALLOWED_HOSTS and not host.endswith(".nsearchives.nseindia.com"):
        return None
    return raw


def _has_valid_announcement_payload_shape(payload: Any) -> bool:
    """Distinguish a valid empty NSE feed from a 200 response with a changed schema."""
    collection_keys = {"data", "rows", "records", "result", "announcements", "corporateannouncements"}
    row_keys = {"symbol", "sm_name", "company name", "subject", "desc", "attchmntfile", "broadcastdate", "an_dt", "seq_id"}

    def visit(value: Any, depth: int = 0) -> bool:
        if depth > 12:
            return False
        if isinstance(value, list):
            if not value:
                return depth == 0
            return any(
                isinstance(item, dict) and bool({str(key).casefold() for key in item} & row_keys)
                for item in value
            )
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() in collection_keys and isinstance(child, list):
                    if not child or any(
                        isinstance(item, dict) and bool({str(field).casefold() for field in item} & row_keys)
                        for item in child
                    ):
                        return True
                if isinstance(child, dict) and visit(child, depth + 1):
                    return True
                if isinstance(child, list) and child and visit(child, depth + 1):
                    return True
        return False

    return visit(payload)


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    candidates: list[tuple[int, int, list[dict[str, Any]]]] = []

    def visit(value: Any, depth: int = 0) -> None:
        if isinstance(value, list):
            rows = [item for item in value if isinstance(item, dict)]
            if rows:
                score = 0
                for row in rows[:40]:
                    keys = {str(k).casefold() for k in row}
                    score += 1 if keys & {
                        "symbol",
                        "sm_name",
                        "company name",
                        "subject",
                        "desc",
                        "attchmntfile",
                        "broadcastdate",
                        "an_dt",
                        "seq_id",
                    } else 0
                if score:
                    candidates.append((score * 1000 + min(len(rows), 500), depth, rows))
            for child in value[:10]:
                visit(child, depth + 1)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child, depth + 1)

    visit(payload)
    if not candidates:
        return []
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return candidates[0][2]


def _table_row_cells(tr) -> list[str]:
    result: list[str] = []
    for cell in tr.find_all(["th", "td"]):
        value = _clean(cell.get_text(" ", strip=True))
        try:
            span = max(1, int(cell.get("colspan") or 1))
        except (TypeError, ValueError):
            span = 1
        result.extend([value] * span)
    return result


def _announcement_page_url(symbol: str) -> str:
    from urllib.parse import urlencode
    return f"{NSE_PAGE_URL}?{urlencode({'symbol': symbol, 'tabIndex': 'equity'})}"


def _extract_stake_percent(text: str) -> float | None:
    """Extract a percentage explicitly associated with a sale/disposal/exit."""
    raw = _clean_multiline(text)
    if not raw:
        return None
    sale_terms = r"sell(?:ing)?|sold|sale|dispose(?:d|s|al)?|disposal|divest(?:ed|ment)?|exit(?:ing)?"
    patterns = (
        rf"(?:{sale_terms})[^\n]{{0,240}}?(\d{{1,3}}(?:\.\d+)?)\s*(?:%|percent)",
        rf"(\d{{1,3}}(?:\.\d+)?)\s*(?:%|percent)\s*(?:stake|shareholding|holding|equity)[^\n]{{0,240}}?(?:{sale_terms})",
        rf"(?:{sale_terms})[^\n]{{0,240}}?(?:stake|shareholding|holding)[^\n]*?(\d{{1,3}}(?:\.\d+)?)\s*(?:%|percent)",
        r"(?:proposed|agreed|completed)\s+(?:sale|disposal|divestment)[^\n]{0,240}?(\d{1,3}(?:\.\d+)?)\s*(?:%|percent)",
    )
    values: list[float] = []
    for pattern in patterns:
        for match in re.finditer(pattern, raw, flags=re.I | re.S):
            try:
                value = float(match.group(1))
            except (TypeError, ValueError):
                continue
            if 0 < value <= 100:
                values.append(value)
    above = [value for value in values if value > 20]
    return max(above) if above else (max(values) if values else None)


def _has_definitive_warrant_allotment(text: str) -> bool:
    raw = _clean_multiline(text)
    hay = _norm(raw)
    if "warrant" not in hay:
        return False
    # A proposal/approval to issue warrants is not an allotment. Also avoid treating
    # "equity shares allotted on conversion of warrants" as a warrant allotment.
    if re.search(r"\b(?:proposed|propose|proposes|recommend(?:ed|ation)?|approval|seeking approval|subject to approval|conversion of warrants)\b", raw, flags=re.I):
        if not re.search(r"\b(?:warrants?\s+(?:have been|were|are|is)\s+allotted|allotted\s+(?:\d[\d,]*(?:\.\d+)?\s+)?warrants?|allotment\s+of\s+(?:\d[\d,]*(?:\.\d+)?\s+)?warrants?)\b", raw, flags=re.I | re.S):
            return False
    return bool(re.search(
        r"\ballotment\s+of\s+(?:.{0,90}?\s+)?(?:(?:fully\s+)?convertible\s+)?warrants?\b|"
        r"\b(?:(?:fully\s+)?convertible\s+)?warrants?\s+(?:have been|were|are|is)?\s*allotted\b|"
        r"\ballotted\b.{0,100}?\b(?:(?:fully\s+)?convertible\s+)?warrants?\b",
        raw,
        flags=re.I | re.S,
    ))


def _has_preferential_issue(text: str) -> bool:
    raw = _clean(text)
    hay = _norm(raw)
    if "preferential" not in hay:
        return False
    # NSE wording appears in both directions, e.g. "preferential issue" and
    # "allotment ... on preferential basis". Require an actual issue/allotment
    # concept near the word preferential; generic mentions are rejected.
    forward = re.search(
        r"\bpreferential\b.{0,240}\b(?:issue|allot(?:ment|ted)|shares?|securities)\b",
        raw,
        flags=re.I | re.S,
    )
    reverse = re.search(
        r"\b(?:issue|allot(?:ment|ted)|shares?|securities)\b.{0,240}\bpreferential\b",
        raw,
        flags=re.I | re.S,
    )
    basis = re.search(
        r"\b(?:issue|allot(?:ment|ted)|shares?|securities)\b.{0,120}\bpreferential\s+basis\b|\bpreferential\s+basis\b.{0,120}\b(?:issue|allot(?:ment|ted)|shares?|securities)\b",
        raw,
        flags=re.I | re.S,
    )
    return bool(forward or reverse or basis)


def classify_capital_event_details(text: str) -> dict[str, Any] | None:
    """Deterministically classify an event from filing content, not just subject."""
    raw = _clean_multiline(text)
    hay = _norm(raw)
    if not hay:
        return None

    # Preferential issue/allotment is identified from explicit transaction language.
    if _has_preferential_issue(raw):
        confidence = 0.97 if re.search(r"preferential\s+(?:issue|allotment)|allot(?:ment|ted).*preferential", raw, re.I | re.S) else 0.90
        return {"event_type": "PREFERENTIAL_ISSUE", "event_sub_type": "ISSUE", "confidence": confidence}

    warrant_conversion = bool(re.search(r"\b(?:conversion|converted|exercise|exercised)\b.{0,140}\bwarrants?\b|\bwarrants?\b.{0,140}\b(?:conversion|converted|exercise|exercised)\b", raw, re.I | re.S))
    if warrant_conversion and re.search(r"\b(?:equity\s+shares?|shares?)\b", raw, re.I):
        return {
            "event_type": "WARRANT",
            "event_sub_type": "CONVERSION",
            "confidence": 0.97,
        }

    if _has_definitive_warrant_allotment(raw):
        return {
            "event_type": "WARRANT",
            "event_sub_type": "ISSUE",
            "confidence": 0.96,
        }

    # Regulation 29 / SAST language is a structured ownership event even when the
    # announcement subject is generic. This is intentionally content-driven.
    regulation_29 = bool(re.search(r"regulation\s*29|sast(?:\s+regulations?)?|substantial\s+acquisition", raw, re.I))
    ownership_signal = bool(re.search(
        r"\b(?:acquir(?:e|ed|ing)|acquisition|purchase|purchased|buy|bought|dispose|disposed|disposal|sell|sold|sale|divest|exit|transfer(?:red)?)\b.{0,220}\b(?:shares?|stake|shareholding|voting\s+rights|equity)\b"
        r"|\b(?:shares?|stake|shareholding|voting\s+rights|equity)\b.{0,220}\b(?:acquir(?:e|ed|ing)|acquisition|purchase|purchased|dispose|disposed|disposal|sell|sold|sale|divest|exit|transfer(?:red)?)\b",
        raw,
        re.I | re.S,
    ))
    control_signal = any(marker in hay for marker in CONTROL_MARKERS)
    if regulation_29 or ownership_signal or control_signal:
        disposal = bool(re.search(r"\b(?:dispose|disposed|disposal|sell|sold|sale|divest|divestment|exit|exiting|transferor)\b", raw, re.I))
        return {
            "event_type": "STRATEGIC_ACQUISITION",
            "event_sub_type": "DISPOSAL" if disposal else "ACQUISITION",
            "confidence": 0.98 if regulation_29 else (0.93 if control_signal else 0.86),
        }
    return None


def _classify(text: str) -> str | None:
    details = classify_capital_event_details(text)
    return str(details.get("event_type")) if details else None

def classify_capital_event(text: str) -> str | None:
    """Public wrapper used by the Regulatory read path to validate stored legacy rows."""
    return _classify(text)

def _investor_category(text: str) -> str | None:
    hay = _norm(text)
    if any(marker in hay for marker in PE_MARKERS):
        return "Private Equity / Institutional Investor"
    if any(marker in hay for marker in CONTROL_MARKERS):
        return "Acquirer / Control Investor"
    if "preferential" in hay:
        return "Preferential Allottee / Investor"
    if "warrant" in hay:
        return "Warrant Allottee / Investor"
    return None


def _unique_candidates(values: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = _clean(value)
        if not candidate:
            continue
        key = _norm(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _normalise_table_text(tables: list[dict[str, Any]] | None) -> str:
    chunks: list[str] = []
    for table in tables or []:
        for row in table.get("rows") or []:
            cells = [_clean(cell) for cell in (row or [])]
            if any(cells):
                chunks.append(" | ".join(cells))
    return "\n".join(chunks)


def _table_rows(tables: list[dict[str, Any]] | None) -> Iterable[list[str]]:
    """Yield cleaned PDF/HTML table rows with empty trailing cells removed."""
    for table in tables or []:
        for raw_row in table.get("rows") or []:
            row = [_clean(cell) for cell in (raw_row or [])]
            while row and not row[-1]:
                row.pop()
            if any(row):
                yield row


def _header_matches(header: str, aliases: tuple[str, ...]) -> bool:
    normalized = _norm(header)
    if not normalized:
        return False
    for alias in aliases:
        needle = _norm(alias)
        if not needle:
            continue
        # Exact header phrases are preferred. A short alias must not match a
        # larger header such as "Category Maximum No., Pre-Preferential Issue".
        if normalized == needle:
            return True
        if len(needle) >= 8 and (f" {needle} " in f" {normalized} " or normalized.startswith(needle + " ") or normalized.endswith(" " + needle)):
            return True
    return False


def _is_placeholder(value: str) -> bool:
    normalized = _norm(value)
    return normalized in {
        "", "-", "—", "–", "na", "n a", "n a", "nil", "none", "null",
        "not disclosed", "not available", "not provided", "the company", "company",
        "category", "category no", "category number", "serial no", "sr no",
        "s no", "s r no", "details not available", "refer annexure", "annexure",
        "not applicable", "n a / not applicable",
    }


_INVESTOR_PROSE_NOISE_MARKERS = (
    "dear sir", "dear madam", "dear sir/madam", "sir/madam",
    "compliance", "with reference to", "in continuation", "we wish to inform",
    "we hereby inform", "we are pleased to inform", "kindly take note",
    "thank you", "thanks", "regards", "faithfully", "secretarial",
    "office", "registered office", "corporate office", "business",
    "engaged in the business", "principal business", "company is",
    "the company", "shareholders", "shareholder", "members",
    "transfer agent", "registrar", "listing department", "stock exchange",
    "subject", "ref no", "reference no", "annexure", "disclaimer",
    "this is to inform", "this announcement", "pursuant to",
)

_INVESTOR_HEADER_NOISE_MARKERS = (
    "pre preferential", "post allotment", "category maximum no",
    "serial no", "category no", "category number", "sr no", "s r no",
    "refer annexure", "details not available",
)


def _looks_like_entity_name(candidate: str) -> bool:
    """Reject PDF prose/column-noise while retaining ordinary fund/company names."""
    normalized = _norm(candidate)
    if not normalized:
        return False
    if any(marker in normalized for marker in _INVESTOR_PROSE_NOISE_MARKERS):
        return False
    if any(marker in normalized for marker in _INVESTOR_HEADER_NOISE_MARKERS):
        return False

    # OCR corruption in NSE letters often turns whole prose lines into comma-separated
    # one-letter fragments, e.g. "ear Sir/Madam, blige, hanking You, ompliance O...".
    words = re.findall(r"[A-Za-z0-9&'./-]+", candidate)
    if not words:
        return False
    short_words = sum(1 for word in words if len(word) <= 2 and word.isalpha())
    if short_words >= 3:
        return False
    if len(words) >= 8 and short_words / max(len(words), 1) >= 0.30:
        return False
    if candidate.count(",") >= 2 and len(words) >= 7:
        return False

    # Filing prose is often sentence-like rather than name-like. Prefer an empty
    # investor over displaying a plausible-looking but incorrect paragraph fragment.
    if len(candidate) > 140:
        return False
    if re.search(r"[.!?]\s*$", candidate) and len(words) >= 5:
        return False
    if re.search(r"\b(?:is|are|was|were|has|have|shall|will|engaged|primarily)\b", normalized) and len(words) >= 7:
        return False

    alpha = sum(char.isalpha() for char in candidate)
    return alpha >= 3


def _clean_investor_candidate(value: object) -> str | None:
    """Return an investor/allottee only when the candidate is structurally name-like."""
    candidate = _clean(value).strip(" \t,;:|-")
    normalized = _norm(candidate)
    # These artifacts commonly come from PDF table borders, OCR-cropped words,
    # and the tail of an unrelated annexure/header; they are never entity names.
    if (not candidate or _is_placeholder(candidate) or
            any(char in candidate for char in ";:|") or
            re.search(r"\b(?:nexure|nitries|nitrie|particulars|category\s+no|sr\s+no)\b", normalized)):
        return None
    if normalized in {
        "s refer annexure", "refer annexure a", "not disclosed", "category no",
    }:
        return None
    return candidate if _looks_like_entity_name(candidate) else None


def clean_investor_name(value: object) -> str | None:
    """Public sanitizer used by snapshot builders for legacy stored investor fields."""
    return _clean_investor_candidate(value)


def _clean_investor_candidates(values: Iterable[object]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        candidate = _clean_investor_candidate(value)
        if not candidate:
            continue
        key = _norm(candidate)
        if key and key not in seen:
            seen.add(key)
            cleaned.append(candidate)
    return cleaned[:12]


def _table_column_records(
    tables: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    """Map wide NSE filing tables into field/value records.

    NSE PDFs commonly put the field names in one or two header rows and the actual
    values in subsequent rows. The previous extractor treated the same cell index
    as the answer even when the header was multi-row, which produced values such as
    ``Category No.`` or blank prices. This mapper first identifies useful header
    columns and then reads the corresponding data rows.
    """
    records: list[dict[str, str]] = []
    field_headers = (
        "allottee", "allottee name", "name allottee", "name allottees", "name of allottee",
        "name of proposed allottee", "proposed allottee", "allottee/investor",
        "investor", "acquirer", "seller", "transferor", "transferee",
        "number of shares", "no. of shares", "quantity", "number of warrants",
        "issue price", "price per share", "price per warrant", "total consideration",
        "aggregate amount", "total value", "shareholding", "stake", "percentage of share capital",
    )

    for table in tables or []:
        rows = [[_clean(cell) for cell in (row or [])] for row in table.get("rows") or []]
        rows = [row for row in rows if any(row)]
        if len(rows) < 2:
            continue
        # Compact vertical NSE annexures are already key/value rows. Do not attempt
        # to reinterpret their first few rows as a multi-row table header.
        if len(rows[0]) <= 3 and _header_matches(rows[0][0], field_headers):
            continue
        # Header can occupy up to three rows. Build combined headers for each of
        # the first few rows, stopping at the first row that looks like data.
        for header_end in range(min(3, len(rows))):
            header_rows = rows[: header_end + 1]
            width = max(len(row) for row in header_rows)
            headers: list[str] = []
            for col in range(width):
                parts: list[str] = []
                for row in header_rows:
                    value = _clean(row[col]) if col < len(row) else ""
                    if not value or re.fullmatch(r"\d+", value):
                        continue
                    if _norm(value) not in {_norm(part) for part in parts}:
                        parts.append(value)
                headers.append(" ".join(parts))

            meaningful = sum(
                _header_matches(header, aliases)
                for header, aliases in (
                    (header, ("allottee", "allottee name", "name allottee", "name allottees", "name of allottee", "name of proposed allottee", "proposed allottee", "allottee/investor", "investor name", "name of investor", "name of acquirer", "acquirer name", "selling shareholder", "seller", "transferor", "transferee"))
                    for header in headers
                )
            )
            numeric_headers = sum(
                _header_matches(header, aliases)
                for header, aliases in (
                    (header, ("number of shares", "no. of shares", "quantity", "number of warrants", "warrants allotted", "number of securities", "issue price", "price per share", "price per warrant", "total consideration", "aggregate amount", "total value", "shareholding %", "stake %", "percentage of share capital", "shares acquired (%)", "shares disposed (%)"))
                    for header in headers
                )
            )
            if meaningful == 0 or numeric_headers == 0:
                continue

            for row in rows[header_end + 1 :]:
                if len(row) < 2:
                    continue
                record: dict[str, str] = {}
                for col, header in enumerate(headers):
                    if not header or col >= len(row):
                        continue
                    value = _clean(row[col])
                    if _is_placeholder(value):
                        continue
                    record[header] = value
                if record:
                    records.append(record)
            # The first viable header interpretation is normally the intended one.
            if records:
                break
    return records


def _record_values(
    records: list[dict[str, str]],
    aliases: tuple[str, ...],
    *,
    limit: int = 20,
) -> list[str]:
    values: list[str] = []
    for record in records:
        for header, value in record.items():
            if _header_matches(header, aliases) and not _is_placeholder(value):
                values.append(value)
                break
    return _unique_candidates(values)[:limit]


def _table_labeled_values(tables: list[dict[str, Any]] | None, labels: tuple[str, ...]) -> list[str]:
    """Extract values from key/value tables and mapped wide filing tables."""
    needles = [_norm(label) for label in labels]
    found: list[str] = []

    def usable(value: str) -> bool:
        return bool(value) and not _is_placeholder(value)

    # First: robust column-aware extraction from wide tables.
    records = _table_column_records(tables)
    for value in _record_values(records, labels, limit=50):
        if usable(value):
            found.append(value)

    # Second: direct key/value rows and short tables. Do not treat a wide header
    # row as a key/value row: its neighbouring cells are column labels, not answers.
    for row in _table_rows(tables):
        if len(row) >= 4:
            continue
        normalized = [_norm(cell) for cell in row]
        for idx, cell in enumerate(normalized):
            if not cell or not any(needle in cell or cell in needle for needle in needles):
                continue
            for candidate in row[idx + 1 : idx + 4]:
                if usable(candidate) and not any(needle in _norm(candidate) for needle in needles):
                    found.append(candidate)

    return _unique_candidates(found)[:50]


def _line_labeled_values(text: str, labels: tuple[str, ...], *, limit: int = 50) -> list[str]:
    """Read PDF labels whose value is on the same or following line."""
    found: list[str] = []
    line_list = [line.strip() for line in _clean_multiline(text).split("\n") if line.strip()]
    ordered = sorted((str(label).strip() for label in labels if str(label).strip()), key=len, reverse=True)
    if not ordered:
        return []
    label_pattern = "|".join(re.escape(label) for label in ordered)
    for index, line in enumerate(line_list):
        match = re.search(rf"(?:^|[|])(?:{label_pattern})(?:\s*\([^)]*\))?\s*(?::|=|-|\||is)?\s*(.*)$", line, flags=re.I)
        if not match:
            normalized_line = _norm(line)
            if not any(_norm(label) in normalized_line for label in ordered):
                continue
            tail = ""
            for label in ordered:
                pos = normalized_line.find(_norm(label))
                if pos >= 0:
                    raw_pos = line.casefold().find(label.casefold())
                    if raw_pos >= 0:
                        tail = line[raw_pos + len(label):].strip(" :-|=")
                    break
        else:
            tail = match.group(1).strip()
        if not tail and index + 1 < len(line_list):
            next_line = line_list[index + 1]
            if next_line:
                tail = next_line.strip()
        if tail and not _is_placeholder(tail) and not any(_norm(label) == _norm(tail) for label in ordered):
            found.append(tail)
        if len(found) >= limit:
            break
    return _unique_candidates(found)[:limit]


def _line_labeled_number(text: str, labels: tuple[str, ...]) -> float | None:
    values = _line_labeled_values(text, labels, limit=20)
    for value in values:
        number = _parse_number(value)
        if number is not None:
            return number
    return None


def _line_labeled_money(text: str, labels: tuple[str, ...]) -> float | None:
    values = _line_labeled_values(text, labels, limit=20)
    for value in values:
        amount = _parse_money_value(value)
        if amount is not None:
            return amount
    return None


def _line_labeled_percent(text: str, labels: tuple[str, ...]) -> float | None:
    values = _line_labeled_values(text, labels, limit=20)
    for value in values:
        match = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", value)
        if match:
            candidate = float(match.group(1))
            if 0 < candidate <= 100:
                return candidate
    return None


def _extract_investors(text: str, tables: list[dict[str, Any]] | None = None) -> list[str]:
    """Extract investor/allottee names using explicit labels and strict validation.

    Generic "investment by/from" and "acquisition by/from" patterns are intentionally
    excluded because they frequently match ordinary filing prose instead of identities.
    """
    found: list[str] = []
    found.extend(_table_labeled_values(
        tables,
        (
            "name of allottee", "allottee name", "name allottee", "name allottees",
            "name of proposed allottee", "proposed allottee", "allottee/investor",
            "name of investor", "investor name", "name of acquirer", "acquirer name", "name of seller", "seller name",
            "name of transferor", "transferor name", "name of transferee", "transferee name",
            "name of person", "name of entity",
        ),
    ))

    patterns = (
        r"(?:name of (?:the )?(?:proposed )?(?:allottee|allottees|investor|investors|acquirer|acquirers|seller|transferor|transferee|entity|person))\s*[:\-]?\s*([^\n;|]{3,180})",
        r"(?:proposed )?(?:allottee|allottees|investor|investors|acquirer|acquirers|seller|transferor|transferee)\s*[:\-]\s*([^\n;|]{3,180})",
        r"(?:shares|warrants|securities)\s+(?:are|is)\s+(?:proposed\s+to\s+be\s+)?allotted\s+to\s+([^\n.;|]{3,180})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            value = _clean(match.group(1))
            value = re.split(
                r"\s+(?:for|at|of|towards|on|subject to|being|having|along with|together with)\b",
                value,
                maxsplit=1,
                flags=re.I,
            )[0].strip(" ,:-")
            cleaned = _clean_investor_candidate(value)
            if cleaned:
                found.append(cleaned)
    return _unique_candidates(found)[:12]


def _first_labeled_number(text: str, labels: tuple[str, ...], units: str = r"") -> float | None:
    label_expr = "|".join(re.escape(label) for label in labels)
    pattern = rf"(?:{label_expr})\s*(?:\([^)]*\))?\s*(?:of|at|:|=|-|\|)?\s*(?:rs\.?|inr|₹)?\s*([0-9][0-9,]*(?:\.\d+)?)\s*{units}"
    match = re.search(pattern, text, flags=re.I | re.S)
    return _parse_number(match.group(1)) if match else None


def _extract_quantity(text: str, event_type: str) -> float | None:
    if event_type == "WARRANT":
        value = _first_labeled_number(
            text,
            (
                "number of warrants allotted", "no. of warrants allotted", "number of warrants",
                "no of warrants", "warrants allotted", "number of warrants issued",
            ),
        )
        if value is not None:
            return value
        patterns = (
            r"([0-9][0-9,]*(?:\.\d+)?)\s+(?:fully convertible\s+)?warrants?\b",
            r"warrants?\s*[:\-]\s*([0-9][0-9,]*(?:\.\d+)?)",
        )
    else:
        value = _first_labeled_number(
            text,
            (
                "number of equity shares allotted", "number of equity shares", "number of shares allotted",
                "no. of equity shares", "no of equity shares", "number of equity shares issued",
                "number of securities allotted", "number of securities",
            ),
        )
        if value is not None:
            return value
        patterns = (
            r"([0-9][0-9,]*(?:\.\d+)?)\s+(?:equity\s+)?shares\b",
            r"shares?\s*[:\-]\s*([0-9][0-9,]*(?:\.\d+)?)",
        )
    patterns += (
        r"(?:issue size|securities offered|number of securities|quantity|no\.? of shares|no\.? of warrants)\s*[:\-]?\s*([0-9][0-9,]*(?:\.\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            value = _parse_number(match.group(1))
            if value is not None:
                return value
    return None


def _extract_price(text: str) -> float | None:
    patterns = (
        r"(?:issue|subscription|exercise|conversion|offer)\s+price(?:\s+per\s+(?:equity\s+)?(?:share|warrant|security))?\s*(?:of|at|@|:|=|\|)?\s*(?:rs\.?|inr|₹)?\s*([0-9][0-9,]*(?:\.\d+)?)",
        r"price\s+per\s+(?:equity\s+)?(?:share|warrant|security)\s*(?:of|at|@|:|=|\|)?\s*(?:rs\.?|inr|₹)?\s*([0-9][0-9,]*(?:\.\d+)?)",
        r"\bprice\s*[:=\-]\s*(?:rs\.?|inr|₹)?\s*([0-9][0-9,]*(?:\.\d+)?)",
        r"(?:at|@|of)\s*(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.\d+)?)\s*(?:per\s+)?(?:share|warrant|security)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            value = _parse_number(match.group(1))
            if value is not None:
                return value
    return None



def _parse_money_value(value: Any) -> float | None:
    """Parse an NSE table money cell into INR, including Cr/Lakh/Mn units."""
    text = _clean(value)
    if not text:
        return None
    number = _parse_number(text)
    if number is None:
        return None
    lower = text.casefold()
    if re.search(r"\b(?:crore|cr)\b", lower):
        return number * 10_000_000.0
    if re.search(r"\b(?:lakh|lac|lacs)\b", lower):
        return number * 100_000.0
    if re.search(r"\b(?:million|mn)\b", lower):
        return number * 1_000_000.0
    return number

def _extract_amount(text: str) -> float | None:
    patterns = (
        r"(?:total|aggregate)\s+(?:consideration|issue\s+size|issue\s+amount|subscription\s+amount|investment\s+amount|value)\s*(?:of|at|:|=|\|)?\s*(?:rs\.?|inr|₹)?\s*([0-9][0-9,.]*(?:\.\d+)?)\s*(crore|cr|lakh|lac|million|mn)?",
        r"(?:aggregate\s+amount|total\s+consideration|total\s+value|aggregate\s+issue\s+size|value)\s*[:\-]?\s*(?:rs\.?|inr|₹)?\s*([0-9][0-9,.]*(?:\.\d+)?)\s*(crore|cr|lakh|lac|million|mn)?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        value = _parse_number(match.group(1))
        if value is None:
            continue
        unit = (match.group(2) or "").casefold()
        if unit in {"crore", "cr"}:
            return value * 10_000_000
        if unit in {"lakh", "lac"}:
            return value * 100_000
        if unit in {"million", "mn"}:
            return value * 1_000_000
        return value
    return None


def _event_numeric_context(text: str, event_type: str) -> str:
    """Keep narrative numeric fallbacks near explicit capital-event language.

    NSE PDFs often contain financial statements, correspondence, and annexures
    alongside the actual transaction. Scanning the entire document for the first
    number labelled only "shares", "price" or "value" can silently import unrelated
    figures. Structured labelled tables are handled separately; this context is only
    used for narrative fallbacks.
    """
    raw = _clean_multiline(text)
    if not raw:
        return ""
    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    # Some HTML/XML documents have one unbroken paragraph. Split at sentence/table
    # boundaries so the relevance check still scopes the extract.
    if len(lines) == 1 and len(lines[0]) > 500:
        lines = [part.strip() for part in re.split(r"(?<=[.;])\s+(?=[A-Z₹])", lines[0]) if part.strip()]

    kind = str(event_type or "").upper()
    ownership = r"(?:acquir(?:e|ed|ing|er|or)|acquisition|purchase|purchased|bought|open offer|takeover|stake|shareholding|transfer(?:red)?|regulation\s*29|sast)"
    capital = r"(?:allot(?:ment|ted|tee|tee\s+name)?|issue|issuance|subscription|conversion|exercise|warrant|preferential)"
    relevance: list[re.Pattern[str]] = []
    if kind == "WARRANT":
        relevance.append(re.compile(r"\bwarrants?\b.*\b(?:allot(?:ment|ted)?|issue|issuance|conversion|converted|exercise|exercised|price|shares?)\b|\b(?:allot(?:ment|ted)?|issue|issuance|conversion|exercise)\b.*\bwarrants?\b", re.I))
    elif kind == "PREFERENTIAL_ISSUE":
        relevance.append(re.compile(r"\bpreferential\b.{0,240}\b(?:issue|allot(?:ment|ted)?|shares?|securities|price|consideration)\b|\b(?:issue|allot(?:ment|ted)?|shares?|securities)\b.{0,240}\bpreferential\b", re.I))
    elif kind == "STRATEGIC_ACQUISITION":
        relevance.append(re.compile(rf"{ownership}.*(?:shares?|stake|holding|price|consideration|value|\d\s*%)|(?:shares?|stake|holding|price|consideration|value|\d\s*%).*{ownership}", re.I))
    else:
        relevance.append(re.compile(rf"(?:{capital}|{ownership}).*(?:shares?|securities|price|consideration|value|stake|\d\s*%)|(?:shares?|securities|price|consideration|value|stake|\d\s*%).*(?:{capital}|{ownership})", re.I))

    # Do not carry clearly unrelated annual-report/financial-statement lines into a
    # match window, even if they happen to sit next to a transaction paragraph.
    unrelated = re.compile(r"\b(?:segment revenue|financial assets|accounting policies|operating decision maker|segment assets|revenue and expense|balance sheet|profit and loss)\b", re.I)
    matched_indexes = {i for i, line in enumerate(lines) if any(pattern.search(line) for pattern in relevance)}
    if not matched_indexes:
        return ""
    selected: set[int] = set()
    for index in matched_indexes:
        selected.add(index)
        for adjacent in (index - 1, index + 1):
            if 0 <= adjacent < len(lines) and len(lines[adjacent]) <= 260 and not unrelated.search(lines[adjacent]):
                selected.add(adjacent)
    return "\n".join(lines[index] for index in sorted(selected))


def _extract_value_fields(
    document_text: str,
    event_type: str,
    tables: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    table_text = _normalise_table_text(tables)
    # Keep actual line boundaries: label/value extraction relies on them to avoid
    # consuming the next field or paragraph as an Allottee name.
    combined = _clean_multiline(f"{document_text}\n{table_text}")
    numeric_context = _event_numeric_context(combined, event_type)
    records = _table_column_records(tables)

    investor_aliases = (
        "name of allottee", "allottee name", "name allottee", "name allottees",
        "name of proposed allottee", "proposed allottee", "allottee/investor", "allottee",
        "name of investor", "investor name", "name of acquirer", "acquirer name", "selling shareholder", "seller", "transferor", "transferee",
    )
    quantity_aliases = (
        "number of equity shares allotted", "number of equity shares", "number of shares allotted",
        "number of shares", "no. of equity shares", "no of equity shares", "no. of shares", "no of shares",
        "number of securities allotted", "number of securities", "quantity", "number of warrants", "warrants allotted",
        "no. of warrants", "no of warrants", "no. of securities", "no of securities",
    )
    price_aliases = (
        "issue price per share", "issue price per warrant", "issue price", "price per share",
        "price per warrant", "purchase price", "sale price", "price",
    )
    amount_aliases = (
        "total consideration", "aggregate consideration", "aggregate amount", "total amount",
        "aggregate issue size", "total issue size", "total value", "aggregate value", "consideration", "value",
    )
    stake_aliases = (
        "percentage of share capital", "shareholding %", "shareholding percentage", "% stake", "stake %",
        "percentage stake", "% of total share capital", "shares acquired (%)", "shares disposed (%)",
        "acquisition (%)", "disposal (%)", "% holding",
    )

    # Prefer direct label/value extraction first. It is the correct interpretation
    # for compact NSE annexures such as "Issue Price | Rs. 155". The wide-record
    # parser then handles the tabular cases where labels live in a header row.
    investors = _table_labeled_values(tables, investor_aliases)
    if not investors:
        investors = _record_values(records, investor_aliases, limit=20)

    quantity = None
    for value in _table_labeled_values(tables, quantity_aliases):
        quantity = _parse_number(value)
        if quantity is not None:
            break
    if quantity is None:
        for value in _record_values(records, quantity_aliases, limit=20):
            quantity = _parse_number(value)
            if quantity is not None:
                break

    price = None
    for value in _table_labeled_values(tables, price_aliases):
        price = _parse_number(value)
        if price is not None:
            break
    if price is None:
        for value in _record_values(records, price_aliases, limit=20):
            price = _parse_number(value)
            if price is not None:
                break

    amount = None
    for value in _table_labeled_values(tables, amount_aliases):
        amount = _parse_money_value(value) or _extract_amount(value)
        if amount is not None:
            break
    if amount is None:
        for value in _record_values(records, amount_aliases, limit=20):
            amount = _parse_money_value(value) or _extract_amount(value)
            if amount is not None:
                break

    stake_percent = None
    for value in _table_labeled_values(tables, stake_aliases):
        match = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", _clean(value))
        if match:
            try:
                candidate = float(match.group(1))
            except ValueError:
                continue
            if 0 < candidate <= 100:
                stake_percent = candidate
                break
    if stake_percent is None:
        for value in _record_values(records, stake_aliases, limit=20):
            match = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", _clean(value))
            if match:
                try:
                    candidate = float(match.group(1))
                except ValueError:
                    continue
                if 0 < candidate <= 100:
                    stake_percent = candidate
                    break

    # Prose/table-text fallbacks remain available for PDFs without machine-readable
    # table labels or for documents where the important value occurs in narrative text.
    if not investors:
        investors = _line_labeled_values(document_text, investor_aliases, limit=20)
    if not investors:
        investors = _extract_investors(combined, tables)
    if numeric_context:
        if quantity is None:
            quantity = _line_labeled_number(numeric_context, quantity_aliases)
        if quantity is None:
            quantity = _extract_quantity(numeric_context, event_type)
        if price is None:
            price = _line_labeled_number(numeric_context, price_aliases)
        if price is None:
            price = _extract_price(numeric_context)
        if amount is None:
            amount = _line_labeled_money(numeric_context, amount_aliases)
        if amount is None:
            amount = _extract_amount(numeric_context)
        if stake_percent is None:
            stake_percent = _line_labeled_percent(numeric_context, stake_aliases)
        if stake_percent is None:
            stake_percent = _extract_stake_percent(numeric_context)

    # Never publish negative/zero quantities, prices, amounts, or impossible stake
    # percentages. If a real issue has both a unit price and quantity, their product
    # is a better fallback than an inconsistent prose amount pulled from nearby text.
    invalid_quantity = quantity is not None and quantity <= 0
    if invalid_quantity:
        # A negative share/warrant count is incompatible with the acquisition/issue
        # fields shown in this desk. Do not leave a price/value from the same likely
        # misaligned table row behind as if it were independently trustworthy.
        quantity = None
        price = None
        amount = None
    if price is not None and price <= 0:
        price = None
    if amount is not None and amount <= 0:
        amount = None
    if stake_percent is not None and not 0 < stake_percent <= 100:
        stake_percent = None
    amount_inconsistent = False
    if quantity is not None and price is not None and amount is not None:
        calculated = quantity * price
        if calculated > 0 and not (calculated * 0.5 <= amount <= calculated * 2.0):
            # Keep the source-supported quantity and price, but do not replace a
            # contradictory source amount with a guessed value. Missing amounts can
            # still be derived below when the source did not provide one at all.
            amount = None
            amount_inconsistent = True

    investors = _clean_investor_candidates(investors)
    return {
        "investors": investors,
        "quantity": quantity,
        "price": price,
        "amount_inr": amount if amount is not None else (None if amount_inconsistent else (price * quantity if price is not None and quantity is not None else None)),
        "stake_percent": stake_percent,
        "table_text": table_text[:20000],
    }


def _extract_labeled_text(text: str, aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        match = re.search(rf"\b{re.escape(alias)}\b\s*[:\-]?\s*([^\n]{{1,500}})", text or "", re.I)
        if match:
            value = _clean(match.group(1))
            if value:
                return value
    return None


def _extract_ownership_metrics(document_text: str, tables: list[dict[str, Any]] | None = None) -> dict[str, float | None]:
    combined = _clean_multiline(document_text) + "\n" + _normalise_table_text(tables)
    pre_aliases = (
        "pre acquisition shareholding", "pre acquisition holding", "shareholding before acquisition",
        "holding before acquisition", "before acquisition", "pre transaction shareholding", "pre transaction holding",
        "prior shareholding", "prior holding", "before transaction",
    )
    post_aliases = (
        "post acquisition shareholding", "post acquisition holding", "shareholding after acquisition",
        "holding after acquisition", "after acquisition", "post transaction shareholding", "post transaction holding",
        "post allotment shareholding", "post transaction", "after transaction",
    )
    def labeled_percent(aliases):
        for alias in aliases:
            m = re.search(rf"{re.escape(alias)}[^\n:]*[:\-]?\s*(\d{{1,3}}(?:\.\d+)?)\s*%", combined, re.I)
            if m:
                value = float(m.group(1))
                if 0 <= value <= 100:
                    return value
        return None
    pre = labeled_percent(pre_aliases)
    post = labeled_percent(post_aliases)
    if pre is None:
        # Common Reg.29 table shorthand.
        m = re.search(r"(?:prior|before)[^\n]{0,100}?(\d{1,3}(?:\.\d+)?)\s*%", combined, re.I)
        if m and float(m.group(1)) <= 100:
            pre = float(m.group(1))
    if post is None:
        m = re.search(r"(?:after|post)[^\n]{0,100}?(\d{1,3}(?:\.\d+)?)\s*%", combined, re.I)
        if m and float(m.group(1)) <= 100:
            post = float(m.group(1))
    return {"pre_holding_pct": pre, "post_holding_pct": post}


def _threshold_status(pre: float | None, post: float | None) -> dict[str, Any]:
    if post is None:
        return {"client_threshold": 20.0, "sast_threshold": 25.0, "state": None, "states": []}
    states: list[str] = []
    if pre is not None:
        # These are intentionally independent: a single ownership move can cross
        # both 20% (client alert) and 25% (SAST) in one filing.
        if pre < 20 <= post:
            states.append("CROSSED_20_CLIENT")
        if pre < 25 <= post:
            states.append("CROSSED_25_SAST")
    if 18 <= post < 20:
        states.append("APPROACHING_20_CLIENT")
    if 23 <= post < 25:
        states.append("APPROACHING_25_SAST")
    if post >= 25:
        states.append("ABOVE_25_SAST")
    return {"client_threshold": 20.0, "sast_threshold": 25.0, "state": states[0] if states else None, "states": states}


def event_chain_key(symbol: str, investors: Iterable[str] | None = None) -> str:
    names = []
    for value in investors or []:
        normalized = _norm(value)
        if normalized and normalized not in names:
            names.append(normalized)
    seed = str(symbol or "").strip().upper() + "|" + "|".join(sorted(names)[:8])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _event_importance(*, event_type: str, event_sub_type: str | None, investor_category: str | None, transaction_value: float | None, holding_change_pct: float | None, threshold_status: dict[str, Any], promoter_or_pac: bool = False) -> dict[str, Any]:
    score = 0
    states = threshold_status.get("states") or []
    if any(str(v).startswith("CROSSED_25") for v in states):
        score += 45
    elif any(str(v).startswith("CROSSED_20") for v in states):
        score += 35
    elif any(str(v).startswith("APPROACHING_") for v in states):
        score += 20
    if holding_change_pct is not None:
        magnitude = abs(float(holding_change_pct))
        score += 10 if magnitude >= 2 else 5 if magnitude >= 0.5 else 0
    if transaction_value is not None:
        score += 25 if transaction_value >= 1_000_000_000 else 15 if transaction_value >= 500_000_000 else 10 if transaction_value >= 100_000_000 else 0
    promoter = promoter_or_pac or ("promoter" in _norm(investor_category))
    if promoter:
        score += 15
    if event_type == "STRATEGIC_ACQUISITION":
        score += 10
    elif event_type == "WARRANT" and event_sub_type == "CONVERSION":
        score += 5
    severity = "CRITICAL" if score >= 60 else "HIGH" if score >= 40 else "MEDIUM" if score >= 20 else "LOW"
    return {"score": score, "severity": severity}


def _document_summary(parsed: dict[str, Any], fields: dict[str, Any]) -> str:
    parts = []
    if fields.get("investors"):
        parts.append(f"Investor(s): {', '.join(fields['investors'][:8])}")
    if fields.get("quantity") is not None:
        parts.append(f"Quantity: {fields['quantity']:,.4f}")
    if fields.get("price") is not None:
        parts.append(f"Price: ₹{fields['price']:,.4f}")
    if fields.get("amount_inr") is not None:
        parts.append(f"Value: ₹{fields['amount_inr']:,.2f}")
    if fields.get("stake_percent") is not None:
        parts.append(f"Stake sold: {fields['stake_percent']:,.2f}%")
    if parsed.get("status"):
        parts.append(f"Document: {parsed['status']}")
    if parsed.get("ocr_used"):
        parts.append(f"OCR pages: {parsed.get('ocr_pages') or 0}")
    return " · ".join(parts)


def _extract_html_document(html_text: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = _clean_multiline(soup.get_text("\n", strip=True))
    if len(text) > DOCUMENT_TEXT_LIMIT:
        text = text[:DOCUMENT_TEXT_LIMIT]
    tables = []
    for table_index, table in enumerate(soup.find_all("table")):
        rows = [_table_row_cells(tr) for tr in table.find_all("tr")]
        rows = [row for row in rows if row]
        if rows:
            tables.append({"table_index": table_index, "rows": rows[:120]})
    return {
        "status": "ok" if text else "empty",
        "document_type": "html",
        "url": url,
        "text": text,
        "tables": tables,
        "sha256": hashlib.sha256((html_text or "").encode("utf-8", errors="ignore")).hexdigest(),
        "bytes": len((html_text or "").encode("utf-8", errors="ignore")),
    }


def _ocr_pdf_document(content: bytes, url: str) -> dict[str, Any]:
    """OCR image/scanned NSE PDFs when normal PDF text extraction is empty."""
    try:
        import fitz  # PyMuPDF
        import pytesseract
        from PIL import Image
    except Exception as exc:
        return {
            "status": "ocr_unavailable",
            "document_type": "pdf",
            "url": url,
            "error": f"OCR dependencies unavailable: {type(exc).__name__}: {exc}",
            "ocr_used": False,
        }

    try:
        document = fitz.open(stream=content, filetype="pdf")
        page_texts: list[str] = []
        ocr_pages = 0
        limit = min(document.page_count, OCR_MAX_PAGES)
        for page_index in range(limit):
            page = document.load_page(page_index)
            pix = page.get_pixmap(matrix=fitz.Matrix(OCR_DPI / 72.0, OCR_DPI / 72.0), alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            text = _clean(pytesseract.image_to_string(image, config="--psm 6"))
            if text:
                page_texts.append(text)
                ocr_pages += 1
        text = "\n\n".join(page_texts)
        if len(text) > DOCUMENT_TEXT_LIMIT:
            text = text[:DOCUMENT_TEXT_LIMIT]
        return {
            "status": "ok" if text else "empty",
            "document_type": "pdf",
            "url": url,
            "text": text,
            "pages": document.page_count,
            "ocr_used": True,
            "ocr_pages": ocr_pages,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
    except Exception as exc:
        return {
            "status": "ocr_error",
            "document_type": "pdf",
            "url": url,
            "error": f"{type(exc).__name__}: {exc}",
            "ocr_used": True,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }


def _extract_pdf_document(content: bytes, url: str) -> dict[str, Any]:
    reader = PdfReader(io.BytesIO(content))
    pages = []
    for page in reader.pages:
        try:
            pages.append(_clean_multiline(page.extract_text() or ""))
        except Exception as exc:
            pages.append(f"[page extraction error: {type(exc).__name__}: {exc}]")

    tables: list[dict[str, Any]] = []
    layout_pages: list[str] = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page_index, page in enumerate(pdf.pages[:50]):
                try:
                    layout_text = _clean_multiline(page.extract_text(x_tolerance=2, y_tolerance=3) or "")
                    if layout_text:
                        layout_pages.append(layout_text)
                    extracted_tables = []
                    strategies = [
                        {},
                        {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
                        {"vertical_strategy": "text", "horizontal_strategy": "text", "snap_tolerance": 3, "join_tolerance": 3, "intersection_tolerance": 5},
                    ]
                    for table_settings in strategies:
                        try:
                            extracted_tables.extend(page.extract_tables(table_settings=table_settings) or [])
                        except Exception:
                            continue
                    seen_table_rows: set[str] = set()
                    for table_index, table in enumerate(extracted_tables):
                        cleaned_rows = []
                        for row in table or []:
                            cleaned = [_clean(cell) for cell in (row or [])]
                            if any(cleaned):
                                key = "|".join(cleaned)
                                if key in seen_table_rows:
                                    continue
                                seen_table_rows.add(key)
                                cleaned_rows.append(cleaned)
                        if cleaned_rows:
                            tables.append({
                                "page": page_index + 1,
                                "table_index": table_index,
                                "rows": cleaned_rows[:120],
                            })
                except Exception:
                    continue
    except Exception:
        pass

    text = "\n\n".join(page for page in pages if page and not page.startswith("[page extraction error:"))
    if len(text.strip()) < OCR_MIN_TEXT_CHARS and layout_pages:
        text = "\n\n".join(layout_pages)
    if len(text) > DOCUMENT_TEXT_LIMIT:
        text = text[:DOCUMENT_TEXT_LIMIT]

    if len(text.strip()) >= OCR_MIN_TEXT_CHARS:
        return {
            "status": "ok",
            "document_type": "pdf",
            "url": url,
            "text": text,
            "tables": tables[:150],
            "pages": len(reader.pages),
            "ocr_used": False,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }

    ocr = _ocr_pdf_document(content, url)
    if ocr.get("status") in {"ok", "empty"}:
        ocr["tables"] = tables[:150]
        return ocr
    return {
        "status": "parse_error" if ocr.get("status") == "ocr_error" else "empty",
        "document_type": "pdf",
        "url": url,
        "text": text,
        "tables": tables[:150],
        "pages": len(reader.pages),
        "ocr_used": bool(ocr.get("ocr_used")),
        "ocr_error": ocr.get("error"),
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }

def _parse_document(content: bytes, content_type: str, url: str) -> dict[str, Any]:
    lower_type = (content_type or "").casefold()
    head = content[:1024].lower()
    # Some NSE announcement attachments are ZIP archives containing a PDF. Never
    # decode archive bytes as text: ZIP headers contain NUL bytes that PostgreSQL
    # rejects in TEXT columns. Extract the first PDF and parse that document instead.
    if content.startswith(b"PK\x03\x04") or "zip" in lower_type or url.casefold().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                candidates = [name for name in archive.namelist() if name.casefold().endswith(".pdf") and not name.endswith("/")]
                if candidates:
                    pdf_name = sorted(candidates, key=lambda name: (name.count("/"), len(name), name.casefold()))[0]
                    pdf_content = archive.read(pdf_name)
                    parsed = _extract_pdf_document(pdf_content, url)
                    parsed["document_type"] = "pdf-in-zip"
                    parsed["archive_member"] = pdf_name[:500]
                    parsed["bytes"] = len(content)
                    parsed["archive_sha256"] = hashlib.sha256(content).hexdigest()
                    return parsed
                return {"status": "empty", "document_type": "zip", "url": url,
                        "text": "", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                        "error": "ZIP attachment contained no PDF file"}
        except Exception as exc:
            return {"status": "parse_error", "document_type": "zip", "url": url,
                    "text": "", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                    "error": f"ZIP extraction failed: {type(exc).__name__}: {exc}"}
    if "pdf" in lower_type or content.startswith(b"%PDF") or b"%pdf" in head[:20]:
        try:
            return _extract_pdf_document(content, url)
        except Exception as exc:
            return {
                "status": "parse_error",
                "document_type": "pdf",
                "url": url,
                "error": f"{type(exc).__name__}: {exc}",
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
    if "html" in lower_type or b"<html" in head or b"<!doctype" in head:
        return _extract_html_document(content.decode("utf-8", errors="replace"), url)
    # NSE may return an XBRL document with an XML content type. Store readable text as
    # evidence even when the capital-action parser does not need XML semantics.
    try:
        xml_text = _clean_multiline(content.decode("utf-8", errors="replace"))
    except Exception:
        xml_text = ""
    return {
        "status": "ok" if xml_text else "empty",
        "document_type": "text",
        "url": url,
        "text": xml_text[:DOCUMENT_TEXT_LIMIT],
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _raw_announcement_identifier(row: dict[str, Any]) -> str | None:
    for key in ("seq_id", "seqId", "id", "newsId", "ANN_ID", "ann_id", "announcement_id", "announcementId"):
        value = _clean(_field(row, key))
        if value:
            return value[:255]
    return None


def _raw_announcement_key(symbol: str, row: dict[str, Any], source_url: str | None) -> str:
    identifier = _raw_announcement_identifier(row)
    identity = identifier or source_url or json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"NSE_CA|{symbol.upper()}|{identity}".encode("utf-8")).hexdigest()


def _transaction_stake_pct(
    pre_holding_pct: float | None,
    post_holding_pct: float | None,
    explicit_transaction_stake_pct: float | None,
) -> float | None:
    """Prefer the disclosed pre/post delta; otherwise use an explicit deal stake.

    The post-transaction holding alone is not a transaction-size measure and must
    never be used to pass the PE >20% acquisition filter.
    """
    if pre_holding_pct is not None and post_holding_pct is not None:
        return float(post_holding_pct) - float(pre_holding_pct)
    return float(explicit_transaction_stake_pct) if explicit_transaction_stake_pct is not None else None


def _transaction_status(text: str) -> str:
    """Conservatively identify event completion from explicit filing language."""
    value = _clean_multiline(text)
    if not value:
        return "REVIEW"
    completed = (
        r"\b(?:has been|have been|was|were|is now)?\s*(?:successfully\s+)?allotted\b",
        r"\b(?:allotment|allotting)\s+(?:of\s+)?(?:equity\s+)?shares?\s+(?:has been\s+)?completed\b",
        r"\b(?:has|have)\s+(?:completed|consummated)\s+(?:the\s+)?acquisition\b",
        r"\b(?:transaction|issue|allotment)\s+(?:has been\s+)?completed\b",
        r"\bshares?\s+(?:have been|were)\s+issued\b",
    )
    if any(re.search(pattern, value, re.I) for pattern in completed):
        return "COMPLETED"
    proposed = (
        r"\bpropos(?:e|es|ed|ing)\b.{0,180}\b(?:issue|allot|acquir|acquisition|warrant|preferential)\b",
        r"\b(?:intend(?:s)?|plans?|will|proposed to)\b.{0,140}\b(?:issue|allot|acquir|warrant|preferential)\b",
        r"\bsubject to (?:shareholder|regulatory|statutory) approval\b",
        r"\bto be allotted\b|\bproposed allotment\b|\bissue of warrants?\b",
    )
    if any(re.search(pattern, value, re.I | re.S) for pattern in proposed):
        return "PROPOSED"
    return "REVIEW"


def _review_candidate_type(text: str) -> str | None:
    """Return a review bucket only when a capital-event family is plausible."""
    value = _norm(text)
    if not value:
        return None
    if "preferential" in value and any(token in value for token in ("issue", "allot", "fund raising", "securit")):
        return "POTENTIAL_PREFERENTIAL_ISSUE"
    if "warrant" in value and any(token in value for token in ("issue", "allot", "conversion", "convert", "exercise", "fund raising")):
        return "POTENTIAL_WARRANT"
    ownership_action = any(token in value for token in ("acquisition", "acquire", "acquired", "takeover", "open offer", "sast", "regulation 29", "stake purchase", "share purchase", "strategic investment"))
    ownership_object = any(token in value for token in ("stake", "shareholding", "shares", "equity", "voting rights", "ownership"))
    if ownership_action and ownership_object:
        return "POTENTIAL_PE_STAKE_ACQUISITION"
    return None


class NSECorporateAnnouncementsCollector:
    source_name = "NSE Corporate Announcements"
    page_url = NSE_PAGE_URL

    def __init__(
        self,
        *,
        request_retries: int = REQUEST_RETRIES,
        window_days: int = DEFAULT_WINDOW_DAYS,
        symbol_concurrency: int = SYMBOL_CONCURRENCY,
        document_concurrency: int = DOCUMENT_CONCURRENCY,
    ) -> None:
        self.request_retries = max(1, int(request_retries))
        self.window_days = max(30, int(window_days))
        self.symbol_concurrency = max(1, int(symbol_concurrency))
        self.document_concurrency = max(1, int(document_concurrency))

    async def _download_document(
        self,
        client: NSEApiClient,
        url: str,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:
        """Fetch and parse an NSE attachment in memory; never write the file to disk."""
        async with semaphore:
            last_error: Exception | None = None
            for candidate_url in (url,):
                try:
                    content, content_type = await client.get_bytes(
                        candidate_url,
                        referer=NSE_PAGE_URL,
                    )
                    parsed = _parse_document(content, content_type or "", candidate_url)
                    parsed["content_type"] = content_type
                    return parsed
                except Exception as exc:
                    last_error = exc
                    logger.debug("NSE corporate document fetch failed url=%s error=%s", candidate_url, exc)
            return {
                "status": "error",
                "url": url,
                "error": f"{type(last_error).__name__}: {last_error}",
            }

    def _normalize_raw(
        self,
        raw: dict[str, Any],
        symbol: str,
        master: dict[str, Any],
    ) -> dict[str, Any]:
        company_name = _clean(
            _field(raw, "companyName", "company name", "sm_name", "company")
            or master.get("legal_name")
            or symbol
        )
        subject = _clean(_field(raw, "subject", "desc", "attchmntText", "announcementText"))
        details = _clean(_field(raw, "details", "attchmntFileText", "description"))
        urls: list[str] = []
        for value in raw.values():
            candidate = _valid_url(value)
            if candidate and candidate not in urls:
                urls.append(candidate)
        attachment_candidates = [
            _valid_url(_field(raw, "attchmntFile", "attachment", "attachmentFile", "fileUrl", "attachment_url"))
        ] + urls
        attachment_candidates = _unique_candidates(attachment_candidates)
        pdf_candidates = [u for u in attachment_candidates if ".pdf" in u.casefold() or "pdf" in u.casefold()]
        attachment = pdf_candidates[0] if pdf_candidates else (attachment_candidates[0] if attachment_candidates else None)
        detail_url = _valid_url(_field(raw, "detail_url", "detailsUrl", "details_url"))
        xbrl_url = _valid_url(_field(raw, "xbrl", "xbrlUrl"))
        event_date = _parse_datetime(
            _field(raw, "broadcastDate", "an_dt", "sort_date", "date", "broadcast_date")
        )
        return {
            "raw": raw,
            "company_name": company_name,
            "nse_symbol": (_clean(_field(raw, "symbol")) or symbol).upper(),
            "subject": subject,
            "details": details,
            "detail_url": detail_url,
            "attachment_url": attachment,
            "xbrl_url": xbrl_url,
            "event_date": event_date,
            "announcement_id": _clean(_field(raw, "seq_id", "seqId", "id", "newsId", "ANN_ID")) or None,
        }

    async def _collect_symbol(
        self,
        client: NSEApiClient,
        master: dict[str, Any],
        start: date,
        end: date,
        symbol_sem: asyncio.Semaphore,
        raw_rows_override: list[dict[str, Any]] | None = None,
        document_sem: asyncio.Semaphore | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        symbol_started = time.monotonic()
        symbol = _clean(master.get("nse_symbol")).upper()
        async with symbol_sem:
            logger.info(
                "NSE corporate API fetch symbol=%s company=%s from=%s to=%s transport=nse-public-api",
                symbol or "<missing>", master.get("legal_name"), start.isoformat(), end.isoformat(),
            )
            if not symbol:
                return [], {
                    "company": master.get("legal_name"),
                    "symbol": symbol,
                    "raw_rows": 0,
                    "candidate_rows": 0,
                    "documents_ok": 0,
                    "documents_failed": 0,
                    "errors": ["Missing NSE Symbol"],
                    "transport": "nse-public-api",
                }

            raw_rows: list[dict[str, Any]] = list(raw_rows_override or [])
            api_errors: list[str] = []
            # Always split historical discovery into bounded windows. In all-company
            # mode the rows are supplied by the symbol-free feed and this network
            # phase is intentionally skipped.
            api_windows = list(_date_windows(start, end, self.window_days))
            if raw_rows_override is None:
                for window_start, window_end in api_windows:
                    try:
                        params = {"index": "equities", "symbol": symbol}
                        if window_start is not None and window_end is not None:
                            params.update({
                                "from_date": window_start.strftime("%d-%m-%Y"),
                                "to_date": window_end.strftime("%d-%m-%Y"),
                            })
                        logger.info(
                            "NSE corporate API request symbol=%s mode=%s from=%s to=%s",
                            symbol, "history-window",
                            window_start.isoformat() if window_start else "none",
                            window_end.isoformat() if window_end else "none",
                        )
                        payload = await client.get_json(
                            "/api/corporate-announcements",
                            params=params,
                            referer=NSE_PAGE_URL,
                        )
                        raw_rows.extend(_extract_rows(payload))
                    except NSEApiError as exc:
                        scope = f"{window_start.isoformat()}..{window_end.isoformat()}"
                        api_errors.append(
                            f"{scope}: {type(exc).__name__}: {exc}"
                        )

        dedup: dict[str, dict[str, Any]] = {}
        for raw in raw_rows:
            item = self._normalize_raw(raw, symbol, master)
            if item.get("nse_symbol") and _norm(item.get("nse_symbol")) != _norm(symbol):
                continue
            event_day = item.get("event_date").date() if isinstance(item.get("event_date"), datetime) else None
            if event_day and not (start <= event_day <= end):
                continue
            subject_norm = _norm(item.get("subject"))
            detail_norm = _norm(item.get("details"))
            key = "|".join(
                (
                    symbol,
                    str(item.get("announcement_id") or ""),
                    str(item.get("event_date") or ""),
                    subject_norm,
                    detail_norm,
                    str(item.get("attachment_url") or item.get("detail_url") or item.get("xbrl_url") or ""),
                )
            )
            dedup[hashlib.sha256(key.encode("utf-8")).hexdigest()] = item

        documents_to_fetch: list[tuple[str, dict[str, Any], str]] = []
        candidate_without_document = 0
        pre_screen_rejected = 0
        review_needed_count = 0
        rejected_subject_samples: list[str] = []
        document_review_markers = (
            "preferential", "warrant", "allotment of securities", "conversion of securities",
            "conversion of warrants", "acquisition", "acquire", "acquired", "takeover",
            "open offer", "stake", "share purchase", "purchase of shares",
            "sale and purchase of shares", "sale or disposal", "disposal", "divestment",
            "private equity", "private placement", "subscription", "strategic investment",
            "investment", "outcome of board meeting",
        )
        for key, item in dedup.items():
            combined = _clean(f"{item.get('subject')} {item.get('details')}")
            combined_norm = _norm(combined)
            candidate = _classify(combined)
            needs_document_review = candidate or any(marker in combined_norm for marker in document_review_markers)
            document_url = item.get("attachment_url") or item.get("detail_url") or item.get("xbrl_url")
            if needs_document_review:
                review_needed_count += 1
                # Capital actions are only surfaced when the NSE API supplied the
                # actual filing attachment. This keeps View Details as a direct PDF
                # link and prevents the desk from exposing the NSE listing/detail UI.
                attachment_url = item.get("attachment_url")
                if attachment_url:
                    documents_to_fetch.append((key, item, attachment_url))
                elif candidate:
                    candidate_without_document += 1
            else:
                pre_screen_rejected += 1
                if len(rejected_subject_samples) < 5:
                    subject_sample = _clean(item.get("subject")) or "<no subject>"
                    rejected_subject_samples.append(subject_sample[:180])

        # Share one semaphore across all symbols in a collection run. A local
        # semaphore per symbol multiplied the effective parallel downloads by the
        # number of scheduled symbols and could overwhelm NSE archives.
        semaphore = document_sem or asyncio.Semaphore(self.document_concurrency)
        documents: dict[str, dict[str, Any]] = {}

        async def fetch_document(key: str, item: dict[str, Any], url: str) -> None:
            document_started = time.monotonic()
            logger.info("NSE corporate document fetch url=%s", url)
            parsed = await self._download_document(client, url, semaphore)
            logger.info(
                "NSE_CORPORATE_DOCUMENT_DONE symbol=%s announcement=%s status=%s bytes=%s elapsed_seconds=%.2f",
                symbol, item.get("announcement_id"), parsed.get("status"), parsed.get("bytes"),
                time.monotonic() - document_started,
            )
            if parsed.get("status") in {"error", "empty", "parse_error"}:
                logger.warning(
                    "NSE corporate document failed url=%s status=%s parser=%s ocr_used=%s error=%s",
                    url, parsed.get("status"), parsed.get("document_type"), parsed.get("ocr_used"),
                    parsed.get("error") or parsed.get("ocr_error"),
                )
            documents[key] = parsed

        await asyncio.gather(*(fetch_document(key, item, url) for key, item, url in documents_to_fetch))

        output: list[dict[str, Any]] = []
        documents_ok = 0
        documents_failed = 0
        target_candidate_rows = 0
        candidates_skipped_document_failure = 0
        classification_rejected_after_document = 0
        for key, item in dedup.items():
            document = documents.get(key) or {
                "status": "missing",
                "url": item.get("attachment_url"),
            }
            if document.get("status") == "ok":
                documents_ok += 1
            elif document.get("status") not in {"missing"}:
                documents_failed += 1

            document_text = _clean_multiline(document.get("text"))
            table_text = _normalise_table_text(document.get("tables") or [])
            combined = _clean(f"{item.get('subject')} {item.get('details')} {document_text} {table_text}")
            classification = classify_capital_event_details(combined)
            event_type = classification.get("event_type") if classification else None
            event_sub_type = classification.get("event_sub_type") if classification else None
            review_candidate = _review_candidate_type(combined)
            classification_status = "CLASSIFIED"
            review_reason = None
            if not event_type:
                if not review_candidate:
                    if document.get("status") == "ok":
                        classification_rejected_after_document += 1
                    continue
                event_type = "REVIEW"
                event_sub_type = review_candidate
                classification_status = "REVIEW"
                review_reason = (
                    f"Potential {review_candidate.replace('POTENTIAL_', '').replace('_', ' ').lower()} "
                    "disclosure; available wording does not support a confident capital-event classification."
                )

            target_candidate_rows += 1
            if document.get("status") != "ok":
                candidates_skipped_document_failure += 1
                reason = document.get("error") or "NSE capital-action attachment is missing or not parseable"
                logger.warning(
                    "NSE corporate candidate requires review without parsed document symbol=%s announcement=%s url=%s status=%s error=%s",
                    symbol, item.get("announcement_id"), document.get("url"), document.get("status"), reason,
                )
                # Keep only filings whose exchange subject/details suggest one of the
                # requested event families. Missing/unreadable attachments are shown
                # as REVIEW rather than silently discarded or assigned invented fields.
                event_type = "REVIEW"
                classification_status = "REVIEW"
                review_reason = f"NSE filing document could not be parsed ({document.get('status') or 'missing'}): {reason}"[:4000]
            # Keep PDF line breaks intact. The label/value fallback parser depends on
            # original line structure; collapsing whitespace here caused table headers
            # and neighbouring labels to leak into the Investor/Allottee field.
            extraction_text = document_text or combined
            parsed_fields = _extract_value_fields(extraction_text, event_type, document.get("tables") or [])
            investors = parsed_fields.get("investors") or _extract_investors(extraction_text, document.get("tables") or [])
            investors = _clean_investor_candidates(investors)
            quantity = parsed_fields.get("quantity")
            price = parsed_fields.get("price")
            amount = parsed_fields.get("amount_inr")
            # Numeric fallbacks have already been scoped by _extract_value_fields.
            # Do not rescan the entire attachment here: it often contains unrelated
            # annual-report percentages, and an inconsistent source amount is kept null.
            stake_percent = parsed_fields.get("stake_percent")

            ownership = _extract_ownership_metrics(extraction_text, document.get("tables") or [])
            pre_holding = ownership.get("pre_holding_pct")
            post_holding = ownership.get("post_holding_pct")
            holding_change = (post_holding - pre_holding) if pre_holding is not None and post_holding is not None else None
            if holding_change is None and event_type == "STRATEGIC_ACQUISITION" and event_sub_type == "ACQUISITION" and stake_percent is not None:
                holding_change = float(stake_percent)
            threshold = _threshold_status(pre_holding, post_holding)
            # Strategic/PE ownership is only a classified event if a source-supported
            # percentage confirms the requested >20% threshold. Unknown percentages go
            # to REVIEW; an explicitly sub-20% stake is not surfaced as a qualifying PE
            # event. The raw filing remains stored either way.
            if event_type == "STRATEGIC_ACQUISITION":
                # A disposal is not a qualifying acquisition, even when the holder's
                # post-transaction ownership remains above 20%.
                if "DISPOSAL" in str(event_sub_type or "").upper():
                    classification_rejected_after_document += 1
                    continue
                # Compare the transaction's acquired stake (pre/post delta where both
                # are disclosed; otherwise an explicit transaction stake). A holder's
                # post-event ownership alone must never qualify a small acquisition.
                qualifying_stake = _transaction_stake_pct(pre_holding, post_holding, stake_percent)
                if qualifying_stake is None:
                    event_type = "REVIEW"
                    classification_status = "REVIEW"
                    event_sub_type = event_sub_type or "POTENTIAL_PE_STAKE_ACQUISITION"
                    review_reason = "Transaction stake could not be confirmed against the required >20% acquisition threshold."
                elif qualifying_stake <= 20:
                    classification_rejected_after_document += 1
                    continue
            promoter_or_pac = bool(re.search(r"promoter|persons?\s+acting\s+in\s+concert|pac", combined, re.I))
            importance = _event_importance(
                event_type=event_type,
                event_sub_type=event_sub_type,
                investor_category=_investor_category(combined),
                transaction_value=amount,
                holding_change_pct=holding_change,
                threshold_status=threshold,
                promoter_or_pac=promoter_or_pac,
            )

            preferred_document_url = item.get("attachment_url")
            document_url = str(preferred_document_url or document.get("url") or item.get("xbrl_url") or "") or None
            summary = _document_summary(document, {**parsed_fields, "investors": investors, "quantity": quantity, "price": price, "amount_inr": amount, "stake_percent": stake_percent})
            detail_payload = {
                "announcement_subject": item.get("subject"),
                "announcement_details": item.get("details"),
                "document_url": document_url,
                "document_type": document.get("document_type"),
                "document_status": document.get("status"),
                "parsed": {
                    "investors": investors,
                    "quantity": quantity,
                    "price": price,
                    "amount_inr": amount,
                    "stake_percent": stake_percent,
                    "investor_category": _investor_category(combined),
                    "event_type": event_type,
                    "event_sub_type": event_sub_type,
                    "transaction_status": _transaction_status(combined),
                    "classification_status": classification_status,
                    "review_reason": review_reason,
                    "pre_holding_pct": pre_holding,
                    "post_holding_pct": post_holding,
                    "holding_change_pct": holding_change,
                    "promoter_or_pac": promoter_or_pac,
                    "purpose": _extract_labeled_text(extraction_text, ("purpose", "object", "reason for acquisition", "purpose of transaction")),
                    "severity": importance["severity"],
                    "importance_score": importance["score"],
                    "threshold_status": threshold,
                    "confidence": classification.get("confidence") if classification else None,
                    "event_chain_key": event_chain_key(symbol, investors),
                },
                "source_references": [{
                    "source": "NSE Corporate Announcements",
                    "announcement_id": item.get("announcement_id"),
                    "document_url": document_url,
                    "event_date": item.get("event_date").isoformat() if item.get("event_date") else None,
                }],
                "raw_nse_subject": item.get("subject"),
                "raw_nse_details": item.get("details"),
                "document_summary": summary,
                "document_sha256": document.get("sha256"),
                "document_bytes": document.get("bytes"),
                "document_pages": document.get("pages"),
                "document_text": document_text,
                "raw_nse_row": item.get("raw"),
            }
            output.append(
                {
                    "event_type": event_type,
                    "nse_announcement_id": item.get("announcement_id"),
                    "event_sub_type": event_sub_type,
                    "transaction_status": _transaction_status(combined),
                    "classification_status": classification_status,
                    "review_reason": review_reason,
                    "pre_holding_pct": pre_holding,
                    "post_holding_pct": post_holding,
                    "holding_change_pct": holding_change,
                    "promoter_or_pac": promoter_or_pac,
                    "severity": importance["severity"],
                    "importance_score": importance["score"],
                    "threshold_status": threshold,
                    "confidence": classification.get("confidence") if classification else None,
                    # Filing identity can be reconciled to the shared Company master after discovery.
                    # Never let a filing's free-text company field replace the symbol-scoped master row.
                    "company_name": master.get("legal_name") or item.get("company_name") or symbol,
                    "stock_name": master.get("legal_name") or item.get("company_name") or symbol,
                    "nse_symbol": symbol,
                    "investor_name": ", ".join(investors[:12]) if investors else None,
                    "investor_category": _investor_category(combined),
                    "action": item.get("subject") or event_type,
                    "event_date": item.get("event_date") or datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc),
                    "transaction_date": item.get("event_date") or datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc),
                    "quantity": quantity,
                    "price": price,
                    "amount_inr": amount,
                    "stake_percent": stake_percent,
                    "source": "NSE",
                    "source_url": document_url or _announcement_page_url(symbol),
                    "title": (item.get("subject") or item.get("company_name") or event_type)[:1000],
                    # Persist normalized fields and human-readable evidence only. The
                    # complete JSON-shaped extractor payload is intentionally not stored.
                    "details": (summary or item.get("details") or "")[:10000],
                    "announcement_subject": str(item.get("subject") or "")[:1000] or None,
                    "announcement_details": str(item.get("details") or "")[:10000] or None,
                    "document_summary": str(summary or "")[:10000] or None,
                    "document_text": str(document_text or "")[:100000] or None,
                    "document_url": document_url,
                    "document_type": document.get("document_type"),
                    "document_status": document.get("status"),
                    "document_sha256": document.get("sha256"),
                    "document_pages": document.get("pages"),
                    "document_bytes": document.get("bytes"),
                    "event_sub_type": event_sub_type,
                    "pre_holding_pct": pre_holding,
                    "post_holding_pct": post_holding,
                    "holding_change_pct": holding_change,
                    "promoter_or_pac": promoter_or_pac,
                    "severity": importance.get("severity"),
                    "importance_score": importance.get("score"),
                    "threshold_status": threshold,
                    "confidence": classification.get("confidence") if classification else None,
                    "purpose": _extract_labeled_text(extraction_text, ("purpose", "object", "reason for acquisition", "purpose of transaction")),
                    "event_chain_key": event_chain_key(symbol, investors),
                    "investor_type": _investor_category(combined),
                    "isin": _clean(_field(item.get("raw") or {}, "isin", "ISIN")) if isinstance(item.get("raw"), dict) else None,
                    "company_id": master.get("company_id"),
                }
            )

        errors: list[str] = list(api_errors)
        if candidate_without_document:
            errors.append(f"{candidate_without_document} candidate NSE capital-action announcement(s) had no Details/Attachment URL")
        if documents_failed:
            errors.append(f"{documents_failed} NSE announcement document(s) failed to fetch or parse")

        logger.info(
            "NSE corporate API symbol=%s company=%s from=%s to=%s raw=%s unique=%s review_needed=%s pre_screen_rejected=%s candidates=%s output_rows=%s documents_fetched=%s documents_ok=%s documents_failed=%s candidate_without_attachment=%s candidate_doc_failure=%s post_doc_rejected=%s elapsed_seconds=%.2f",
            symbol, master.get("legal_name"), start.isoformat(), end.isoformat(),
            len(raw_rows), len(dedup), review_needed_count, pre_screen_rejected,
            target_candidate_rows, len(output), len(documents_to_fetch), documents_ok, documents_failed,
            candidate_without_document, candidates_skipped_document_failure, classification_rejected_after_document,
            time.monotonic() - symbol_started,
        )
        if rejected_subject_samples:
            logger.debug(
                "NSE_CORPORATE_PRE_SCREEN_REJECT_SAMPLES symbol=%s company=%s sample_subjects=%s",
                symbol, master.get("legal_name"), rejected_subject_samples[:5],
            )
        return output, {
            "company": master.get("legal_name"),
            "symbol": symbol,
            "raw_rows": len(raw_rows),
            "unique_announcements": len(dedup),
            "review_needed_count": review_needed_count,
            "pre_screen_rejected": pre_screen_rejected,
            "rejected_subject_samples": rejected_subject_samples,
            "candidate_rows": target_candidate_rows,
            "output_rows": len(output),
            "documents_ok": documents_ok,
            "documents_failed": documents_failed,
            "documents_fetched": len(documents_to_fetch),
            "candidate_without_document": candidate_without_document,
            "candidates_skipped_document_failure": candidates_skipped_document_failure,
            "classification_rejected_after_document": classification_rejected_after_document,
            "pages": len(api_windows),
            "api_rows": len(raw_rows),
            "api_windows": len(api_windows),
            "errors": errors,
            "transport": "nse-public-api",
        }

    async def collect_all(
        self,
        history_days: int = MAX_HISTORY_DAYS,
        on_batch: Callable[[list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]], int, int], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Discover Corporate Announcements for all NSE equities from the broad feed."""
        start, end = history_window(history_days)
        started = time.monotonic()
        async with NSEApiClient(retries=self.request_retries) as client:
            raw_rows: list[dict[str, Any]] = []
            raw_filings_by_key: dict[str, dict[str, Any]] = {}
            window_stats: list[dict[str, Any]] = []
            source_errors: dict[str, str] = {}
            feed_window_errors: dict[str, str] = {}
            windows = list(_date_windows(start, end, self.window_days))
            feed_started = time.monotonic()
            for ws, we in windows:
                window_started = time.monotonic()
                try:
                    payload = await client.get_json(
                        "/api/corporate-announcements",
                        params={
                            "index": "equities",
                            "from_date": ws.strftime("%d-%m-%Y"),
                            "to_date": we.strftime("%d-%m-%Y"),
                        },
                        referer=NSE_PAGE_URL,
                    )
                    if not _has_valid_announcement_payload_shape(payload):
                        raise NSEApiError(
                            "Unexpected NSE corporate-announcements payload schema; refusing to treat it as an empty feed"
                        )
                    window_rows = _extract_rows(payload)
                    payload_hash = hashlib.sha256(
                        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
                    ).hexdigest()
                    for raw in window_rows:
                        raw_rows.append(raw)
                        raw_symbol = _clean(_field(raw, "symbol", "SYMBOL"))
                        symbol = raw_symbol.upper() if raw_symbol else "UNKNOWN"
                        master_name = _clean(_field(raw, "companyName", "sm_name", "company name", "company", "company_name")) or (symbol if raw_symbol else "")
                        normalized_raw = self._normalize_raw(raw, symbol, {"legal_name": master_name or symbol, "nse_symbol": raw_symbol or None})
                        source_url = normalized_raw.get("attachment_url") or normalized_raw.get("detail_url") or normalized_raw.get("xbrl_url")
                        filing_key = _raw_announcement_key(symbol, dict(raw), source_url)
                        event_date = normalized_raw.get("event_date")
                        raw_filings_by_key[filing_key] = {
                            "source": "NSE_CA", "source_key": filing_key,
                            "source_filing_id": _raw_announcement_identifier(dict(raw)),
                            "nse_symbol": raw_symbol.upper() if raw_symbol else None,
                            "company_name": master_name or None,
                            "filing_date": event_date.isoformat() if event_date else None,
                            "source_url": source_url, "raw_payload": dict(raw),
                            "window_start": ws.isoformat(), "window_end": we.isoformat(),
                        }
                    window_stats.append({
                        "source": "NSE_CA", "from_date": ws.isoformat(), "to_date": we.isoformat(),
                        "status": "SUCCESS", "http_status": 200, "records_received": len(window_rows),
                        "content_hash": payload_hash,
                        "duration_ms": int((time.monotonic() - window_started) * 1000), "error": None,
                    })
                except NSEApiError as exc:
                    window_key = f"{ws.isoformat()}..{we.isoformat()}"
                    message = f"{type(exc).__name__}: {exc}"
                    feed_window_errors[window_key] = message
                    source_errors[f"feed-window:{window_key}"] = message
                    window_stats.append({
                        "source": "NSE_CA", "from_date": ws.isoformat(), "to_date": we.isoformat(),
                        "status": "FAILED", "http_status": exc.status_code, "records_received": 0, "content_hash": None,
                        "duration_ms": int((time.monotonic() - window_started) * 1000), "error": message,
                    })
                except Exception as exc:
                    window_key = f"{ws.isoformat()}..{we.isoformat()}"
                    message = f"{type(exc).__name__}: {exc}"
                    feed_window_errors[window_key] = message
                    source_errors[f"feed-window:{window_key}"] = message
                    window_stats.append({
                        "source": "NSE_CA", "from_date": ws.isoformat(), "to_date": we.isoformat(),
                        "status": "FAILED", "http_status": None, "records_received": 0, "content_hash": None,
                        "duration_ms": int((time.monotonic() - window_started) * 1000), "error": message,
                    })
                    logger.exception("NSE corporate announcement window failed unexpectedly from=%s to=%s", ws, we)

            grouped: dict[str, list[dict[str, Any]]] = {}
            names: dict[str, str] = {}
            for raw in raw_rows:
                symbol = _clean(_field(raw, "symbol", "SYMBOL")).upper()
                if not symbol:
                    continue
                grouped.setdefault(symbol, []).append(raw)
                names.setdefault(symbol, _clean(_field(raw, "sm_name", "company name", "company", "company_name")) or symbol)

            logger.info(
                "NSE_CORPORATE_BROAD_FEED_DONE windows=%s raw_rows=%s symbols=%s feed_errors=%s elapsed_seconds=%.1f",
                len(windows), len(raw_rows), len(grouped), len(feed_window_errors), time.monotonic() - feed_started,
            )
            sem = asyncio.Semaphore(self.symbol_concurrency)
            document_sem = asyncio.Semaphore(self.document_concurrency)
            tasks: dict[asyncio.Task, tuple[str, dict[str, Any]]] = {}
            for symbol, rows_for_symbol in grouped.items():
                master = {"company_id": None, "legal_name": names.get(symbol) or symbol, "nse_symbol": symbol}
                task = asyncio.create_task(
                    self._collect_symbol(
                        client, master, start, end, sem,
                        raw_rows_override=rows_for_symbol, document_sem=document_sem,
                    )
                )
                tasks[task] = (symbol, master)

            dedup_rows: dict[str, dict[str, Any]] = {}
            stats_list: list[dict[str, Any]] = []
            completed = 0
            callback_buffer: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
            callback_start = 0
            while tasks:
                done, _ = await asyncio.wait(list(tasks), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    symbol, master = tasks.pop(task)
                    try:
                        rows, stats = task.result()
                    except Exception as exc:
                        rows, stats = [], {"symbol": symbol, "errors": [f"{type(exc).__name__}: {exc}"]}
                    # Each symbol task only sees feed windows that completed. When
                    # any window failed, downstream persistence must preserve prior
                    # rows rather than replacing a potentially incomplete history.
                    stats["feed_complete"] = not feed_window_errors
                    stats_list.append(stats)
                    if stats.get("errors"):
                        source_errors.setdefault(symbol, "; ".join(str(v) for v in stats["errors"]))
                    for row in rows:
                        row["nse_symbol"] = symbol
                        row["company_name"] = master.get("legal_name") or symbol
                        key = str(row.get("source_key") or hashlib.sha256(repr(row).encode()).hexdigest())
                        dedup_rows[key] = row
                    completed += 1
                    if on_batch:
                        callback_buffer.append((master, rows, stats))
                if on_batch and (len(callback_buffer) >= CALLBACK_BATCH_SIZE or not tasks):
                    await on_batch(callback_buffer, callback_start, max(len(grouped), 1))
                    callback_start += len(callback_buffer)
                    callback_buffer = []

        rows = list(dedup_rows.values())
        rows.sort(key=lambda item: item.get("event_date") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        logger.info(
            "NSE corporate all-company ingestion complete from=%s to=%s feed_windows=%s feed_complete=%s raw_rows=%s symbols_with_announcements=%s symbol_captures_ok=%s rows=%s source_error_count=%s elapsed=%.1fs",
            start.isoformat(), end.isoformat(), len(windows), not feed_window_errors, len(raw_rows), len(grouped),
            sum(1 for stats in stats_list if not (stats.get("errors") or [])), len(rows), len(source_errors), time.monotonic() - started,
        )
        return {
            "rows": rows,
            "raw_filings": list(raw_filings_by_key.values()),
            "window_stats": window_stats,
            "source_errors": source_errors,
            "errors": source_errors,
            "scanned": len(raw_rows),
            "candidate_rows": sum(int(s.get("candidate_rows") or 0) for s in stats_list),
            "target_candidates": len(rows),
            "pages": len(windows),
            "complete": not source_errors and len(stats_list) == len(grouped),
            "feed_complete": not feed_window_errors,
            "feed_window_errors": feed_window_errors,
            "symbols_with_announcements": len(grouped),
            "companies_scanned": len(stats_list),
            "review_needed_count": sum(int(s.get("review_needed_count") or 0) for s in stats_list),
            "pre_screen_rejected": sum(int(s.get("pre_screen_rejected") or 0) for s in stats_list),
            "candidate_without_document": sum(int(s.get("candidate_without_document") or 0) for s in stats_list),
            "candidates_skipped_document_failure": sum(int(s.get("candidates_skipped_document_failure") or 0) for s in stats_list),
            "classification_rejected_after_document": sum(int(s.get("classification_rejected_after_document") or 0) for s in stats_list),
            "symbol_stats": stats_list,
            "documents_ok": sum(int(s.get("documents_ok") or 0) for s in stats_list),
            "documents_failed": sum(int(s.get("documents_failed") or 0) for s in stats_list),
            "scope": "all_nse_equities",
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
        }

    async def collect(
        self,
        history_days: int = MAX_HISTORY_DAYS,
        allowed_companies: Iterable[dict[str, Any]] | None = None,
        on_batch: Callable[[list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]], int, int], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Collect company-scoped announcements in bounded batches.

        ``on_batch`` lets the database route commit successful companies while a
        two-year crawl is still running. This keeps the worker durable and prevents
        the FE from seeing an empty snapshot until all 100+ companies finish.
        """
        start, end = history_window(history_days)
        started = time.monotonic()
        if allowed_companies is None:
            return await self.collect_all(history_days=history_days, on_batch=on_batch)
        masters = [dict(row) for row in allowed_companies]
        missing = [str(row.get("legal_name") or row.get("company_id")) for row in masters if not _clean(row.get("nse_symbol"))]
        masters = [row for row in masters if _clean(row.get("nse_symbol"))]
        if missing:
            logger.warning(
                "NSE corporate coverage rows skipped because no NSE symbol exists: %s",
                ", ".join(missing[:25]),
            )
        if not masters:
            return {
                "rows": [],
                "errors": {"NSE Coverage Master": f"No active approved Coverage List company has an NSE symbol. Skipped: {', '.join(missing[:25])}"},
                "source_errors": {"NSE Coverage Master": "No NSE-listed Coverage List companies are available for this desk."},
                "scanned": 0,
                "candidate_rows": 0,
                "target_candidates": 0,
                "complete": False,
                "missing_symbols": missing,
                "skipped_companies": missing,
                "from_date": start.isoformat(),
                "to_date": end.isoformat(),
                "symbol_stats": [],
            }

        async with NSEApiClient(retries=self.request_retries) as client:
            symbol_sem = asyncio.Semaphore(self.symbol_concurrency)
            document_sem = asyncio.Semaphore(self.document_concurrency)
            dedup_rows: dict[str, dict[str, Any]] = {}
            source_errors: dict[str, str] = {}
            symbol_stats: list[dict[str, Any]] = []
            raw_scanned = 0
            candidate_count = 0
            complete_symbols = 0

            tasks: dict[asyncio.Task, tuple[int, dict[str, Any]]] = {}
            for index, master in enumerate(masters):
                task = asyncio.create_task(
                    self._collect_symbol(client, master, start, end, symbol_sem, document_sem=document_sem)
                )
                tasks[task] = (index, master)

            completed_symbols = 0
            callback_buffer: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
            callback_start = 0
            while tasks:
                done, _ = await asyncio.wait(list(tasks), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    index, master = tasks.pop(task)
                    company_name = str(master.get("legal_name") or master.get("nse_symbol"))
                    try:
                        rows, stats = task.result()
                    except Exception as exc:
                        stats = {
                            "company": company_name,
                            "symbol": master.get("nse_symbol"),
                            "errors": [f"{type(exc).__name__}: {exc}"],
                        }
                        rows = []

                    symbol_stats.append(stats)
                    raw_scanned += int(stats.get("raw_rows") or 0)
                    candidate_count += int(stats.get("candidate_rows") or 0)
                    errors = stats.get("errors") or []
                    symbol_key = _clean(master.get("nse_symbol")).upper() if _clean(master.get("nse_symbol")) else company_name
                    if errors:
                        source_errors[symbol_key] = "; ".join(str(item) for item in errors)
                    else:
                        complete_symbols += 1
                    for row in rows:
                        identity = str(row.get("nse_announcement_id") or "")
                        basis = "|".join(
                            [
                                str(master.get("company_id") or ""),
                                identity,
                                _norm(row.get("investor_name")),
                                str(row.get("event_date") or ""),
                                _norm(row.get("title")),
                            ]
                        )
                        key = identity or hashlib.sha256(basis.encode("utf-8")).hexdigest()
                        # Event classification is mutable as new filing details arrive;
                        # identity must be anchored to the underlying announcement.
                        scoped_key = hashlib.sha256(
                            f"NSE|{master.get('company_id')}|{key}".encode("utf-8")
                        ).hexdigest()
                        row["source_key"] = scoped_key
                        dedup_rows[scoped_key] = row

                    completed_symbols += 1
                    if on_batch:
                        callback_buffer.append((master, rows, stats))
                if on_batch and (len(callback_buffer) >= CALLBACK_BATCH_SIZE or not tasks):
                    await on_batch(callback_buffer, callback_start, len(masters))
                    callback_start += len(callback_buffer)
                    callback_buffer = []

        rows = list(dedup_rows.values())
        rows.sort(
            key=lambda item: item.get("event_date") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        documents_ok = sum(int(item.get("documents_ok") or 0) for item in symbol_stats)
        documents_failed = sum(int(item.get("documents_failed") or 0) for item in symbol_stats)
        candidates_without_document = sum(int(item.get("candidate_without_document") or 0) for item in symbol_stats)
        for stats in symbol_stats:
            symbol = str(stats.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            errors = stats.get("errors") or []
            if errors:
                source_errors.setdefault(symbol, "; ".join(str(item) for item in errors))

        complete = complete_symbols == len(masters) and not source_errors and len(symbol_stats) == len(masters)
        logger.info(
            "NSE Corporate Announcements complete mode=company-scoped from=%s to=%s symbols_success=%s/%s raw=%s candidates=%s deduped=%s docs_ok=%s docs_failed=%s source_error_count=%s elapsed=%.1fs",
            start.isoformat(), end.isoformat(), complete_symbols, len(masters), raw_scanned, candidate_count,
            len(rows), documents_ok, documents_failed, len(source_errors), time.monotonic() - started,
        )
        return {
            "rows": rows,
            "errors": source_errors,
            "source_errors": source_errors,
            "scanned": raw_scanned,
            "candidate_rows": candidate_count,
            "target_candidates": len(rows),
            "pages": len(symbol_stats),
            "complete": complete,
            "missing_symbols": missing,
            "skipped_companies": missing,
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "companies_scanned": len(symbol_stats),
            "symbol_stats": symbol_stats,
            "documents_ok": documents_ok,
            "documents_failed": documents_failed,
            "candidate_without_document": candidates_without_document,
            "review_needed_count": sum(int(item.get("review_needed_count") or 0) for item in symbol_stats),
            "pre_screen_rejected": sum(int(item.get("pre_screen_rejected") or 0) for item in symbol_stats),
            "candidates_skipped_document_failure": sum(int(item.get("candidates_skipped_document_failure") or 0) for item in symbol_stats),
            "classification_rejected_after_document": sum(int(item.get("classification_rejected_after_document") or 0) for item in symbol_stats),
        }


def parse_stored_details(details: str | None) -> dict[str, Any]:
    """Best-effort decoder used by the PE/Warrant FE detail endpoint."""
    text = _clean(details)
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {"document_text": text}
    except Exception:
        return {"document_text": text}
  