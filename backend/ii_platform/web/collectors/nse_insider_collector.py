from __future__ import annotations

"""NSE Regulation 7(2) collector.

This collector uses NSE's public Insider Trading JSON endpoint for filing discovery.
For every Coverage List NSE symbol it:

1. establishes a lightweight NSE HTTP session and queries the public PIT endpoint
   with company/date filters;
2. extracts filing rows and follows each Details/XBRL link returned by the API;
3. uses the linked NSE filing document as the source of truth;
4. fetches the NSE-hosted Details HTML/iXBRL/XML evidence;
5. parses the general filing metadata and PIT disclosure table(s);
6. emits one normalized row per transaction where the filing exposes multiple
   transactions; and
7. reports per-symbol completeness/errors so a partial run is never mistaken for
   a complete historical snapshot.

The public interface intentionally preserves the keys used by the Regulatory Desk
routes: ``rows``, ``source_errors``, ``complete``, ``symbol_stats``, etc.
"""

import asyncio
import hashlib
import html as html_lib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from lxml import etree

from ii_platform.nse import NSEApiClient, NSEApiError

logger = logging.getLogger(__name__)

NSE_PAGE_URL = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"
NSE_ARCHIVE_PAGE_URL = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading-archive-data"
NSE_TIMEZONE = ZoneInfo("Asia/Kolkata")
NSE_ARCHIVE_HOST = "nsearchives.nseindia.com"
NSE_ALLOWED_HOSTS = {
    "www.nseindia.com", "nseindia.com", NSE_ARCHIVE_HOST,
    "www.nseindia.in", "nseindia.in", "nsearchives.nseindia.in", "archives.nseindia.in",
}

MAX_HISTORY_DAYS = 2557
DEFAULT_DOCUMENT_WINDOW_DAYS = 120
RESPONSE_SPLIT_THRESHOLD = 90
REQUEST_RETRIES = 5
SYMBOL_CONCURRENCY = 4
DETAIL_CONCURRENCY = 8
DETAIL_TEXT_LIMIT = 160_000

# Historical filing links can be HTML/iXBRL or XML depending on the filing.
# The public API supplies the filing link; the document itself remains evidence.
XBRL_PIT_CUTOFF = date(2026, 5, 4)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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


def exact_seven_year_window(today: date | None = None) -> tuple[date, date]:
    end = today or datetime.now(NSE_TIMEZONE).date()
    try:
        start = end.replace(year=end.year - 7)
    except ValueError:
        start = end.replace(year=end.year - 7, month=2, day=28)
    return start, end


def exact_two_year_window(today: date | None = None) -> tuple[date, date]:
    """Backward-compatible helper for older integrations."""
    end = today or datetime.now(NSE_TIMEZONE).date()
    try:
        return end.replace(year=end.year - 2), end
    except ValueError:
        return end.replace(year=end.year - 2, month=2, day=28), end


def history_window(history_days: int) -> tuple[date, date]:
    history_days = max(1, int(history_days))
    if history_days >= MAX_HISTORY_DAYS:
        return exact_seven_year_window()
    end = datetime.now(NSE_TIMEZONE).date()
    return end - timedelta(days=history_days - 1), end


def _clean(value: Any) -> str:
    if value is None:
        return ""
    value = html_lib.unescape(str(value))
    return re.sub(r"\s+", " ", value).strip()


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


def _parse_number(value: Any) -> float | None:
    text = _clean(value).replace(",", "")
    if not text or _norm(text) in {"", "na", "n a", "none", "null", "n d", "not disclosed"}:
        return None
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?", text, flags=re.I)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=NSE_TIMEZONE)
    text = _clean(value)
    if not text:
        return None
    # ISO forms first.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=NSE_TIMEZONE)
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=NSE_TIMEZONE)
        except ValueError:
            continue
    match = re.search(
        r"(\d{1,2}[-/]\d{1,2}[-/]\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
        text,
    )
    if match and match.group(1) != text:
        return _parse_datetime(match.group(1))
    match = re.search(
        r"(\d{1,2}-[A-Za-z]{3,9}-\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
        text,
    )
    if match and match.group(1) != text:
        return _parse_datetime(match.group(1))
    return None


def _valid_url(value: Any) -> str | None:
    raw = _clean(value)
    if not raw:
        return None
    raw = raw.replace("\\/", "/")
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = urljoin("https://www.nseindia.com", raw)
    parsed = urlparse(raw)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"}:
        return None
    if not (
        host in NSE_ALLOWED_HOSTS
        or host.endswith(".nsearchives.nseindia.com")
        or host.endswith(".nseindia.in")
    ):
        return None
    return raw


def _pick(row: dict[str, Any], *keys: str) -> Any:
    lowered = {str(k).casefold(): v for k, v in row.items()}
    for key in keys:
        candidate = lowered.get(key.casefold())
        if candidate not in (None, ""):
            return candidate
    return None


def _iter_string_values(value: Any):
    """Yield string leaves from arbitrarily nested NSE response payloads."""
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_string_values(child)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _iter_string_values(child)
    elif isinstance(value, str):
        yield value


def _alternate_ixbrl_urls(url: str | None) -> list[str]:
    """Return the HTML/iXBRL sibling for an NSE XBRL URL when available."""
    raw = _valid_url(url)
    if not raw:
        return []
    parsed = urlparse(raw)
    path = parsed.path
    candidates: list[str] = [raw]
    if "/corporate/xbrl/" in path and path.lower().endswith(".xml"):
        html_path = path.replace("/corporate/xbrl/", "/corporate/ixbrl/")[:-4] + ".html"
        candidates.append(parsed._replace(path=html_path).geturl())
    elif "/corporate/ixbrl/" in path and path.lower().endswith(".html"):
        xml_path = path.replace("/corporate/ixbrl/", "/corporate/xbrl/")[:-5] + ".xml"
        candidates.append(parsed._replace(path=xml_path).geturl())
    return list(dict.fromkeys(candidates))


def _find_detail_url(row: dict[str, Any]) -> str | None:
    """Find a Details/iXBRL/XBRL URL even when NSE nests it under another key."""
    preferred_keys = (
        "detail_url", "details_url", "detailUrl", "details", "xbrl", "xbrlUrl",
        "xbrl_url", "xbrlLink", "detailsLink", "detailsUrl", "attachment",
    )
    value = _pick(row, *preferred_keys)
    candidate = _valid_url(value)
    if candidate:
        return candidate
    for raw in _iter_string_values(row):
        candidate = _valid_url(raw)
        if candidate and "/corporate/" in candidate and ("xbrl" in candidate.casefold() or "ixbrl" in candidate.casefold()):
            return candidate
    return None


def _looks_like_regulation_7_2(value: Any) -> bool:
    text = _norm(value)
    return bool(
        text
        and (
            "regulation 7 2" in text
            or "regulation 7 2 read" in text
            or text in {"7 2", "7 2 pit", "reg 7 2"}
        )
    )


def _has_valid_pit_payload_shape(payload: Any) -> bool:
    """Validate NSE PIT response structure, distinguishing empty from changed schema."""
    collection_keys = {"data", "rows", "records", "result", "pit", "corporatespit", "corporatepit"}
    row_keys = {
        "symbol", "company", "anex", "acqname", "personcategory", "secacq",
        "secval", "tdptransactiontype", "pid", "did", "xbrl",
    }

    def visit(value: Any, depth: int = 0) -> bool:
        if depth > 10:
            return False
        if isinstance(value, list):
            if not value:
                return depth == 0
            return any(
                isinstance(row, dict) and bool({str(key).casefold() for key in row} & row_keys)
                for row in value[:100]
            )
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() in collection_keys and isinstance(child, list):
                    if not child or any(
                        isinstance(row, dict) and bool({str(field).casefold() for field in row} & row_keys)
                        for row in child[:100]
                    ):
                        return True
                if isinstance(child, (dict, list)) and visit(child, depth + 1):
                    return True
        return False

    return visit(payload)


def _raw_filing_identifier(row: dict[str, Any]) -> str | None:
    for key in ("pid", "PID", "did", "DID", "seq_id", "seqId", "id", "filingId", "filing_id"):
        value = _clean(row.get(key))
        if value:
            return value[:255]
    return None


def _raw_filing_key(symbol: str, row: dict[str, Any], detail_url: str | None) -> str:
    filing_id = _raw_filing_identifier(row)
    identity = filing_id or detail_url or json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"NSE_PIT|{symbol.upper()}|{identity}".encode("utf-8")).hexdigest()


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    """Recursively locate the strongest list-of-dicts candidate in NSE JSON."""
    candidates: list[tuple[int, int, list[dict[str, Any]]]] = []

    def visit(value: Any, depth: int = 0) -> None:
        if isinstance(value, list):
            rows = [item for item in value if isinstance(item, dict)]
            if rows:
                score = 0
                for row in rows[:30]:
                    keys = {str(k).casefold() for k in row}
                    score += 1 if keys & {
                        "symbol",
                        "company",
                        "anex",
                        "acqname",
                        "personcategory",
                        "xbrl",
                        "pid",
                        "did",
                        "date",
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


def _source_key(
    symbol: str,
    row: dict[str, Any],
    detail_url: str | None,
    tx_index: int = 0,
) -> str:
    stable = {
        "symbol": symbol.upper(),
        "pid": _clean(_pick(row, "pid", "nse_pid", "nsepid")),
        "did": _clean(_pick(row, "did", "nse_did", "nsedid")),
        "date": _clean(_pick(row, "date", "broadcastDate", "broadcast_at")),
        "person": _clean(_pick(row, "acqName", "person_name", "name", "clientName")),
        "action": _clean(_pick(row, "tdpTransactionType", "action", "transactionType")),
        "quantity": _clean(_pick(row, "secAcq", "quantity", "securitiesAcquired")),
        "detail": detail_url or "",
        "tx_index": tx_index,
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _expand_table_row(tr) -> list[str]:
    values: list[str] = []
    for cell in tr.find_all(["th", "td"]):
        text = _clean(cell.get_text(" ", strip=True))
        try:
            span = max(1, int(cell.get("colspan") or 1))
        except (TypeError, ValueError):
            span = 1
        values.extend([text] * span)
    return values


def _table_matrix(table) -> list[list[str]]:
    """Expand an HTML table into a rectangular matrix honoring rowspan/colspan."""
    grid: list[list[str | None]] = []
    for row_index, tr in enumerate(table.find_all("tr")):
        while len(grid) <= row_index:
            grid.append([])
        col_index = 0
        for cell in tr.find_all(["th", "td"]):
            while col_index < len(grid[row_index]) and grid[row_index][col_index] is not None:
                col_index += 1
            text = _clean(cell.get_text(" ", strip=True))
            try:
                rowspan = max(1, int(cell.get("rowspan") or 1))
            except (TypeError, ValueError):
                rowspan = 1
            try:
                colspan = max(1, int(cell.get("colspan") or 1))
            except (TypeError, ValueError):
                colspan = 1
            required_rows = row_index + rowspan
            while len(grid) < required_rows:
                grid.append([])
            for rr in range(row_index, row_index + rowspan):
                while len(grid[rr]) < col_index + colspan:
                    grid[rr].append(None)
                for cc in range(col_index, col_index + colspan):
                    grid[rr][cc] = text
            col_index += colspan
    width = max((len(row) for row in grid), default=0)
    return [[_clean(row[col]) if col < len(row) and row[col] is not None else "" for col in range(width)] for row in grid if any(cell is not None and _clean(cell) for cell in row)]


def _best_header_rows(matrix: list[list[str]]) -> tuple[list[str], int]:
    if not matrix:
        return [], 0
    candidates: list[tuple[int, int]] = []
    markers = (
        "name of the person", "category of person", "transaction type",
        "security", "holding", "date of allotment", "date of transaction",
        "value of security", "no of security", "mode of acquisition",
        "from date", "to date",
    )
    for index, row in enumerate(matrix[:15]):
        hay = " | ".join(_norm(value) for value in row)
        score = sum(2 for marker in markers if marker in hay)
        if len(row) >= 6:
            score += 1
        if score:
            candidates.append((score, index))
    if not candidates:
        return matrix[0], 0
    _, bottom = max(candidates)
    start = max(0, bottom - 2)
    while start < bottom and not any(_clean(v) for v in matrix[start]):
        start += 1
    width = max(len(matrix[idx]) for idx in range(start, bottom + 1))
    headers: list[str] = []
    for col in range(width):
        parts: list[str] = []
        for idx in range(start, bottom + 1):
            value = _clean(matrix[idx][col]) if col < len(matrix[idx]) else ""
            if value and _norm(value) not in {_norm(part) for part in parts}:
                # Ignore purely ordinal header rows (1, 2, 3...).
                if not re.fullmatch(r"\d+", value):
                    parts.append(value)
        headers.append(" ".join(parts))
    return headers, bottom


def _canonical_header(header: str) -> str:
    value = _norm(header)
    mapping = {
        "name of the person": "person_name",
        "name person": "person_name",
        "client name": "person_name",
        "category of person": "client_category",
        "person category": "client_category",
        "designation of person": "designation",
        "designation": "designation",
        "type of instrument": "instrument",
        "description of type of instrument applicable in case of other is selected": "instrument_description",
        "no of security no of securities securities held prior to acquisition disposal": "holding_before",
        "no of security holdings prior to acquisition disposal": "holding_before",
        "security held prior to acquisition disposal no of security": "holding_before",
        "no of securities held prior to acquisition disposal": "holding_before",
        "holding before": "holding_before",
        "% of shareholding % holding prior": "holding_before_percent",
        "% of shareholding": "holding_before_percent",
        "no of security acquired disposed": "quantity",
        "no of securities acquired disposed": "quantity",
        "securities acquired disposed no of security": "quantity",
        "quantity": "quantity",
        "value of security": "security_value",
        "value of securities": "security_value",
        "transaction type": "action",
        "no of security after acquisition disposal": "post_transaction_holding",
        "no of securities after acquisition disposal": "post_transaction_holding",
        "holding after": "post_transaction_holding",
        "% of shareholding after": "post_transaction_percent",
        "% holding after": "post_transaction_percent",
        "date of transaction": "transaction_date",
        "date of allotment advice acquisition of shares sale of shares specify": "transaction_date",
        "date of allotment advice acquisition of shares disposal of shares specify": "transaction_date",
        "from date": "transaction_date_from",
        "to date": "transaction_date_to",
        "date of acquisition disposal": "transaction_date",
        "date of allotment advice acquisition of shares sale of shares specify": "transaction_date",
        "date of allotment advice acquisition of shares sale of shares specify from": "transaction_date_from",
        "date of allotment advice acquisition of shares sale of shares specify to": "transaction_date_to",
        "mode of acquisition disposal": "transaction_mode",
        "mode of acquisition disposal on market public rights preferential offer off market inter se transfer esops etc": "transaction_mode",
        "nature of transaction": "action",
        "exchange": "exchange",
        "remarks": "remarks",
        "period": "period",
    }
    if value in mapping:
        return mapping[value]
    # More permissive keyword mapping.
    if "name of the person" in value or value == "name of person":
        return "person_name"
    if "category of person" in value or "person category" in value:
        return "client_category"
    if "transaction type" in value:
        return "action"
    if "value of security" in value or "value of securities" in value:
        return "security_value"
    if "no of security" in value or "no of securities" in value:
        if "acquired" in value or "disposed" in value:
            return "quantity"
        if "prior" in value or "before" in value:
            return "holding_before"
        if "after" in value or "post" in value:
            return "post_transaction_holding"
    if "percent of shareholding" in value or "of shareholding" in value or "percent holding" in value:
        if "prior" in value or "before" in value:
            return "holding_before_percent"
        if "after" in value or "post" in value:
            return "post_transaction_percent"
    if "date of allotment advice" in value or ("date" in value and "acquisition" in value and "sale of shares" in value):
        if " from" in f" {value}":
            return "transaction_date_from"
        if " to" in f" {value}":
            return "transaction_date_to"
        return "transaction_date"
    if "date" in value and "transaction" in value:
        return "transaction_date"
    return ""


def _parse_detail_html(html_text: str, url: str | None) -> dict[str, Any]:
    soup = BeautifulSoup(html_text or "", "lxml")
    # Drop navigation/scripts before collecting evidence text.
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    plain_text = _clean(soup.get_text(" ", strip=True))
    if len(plain_text) > DETAIL_TEXT_LIMIT:
        plain_text = plain_text[:DETAIL_TEXT_LIMIT]

    general: dict[str, str] = {}
    transactions: list[dict[str, Any]] = []
    parsed_tables: list[dict[str, Any]] = []

    for table_index, table in enumerate(soup.find_all("table")):
        matrix = _table_matrix(table)
        if not matrix:
            continue
        headers, header_row_index = _best_header_rows(matrix)
        canonical_headers = [_canonical_header(value) for value in headers]
        tx_header_hits = sum(
            key in {"person_name", "client_category", "quantity", "security_value", "action", "post_transaction_holding"}
            for key in canonical_headers
        )
        table_capture = {
            "table_index": table_index,
            "headers": headers,
            "rows": matrix[:100],
        }
        parsed_tables.append(table_capture)

        if tx_header_hits >= 2 and len(headers) >= 4:
            for row_index in range(header_row_index + 1, len(matrix)):
                values = matrix[row_index]
                if len(values) < 3:
                    continue
                if all(not _clean(value) or _norm(value) in {"-", "na", "n/a"} for value in values):
                    continue
                tx: dict[str, Any] = {}
                for col, key in enumerate(canonical_headers):
                    if key and col < len(values):
                        value = _clean(values[col])
                        if value:
                            tx[key] = value
                if tx.get("transaction_date_from") and not tx.get("transaction_date"):
                    tx["transaction_date"] = tx["transaction_date_from"]
                if tx.get("transaction_date_from") and tx.get("transaction_date_to"):
                    tx["period"] = f"{tx['transaction_date_from']} to {tx['transaction_date_to']}"
                if any(tx.get(key) for key in ("person_name", "quantity", "security_value", "action", "transaction_date", "transaction_mode")):
                    transactions.append(tx)
            continue

        # General-information tables are usually 2-column label/value tables.
        if len(matrix[0]) >= 2:
            for row in matrix[:80]:
                if len(row) < 2:
                    continue
                label = _clean(row[0])
                value = _clean(row[1])
                if label and value and len(label) <= 120:
                    general[_norm(label)] = value

    def general_value(*labels: str) -> str | None:
        for label in labels:
            candidate = general.get(_norm(label))
            if candidate:
                return candidate
        for key, candidate in general.items():
            for label in labels:
                if _norm(label) in key or key in _norm(label):
                    return candidate
        return None

    return {
        "status": "ok" if plain_text else "empty",
        "url": url,
        "text": plain_text,
        "general": general,
        "nse_symbol": general_value("NSE Symbol", "Symbol"),
        "company_name": general_value("Company Name", "Company"),
        "scrip_code": general_value("Scrip Code"),
        "isin": general_value("ISIN Code", "ISIN"),
        "name_of_signatory": general_value("Name of the Signatory"),
        "designation_of_signatory": general_value("Designation of Signatory"),
        "place": general_value("Place"),
        "date_of_filing": general_value("Date of Filing"),
        "regulation": general_value("Disclosure under Regulation", "Regulation"),
        "revised_filing": general_value("Revised Filing", "Revision"),
        "transactions": transactions,
        "tables": parsed_tables,
        "sha256": hashlib.sha256((html_text or "").encode("utf-8", errors="ignore")).hexdigest(),
        "bytes": len((html_text or "").encode("utf-8", errors="ignore")),
    }


def _parse_xml_payload(content: bytes, url: str) -> dict[str, Any]:
    """Parse an NSE XBRL artifact without using the NSE JSON API.

    Newer PIT filings are often delivered as XBRL/XML.  We group facts by
    ``contextRef`` and map the documented PIT concepts to the normalized
    transaction fields.  When the artifact is iXBRL HTML, the HTML parser is
    preferred before the XML fallback.
    """
    raw_text = content.decode("utf-8", errors="replace")
    if b"<html" in content[:5000].lower() or b"<!doctype" in content[:5000].lower():
        parsed = _parse_detail_html(raw_text, url)
        parsed["parser"] = "ixbrl-html"
        return parsed

    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=True)
    root = etree.fromstring(content, parser=parser)
    groups: dict[str, dict[str, list[str]]] = {}
    all_facts: list[tuple[str, str, str]] = []

    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        local = node.tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
        text = _clean(node.text)
        if not text:
            continue
        context = _clean(node.attrib.get("contextRef")) or "__global__"
        groups.setdefault(context, {}).setdefault(_norm(local), []).append(text)
        all_facts.append((local, text, context))

    def first_value(group: dict[str, list[str]], *patterns: str) -> str | None:
        for key, values in group.items():
            for pattern in patterns:
                if pattern in key:
                    return values[0]
        return None

    def build_tx(group: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "person_name": first_value(group, "acqname", "acquiringperson", "acquirername", "disposername", "nameofperson"),
            "client_category": first_value(group, "personcategory", "categoryofperson", "clientcategory"),
            "instrument": first_value(group, "sectype", "securitytype", "typeofsecurity", "typeofinstrument"),
            "holding_before": first_value(group, "befacqsharesno", "holdingbefore", "sharesheldprior", "securityheldprior", "noofsecurityheldprior"),
            "holding_before_percent": first_value(group, "befacqsharesper", "holdingbeforepercent", "shareholdingprior", "percentshareholdingprior"),
            "quantity": first_value(group, "secacq", "securitiesacquired", "securitiesdisposed", "noofsecurityacq", "noofsecuritiesacq", "quantity"),
            "security_value": first_value(group, "secval", "valueofsecurity", "valueofsecurities", "transactionvalue", "securityvalue"),
            "action": first_value(group, "tdptransactiontype", "transactiontype", "natureoftransaction", "acquisitiondisposal"),
            "post_transaction_holding": first_value(group, "afteracqsharesno", "posttransactionholding", "sharesheldpost", "afteracquisitiondisposal"),
            "post_transaction_percent": first_value(group, "afteracqsharesper", "posttransactionpercent", "shareholdingafter"),
            "transaction_mode": first_value(group, "acqmode", "modeofacquisition", "transactionmode"),
            "transaction_date_from": first_value(group, "acqfromdt", "acquisitionfrom", "fromdate"),
            "transaction_date_to": first_value(group, "acqtodt", "acquisitionto", "todate"),
            "transaction_date": first_value(group, "dateoftransaction", "transactiondate", "intimdt", "dateofallotment"),
            "remarks": first_value(group, "remarks", "remark"),
        }

    transactions: list[dict[str, Any]] = []
    for context, group in groups.items():
        if context == "__global__":
            continue
        tx = build_tx(group)
        signal = any(tx.get(key) for key in ("person_name", "quantity", "action", "security_value", "transaction_date"))
        if signal:
            transactions.append(tx)

    if not transactions:
        # Some NSE XBRL files use a separate context for every fact or omit
        # contextRef on the transaction facts. A conservative flat pass keeps the
        # disclosure instead of silently dropping it.
        flat: dict[str, list[str]] = {}
        for local, text, _ in all_facts:
            flat.setdefault(_norm(local), []).append(text)
        tx = build_tx(flat)
        if any(tx.get(key) for key in ("person_name", "quantity", "action", "security_value", "transaction_date")):
            transactions = [tx]

    # If the XML uses prefixed taxonomy aliases that do not contain our preferred
    # semantic stems, inspect the element names for the strongest known PIT hints.
    if not transactions:
        hint_values = {key: [] for key in (
            "person_name", "client_category", "quantity", "security_value",
            "action", "transaction_date", "transaction_mode",
        )}
        aliases = {
            "person_name": ("acqname", "nameoftheperson", "nameofacquirer", "nameofdisposer"),
            "client_category": ("personcategory", "categoryofperson"),
            "quantity": ("secacq", "noofsecuritiesacquired", "noofsecurityacquired", "quantity"),
            "security_value": ("secval", "valueofsecurity", "transactionvalue"),
            "action": ("tdptransactiontype", "transactiontype", "acquisitiondisposal"),
            "transaction_date": ("acqfromdt", "acqtodt", "dateoftransaction", "intimdt"),
            "transaction_mode": ("acqmode", "modeofacquisition"),
        }
        for local, value, _ in all_facts:
            key = _norm(local)
            for target, patterns in aliases.items():
                if any(pattern in key for pattern in patterns):
                    hint_values[target].append(value)
        if any(hint_values.values()):
            transactions = [{
                "person_name": next(iter(hint_values["person_name"]), None),
                "client_category": next(iter(hint_values["client_category"]), None),
                "quantity": next(iter(hint_values["quantity"]), None),
                "security_value": next(iter(hint_values["security_value"]), None),
                "action": next(iter(hint_values["action"]), None),
                "transaction_date": next(iter(hint_values["transaction_date"]), None),
                "transaction_mode": next(iter(hint_values["transaction_mode"]), None),
            }]

    return {
        "status": "ok",
        "url": url,
        "parser": "xbrl-xml",
        "root_tag": root.tag.rsplit("}", 1)[-1],
        "elements": [{"tag": tag, "text": text, "contextRef": ctx} for tag, text, ctx in all_facts[:2500]],
        "transactions": transactions,
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }


class NSEInsiderTradingCollector:
    page_url = NSE_PAGE_URL
    archive_page_url = NSE_ARCHIVE_PAGE_URL

    def __init__(
        self,
        *,
        request_retries: int = REQUEST_RETRIES,
        document_window_days: int = DEFAULT_DOCUMENT_WINDOW_DAYS,
        symbol_concurrency: int = SYMBOL_CONCURRENCY,
        detail_concurrency: int = DETAIL_CONCURRENCY,
        quote_refresh: bool = False,
    ) -> None:
        self.request_retries = max(1, int(request_retries))
        self.document_window_days = max(30, int(document_window_days))
        self.symbol_concurrency = max(1, int(symbol_concurrency))
        self.detail_concurrency = max(1, int(detail_concurrency))
        # Quote-by-symbol is intentionally disabled in the Regulatory PIT
        # ingestion path. A full-universe quote fan-out can re-trigger NSE 403
        # protections. CMP should come from a dedicated, rate-limited worker
        # quote stage or an existing cached market snapshot.
        self.quote_refresh = bool(quote_refresh)

    async def _fetch_detail_http(
        self,
        client: NSEApiClient,
        url: str,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:
        """Fetch a filing document discovered in an NSE API response.

        The API supplies the filing link. This method fetches only the linked
        NSE-hosted document; it never discovers filings by parsing the NSE UI.
        """
        async with semaphore:
            candidates = _alternate_ixbrl_urls(url) or [_valid_url(url) or url]
            last_error: Exception | None = None
            for candidate_url in candidates:
                try:
                    data, content_type = await client.get_bytes(
                        candidate_url,
                        referer=NSE_PAGE_URL,
                    )
                    lower_type = (content_type or "").casefold()
                    lower_head = data[:1600].lower()
                    if (
                        "html" in lower_type
                        or b"<html" in lower_head
                        or b"<!doctype" in lower_head
                        or b"<table" in lower_head
                    ):
                        parsed = _parse_detail_html(
                            data.decode("utf-8", errors="replace"),
                            candidate_url,
                        )
                        parsed["status"] = "ok" if parsed.get("text") else "empty"
                        parsed["parser"] = "ixbrl-html" if "/ixbrl/" in candidate_url else "html"
                        if parsed["status"] == "empty":
                            parsed["error"] = "NSE Details HTML returned no readable filing content"
                        return parsed
                    return _parse_xml_payload(data, candidate_url)
                except (NSEApiError, UnicodeDecodeError, ValueError, etree.XMLSyntaxError) as exc:
                    last_error = exc
                    logger.debug("NSE PIT document fetch/parse failed url=%s error=%s", candidate_url, exc)
                except Exception as exc:
                    last_error = exc
                    logger.debug("NSE PIT document fetch failed url=%s error=%s", candidate_url, exc)
            return {
                "status": "error",
                "url": url,
                "parser": "document-api",
                "error": f"{type(last_error).__name__}: {last_error}",
            }

    async def _build_detail_rows(
        self,
        client: NSEApiClient,
        symbol: str,
        raw_rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        # Deduplicate raw filings before downloading details.
        filings: dict[str, dict[str, Any]] = {}
        for raw in raw_rows:
            raw_copy = {str(k): v for k, v in raw.items()}
            detail_url = _find_detail_url(raw_copy)
            filing_date = _parse_datetime(_pick(raw_copy, "date", "broadcastDate", "broadcast_at"))
            if filing_date is None:
                filing_date = datetime.combine(exact_two_year_window()[1], datetime.min.time(), tzinfo=NSE_TIMEZONE)
            key = _source_key(symbol, raw_copy, detail_url)
            filings.setdefault(
                key,
                {
                    "key": key,
                    "raw": raw_copy,
                    "detail_url": detail_url,
                    "filing_date": filing_date,
                },
            )

        semaphore = asyncio.Semaphore(self.detail_concurrency)
        detail_results: dict[str, dict[str, Any]] = {}

        async def fetch_one(key: str, filing: dict[str, Any]) -> None:
            detail_url = filing.get("detail_url")
            if not detail_url:
                detail_results[key] = {"status": "missing_url", "error": "NSE Details/XBRL URL is missing"}
                return
            result = await self._fetch_detail_http(client, detail_url, semaphore)
            detail_results[key] = result

        await asyncio.gather(*(fetch_one(key, filing) for key, filing in filings.items()))

        normalized_rows: list[dict[str, Any]] = []
        detail_errors = 0
        detail_ok = 0

        for key, filing in filings.items():
            raw = filing["raw"]
            detail_url = filing.get("detail_url")
            detail = detail_results.get(key) or {"status": "missing"}
            if detail.get("status") == "ok":
                detail_ok += 1
            else:
                detail_errors += 1

            if detail.get("status") != "ok":
                # NSE's listing response itself contains the PIT transaction facts.
                # An unavailable XBRL/HTML attachment must not discard those facts.
                # We try the listing-row fallback below and count the document error
                # separately so the run remains auditable.
                logger.warning(
                    "NSE PIT attachment unavailable; attempting listing-row facts symbol=%s detail_url=%s status=%s error=%s",
                    symbol, detail_url, detail.get("status"), detail.get("error"),
                )

            listing_symbol = _clean(_pick(raw, "symbol", "SYMBOL")) or symbol
            company_name = _clean(
                _pick(raw, "company", "companyName", "COMPANY")
                or detail.get("company_name")
                or listing_symbol
            )
            regulation = _clean(
                _pick(raw, "anex", "regulation", "REGULATION")
                or detail.get("regulation")
                or "Regulation 7(2)"
            )
            if not _looks_like_regulation_7_2(regulation):
                # The archive page may omit the regulation label; the page itself is
                # the Regulation 7(2) archive, so keep those rows.
                if detail_url or detail.get("status") != "missing_url":
                    regulation = "Regulation 7(2)"
                else:
                    continue

            transactions = list(detail.get("transactions") or []) if isinstance(detail, dict) else []
            listing_fallback_used = False
            if not transactions:
                # Prefer the full XBRL/HTML parse, but fall back to the structured
                # fields already supplied by /api/corporates-pit (or its CSV export).
                # This prevents a bad attachment from turning a usable disclosure
                # row into a silently missing transaction.
                listing_tx = {
                    "person_name": _pick(raw, "acqName", "name", "personName", "person_name"),
                    "client_category": _pick(raw, "personCategory", "categoryOfPerson", "clientCategory"),
                    "instrument": _pick(raw, "secType", "securityTypePrior"),
                    "holding_before": _pick(raw, "befAcqSharesNo", "holdingBefore"),
                    "holding_before_percent": _pick(raw, "befAcqSharesPer", "holdingBeforePercent"),
                    "quantity": _pick(raw, "secAcq", "quantity", "buyQuantity", "sellquantity", "sellQuantity"),
                    "security_value": _pick(raw, "secVal", "value", "buyValue", "sellValue"),
                    "action": _pick(raw, "tdpTransactionType", "action", "transactionType"),
                    "post_transaction_holding": _pick(raw, "afterAcqSharesNo", "postTransactionHolding"),
                    "post_transaction_percent": _pick(raw, "afterAcqSharesPer", "postTransactionPercent"),
                    "transaction_date_from": _pick(raw, "acqfromDt", "acquisitionFrom"),
                    "transaction_date_to": _pick(raw, "acqtoDt", "acquisitionTo"),
                    "transaction_date": _pick(raw, "acqtoDt", "acqfromDt", "intimDt", "dateOfTransaction", "date"),
                    "transaction_mode": _pick(raw, "acqMode", "transactionMode"),
                    "remarks": _pick(raw, "remarks", "remark"),
                }
                usable_listing_facts = any(
                    _clean(listing_tx.get(field))
                    for field in ("person_name", "action", "quantity", "security_value", "transaction_date", "transaction_mode")
                )
                if usable_listing_facts:
                    transactions = [listing_tx]
                    listing_fallback_used = True
                    logger.info(
                        "NSE_PIT_LISTING_ROW_FALLBACK symbol=%s source_row_fields=%s",
                        symbol, len(raw),
                    )
                else:
                    detail_errors += 1
                    logger.warning(
                        "NSE PIT filing contained neither parsed attachment transactions nor usable listing facts symbol=%s url=%s parser=%s sha256=%s",
                        symbol, detail_url, detail.get("parser"), detail.get("sha256"),
                    )
                    continue

            for tx_index, tx in enumerate(transactions):
                person_name = _clean(
                    tx.get("person_name")
                    or _pick(raw, "acqName", "name", "personName")
                    or detail.get("name_of_signatory")
                )
                category = _clean(
                    tx.get("client_category")
                    or _pick(raw, "personCategory", "categoryOfPerson", "clientCategory")
                )
                quantity = _parse_number(
                    tx.get("quantity")
                    or _pick(raw, "secAcq", "quantity")
                )
                security_value = _parse_number(
                    tx.get("security_value")
                    or _pick(raw, "secVal", "value")
                )
                holding_before = _parse_number(
                    tx.get("holding_before")
                    or _pick(raw, "befAcqSharesNo", "holdingBefore")
                )
                holding_before_percent = _parse_number(
                    tx.get("holding_before_percent")
                    or _pick(raw, "befAcqSharesPer", "holdingBeforePercent")
                )
                post_holding = _parse_number(
                    tx.get("post_transaction_holding")
                    or _pick(raw, "afterAcqSharesNo", "postTransactionHolding")
                )
                post_percent = _parse_number(
                    tx.get("post_transaction_percent")
                    or _pick(raw, "afterAcqSharesPer", "postTransactionPercent")
                )
                percent_change = None
                if holding_before_percent is not None and post_percent is not None:
                    percent_change = post_percent - holding_before_percent

                action = _clean(
                    tx.get("action")
                    or _pick(raw, "tdpTransactionType", "action", "transactionType")
                )
                transaction_mode = _clean(
                    tx.get("transaction_mode")
                    or _pick(raw, "acqMode", "transactionMode")
                )
                # Keep the underlying transaction date distinct from the filing
                # broadcast date. If a listing row omits the trade date, leave it
                # missing here; the persistence layer records a disclosed fallback
                # to broadcast_at instead of silently presenting it as a trade date.
                transaction_date = _parse_datetime(
                    tx.get("transaction_date_from")
                    or tx.get("transaction_date")
                    or _pick(raw, "acqtoDt", "acqfromDt", "intimDt", "dateOfTransaction")
                )
                broadcast = _parse_datetime(
                    _pick(raw, "date", "broadcastDate", "broadcast_at")
                    or detail.get("date_of_filing")
                ) or filing["filing_date"]

                price = _parse_number(tx.get("price"))
                if price is None and security_value is not None and quantity not in (None, 0):
                    price = security_value / quantity

                period = _clean(tx.get("period"))
                if not period:
                    from_date = _parse_datetime(tx.get("transaction_date_from") or _pick(raw, "acqfromDt", "acquisitionFrom"))
                    to_date = _parse_datetime(tx.get("transaction_date_to") or _pick(raw, "acqtoDt", "acquisitionTo"))
                    if from_date and to_date:
                        period = f"{from_date.astimezone(NSE_TIMEZONE):%d-%b-%Y} to {to_date.astimezone(NSE_TIMEZONE):%d-%b-%Y}"
                    elif from_date:
                        period = from_date.astimezone(NSE_TIMEZONE).strftime("%d-%b-%Y")

                full_source_data = {
                    "source": "NSE",
                    "listing_row": raw,
                    "details_url": detail_url,
                    "details": detail,
                    "transaction_index": tx_index,
                    "normalization": "listing-row-fallback" if listing_fallback_used else "parsed-details-or-xbrl",
                    "listing_row_fallback_used": listing_fallback_used,
                    "attachment_parse_status": detail.get("status"),
                    "transaction_date_inferred_from_broadcast": transaction_date is None and broadcast is not None,
                    "archive_partition_checked": filing["filing_date"].date() < XBRL_PIT_CUTOFF,
                }
                normalized_rows.append(
                    {
                        "company_name": company_name,
                        "nse_symbol": listing_symbol.upper(),
                        "regulation": regulation or "Regulation 7(2)",
                        "person_name": person_name or "N/D",
                        "client_category": category or "N/D",
                        "security_type_prior": _clean(tx.get("instrument")) or _clean(_pick(raw, "secType")),
                        "holding_before": holding_before,
                        "holding_before_percent": holding_before_percent,
                        "quantity": quantity,
                        "security_value": security_value,
                        "action": action or "N/D",
                        "security_type_post": _clean(_pick(raw, "securitiesTypePost")),
                        "post_transaction_holding": post_holding,
                        "post_transaction_percent": post_percent,
                        "percent_change": percent_change,
                        "price": price,
                        "period": period or None,
                        "transaction_mode": transaction_mode or None,
                        "transaction_date": transaction_date,
                        "broadcast_date": broadcast.isoformat() if broadcast else None,
                        "buy_value": _parse_number(_pick(raw, "buyValue")),
                        "buy_quantity": _parse_number(_pick(raw, "buyQuantity")),
                        "sell_value": _parse_number(_pick(raw, "sellValue")),
                        "sell_quantity": _parse_number(_pick(raw, "sellquantity", "sellQuantity")),
                        "exchange": _clean(_pick(raw, "exchange")) or None,
                        "remarks": _clean(tx.get("remarks") or _pick(raw, "remarks", "remark")) or None,
                        "source_url": _valid_url(detail.get("url")) or detail_url,
                        "submission_type": _clean(_pick(raw, "submissionType", "typeOfSubmission")) or None,
                        "revision_remarks": _clean(_pick(raw, "revisionRemarks", "revision_remarks")) or None,
                        "nse_pid": _clean(_pick(raw, "pid", "PID")) or None,
                        "nse_did": _clean(_pick(raw, "did", "DID")) or None,
                        "source": "NSE",
                        "source_key": hashlib.sha256(
                            json.dumps(
                                {
                                    "symbol": symbol.upper(),
                                    "detail": detail_url or "",
                                    "tx": tx,
                                },
                                sort_keys=True,
                                ensure_ascii=False,
                            ).encode("utf-8")
                        ).hexdigest(),
                        "source_data": full_source_data,
                    }
                )

        return normalized_rows, {
            "filings": len(filings),
            "details_ok": detail_ok,
            "details_errors": detail_errors,
            "transactions": len(normalized_rows),
        }


    async def _process_symbol_rows(
        self,
        client: NSEApiClient,
        symbol: str,
        raw_rows: list[dict[str, Any]],
        start: date,
        end: date,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Normalize already-discovered PIT rows for one symbol.

        Filing discovery is now exchange-wide and happens once per date window in
        ``collect``. This helper only filters the shared response to one symbol,
        follows filing detail links, and performs normalization/enrichment.
        """
        raw_rows = [dict(row) for row in raw_rows]
        prepared: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_rows:
            api_symbol = _clean(_pick(raw, "symbol", "SYMBOL"))
            if api_symbol and _norm(api_symbol) != _norm(symbol):
                continue
            item = dict(raw)
            item["anex"] = _clean(item.get("regulation") or "Regulation 7(2)")
            item["submissionType"] = _clean(item.get("submission_type"))
            item["revisionRemarks"] = _clean(item.get("revision_remarks"))
            item["company"] = _clean(item.get("company"))
            item["date"] = _clean(item.get("date") or item.get("intimDt"))
            item["detail_url"] = _find_detail_url(item)
            item["xbrl"] = _valid_url(_pick(item, "xbrl", "xbrl_url"))
            filing_key = _source_key(symbol, item, item.get("detail_url") or item.get("xbrl"))
            if filing_key in seen:
                continue
            seen.add(filing_key)
            prepared.append(item)

        normalized_rows, detail_stats = await self._build_detail_rows(client, symbol, prepared)

        # Keep quote enrichment best-effort. Importantly, a quote 403 does not
        # poison PIT ingestion because Current Market Price is an enrichment field,
        # not the filing source of truth.
        current_price = None
        quote_change_percent = None
        quote_error = None
        if self.quote_refresh:
            try:
                quote = await client.get_equity_quote(symbol)
                current_price = quote.get("last_price")
                quote_change_percent = quote.get("change_percent")
            except NSEApiError as exc:
                quote_error = f"{type(exc).__name__}: {exc}"
                logger.info("NSE PIT quote skipped symbol=%s error=%s", symbol, quote_error)
            except Exception as exc:
                quote_error = f"{type(exc).__name__}: {exc}"
                logger.info("NSE PIT quote unavailable symbol=%s error=%s", symbol, quote_error)
        else:
            quote_error = "quote refresh disabled in PIT collector; use worker quote snapshot"

        if current_price is not None and normalized_rows:
            fetched_at = datetime.now(NSE_TIMEZONE).isoformat()
            for normalized in normalized_rows:
                source_data = normalized.get("source_data")
                if not isinstance(source_data, dict):
                    source_data = {}
                    normalized["source_data"] = source_data
                source_data["current_price"] = current_price
                source_data["current_price_change_percent"] = quote_change_percent
                source_data["current_price_fetched_at"] = fetched_at

        errors: list[str] = []
        if detail_stats.get("details_errors"):
            errors.append(f"{detail_stats['details_errors']} NSE Details/XBRL document(s) failed to fetch or parse")

        stats = {
            "symbol": symbol,
            "raw_rows": len(raw_rows),
            "unique_filings": len(prepared),
            "normalized_transactions": len(normalized_rows),
            "details_ok": int(detail_stats.get("details_ok") or 0),
            "details_errors": int(detail_stats.get("details_errors") or 0),
            "pages": 1,
            "errors": errors,
            "warnings": [quote_error] if quote_error else [],
            "current_price": current_price,
            "current_price_error": quote_error,
            "transport": "nse-public-api-bulk",
            "api_windows": 1,
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
        }
        if prepared or normalized_rows or stats.get("errors"):
            logger.info(
                "NSE PIT symbol processed symbol=%s rows=%s transactions=%s details_ok=%s details_errors=%s",
                symbol, len(prepared), len(normalized_rows), stats["details_ok"], stats["details_errors"],
            )
        else:
            logger.debug("NSE PIT symbol processed symbol=%s rows=0 transactions=0", symbol)
        return normalized_rows, stats

    async def collect(
        self,
        history_days: int = MAX_HISTORY_DAYS,
        symbols: list[str] | tuple[str, ...] | set[str] | None = None,
        on_batch: Callable[[list[tuple[str, list[dict[str, Any]], dict[str, Any]]], int, int], Awaitable[None]] | None = None,
        on_raw_window: Callable[[list[dict[str, Any]], dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Collect Regulation 7(2) disclosures with exchange-wide discovery.

        ``symbols`` is a local filter only. NSE is queried WITHOUT a symbol filter,
        so every listed-company disclosure in the requested date window is
        discovered in one request per window. This is the key anti-403/performance
        change: the worker no longer fans out one API request per company.
        """
        start, end = history_window(history_days)
        started = time.monotonic()
        requested_symbols = list(dict.fromkeys(
            str(value).strip().upper()
            for value in (symbols or [])
            if str(value).strip()
        ))
        if not requested_symbols:
            return {
                "rows": [],
                "source_errors": {"NSE Company Master": "No NSE symbols were supplied to the local filter."},
                "complete": False,
                "scanned": 0,
                "deduped": 0,
                "regulation_7_2": 0,
                "xml_errors": 0,
                "detail_errors": 0,
                "symbol_stats": [],
                "raw_filings": [],
                "window_stats": [],
                "from_date": start.isoformat(),
                "to_date": end.isoformat(),
            }

        requested_set = {_norm(symbol) for symbol in requested_symbols}
        raw_by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in requested_symbols}
        source_errors: dict[str, str] = {}
        symbol_stats: list[dict[str, Any]] = []
        all_rows: list[dict[str, Any]] = []
        raw_filings_by_key: dict[str, dict[str, Any]] = {}
        window_stats: list[dict[str, Any]] = []
        total_scanned = 0

        async with NSEApiClient(retries=self.request_retries) as client:
            for window_start, window_end in _date_windows(start, end, self.document_window_days):
                window_started = time.monotonic()
                window_raw_filings: list[dict[str, Any]] = []
                abort_after_window = False
                try:
                    payload = await client.get_pit_disclosures(
                        window_start.strftime("%d-%m-%Y"),
                        window_end.strftime("%d-%m-%Y"),
                    )
                    if not _has_valid_pit_payload_shape(payload):
                        raise NSEApiError(
                            "Unexpected NSE PIT payload schema; refusing to treat it as an empty filing window"
                        )
                    window_rows = _extract_rows(payload)
                    if not window_rows:
                        top_keys = list(payload.keys())[:30] if isinstance(payload, dict) else [type(payload).__name__]
                        logger.warning(
                            "NSE_PIT_WINDOW_ZERO_ROWS from=%s to=%s top_level_keys=%s payload_shape=%s",
                            window_start, window_end, top_keys,
                            ("list:" + str(len(payload))) if isinstance(payload, list) else "dict",
                        )
                    payload_hash = hashlib.sha256(
                        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
                    ).hexdigest()
                    total_scanned += len(window_rows)
                    matched_symbols: set[str] = set()
                    for raw in window_rows:
                        raw_symbol = _clean(_pick(raw, "symbol", "SYMBOL"))
                        # Preserve every raw source row, even if NSE omitted its
                        # symbol. Rows without a symbol remain auditable but cannot
                        # be attached to the local company master or normalized.
                        symbol_key = raw_symbol.upper() if raw_symbol else "UNKNOWN"
                        detail_url = _find_detail_url(dict(raw))
                        filing_key = _raw_filing_key(symbol_key, dict(raw), detail_url)
                        filing_date = _parse_datetime(_pick(raw, "date", "broadcastDate", "broadcast_at", "intimDt"))
                        raw_filing = {
                            "source": "NSE_PIT",
                            "source_key": filing_key,
                            "source_filing_id": _raw_filing_identifier(dict(raw)),
                            "nse_symbol": raw_symbol.upper() if raw_symbol else None,
                            "company_name": _clean(_pick(raw, "company", "companyName", "COMPANY")) or (symbol_key if raw_symbol else None),
                            "filing_date": filing_date.isoformat() if filing_date else None,
                            "source_url": detail_url,
                            "raw_payload": dict(raw),
                            "window_start": window_start.isoformat(),
                            "window_end": window_end.isoformat(),
                        }
                        raw_filings_by_key[filing_key] = raw_filing
                        window_raw_filings.append(raw_filing)
                        if not raw_symbol:
                            continue
                        matched_symbols.add(symbol_key)
                        if _norm(symbol_key) not in requested_set:
                            # Preserve raw rows outside the local company master for
                            # audit, but only normalize master-approved NSE symbols.
                            continue
                        raw_by_symbol.setdefault(symbol_key, []).append(dict(raw))
                    logger.info(
                        "NSE PIT bulk window complete from=%s to=%s rows=%s matched_symbols=%s",
                        window_start.isoformat(), window_end.isoformat(), len(window_rows),
                        len(matched_symbols),
                    )
                    # A valid HTTP-200 payload containing no filing rows is an
                    # EMPTY observation, not a successful ingestion. This matters
                    # because this callback persists the run record immediately,
                    # before the collector knows whether every requested window is
                    # empty. Keep that distinction visible in the audit table.
                    window_stats.append({
                        "source": "NSE_PIT", "from_date": window_start.isoformat(),
                        "to_date": window_end.isoformat(),
                        "status": "SUCCESS" if window_rows else "EMPTY",
                        "http_status": 200,
                        "records_received": len(window_rows), "content_hash": payload_hash,
                        "duration_ms": int((time.monotonic() - window_started) * 1000),
                        "error": None,
                    })
                except NSEApiError as exc:
                    message = f"{window_start.isoformat()}..{window_end.isoformat()}: {type(exc).__name__}: {exc}"
                    source_errors["NSE PIT bulk"] = message
                    window_stats.append({
                        "source": "NSE_PIT", "from_date": window_start.isoformat(),
                        "to_date": window_end.isoformat(), "status": "FAILED", "http_status": exc.status_code,
                        "records_received": 0, "content_hash": None,
                        "duration_ms": int((time.monotonic() - window_started) * 1000),
                        "error": message,
                    })
                    logger.error("NSE PIT bulk discovery failed: %s", message)
                    # A 401/403 trips the NSE client's circuit breaker. Do not
                    # fan out to per-symbol retries after that point; preserve the
                    # last successful DB/Redis snapshot and let the next worker
                    # cycle retry after cooldown.
                    if exc.status_code in {401, 403} or "temporarily blocked" in str(exc).lower():
                        abort_after_window = True
                except Exception as exc:
                    source_errors["NSE PIT bulk"] = (
                        f"{window_start.isoformat()}..{window_end.isoformat()}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    window_stats.append({
                        "source": "NSE_PIT", "from_date": window_start.isoformat(),
                        "to_date": window_end.isoformat(), "status": "FAILED",
                        "records_received": 0, "content_hash": None,
                        "duration_ms": int((time.monotonic() - window_started) * 1000),
                        "error": source_errors["NSE PIT bulk"],
                    })
                    logger.exception("NSE PIT bulk discovery failed unexpectedly")
                    abort_after_window = True

                # Persist every completed API window before expensive detail parsing
                # begins. If a later symbol/document fails or the worker is restarted,
                # the raw NSE response and audit status are still durable.
                if on_raw_window:
                    await on_raw_window(window_raw_filings, window_stats[-1])
                if abort_after_window:
                    break

            # An HTTP-200/valid-JSON response with zero rows is not enough evidence
            # to mark the Insider Desk successfully reconciled. It can indicate a
            # source query/response-contract problem, and the previous stored data
            # must be preserved. Do not emit 2,793 meaningless per-symbol zero logs.
            if not source_errors and total_scanned == 0:
                empty_message = (
                    "NSE PIT returned a valid JSON structure but zero filing rows "
                    f"for the entire requested window {start.isoformat()}..{end.isoformat()}; "
                    "sync left incomplete for verification"
                )
                source_errors["NSE PIT empty response"] = empty_message
                for stat in window_stats:
                    stat["status"] = "EMPTY_UNVERIFIED"
                    stat["error"] = empty_message
                logger.warning("NSE_PIT_EMPTY_RESPONSE_UNVERIFIED from=%s to=%s windows=%s", start, end, len(window_stats))
                return {
                    "rows": [], "source_errors": source_errors, "complete": False,
                    "scanned": 0, "deduped": 0, "regulation_7_2": 0,
                    "xml_errors": 0, "detail_errors": 0, "details_ok": 0,
                    "symbol_stats": [], "raw_filings": [], "window_stats": window_stats,
                    "from_date": start.isoformat(), "to_date": end.isoformat(),
                    "symbols_requested": requested_symbols, "symbols_completed": 0,
                    "transport": "nse-public-api-bulk",
                }

            # No bulk discovery errors: process only the symbols present in the
            # local company master. NSE is queried only through the exchange-wide
            # feed above; symbol processing below is local parsing/grouping. Persist
            # in bounded batches instead of committing once per symbol (the universe
            # can contain ~2,800 symbols, which otherwise causes thousands of commits).
            persist_batch_size = 50
            pending_batch: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
            pending_batch_start = 0
            if not source_errors:
                for index, symbol in enumerate(requested_symbols):
                    rows, stats = await self._process_symbol_rows(
                        client,
                        symbol,
                        raw_by_symbol.get(symbol, []),
                        start,
                        end,
                    )
                    symbol_stats.append(stats)
                    all_rows.extend(rows)
                    pending_batch.append((symbol, rows, stats))
                    is_last = index == len(requested_symbols) - 1
                    if on_batch and (len(pending_batch) >= persist_batch_size or is_last):
                        await on_batch(pending_batch, pending_batch_start, len(requested_symbols))
                        pending_batch_start = index + 1
                        pending_batch = []
            else:
                # A failed bulk feed is not evidence that every company had zero
                # disclosures. Do not run per-symbol callbacks or stale-row cleanup;
                # retain prior normalized rows and retry this source window later.
                bulk_error = source_errors.get("NSE PIT bulk", "NSE PIT bulk feed failed")
                symbol_stats = [{
                    "symbol": "NSE_PIT_BULK",
                    "raw_rows": total_scanned,
                    "unique_filings": 0,
                    "normalized_transactions": 0,
                    "details_ok": 0,
                    "details_errors": 0,
                    "pages": len(window_stats),
                    "errors": [bulk_error],
                    "transport": "nse-public-api-bulk",
                    "api_windows": len(window_stats),
                }]
                logger.warning(
                    "NSE_PIT_SYMBOL_PROCESSING_SKIPPED reason=bulk_feed_failure requested_symbols=%s errors=%s",
                    len(requested_symbols), list(source_errors),
                )

        final_by_key = {
            str(row.get("source_key") or hashlib.sha256(repr(row).encode()).hexdigest()): row
            for row in all_rows
        }
        rows = list(final_by_key.values())
        rows.sort(
            key=lambda row: (
                row.get("transaction_date") or datetime.min.replace(tzinfo=NSE_TIMEZONE),
                row.get("nse_symbol") or "",
            ),
            reverse=True,
        )
        detail_errors = sum(int(item.get("details_errors") or 0) for item in symbol_stats)
        details_ok = sum(int(item.get("details_ok") or 0) for item in symbol_stats)
        complete = not source_errors and len(symbol_stats) == len(requested_symbols)
        logger.info(
            "NSE PIT bulk ingestion complete from=%s to=%s symbols=%s rows=%s details_ok=%s detail_errors=%s errors=%s elapsed=%.1fs",
            start.isoformat(), end.isoformat(), len(requested_symbols), len(rows),
            details_ok, detail_errors, list(source_errors), time.monotonic() - started,
        )
        return {
            "rows": rows,
            "source_errors": source_errors,
            "complete": complete,
            "scanned": total_scanned,
            "deduped": len(rows),
            "regulation_7_2": len(rows),
            "xml_errors": 0,
            "detail_errors": detail_errors,
            "details_ok": details_ok,
            "symbol_stats": symbol_stats,
            "raw_filings": list(raw_filings_by_key.values()),
            "window_stats": window_stats,
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "symbols_requested": requested_symbols,
            "symbols_completed": len(requested_symbols) if complete else 0,
            "transport": "nse-public-api-bulk",
        }
