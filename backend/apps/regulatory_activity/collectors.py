from __future__ import annotations

"""NSE regulatory collectors implemented using HTTP, HTML, XML/iXBRL and CSV.

No browser automation, PDF parsing, OCR or fixed wall-clock collection deadline is
used. Sources are fetched concurrently through one cookie-aware HTTP session.
"""

import asyncio
import csv
import html as html_lib
import io
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from apps.regulatory_activity.normalization import (
    clean_text, norm, normalize_buy_sell, normalize_issue_action,
    parse_datetime, parse_number, normalized_event,
)

logger = logging.getLogger(__name__)

NSE = "https://www.nseindia.com"
INSIDER_URL = f"{NSE}/companies-listing/corporate-filings-insider-trading"
INSIDER_ARCHIVE_URL = f"{NSE}/companies-listing/corporate-filings-insider-trading-archive-data"
QIP_URL = f"{NSE}/companies-listing/corporate-filings-QIP"
PREF_URL = f"{NSE}/companies-listing/corporate-filings-PREF"
DEALS_PAGE_URL = f"{NSE}/report-detail/display-bulk-and-block-deals"
DEALS_API = f"{NSE}/api/historicalOR/bulk-block-short-deals"
# Current NSE corporate filing JSON feeds. The older /api/corporates-pit
# endpoint does not back the Insider Trading page's historical PIT table.
PIT_API = f"{NSE}/api/corporates-pit-gg"
QIP_API = f"{NSE}/api/corporate-further-issues-qip"
PREF_API = f"{NSE}/api/corporate-further-issues-pref"
ISSUE_API_STAGES = {
    "QIP": {"In-Principle": "FIQIPIP", "Listing Stage": "FIQIPLS"},
    "PREFERENTIAL_WARRANT": {"In-Principle": "FIPREFIP", "Listing Stage": "FIPREFLS"},
}
INSIDER_RSS_URL = "https://nsearchives.nseindia.com/content/RSS/InsiderTrading.xml"
NSE_HOSTS = {
    "www.nseindia.com", "nseindia.com", "nsearchives.nseindia.com", "archives.nseindia.com",
    # NSE's current Insider Trading detail links are served from the .in host
    # family (for example nsearchives.nseindia.in/corporate/ixbrl/*.html).
    "www.nseindia.in", "nseindia.in", "nsearchives.nseindia.in", "archives.nseindia.in",
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-IN,en;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
}


class NSEHttpSession:
    """Shared NSE HTTP session with no read/connect timeout and bounded concurrency."""

    def __init__(self, *, concurrency: int = 16, retries: int = 5) -> None:
        self._client = httpx.AsyncClient(
            timeout=None,  # deliberately no time boundary for slow NSE pages/filings
            limits=httpx.Limits(max_connections=max(8, concurrency * 2), max_keepalive_connections=max(4, concurrency)),
            follow_redirects=True,
            headers=HTTP_HEADERS,
        )
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self.retries = max(1, retries)
        self._closed = False
        self._primed_hosts: set[str] = set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme not in {"http", "https"} or not (
            host in NSE_HOSTS or host.endswith(".nseindia.com")
            or host.endswith(".nsearchives.nseindia.com") or host.endswith(".nseindia.in")
        ):
            raise ValueError(f"Refusing non-NSE source URL: {url}")
        if parsed.path.casefold().endswith(".pdf"):
            raise ValueError("PDF documents are not supported by the regulatory scraper; only HTML/XML/CSV/JSON is fetched")
        return url

    async def _prime(self, url: str) -> None:
        host = (urlparse(url).hostname or "").casefold()
        if host in self._primed_hosts:
            return
        self._primed_hosts.add(host)
        # Session priming is an ordinary HTTP GET, never a browser navigation.
        try:
            async with self._semaphore:
                await self._client.get(f"https://{host}/")
        except httpx.HTTPError as exc:
            logger.debug("NSE_HTTP_SESSION_PRIME_INCOMPLETE host=%s error=%s", host, str(exc)[:180])

    async def get_bytes(self, url: str, *, referer: str | None = None, accept: str = "*/*") -> tuple[bytes, str]:
        url = self._validate_url(url)
        headers = {"Accept": accept}
        if referer:
            headers["Referer"] = referer
        last_error: Exception | None = None
        reauth_attempted = False
        for attempt in range(self.retries):
            try:
                async with self._semaphore:
                    response = await self._client.get(url, headers=headers)
                if response.status_code in {401, 403} and not reauth_attempted:
                    reauth_attempted = True
                    await self._prime(url)
                    continue
                if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                    if attempt + 1 < self.retries:
                        await asyncio.sleep(min(20.0, 1.5 ** attempt))
                        continue
                response.raise_for_status()
                return response.content, response.headers.get("content-type", "")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # There is no configured request timeout; this handles connection
                # resets/DNS/socket errors, not elapsed-time cut-offs.
                last_error = exc
                if attempt + 1 < self.retries:
                    await asyncio.sleep(min(20.0, 1.5 ** attempt))
                    continue
                raise
        if last_error:
            raise last_error
        raise RuntimeError(f"NSE request failed after {self.retries} attempts: {url}")

    async def get_text(self, url: str, *, referer: str | None = None, accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8") -> str:
        payload, _content_type = await self.get_bytes(url, referer=referer, accept=accept)
        return payload.decode("utf-8-sig", errors="replace")


# Compatibility alias for the previous collector tests/callers; implementation is HTTP-only.
NSEApiClient = NSEHttpSession


def _date_query(start: date, end: date) -> str:
    return f"from_date={start:%d-%m-%Y}&to_date={end:%d-%m-%Y}"


def _with_query(url: str, **params: Any) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        query[key] = [str(value)]
    encoded = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=encoded))


def _date_window_url(url: str, start: date, end: date) -> str:
    return _with_query(url, from_date=start.strftime("%d-%m-%Y"), to_date=end.strftime("%d-%m-%Y"))

def _valid_nse_url(value: Any, base: str = NSE) -> str | None:
    raw = clean_text(value)
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    raw = urljoin(base, raw)
    parsed = urlparse(raw)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not (
        host in NSE_HOSTS or host.endswith(".nseindia.com")
        or host.endswith(".nsearchives.nseindia.com") or host.endswith(".nseindia.in")
    ):
        return None
    return raw


def _url_from_link_handler(handler: Any, base_url: str) -> str | None:
    """Recover an explicit NSE URL embedded in an anchor's JavaScript handler."""
    source = clean_text(handler)
    if not source:
        return None
    candidates: list[str] = []
    # Common patterns: window.open('/path'), openPopup("https://..."),
    # or a quoted URL passed to an NSE modal helper. Do not execute the handler.
    for match in re.finditer(r"['\"]((?:https?://|//|/)[^'\"]+)['\"]", source, re.I):
        candidate = match.group(1).strip()
        candidate = candidate.replace('\\/', '/')
        normalized = _valid_nse_url(candidate, base_url)
        if normalized:
            parsed = urlparse(normalized)
            base = urlparse(base_url)
            if parsed.path != base.path or parsed.query != base.query:
                candidates.append(normalized)
    return candidates[0] if candidates else None


def _date_query(start: date, end: date) -> str:
    return f"from_date={start:%d-%m-%Y}&to_date={end:%d-%m-%Y}"


def _table_payloads(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "html.parser")
    tables: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        headers = [clean_text(cell.get_text(" ", strip=True)) or "" for cell in table.find_all("th")]
        rows = []
        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if not cells:
                continue
            values = [clean_text(cell.get_text(" ", strip=True)) or "" for cell in cells]
            links = []
            for anchor in tr.find_all("a"):
                links.append({
                    "text": clean_text(anchor.get_text(" ", strip=True)) or clean_text(anchor.get("title")) or "",
                    "href": _valid_nse_url(anchor.get("href"), NSE),
                    "title": clean_text(anchor.get("title")),
                    "aria": clean_text(anchor.get("aria-label")),
                })
            if any(values):
                rows.append({"cells": values, "links": links})
        if rows:
            tables.append({"headers": headers, "rows": rows})
    return tables


def _expand_table_grid(table) -> tuple[list[list[str]], list[bool], list[bool]]:
    """Expand HTML table rowspan/colspan cells into an aligned text grid.

    NSE iXBRL disclosures use multi-level headers: a grouped header such as
    ``Securities acquired / disposed`` spans several columns, followed by a
    second row containing ``No. of security``, ``Value of security`` and
    ``Transaction type``. The first row can also be a lone ``Sr. No.`` cell.
    Expanding spans lets the parser reconstruct the full header path per value.
    """
    grid: list[list[str]] = []
    has_header: list[bool] = []
    has_data: list[bool] = []
    active_spans: dict[int, tuple[int, str]] = {}

    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        slots: dict[int, str] = {}
        next_spans: dict[int, tuple[int, str]] = {}
        for column, (remaining, value) in active_spans.items():
            slots[column] = value
            if remaining > 1:
                next_spans[column] = (remaining - 1, value)

        column = 0
        for cell in cells:
            while column in slots:
                column += 1
            value = clean_text(cell.get_text(" ", strip=True)) or ""
            try:
                colspan = max(1, min(80, int(cell.get("colspan", 1) or 1)))
            except (TypeError, ValueError):
                colspan = 1
            try:
                rowspan = max(1, min(80, int(cell.get("rowspan", 1) or 1)))
            except (TypeError, ValueError):
                rowspan = 1
            for offset in range(colspan):
                target = column + offset
                slots[target] = value
                if rowspan > 1:
                    next_spans[target] = (rowspan - 1, value)
            column += colspan

        if slots:
            width = max(slots) + 1
            grid.append([slots.get(idx, "") for idx in range(width)])
            has_header.append(any(cell.name == "th" for cell in cells))
            has_data.append(any(cell.name == "td" for cell in cells))
        active_spans = next_spans

    return grid, has_header, has_data


def _header_score(row: list[str]) -> int:
    value = norm(" ".join(row))
    markers = (
        "name of the person", "category of person", "type of instrument",
        "securities held prior", "securities acquired", "securities disposed",
        "securities held post", "transaction type", "date of allotment",
        "mode of acquisition", "exchange on which", "number of securities",
        "date of allotment advice", "acquisition of shares", "sale of shares",
    )
    return sum(1 for marker in markers if marker in value)


def _humanize_taxonomy_name(value: str) -> str:
    value = (value or "").split(":")[-1].replace("_", " ").replace("-", " ")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    value = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", value)
    return clean_text(value) or ""


def _facts_from_xml(xml_text: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Read XBRL/XML facts by local tag name and contextRef; no PDF/OCR path."""
    soup = BeautifulSoup(xml_text or "", "xml")
    facts: dict[str, Any] = {}
    contexts: dict[str, dict[str, Any]] = {}
    body_parts: list[str] = []
    for node in soup.find_all(True):
        raw_name = node.name or ""
        local_name = raw_name.split(":")[-1]
        # Context and unit nodes are structure, not disclosure facts.
        if local_name.casefold() in {"context", "period", "entity", "identifier", "unit", "measure", "scenario", "segment"}:
            continue
        if node.find(True) is not None:
            continue
        value = clean_text(node.get_text(" ", strip=True))
        if not value:
            continue
        label = _humanize_taxonomy_name(local_name)
        if not label:
            continue
        facts.setdefault(label, value)
        context_ref = node.get("contextref") or node.get("contextRef")
        if context_ref:
            contexts.setdefault(str(context_ref), {})[label] = value
        body_parts.append(f"{label}: {value}")

    rows: list[dict[str, Any]] = []
    for context_ref, values in contexts.items():
        if len(values) >= 2:
            rows.append({**values, "_context_ref": context_ref})
    body = clean_text("; ".join(body_parts)) or ""
    return facts, rows, body


def _facts_from_html(html: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Extract NSE iXBRL filing facts and transaction rows.

    Handles both key/value filing metadata and multi-level PIT disclosure grids.
    In particular, it does not assume the first row of a transaction table is
    its header (NSE commonly puts ``Sr. No.`` on a separate leading row).
    """
    document = html or ""
    head = document.lstrip()[:300].casefold()
    if head.startswith("<?xml") or ("<xbrl" in head and "<html" not in head):
        return _facts_from_xml(document)
    soup = BeautifulSoup(document, "html.parser")
    facts: dict[str, Any] = {}
    data_tables: list[dict[str, Any]] = []
    header_markers = (
        "allottee", "security", "share", "person", "investor", "holding",
        "acquirer", "transaction", "quantity", "securities", "promoter",
    )

    for table in soup.find_all("table"):
        grid, header_flags, data_flags = _expand_table_grid(table)
        if not grid:
            continue

        # General-information tables are predominantly label/value pairs. Only
        # take direct table cells so nested tables cannot corrupt the mapping.
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            if len(cells) not in (2, 3):
                continue
            values = [clean_text(cell.get_text(" ", strip=True)) for cell in cells]
            values = [value for value in values if value]
            if len(values) not in (2, 3):
                continue
            label = values[0]
            joined_value = " | ".join(values[1:])
            label_norm = norm(label)
            if (
                label and joined_value
                and label_norm not in {"symbol", "company name", "sr no", "sr number"}
                and not re.fullmatch(r"\d+", label_norm)
            ):
                facts[label] = joined_value

        # Find the real header row by content rather than assuming row zero.
        # Include preceding and following <th> rows so grouped/leaf headings
        # are combined into a single, searchable column name.
        search_limit = min(len(grid), 9)
        scores = [(_header_score(grid[idx]), idx) for idx in range(search_limit)]
        best_score, best_index = max(scores, key=lambda item: item[0], default=(0, -1))
        joined_header = norm(" ".join(" ".join(row) for row in grid[:search_limit]))
        looks_like_pit = any(marker in joined_header for marker in header_markers)
        table_width = max((len(row) for row in grid[:search_limit]), default=0)
        # Two-column label/value tables are filing facts, not transaction/allottee
        # grids. Requiring a wider row avoids misreading labels as column headings.
        if best_score < 2 and not (looks_like_pit and best_index >= 0 and table_width >= 3):
            continue

        # NSE's PIT table can split the heading across multiple rows and use
        # rowspan/colspan. Treat all leading header rows before the first data
        # row as header paths, including a preceding standalone Sr. No. row.
        first_data_index = None
        for idx in range(max(0, best_index + 1), len(grid)):
            if data_flags[idx] and len([v for v in grid[idx] if v]) >= 2:
                first_data_index = idx
                break
        if first_data_index is None:
            # Some HTML variants use <td> for headers; select the best-scoring
            # row and start after it, retaining one subsequent unit/subheader row.
            first_data_index = min(len(grid), best_index + 2)

        header_rows = grid[:first_data_index]
        width = max((len(row) for row in header_rows), default=0)
        if width < 2:
            continue
        headers: list[str] = []
        for col in range(width):
            path_values: list[str] = []
            for header_row in header_rows:
                value = clean_text(header_row[col]) if col < len(header_row) else None
                if value and (not path_values or norm(value) != norm(path_values[-1])):
                    path_values.append(value)
            # A grouped label can be repeated over every spanned cell. A
            # column path such as ``Securities acquired / disposed / No. of
            # security`` remains unique and can be searched by label token.
            headers.append(" / ".join(path_values) if path_values else f"column_{col + 1}")

        for row_index in range(first_data_index, len(grid)):
            values = grid[row_index]
            nonempty = [clean_text(value) for value in values if clean_text(value)]
            if len(nonempty) < 2:
                continue
            # Ignore a repeated header that NSE can insert after pagination.
            row_norm = norm(" ".join(nonempty))
            if _header_score(nonempty) >= 2 and any(word in row_norm for word in ("name of the person", "transaction type", "type of instrument")):
                continue
            record: dict[str, Any] = {}
            for col, value in enumerate(values):
                value = clean_text(value)
                if not value:
                    continue
                label = headers[col] if col < len(headers) else f"column_{col + 1}"
                if norm(label) in {"sr no", "sr number", "serial number"}:
                    record[label] = value
                elif label:
                    record[label] = value
            if len(record) >= 2:
                data_tables.append(record)

    body_text = clean_text(soup.get_text(" ", strip=True)) or ""
    # Inline XBRL facts supplement the visible tables.  The NSE PIT template
    # frequently encodes the transaction grid as ix:nonFraction/ix:nonNumeric
    # facts grouped by contextRef rather than ordinary HTML <tr> elements.
    # Collect both the flat filing facts and one row per context so the normal
    # Insider parser can process those disclosures just like a visible table.
    ixbrl_contexts: dict[str, dict[str, Any]] = {}
    for node in soup.find_all(lambda tag: tag.name and (
        tag.name.startswith("ix:") or tag.name in {"nonfraction", "nonnumeric"}
    )):
        raw_name = clean_text(node.get("name"))
        name = _humanize_taxonomy_name(raw_name or node.get("contextref") or node.name)
        value = clean_text(node.get_text(" ", strip=True))
        if not name or not value:
            continue
        # iXBRL numeric facts can be expressed in scaled units (e.g. scale=3
        # means the rendered value is thousands).  Keep the display fact in
        # details_json and apply the scale only to the context fact used for
        # numeric extraction.
        fact_value: Any = value
        local_tag = (node.name or "").split(":")[-1].casefold()
        context_value: Any = value
        if local_tag == "nonfraction":
            numeric = parse_number(value)
            scale = node.get("scale")
            if numeric is not None:
                try:
                    if scale not in (None, ""):
                        numeric *= 10 ** int(scale)
                    if str(node.get("sign") or "").strip() == "-":
                        numeric *= -1
                    context_value = numeric
                except (TypeError, ValueError, OverflowError):
                    context_value = value
        facts.setdefault(name, fact_value)
        context_ref = clean_text(node.get("contextref") or node.get("contextRef"))
        if context_ref:
            ixbrl_contexts.setdefault(context_ref, {})[name] = context_value

    # A context may describe issuer-level data, a single transaction or a
    # single allottee. Only add useful contexts; retain existing parsed HTML
    # table rows first so we do not duplicate the same visible transaction.
    for context_ref, values in ixbrl_contexts.items():
        if len(values) < 2:
            continue
        row = {**values, "_context_ref": context_ref}
        row_identity = tuple(sorted((norm(key), norm(value)) for key, value in values.items()))
        if not any(
            tuple(sorted((norm(key), norm(value)) for key, value in existing.items() if not str(key).startswith("_"))) == row_identity
            for existing in data_tables
        ):
            data_tables.append(row)
    return facts, data_tables, body_text

def _get_field_entry(mapping: dict[str, Any], *terms: str) -> tuple[str, Any] | None:
    """Return a source field using alias priority without broad matches winning.

    NSE's listing-stage XBRL documents often contain both ``Minimum Issue
    Price`` and ``Issue Price``.  A substring-only lookup for ``issue price``
    could select the minimum price simply because it appeared first in the
    document.  Exact aliases are therefore checked across the whole alias list
    before any contains-match is considered.
    """
    indexed = [
        (str(key), norm(key), value)
        for key, value in mapping.items()
        if clean_text(value)
    ]

    # Prefer exact label matches, in the order supplied by the caller.
    for term in terms:
        wanted = norm(term)
        if not wanted:
            continue
        for original, actual, value in indexed:
            if wanted == actual:
                return original, value

    # Fallback for labels with unit suffixes or extra NSE wording. For each
    # alias, prefer the shortest containing label to avoid choosing a broader
    # field when a more specific field is present.
    for term in terms:
        wanted = norm(term)
        if not wanted:
            continue
        matches = [item for item in indexed if wanted in item[1]]
        if matches:
            match = min(matches, key=lambda item: len(item[1]))
            return match[0], match[2]
    return None


def _get_field(mapping: dict[str, Any], *terms: str) -> Any:
    """Return the first field matching the caller's alias priority."""
    found = _get_field_entry(mapping, *terms)
    return found[1] if found else None


def _issue_lookup_facts(
    facts: dict[str, Any], tables: list[dict[str, Any]],
) -> tuple[dict[str, Any], set[str]]:
    """Build issue facts while preferring scaled inline-XBRL context values.

    ``_facts_from_html`` retains readable fact text and also builds context rows
    with the numeric ``scale`` already applied.  The former implementation used
    ``setdefault`` for every context, so the unscaled flat value always won.
    Prefer the most informative context rows, then use visible tables and flat
    facts as fallback.
    """
    context_rows = [row for row in tables if isinstance(row, dict) and row.get("_context_ref")]
    value_alias_groups = (
        ("date of allotment", "allotment date", "date of issue", "issue date"),
        ("number of shares allotted", "total number of shares allotted", "no of equity securities", "no of warrants"),
        ("issue price", "offer price", "minimum issue price"),
        ("final issue size", "amount raised", "amount inr in crores", "consideration amount"),
        ("name of allottee", "category of allottee", "number of allottees"),
    )

    def score(row: dict[str, Any]) -> int:
        return sum(1 for aliases in value_alias_groups if _get_field(row, *aliases) is not None)

    context_rows.sort(key=score, reverse=True)
    context_values: dict[str, Any] = {}
    for row in context_rows:
        for key, value in row.items():
            if key != "_context_ref" and clean_text(value):
                context_values.setdefault(key, value)

    lookup = dict(facts)
    lookup.update(context_values)  # scaled iXBRL values override flat display values
    for row in tables:
        if row.get("_context_ref"):
            continue
        for key, value in row.items():
            if clean_text(value):
                lookup.setdefault(key, value)
    return lookup, {norm(key) for key in context_values}


def _normalize_issue_amount(value: Any, label: Any, *, already_scaled: bool = False) -> float | None:
    """Convert a disclosed issue amount to INR using the source field's unit."""
    amount = parse_number(value)
    if amount is None:
        return None
    if already_scaled:
        return amount
    value_text = norm(value)
    if any(unit in value_text for unit in ("lakh", "lakhs", "lac", "lacs", "crore", "crores")):
        # parse_number has already applied the unit embedded in the value.
        return amount
    label_text = norm(label)
    if "lakh" in label_text or "lac" in label_text:
        return amount * 100_000
    if "crore" in label_text:
        return amount * 10_000_000
    return amount


def _first_matching_text(*values: Any, markers: tuple[str, ...] = ()) -> str | None:
    for value in values:
        text = clean_text(value)
        if text and (not markers or any(marker in norm(text) for marker in markers)):
            return text
    return None


def _extract_allottee_records(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract separate allottee/investor rows without flattening repeated names."""
    records: list[dict[str, Any]] = []
    name_aliases = (
        "name of allottee", "allottee name", "name of investor", "investor name",
        "name of qib", "qib name", "name of subscriber", "subscriber name",
        "name of applicant", "applicant name", "name of allottee / investor",
        "name of investor / allottee", "allottee name / investor name",
        "allottee / investor", "allottee investor", "name of the allottee",
        "name of allottees", "name of the investor", "name of the subscriber",
        "name of institutional investor", "investor / allottee name",
    )
    for row in tables:
        name = _get_field(row, *name_aliases)
        name = clean_text(name)
        if not name or norm(name) in {"allottee", "name of allottee", "investor", "name of investor", "qib"}:
            continue
        quantity = _get_field(
            row, "number of securities allotted", "no of securities allotted",
            "securities allotted", "number of equity shares allotted", "no of equity shares allotted",
            "number of shares allotted", "no of shares allotted",
            "number of equity shares listed", "number of equity shares", "no of equity shares",
            "number of shares listed", "number of shares", "no of shares", "quantity allotted", "quantity",
            "number of warrants", "no of warrants", "securities to be allotted",
            "shares to be allotted", "number of shares subscribed", "number of securities subscribed",
            "number of equity shares listed", "number of securities issued",
            "no of equity shares listed", "total number of securities allotted",
        )
        price = _get_field(row, "issue price", "price per security", "price per share", "allotment price", "offer price", "offer price per security")
        value = _get_field(
            row, "consideration amount", "allotment value", "value of securities allotted",
            "value of securities", "investment amount", "subscription amount", "amount invested",
            "amount raised in lakhs", "amount raised", "total amount allotted", "amount",
        )
        records.append({"person": name, "quantity": quantity, "price": price, "value": value, "raw": row})
    unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for record in records:
        key = (
            norm(record.get("person")),
            norm(record.get("quantity")),
            norm(record.get("price")),
            norm(record.get("value")),
        )
        unique[key] = record
    return list(unique.values())


def _parse_issue_detail(
    *, category: str, row: dict[str, Any], popup_html: str, page_url: str,
    company_name: str, source_url: str | None = None,
    source_metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Parse an issue popup into aggregate or per-allottee normalized event rows."""
    facts, tables, body = _facts_from_html(popup_html)
    detail_facts, scaled_fact_keys = _issue_lookup_facts(facts, tables)
    # The paginated NSE listing JSON carries the same authoritative issuer
    # fields as the disclosure template (and sometimes carries fields omitted
    # from an older XBRL template). Retain those values as named facts so the
    # validation layer can distinguish a complete API+XBRL event from a sparse
    # HTML shell.
    listing_facts = {
        "Company Name": company_name,
        "Symbol": row.get("symbol"),
        "ISIN": row.get("isin"),
        "Date of Allotment of Shares": row.get("allotment_date"),
        "Board Resolution Date": row.get("board_resolution_date"),
        "Stage": row.get("stage"),
        "Number of Shares Allotted": row.get("quantity"),
        "Issue Price": row.get("price"),
        "Amount Raised": row.get("value"),
        "Type of Security": row.get("security_type"),
        "Number of Allottees": row.get("allottee_count"),
        "Category of Allottee": row.get("allottee_category"),
    }
    lookup_facts = dict(detail_facts)
    for label, value in listing_facts.items():
        if value not in (None, ""):
            lookup_facts.setdefault(label, value)
    body_norm = norm(body)
    fact_labels = norm(" ".join(str(key) for key in lookup_facts))
    issuer_markers = (
        "company details", "company name", "name of the company", "corporate identity number", "cin", "isin", "symbol",
        "date of submission", "date of shareholders approval",
    )
    issue_markers = (
        "object of the issue", "details of security", "issue size", "allottee",
        "issue price", "number of securities", "amount raised", "offer price",
        "date of allotment", "date of issue",
    )
    issuer_found = any(marker in body_norm or marker in fact_labels for marker in issuer_markers)
    issue_found = any(marker in body_norm or marker in fact_labels for marker in issue_markers)
    # Some NSE XBRL templates expose taxonomy labels that do not contain the
    # words used by the popup's visible labels.  The listing JSON is itself an
    # authoritative issuer-level disclosure, so accept a valid XML/HTML filing
    # when it contains facts and a matching listing row rather than rejecting
    # it solely because one of the legacy label markers is absent.
    is_structured_document = bool(popup_html and (len(facts) >= 2 or tables))
    has_listing_issuer = bool(clean_text(company_name) or clean_text(row.get("symbol")))
    has_issue_data = bool(
        issue_found
        or row.get("allotment_date")
        or row.get("quantity") is not None
        or row.get("price") is not None
        or row.get("value") is not None
        or any(marker in body_norm or marker in fact_labels for marker in ("allot", "issue", "security", "share", "warrant", "consideration"))
    )
    if not popup_html or not is_structured_document or not (issuer_found or has_listing_issuer) or not has_issue_data:
        return []

    stage = clean_text(row.get("stage") or _get_field(lookup_facts, "stage"))
    stage_action = normalize_issue_action(stage)
    if category == "QIP":
        activity_type, subtype = "QIP", None
        action_raw = _get_field(
            lookup_facts, "allotment status", "issue stage", "stage", "status",
            "type of issue", "nature of issue",
        )
        # Stage describes the lifecycle. Instrument labels such as "Equity"
        # must not turn an In-Principle QIP disclosure into an "Issued" event.
        action = stage_action or normalize_issue_action(action_raw, default="Issued")
    else:
        activity_type = "PREFERENTIAL_WARRANT"
        # Classify from the actual security/instrument disclosure, not any incidental
        # mention of warrants elsewhere in the popup narrative.
        security_type = _get_field(
            lookup_facts, "type of security", "details of security", "security type",
            "name of security", "instrument type", "nature of issue", "type of issue",
        ) or row.get("security_type") or ""
        instrument_values = [
            clean_text(value) for key, value in lookup_facts.items()
            if any(term in norm(key) for term in ("type of security", "details of security", "security type", "name of security", "instrument", "nature of issue", "type of issue"))
        ]
        explicit_instrument = norm(" ".join(value for value in instrument_values if value) or security_type)
        subtype = "WARRANT" if re.search(r"\bwarrants?\b", explicit_instrument) else "PREFERENTIAL"
        action_raw = _get_field(
            lookup_facts, "allotment status", "issue stage", "stage", "status",
            "type of issue", "nature of issue",
        )
        action = stage_action or normalize_issue_action(action_raw or "Preferential issue", default="Issued")

    # Event date is the actual allotment/issue date. Submission/filing date is
    # kept separately and is only used as an explicitly marked PARTIAL fallback.
    allotment_date_raw = _get_field(
        lookup_facts,
        "date of allotment", "allotment date", "date of allotment of shares",
        "date of allotment of securities", "final allotment date",
        "date on which shares were allotted", "date on which securities were allotted",
    ) or row.get("allotment_date")
    issue_date_raw = _get_field(
        lookup_facts, "date of issue", "issue date", "date of issuance", "date of allotment/issue",
    )
    row_date_raw = row.get("date")
    row_date_basis = row.get("date_basis")
    actual_date_raw = allotment_date_raw or issue_date_raw
    actual_date_basis = "allotment_date" if allotment_date_raw else "issue_date" if issue_date_raw else None
    if not actual_date_raw and row_date_basis in {"allotment_date", "issue_date"}:
        actual_date_raw = row_date_raw
        actual_date_basis = row_date_basis
    actual_date = parse_datetime(actual_date_raw)
    # Listing-stage disclosures with a concrete allotment date describe an
    # allotment, even if their stage label itself only says "Listing Stage".
    if actual_date:
        action = "Allotted"

    filing_date_raw = _get_field(lookup_facts, "date of filing", "filing date", "date of submission", "submission date")
    disclosure_date = (
        parse_datetime(filing_date_raw)
        or (parse_datetime(row_date_raw) if row_date_basis in {"filing_date", "submission_date"} else None)
        or parse_datetime(row.get("disclosure_date"))
    )
    board_resolution_raw = _get_field(
        lookup_facts,
        "board resolution date", "date of shareholders approval for placement with qibs",
        "date of shareholders approval", "board resolution approving the issue of specified securities",
    )
    board_resolution_date = (
        parse_datetime(board_resolution_raw)
        or (parse_datetime(row.get("board_resolution_date")) if row.get("board_resolution_date") else None)
        or (parse_datetime(row_date_raw) if row_date_basis == "board_resolution_date" else None)
    )
    if actual_date:
        event_date, event_date_basis = actual_date, actual_date_basis
    elif board_resolution_date:
        event_date, event_date_basis = board_resolution_date, "board_resolution_fallback"
    elif disclosure_date:
        event_date, event_date_basis = disclosure_date, "filing_date_fallback"
    else:
        event_date, event_date_basis = None, None
    if event_date is None:
        return []

    quantity_aliases = (
        # QIP post-allotment fields
        "no of shares allotted", "number of shares allotted", "total number of shares allotted",
        "total number of equity shares allotted", "total number of securities allotted",
        "number of equity shares allotted", "number of equity shares listed",
        # QIP in-principle fields (the security type determines the exact label)
        "no of equity securities", "number of equity securities", "tentative no of shares",
        "tentative number of shares", "no of non convertible debt securities",
        "number of non convertible debt securities", "no of convertible securities",
        "number of convertible securities", "no of warrants", "number of warrants",
        "no of units", "number of units", "number of securities allotted", "number of securities",
        "total quantity allotted", "quantity allotted", "total shares allotted",
        "issue size applied for no of shares", "issue size applied for number of shares",
        "number of securities applied for", "no of securities applied for",
        "security type no of securities", "number of securities proposed to be issued",
        "no of securities to be issued", "number of equity shares",
    )
    quantity_entry = (
        _get_field_entry(detail_facts, *quantity_aliases)
        or _get_field_entry(listing_facts, *quantity_aliases)
    )
    aggregate_qty = (quantity_entry[1] if quantity_entry else None) or row.get("quantity")

    price_aliases = (
        # Final listing-stage price must win over the in-principle minimum.
        "issue price per share", "issue price per security", "offer price per security",
        "offer price per share", "offer price", "allotment price", "price per security",
        "price per share", "issue price",
        # In-principle documents disclose only a minimum price.
        "minimum issue price per security", "minimum issue price", "min issue price per security",
        "min issue price", "minimum issue price per share",
    )
    price_entry = (
        _get_field_entry(detail_facts, *price_aliases)
        or _get_field_entry(listing_facts, *price_aliases)
    )
    aggregate_price = (price_entry[1] if price_entry else None) or row.get("price")

    value_aliases = (
        # QIP listing-stage and pre-allotment values
        "final issue size", "final amount of issue size", "total issue size",
        "estimated total issue amount", "amount in inr crores", "amount inr in crores",
        "amount in crores", "issue size applied for rs in crores",
        "issue size applied for in crores", "security type amount to be raised",
        "amount to be raised", "amount raised",
        # Preferential listing-stage value (NSE specifies Rs. in Lakhs)
        "amount raised rs in lakhs", "amount raised in lakhs", "total amount raised",
        "total consideration amount", "total consideration", "consideration amount",
        "amount of issue", "amount of securities allotted", "total issue consideration",
    )
    value_entry = (
        _get_field_entry(detail_facts, *value_aliases)
        or _get_field_entry(listing_facts, *value_aliases)
    )
    aggregate_value_raw = (value_entry[1] if value_entry else None) or row.get("value")
    value_label = value_entry[0] if value_entry else ""
    aggregate_value = _normalize_issue_amount(
        aggregate_value_raw, value_label,
        # Listing API candidates are normalized to INR before they reach this
        # parser. Only detail-document facts need the XBRL scale/unit check.
        already_scaled=bool(value_entry and norm(value_entry[0]) in scaled_fact_keys),
    ) if aggregate_value_raw is not None else None

    disclosed_person = _get_field(
        detail_facts,
        "name of allottee", "allottee name", "name of investor", "investor name",
        "qib name", "name of qib", "name of subscriber", "category of allottee",
        "allottee category", "category of investor",
    ) or _get_field(
        listing_facts, "name of allottee", "allottee name", "name of investor",
        "investor name", "category of allottee", "allottee category", "category of investor",
    ) or row.get("allottee_category")
    allottee_rows = _extract_allottee_records(tables)
    allottee_count = (
        _get_field(detail_facts, "number of allottees", "no of allottees", "allottee count")
        or _get_field(listing_facts, "number of allottees", "no of allottees", "allottee count")
        or row.get("allottee_count")
    )
    disclosed_allottee_names: list[str] = []
    if not allottee_rows and disclosed_person:
        disclosed_allottee_names = [clean_text(disclosed_person)]
        parsed_count = parse_number(allottee_count)
        if parsed_count is not None and parsed_count > 1:
            # A flat XBRL fact can expose only the first allottee's name even
            # when the issuer-level listing says several investors received
            # securities. Do not attribute the aggregate issue size to that
            # one investor; label it as an aggregate and retain the name below.
            disclosed_person = f"Multiple allottees ({int(parsed_count)})"
        else:
            allottee_rows = [{"person": clean_text(disclosed_person), "quantity": None, "price": None, "value": None, "raw": {}}]
    if not allottee_rows and not disclosed_person and allottee_count:
        disclosed_person = f"{clean_text(allottee_count)} allottees"

    # If the popup gives multiple named allottees, make one normalized event per
    # named row. Never repeat the issuer's aggregate quantity/value across those
    # rows: that would overstate the issue size.
    allocations: list[dict[str, Any]]
    allottee_rows_have_allocation_values = any(
        parse_number(record.get(field)) is not None
        for record in allottee_rows for field in ("quantity", "price", "value")
    )
    if len(allottee_rows) > 1 and not allottee_rows_have_allocation_values and any(
        parse_number(value) is not None for value in (aggregate_qty, aggregate_price, aggregate_value)
    ):
        # Some XBRL templates disclose the allottee names in a context table,
        # but put security quantity/price/consideration only at issuer level.
        # Do not repeat issuer totals for every investor. Emit one aggregate
        # event and keep every disclosed allottee as a structured detail list.
        allocations = [{
            "person": f"Multiple allottees ({len(allottee_rows)})",
            "quantity": aggregate_qty,
            "price": aggregate_price,
            "value": aggregate_value,
            "allottee_row": {},
            "allottees": allottee_rows,
        }]
    elif allottee_rows:
        allocations = []
        for allocation in allottee_rows:
            row_qty = allocation.get("quantity")
            row_price = allocation.get("price")
            row_value = allocation.get("value")
            single_allottee = len(allottee_rows) == 1
            allocations.append({
                "person": allocation.get("person"),
                "quantity": row_qty if parse_number(row_qty) is not None else (aggregate_qty if single_allottee else None),
                "price": row_price if parse_number(row_price) is not None else (aggregate_price if single_allottee else None),
                "value": row_value if parse_number(row_value) is not None else (aggregate_value if single_allottee else None),
                "allottee_row": allocation.get("raw") or {},
            })
    else:
        allocations = [{"person": disclosed_person, "quantity": aggregate_qty, "price": aggregate_price, "value": aggregate_value, "allottee_row": {}}]

    symbol = clean_text(row.get("symbol")) or _get_field(lookup_facts, "symbol")
    isin = _get_field(lookup_facts, "isin") or row.get("isin")
    url = source_url or _valid_nse_url(row.get("company_link"), page_url) or page_url
    metadata_base = {
        "collector": "NSE company-name popup",
        "listing_page": page_url,
        "event_date_basis": event_date_basis,
        "allotment_date": allotment_date_raw,
        "issue_date": issue_date_raw,
        "filing_date": filing_date_raw,
        "listing_date_label": row.get("date_header"),
        "listing_date_value": row_date_raw,
        **(source_metadata or {}),
    }
    events: list[dict[str, Any]] = []
    for allocation_index, allocation in enumerate(allocations, start=1):
        quantity = allocation.get("quantity")
        price = allocation.get("price")
        value = allocation.get("value")
        person = allocation.get("person")
        populated = sum(value is not None for value in (person, action, quantity, price, value))
        quantity_number = parse_number(quantity)
        price_number = parse_number(price)
        value_number = parse_number(value)
        reported_value_number = value_number
        value_basis = "exchange_disclosed" if value_number is not None else None
        price_basis = "exchange_disclosed" if price_number is not None else None

        # For an actual allotment, quantity × issue/offer price is the most
        # reliable INR total. NSE's PREF value is in Rs. Lakhs, while QIP
        # templates may expose scaled XBRL values or a display value with no
        # unit suffix. Reconcile a material mismatch instead of showing, for
        # example, ₹0.30 alongside 1.2 crore shares at ₹127.
        if actual_date and quantity_number is not None and quantity_number > 0:
            if price_number is None and value_number is not None and value_number > 0:
                price_number = value_number / quantity_number
                price = price_number
                price_basis = "derived_from_value_and_quantity"
            elif price_number is not None and price_number > 0:
                calculated_value = quantity_number * price_number
                if value_number is None or value_number <= 0:
                    value_number = calculated_value
                    value = value_number
                    value_basis = "derived_from_quantity_times_price"
                elif abs(value_number - calculated_value) > max(1.0, calculated_value * 0.02):
                    value_number = calculated_value
                    value = value_number
                    value_basis = "reconciled_from_quantity_times_price"

        is_detailed = bool(
            actual_date and quantity_number is not None and quantity_number > 0
            and ((price_number is not None and price_number > 0)
                 or (value_number is not None and value_number > 0))
        )
        popup_text = body[:16000] or "; ".join(
            f"{key}: {clean_text(value)}" for key, value in lookup_facts.items()
            if clean_text(value)
        )[:16000]
        details = {
            "company_popup_facts": lookup_facts,
            "detail_tables": tables,
            "popup_text": popup_text,
            "listing_row": row.get("raw") or {},
            "allottee_row": allocation.get("allottee_row") or {},
            "allottees": allocation.get("allottees") or disclosed_allottee_names,
            "allottee_count": clean_text(allottee_count) or len(allottee_rows),
            "aggregate_issue": {
                "quantity": aggregate_qty,
                "price": aggregate_price,
                "value": aggregate_value,
                "reported_value_inr": reported_value_number,
                "final_value_inr": value_number,
                "value_basis": value_basis,
                "price_basis": price_basis,
            },
            "source_page": page_url,
            "source_url": url,
            "extraction_method": "company_name_popup",
        }
        event = normalized_event(
            exchange="NSE", activity_type=activity_type, activity_sub_type=subtype,
            company_name=company_name, symbol=symbol, isin=isin, event_date=event_date,
            disclosure_date=disclosure_date, person=person, action=action,
            quantity=quantity, price=price, value=value, source_url=url,
            details=details,
            identity=[
                "nse_issue_event", category, symbol or isin or norm(company_name),
                url, clean_text(row.get("stage")),
                # Include the date only when no distinct filing URL exists.
                event_date.date().isoformat() if url == page_url else None,
                norm(person), allocation_index,
            ],
            status="DETAILED" if is_detailed else "PARTIAL",
            source_metadata={
                **metadata_base,
                "allocation_index": allocation_index,
                "allocation_count": len(allocations),
                "disclosed_allottees_count": clean_text(allottee_count) or len(allottee_rows),
                "allottee_row_extracted": bool(allocation.get("allottee_row")),
            },
        )
        if event:
            events.append(event)
    return events

def _find_header_index(headers: list[str], *tokens: str) -> int | None:
    for token in tokens:
        wanted = norm(token)
        for index, value in enumerate(headers):
            text = norm(value)
            if wanted and wanted in text:
                return index
    return None


def _issue_row_date_basis(header: Any) -> str | None:
    label = norm(header)
    if "allotment" in label:
        return "allotment_date"
    if "issue date" in label or label == "date of issue":
        return "issue_date"
    if "board resolution" in label:
        return "board_resolution_date"
    if "submission" in label:
        return "submission_date"
    if "filing" in label or "broadcast" in label:
        return "filing_date"
    if "date" in label:
        return "filing_date"
    return None

def _parse_insider_detail_events(
    item: dict[str, Any], detail_url: str, detail_html: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize each PIT transaction row; never substitute filing date silently."""
    facts, transactions, body = _facts_from_html(detail_html)
    if not transactions:
        return [], {"facts": len(facts), "transaction_rows": 0, "body_chars": len(body)}

    company = item.get("company") or _get_field(facts, "company name", "name of company", "entity name")
    symbol = item.get("symbol") or _get_field(facts, "nse symbol", "symbol")
    isin = _get_field(facts, "isin code", "isin")
    filing_date_raw = _get_field(facts, "date of filing", "filing date", "date of submission")
    filing_date = parse_datetime(filing_date_raw)
    disclosure_date = parse_datetime(item.get("date")) or filing_date
    events: list[dict[str, Any]] = []

    for row_number, tx in enumerate(transactions, start=1):
        combined = {**facts, **tx}
        person = _get_field(
            combined,
            "name of the person", "name of person", "name of acquirer",
            "name of person acquiring", "name of person disposing",
            "acquirer name", "seller name", "name of promoter",
            "name of designated person", "name of immediate relative",
        )
        raw_action = _get_field(
            combined, "transaction type", "type of transaction",
            "acquisition or disposal", "buy sell", "nature of transaction",
            "mode of acquisition", "mode of disposal", "transaction",
        )
        action = normalize_buy_sell(raw_action) or clean_text(raw_action)
        quantity = _get_field(
            combined,
            "securities acquired / disposed no of security",
            "no of securities acquired", "number of securities acquired",
            "number of securities", "quantity", "shares acquired",
            "shares disposed", "securities acquired", "securities sold",
            "no of security", "number of security", "no of securities acquired disposed",
        )
        price = _get_field(
            combined, "price per share", "average price", "price per security",
            "transaction price", "trade price", "price",
        )
        value = _get_field(
            combined, "value of securities", "value of security", "total value of transaction",
            "transaction value", "consideration amount", "consideration",
        )
        # The NSE PIT disclosure sometimes publishes transaction consideration
        # and number of securities but no separate unit-price column.  Preserve
        # a defensible implied average price (and label its basis) rather than
        # leaving Price blank or inventing an exchange quote as transaction price.
        price_basis = "disclosed"
        if parse_number(price) is None:
            quantity_number = parse_number(quantity)
            value_number = parse_number(value)
            if quantity_number and value_number is not None and quantity_number > 0:
                price = value_number / quantity_number
                price_basis = "calculated_from_disclosed_value_and_quantity"
        holding_before = _get_field(
            combined, "holding before", "pre transaction holding",
            "securities held prior to acquisition", "prior to acquisition / disposal",
            "shares held prior", "securities held prior", "prior holding",
        )
        holding_after = _get_field(
            combined, "holding after", "post transaction holding",
            "securities held post acquisition", "post acquisition / disposal",
            "shares held post", "securities held post", "post holding",
            "shares held after", "securities held after", "shares held subsequent",
        )
        transaction_date_raw = _get_field(
            combined,
            # The current NSE PIT HTML template renders the transaction period
            # as a grouped "Date of allotment advice / acquisition of shares /
            # sale of shares" heading with leaf columns "From date" and "To
            # date". Prefer the start date as the event date; the filing date
            # is only a disclosure date and must not collapse different trade
            # dates into one group.
            "from date", "date of transaction", "transaction date",
            "date of acquisition", "date of sale", "date of purchase",
            "date of disposal", "date of acquisition or disposal",
            "date of allotment advice",
            "date of allotment advice acquisition of shares sale of shares specify",
            "acquisition of shares", "sale of shares specify",
        )
        transaction_date = parse_datetime(transaction_date_raw)
        # The event_date column is mandatory. A filing-date fallback remains
        # visibly PARTIAL and is explicitly labelled in source_metadata.
        parsed_date = transaction_date or filing_date or disclosure_date
        if parsed_date is None or not company:
            continue

        event_date_basis = "transaction_date" if transaction_date else "filing_date_fallback"
        details = {
            "filing_facts": facts,
            "transaction": tx,
            "details_text": body[:16000],
            "details_url": detail_url,
            "listing_row": item.get("raw") or {},
            "raw_transaction_action": raw_action,
            "raw_transaction_date": transaction_date_raw,
            "transaction_price_basis": price_basis,
            "extraction_method": "NSE Details HTML/iXBRL",
        }
        is_detailed = bool(
            person and normalize_buy_sell(raw_action) and parse_number(quantity) is not None
            and transaction_date is not None
        )
        event = normalized_event(
            exchange="NSE", activity_type="INSIDER_TRADING", activity_sub_type=None,
            company_name=company, symbol=symbol, isin=isin,
            event_date=parsed_date, disclosure_date=disclosure_date,
            person=person, action=action, quantity=quantity, price=price,
            value=value, holding_before=holding_before,
            holding_after=holding_after, source_url=detail_url,
            details=details,
            identity=[symbol, detail_url, row_number, parsed_date.isoformat(), person, action, quantity],
            status="DETAILED" if is_detailed else "PARTIAL",
            source_metadata={
                "listing_url": INSIDER_URL,
                "detail_url": detail_url,
                "filing_date": filing_date_raw,
                "transaction_date": transaction_date_raw,
                "event_date_basis": event_date_basis,
                "parser": "nse_ixbrl_multilevel_table_v3",
                "transaction_price_basis": price_basis,
            },
        )
        if event:
            events.append(event)

    return events, {
        "facts": len(facts),
        "transaction_rows": len(transactions),
        "events": len(events),
        "body_chars": len(body),
    }

def _csv_value(row: dict[str, Any], *keys: str) -> Any:
    indexed = {norm(k): v for k, v in row.items()}
    for key in keys:
        wanted = norm(key)
        for actual, value in indexed.items():
            if wanted == actual or wanted in actual:
                if clean_text(value):
                    return value
    return None


def _deal_rows_from_response(content: bytes, content_type: str) -> tuple[list[dict[str, Any]], str]:
    """Parse only a recognizable NSE CSV/JSON deals response; reject HTML shells."""
    raw_text = content.decode("utf-8-sig", errors="replace") if content else ""
    stripped = raw_text.strip()
    if not stripped:
        raise ValueError("NSE deals endpoint returned an empty body")
    if stripped.startswith("{") or "json" in (content_type or "").casefold():
        import json
        payload = json.loads(stripped)
        if not isinstance(payload, dict) or "data" not in payload:
            raise ValueError("NSE deals JSON response did not contain the expected data field")
        data = payload.get("data")
        if data is None:
            return [], "EMPTY"
        if isinstance(data, dict):
            for key in ("data", "rows", "result", "records"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise ValueError("NSE deals JSON data field was not a row array")
        rows = [dict(row) for row in data if isinstance(row, dict)]
        if data and not rows:
            raise ValueError("NSE deals JSON contained no recognizable object rows")
        return rows, "SUCCESS" if rows else "EMPTY"

    if "<html" in stripped[:500].casefold() or "<!doctype" in stripped[:500].casefold():
        raise ValueError("NSE deals endpoint returned HTML instead of CSV")
    if norm(stripped) in {"no records found", "no data available", "no data found"}:
        return [], "EMPTY"
    reader = csv.DictReader(io.StringIO(raw_text))
    headers = [norm(value) for value in (reader.fieldnames or [])]
    header_text = " ".join(headers)
    has_symbol = any(term in header_text for term in ("symbol", "security name", "scrip code"))
    has_client = "client" in header_text
    has_quantity = any(term in header_text for term in ("quantity", "qty"))
    has_price = "price" in header_text
    if not (has_symbol and has_client and has_quantity and has_price):
        raise ValueError(f"NSE deals CSV schema unrecognized headers={headers[:16]}")
    rows = [dict(row) for row in reader if any(clean_text(value) for value in row.values())]
    return rows, "SUCCESS" if rows else "EMPTY"

def _link_payload(anchor, base_url: str) -> dict[str, Any]:
    raw_href = clean_text(anchor.get("href"))
    raw_onclick = clean_text(anchor.get("onclick"))
    direct_url = _valid_nse_url(raw_href, base_url) if raw_href and raw_href not in {"#", "javascript:void(0)", "javascript:;"} and not raw_href.casefold().startswith("javascript:") else None
    handler_url = _url_from_link_handler(raw_onclick, base_url)
    return {
        "text": clean_text(anchor.get_text(" ", strip=True)) or clean_text(anchor.get("title")) or "",
        "href": direct_url,
        "raw_href": raw_href,
        "onclick": raw_onclick,
        "handler_url": handler_url,
        "title": clean_text(anchor.get("title")),
        "aria": clean_text(anchor.get("aria-label")),
        "data": {str(key): clean_text(value) for key, value in anchor.attrs.items() if str(key).startswith("data-")},
    }


def _parse_listing_html(html_text: str, base_url: str) -> dict[str, Any]:
    """Parse HTML listing rows, their direct source links and pagination metadata."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    body_text = clean_text(soup.get_text(" ", strip=True)) or ""
    tables: list[dict[str, Any]] = []
    for table_index, table in enumerate(soup.find_all("table")):
        trs = table.find_all("tr")
        if not trs:
            continue
        header_index = None
        headers: list[str] = []
        for index, tr in enumerate(trs[:8]):
            cells = tr.find_all(["th", "td"], recursive=False)
            values = [clean_text(cell.get_text(" ", strip=True)) or "" for cell in cells]
            combined = norm(" ".join(values))
            if values and (
                ("symbol" in combined and ("company" in combined or "isin" in combined))
                or ("company" in combined and any(term in combined for term in ("date", "details", "regulation", "submission")))
                or ("broadcast" in combined and "company" in combined)
            ):
                header_index, headers = index, values
                break
        if header_index is None:
            continue
        rows: list[dict[str, Any]] = []
        for row_index, tr in enumerate(trs[header_index + 1:], start=header_index + 1):
            cells_dom = tr.find_all(["td", "th"], recursive=False)
            values = [clean_text(cell.get_text(" ", strip=True)) or "" for cell in cells_dom]
            if not any(values):
                continue
            links = [_link_payload(anchor, base_url) for anchor in tr.find_all("a")]
            rows.append({"cells": values, "links": links, "row_index": row_index})
        if rows:
            tables.append({"table_index": table_index, "headers": headers, "rows": rows})

    total_records = None
    current_page = 1
    total_pages = None
    match = re.search(r"displaying\s+(\d+)\s*[-–]\s*(\d+)\s+of\s+([\d,]+)", body_text, re.I)
    if match:
        total_records = int(match.group(3).replace(",", ""))
        current_page = max(1, (int(match.group(1)) - 1) // max(1, int(match.group(2)) - int(match.group(1)) + 1) + 1)
        page_size = max(1, int(match.group(2)) - int(match.group(1)) + 1)
        total_pages = (total_records + page_size - 1) // page_size
    page_match = re.search(r"page\s*(\d+)\s*of\s*(\d+)", body_text, re.I)
    if page_match:
        current_page, total_pages = int(page_match.group(1)), int(page_match.group(2))
    next_url = None
    for anchor in soup.find_all("a"):
        label = norm(" ".join([anchor.get_text(" ", strip=True), str(anchor.get("aria-label") or ""), str(anchor.get("title") or "")]))
        rel = " ".join(anchor.get("rel") or [])
        if "next" not in label and "next" not in norm(rel):
            continue
        link = _link_payload(anchor, base_url)
        next_url = link.get("href") or link.get("handler_url")
        if next_url:
            break
    if not next_url:
        for node in soup.find_all(["button", "input"]):
            label = norm(" ".join([str(node.get_text(" ", strip=True)), str(node.get("value") or ""), str(node.get("aria-label") or ""), str(node.get("title") or "")]))
            if "next" in label:
                handler = node.get("onclick") or node.get("data-url") or node.get("data-href")
                next_url = _url_from_link_handler(handler, base_url) or _valid_nse_url(handler, base_url)
                break
    download_urls: list[str] = []
    for node in soup.find_all(["a", "button", "input"]):
        label = norm(" ".join([
            node.get_text(" ", strip=True), str(node.get("value") or ""),
            str(node.get("title") or ""), str(node.get("aria-label") or ""),
        ]))
        raw_href = node.get("href") or node.get("data-url") or node.get("data-href")
        handler = node.get("onclick")
        target = _valid_nse_url(raw_href, base_url) or _url_from_link_handler(handler, base_url)
        if target and ("csv" in label or ".csv" in target.casefold() or "csv" in norm(handler)):
            if target not in download_urls:
                download_urls.append(target)

    # Include tables embedded as JSON by NSE's server-side templates.
    if not tables:
        for script in soup.find_all("script"):
            text = script.string or script.get_text()
            if not text or not text.strip().startswith(("{", "[")):
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            for candidate in _json_rows(payload):
                if candidate:
                    tables.append({"table_index": len(tables), "headers": list(candidate[0]), "rows": [
                        {"cells": [str(value or "") for value in item.values()], "links": [], "row_index": idx}
                        for idx, item in enumerate(candidate)
                    ]})
    return {
        "tables": tables, "next_url": next_url,
        "current_page": current_page, "total_pages": total_pages,
        "total_records": total_records, "body_text": body_text,
        "download_urls": download_urls, "html_chars": len(html_text or ""),
    }


def _json_rows(payload: Any) -> list[list[dict[str, Any]]]:
    """Find row arrays in inline JSON without assuming one NSE payload wrapper."""
    found: list[list[dict[str, Any]]] = []
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for value in item.values():
                if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
                    headers = norm(" ".join(str(key) for key in value[0].keys()))
                    if "company" in headers or "symbol" in headers or "client" in headers:
                        found.append(value)
                else:
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return found


async def _fetch_listing_pages(client: Any, listing_url: str, start: date, end: date, *, page_limit: int = 1000) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch every HTML page. Count metadata is cross-checked against pagination."""
    current_url = _date_window_url(listing_url, start, end)
    pages: list[dict[str, Any]] = []
    all_tables: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_signatures: set[str] = set()
    pagination_complete = True
    pagination_issue = None
    page_key: str | None = None
    while current_url and len(pages) < page_limit:
        if current_url in seen_urls:
            pagination_complete = False
            pagination_issue = "repeated_listing_url"
            break
        seen_urls.add(current_url)
        text = await client.get_text(current_url, referer=listing_url)
        parsed = _parse_listing_html(text, current_url)
        signature_payload = "|".join(
            norm(" ".join(row.get("cells") or []))
            for table in parsed["tables"] for row in table.get("rows", [])
        )
        signature = str(hash(signature_payload))
        if signature and signature in seen_signatures:
            pagination_complete = False
            pagination_issue = "duplicate_page_content"
            break
        seen_signatures.add(signature)
        for table in parsed["tables"]:
            table = dict(table)
            table["page_index"] = len(pages)
            all_tables.append(table)
        pages.append({"url": current_url, **{key: parsed[key] for key in ("current_page", "total_pages", "total_records", "html_chars", "download_urls")}})

        next_url = parsed.get("next_url")
        if next_url:
            current_url = next_url
            continue
        current_page = parsed.get("current_page") or len(pages)
        total_pages = parsed.get("total_pages")
        if total_pages and current_page < total_pages:
            target_page = current_page + 1
            # Some NSE pages use an input-driven paginator whose HTML has no href.
            # Probe common query names and accept only a genuinely changed page.
            keys = [page_key] if page_key else ["page", "pageNo", "pageno", "page_no", "pageNumber", "page_number"]
            candidate_found = False
            for key in [item for item in keys if item]:
                candidate_url = _with_query(current_url, **{key: target_page})
                if candidate_url in seen_urls:
                    continue
                candidate_text = await client.get_text(candidate_url, referer=listing_url)
                candidate = _parse_listing_html(candidate_text, candidate_url)
                candidate_signature = str(hash("|".join(
                    norm(" ".join(row.get("cells") or []))
                    for table in candidate["tables"] for row in table.get("rows", [])
                )))
                if candidate_signature and candidate_signature != signature:
                    page_key = key
                    current_url = candidate_url
                    candidate_found = True
                    break
            if not candidate_found:
                pagination_complete = False
                pagination_issue = f"NSE reports {total_pages} pages but no working HTTP page link/parameter was found"
                break
            continue
        if parsed.get("total_records") is not None:
            observed_rows = sum(len(table.get("rows") or []) for table in all_tables)
            if observed_rows < int(parsed["total_records"]):
                pagination_complete = False
                pagination_issue = f"listing count says {parsed['total_records']} records; parsed only {observed_rows}"
        current_url = None
    if len(pages) >= page_limit and current_url:
        pagination_complete = False
        pagination_issue = f"page_safety_limit_reached:{page_limit}"
    if not pages:
        raise RuntimeError(f"NSE listing returned no HTML pages: {listing_url}")
    return all_tables, {
        "pages_read": len(pages), "pagination_complete": pagination_complete,
        "pagination_issue": pagination_issue, "page_count_reported": max((p.get("total_pages") or 0 for p in pages), default=0),
        "record_count_reported": max((p.get("total_records") or 0 for p in pages), default=0),
        "listing_pages": pages,
    }


def _extract_company_candidates(tables: list[dict[str, Any]], category: str, base_url: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for table in tables:
        headers = table.get("headers") or []
        header_text = norm(" ".join(headers))
        if category == "INSIDER_TRADING":
            if not any(token in header_text for token in ("regulation", "details", "submission", "broadcast", "company")):
                continue
        elif not ("company" in header_text and ("symbol" in header_text or "isin" in header_text)):
            continue
        company_idx = _find_header_index(headers, "company name", "company")
        symbol_idx = _find_header_index(headers, "symbol", "nse symbol")
        isin_idx = _find_header_index(headers, "isin")
        if category == "INSIDER_TRADING":
            date_idx = _find_header_index(headers, "broadcast date", "date of filing", "submission date", "date")
        else:
            date_idx = _find_header_index(headers, "date of allotment", "allotment date", "date of issue", "issue date", "submission date", "date of submission", "board resolution date", "date of filing", "filing date", "date")
        stage_idx = _find_header_index(headers, "stage")
        for row in table.get("rows") or []:
            cells, links = row.get("cells") or [], row.get("links") or []
            company = cells[company_idx] if company_idx is not None and company_idx < len(cells) else None
            symbol = cells[symbol_idx] if symbol_idx is not None and symbol_idx < len(cells) else None
            isin = cells[isin_idx] if isin_idx is not None and isin_idx < len(cells) else None
            row_date = cells[date_idx] if date_idx is not None and date_idx < len(cells) else None
            date_header = headers[date_idx] if date_idx is not None and date_idx < len(headers) else None
            stage = cells[stage_idx] if stage_idx is not None and stage_idx < len(cells) else None
            if not company:
                company = next((link.get("text") for link in links if link.get("text") and norm(link.get("text")) not in {"details", "xbrl", "xml"}), None)
            if not company and not symbol:
                continue
            detail_link = None
            if category == "INSIDER_TRADING":
                detail_link = next((link for link in links if (
                    (link.get("href") or link.get("handler_url"))
                    and ("detail" in norm(link.get("text")) or "detail" in norm(link.get("title")) or ".html" in (link.get("href") or "").casefold() or ".xml" in (link.get("href") or "").casefold())
                )), None)
            else:
                # NSE issue tables expose an XBRL link per row; prefer it over
                # the company-name modal handler, and never select PDF/ZIP files.
                detail_link = next((link for link in links if (
                    (link.get("href") or link.get("handler_url"))
                    and ("xbrl" in norm(link.get("text") + " " + link.get("title"))
                         or (urlparse(link.get("href") or "").path.casefold().endswith((".html", ".htm", ".xml", ".xhtml"))))
                )), None)
                if not detail_link:
                    detail_link = next((link for link in links if (
                        link.get("text") and norm(link.get("text")) == norm(company)
                        and (link.get("href") or link.get("handler_url"))
                    )), None)
            detail_url = None
            if detail_link:
                detail_url = detail_link.get("href") or detail_link.get("handler_url") or _url_from_link_handler(detail_link.get("onclick"), base_url)
            if category == "INSIDER_TRADING" and not detail_url:
                # Only detail rows can be expanded; CSV rows may carry URLs in a cell.
                for cell in cells:
                    match = re.search(r"https?://[^\s]+\.(?:html?|xml)(?:\?[^\s]*)?", cell or "", re.I)
                    if match:
                        detail_url = _valid_nse_url(match.group(0), base_url)
                        break
            if category == "INSIDER_TRADING" and not detail_url:
                continue
            date_basis = _issue_row_date_basis(date_header) if category != "INSIDER_TRADING" else "filing_date"
            candidates.append({
                "company": clean_text(company) or clean_text(symbol), "symbol": clean_text(symbol),
                "isin": clean_text(isin), "date": clean_text(row_date), "date_header": date_header,
                "date_basis": date_basis, "stage": clean_text(stage), "detail_url": _valid_nse_url(detail_url, base_url) if detail_url else None,
                "company_link_raw": detail_link.get("raw_href") if detail_link else None,
                "company_link_onclick": detail_link.get("onclick") if detail_link else None,
                "table_index": table.get("table_index"), "row_index": row.get("row_index"),
                "page_index": table.get("page_index", 0),
                "raw": {"headers": headers, "cells": cells, "links": links},
            })
    # De-duplicate the same record that may be present in repeated source tables.
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in candidates:
        key = (norm(item.get("symbol")), norm(item.get("company")), norm(item.get("detail_url")), norm(item.get("stage")), norm(item.get("date")))
        unique[key] = item
    return list(unique.values())


async def _fetch_document(client: Any, url: str, referer: str) -> tuple[str, str]:
    if not url:
        raise ValueError("Missing filing URL")
    parsed = urlparse(url)
    if parsed.path.casefold().endswith(".pdf"):
        raise ValueError("PDF is not a supported filing format")
    content, content_type = await client.get_bytes(url, referer=referer, accept="text/html,application/xhtml+xml,application/xml,text/xml,*/*")
    if not content:
        raise ValueError("Empty NSE filing response")
    prefix = content[:500].lstrip().lower()
    if b"<!doctype html" in prefix or b"<html" in prefix:
        text = content.decode("utf-8-sig", errors="replace")
        blocked = norm(BeautifulSoup(text, "html.parser").get_text(" ", strip=True))
        if any(term in blocked for term in ("access denied", "captcha", "too many requests", "request rejected")):
            raise RuntimeError("NSE returned an access/anti-bot response instead of filing content")
        return text, content_type or "text/html"
    if prefix.startswith(b"{") or "json" in (content_type or "").casefold():
        raise ValueError("Expected an HTML/XML filing document, received JSON")
    if b"%PDF" in prefix:
        raise ValueError("NSE returned a PDF; PDF parsing is intentionally disabled")
    return content.decode("utf-8-sig", errors="replace"), content_type or "application/xml"


def _source_link_for_candidate(item: dict[str, Any], listing_url: str) -> tuple[str, str]:
    url = _valid_nse_url(item.get("detail_url"), listing_url)
    if url and not urlparse(url).path.casefold().endswith((".pdf", ".zip")):
        return url, "row_xbrl_or_details_link"
    # Company-name modal handlers may not expose a standalone URL. Preserve
    # the listing page rather than fabricating a permalink; retain raw handler.
    return listing_url, "listing_page_only_no_standalone_filing_url"


def _failed_event(category: str, item: dict[str, Any], source_url: str, reason: str, event_date: Any = None) -> dict[str, Any] | None:
    parsed_date = parse_datetime(event_date or item.get("date"))
    if not parsed_date:
        return None
    activity = "INSIDER_TRADING" if category == "INSIDER_TRADING" else "QIP" if category == "QIP" else "PREFERENTIAL_WARRANT"
    subtype = None if category in {"INSIDER_TRADING", "QIP"} else "PREFERENTIAL"
    return normalized_event(
        exchange="NSE", activity_type=activity, activity_sub_type=subtype,
        company_name=item.get("company"), symbol=item.get("symbol"), isin=item.get("isin"),
        event_date=parsed_date, disclosure_date=parsed_date, source_url=source_url,
        status="FAILED", details={"failure_reason": reason, "listing_row": item.get("raw") or {}},
        identity=[category, source_url, item.get("symbol"), item.get("company"), parsed_date.isoformat(), "failed"],
        source_metadata={"failure_reason": reason, "listing_row_date_basis": item.get("date_basis"), "source_link_kind": "record_link" if source_url != listing_url_for(category) else "listing_page"},
    )


def listing_url_for(category: str) -> str:
    return {"INSIDER_TRADING": INSIDER_URL, "QIP": QIP_URL, "PREFERENTIAL_WARRANT": PREF_URL}.get(category, NSE)


def _json_rows_from_payload(payload: Any) -> tuple[list[dict[str, Any]], str]:
    """Return an NSE JSON row array and the wrapper key that contained it.

    NSE endpoints usually return {"data": [...]}, but some responses wrap the
    rows in result/records. Empty arrays are valid, while an unknown object shape
    is not silently interpreted as an empty listing.
    """
    if isinstance(payload, list):
        if all(isinstance(item, dict) for item in payload):
            return [dict(item) for item in payload], "root_list"
        raise ValueError("NSE JSON root list contains non-object records")
    if isinstance(payload, dict):
        for key in ("data", "rows", "records", "result", "results"):
            if key not in payload:
                continue
            value = payload.get(key)
            if value is None:
                return [], key
            if isinstance(value, list):
                if not all(isinstance(item, dict) for item in value):
                    raise ValueError(f"NSE JSON {key!r} contains non-object rows")
                return [dict(item) for item in value], key
            if isinstance(value, dict):
                for nested_key in ("data", "rows", "records", "result", "results"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list) and all(isinstance(item, dict) for item in nested):
                        return [dict(item) for item in nested], f"{key}.{nested_key}"
        # Search nested objects only when their arrays look like company/filing rows.
        for nested_key, value in payload.items():
            if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                keys = norm(" ".join(str(k) for k in value[0].keys()))
                if any(token in keys for token in ("company", "symbol", "client", "isin")):
                    return [dict(item) for item in value], str(nested_key)
    raise ValueError(
        "Unrecognized NSE JSON response shape; expected a data/rows/records array"
    )


async def _fetch_json_rows_diagnostic(
    client: Any, url: str, referer: str, *, source: str, stage: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload_bytes, content_type = await client.get_bytes(
        url, referer=referer, accept="application/json,text/plain,*/*"
    )
    if not payload_bytes:
        raise ValueError(f"{source} returned an empty response body")
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        sample = payload_bytes[:240].decode("utf-8", errors="replace").replace("\n", " ")
        raise ValueError(
            f"{source} did not return JSON (content_type={content_type!r}, bytes={len(payload_bytes)}, sample={sample!r})"
        ) from exc
    rows, wrapper = _json_rows_from_payload(payload)
    keys = sorted(str(key) for key in rows[0].keys()) if rows else []
    diagnostic = {
        "source": source, "stage": stage, "url": url,
        "content_type": content_type, "payload_bytes": len(payload_bytes),
        "wrapper": wrapper, "records": len(rows), "sample_keys": keys[:40],
        "payload_shape": "rows" if rows else "valid_empty_rows",
    }
    logger.info(
        "NSE_REGULATORY_API_DISCOVERY source=%s stage=%s records=%s wrapper=%s bytes=%s content_type=%s sample_keys=%s",
        source, stage or "-", len(rows), wrapper, len(payload_bytes), content_type, keys[:20],
    )
    return rows, diagnostic


async def _fetch_json_rows(client: Any, url: str, referer: str) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that do not need source diagnostics."""
    try:
        rows, _diagnostic = await _fetch_json_rows_diagnostic(
            client, url, referer, source="generic_nse_json"
        )
        return rows
    except Exception as exc:
        logger.debug("NSE regulatory JSON source unavailable url=%s error=%s", url, str(exc)[:240])
        return []


def _api_value_entry(row: dict[str, Any], *keys: str) -> tuple[str, Any] | None:
    """Case/underscore-insensitive lookup that preserves the matched API key."""
    indexed = [(str(key), norm(key), value) for key, value in row.items()]
    for key in keys:
        wanted = norm(key)
        for original, actual, value in indexed:
            if (actual == wanted or wanted in actual) and clean_text(value):
                return original, value
    return None


def _api_value(row: dict[str, Any], *keys: str) -> Any:
    found = _api_value_entry(row, *keys)
    return found[1] if found else None


def _api_document_url(row: dict[str, Any], base_url: str) -> str | None:
    """Recover an exact HTML/XML/XBRL URL exposed by an NSE API row.

    The scraper intentionally does not guess archive paths from bare filenames.
    """
    candidate_keys = (
        "ixbrl", "xmlFileName", "xbrlFileName", "xbrl", "detailsUrl", "detailUrl",
        "attachmentUrl", "attachmentFile", "attchmntFile", "filingUrl", "documentUrl",
        "fileUrl", "link", "url",
    )
    raw_value = _api_value(row, *candidate_keys)
    if not raw_value:
        # Some JSON feeds return a literal anchor fragment in ixbrl/XML fields.
        for key, value in row.items():
            if norm(key) in {"ixbrl", "xml file name", "xbrl file name"} and clean_text(value):
                match = re.search(r"(?:href\s*=\s*['\"])([^'\"]+)", str(value), re.I)
                if match:
                    raw_value = match.group(1)
                    break
    if not raw_value:
        return None
    text = clean_text(raw_value)
    if not text:
        return None
    if "<a" in text.casefold():
        soup = BeautifulSoup(text, "html.parser")
        anchor = soup.find("a", href=True)
        text = clean_text(anchor.get("href")) if anchor else None
    if not text:
        return None
    text = html_lib.unescape(text).strip()
    if re.match(r"^(?:www\.|nsearchives\.|archives\.)?nseindia\.(?:com|in)/", text, re.I):
        text = "https://" + text
    parsed_value = urlparse(text)
    # Do not join a bare filename to the listing URL: that would invent a path.
    if not parsed_value.scheme and not text.startswith(("/", "//")) and "/" not in text:
        return None
    url = _valid_nse_url(text, base_url)
    if not url:
        return None
    path = urlparse(url).path.casefold()
    if path.endswith((".pdf", ".zip", ".xls", ".xlsx")):
        return None
    # NSE iXBRL URLs are occasionally served without an extension. Keep those
    # only when the API field itself explicitly denotes an XBRL/detail resource.
    return url


def _insider_api_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for raw in rows:
        company = _api_value(raw, "companyName", "company", "securityName", "issuerName")
        symbol = _api_value(raw, "symbol", "nseSymbol")
        if not company and not symbol:
            continue
        detail_url = _api_document_url(raw, INSIDER_URL)
        disclosure = _api_value(raw, "broadcastDateTime", "broadcastDate", "dateOfFiling", "filingDate", "date")
        candidates.append({
            "company": clean_text(company) or clean_text(symbol),
            "symbol": clean_text(symbol),
            "date": disclosure,
            "date_basis": "filing_date",
            "detail_url": detail_url,
            "raw": raw,
            "listing_source": "nse_corporates_pit_gg_api",
        })
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in candidates:
        key = (norm(item.get("symbol")), norm(item.get("date")), norm(item.get("detail_url") or json.dumps(item.get("raw"), sort_keys=True, default=str)))
        unique[key] = item
    return list(unique.values())


def _issue_api_candidate(category: str, raw: dict[str, Any], stage: str) -> dict[str, Any] | None:
    if category == "QIP":
        company = _api_value(raw, "companyName", "nameOfTheCompany", "nameOfListedEntity", "company")
        symbol = _api_value(raw, "nseSymbol", "nsesymbol", "symbol")
        allotment = _api_value(raw, "dtOfAllotmentOfShares", "dateOfAllotmentOfShares", "allotmentDate")
        board = _api_value(
            raw, "dateBrdResol", "boardResolutionDate", "dateBrdResolution",
            "dateOfShareholdersApprovalForPlacementWithQIBs", "dateOfShareholdersApproval",
        )
        submission = _api_value(raw, "dateOfSubmission", "submissionDate")
        quantity = _api_value(
            raw,
            "noOfSharesAllotted", "noOfEquitySharesListed", "numberOfSharesAllotted",
            "totalNumberOfSharesAllotted", "totalNumOfSharesAllotted",
            "noOfEquitySecurities", "numberOfEquitySecurities", "tentativeNoOfShares",
            "noOfNonConvertibleDebtSecurities", "numberOfNonConvertibleDebtSecurities",
            "noOfConvertibleSecurities", "numberOfConvertibleSecurities",
            "noOfWarrants", "numberOfWarrants", "noOfUnits", "numberOfUnits",
            "numberOfSecurities", "issueSizeAppliedForNoOfShares",
            "noOfSecuritiesAppliedFor", "numberOfSecuritiesAppliedFor",
            "securityTypeNoOfSecurities", "numberOfSecuritiesProposedToBeIssued",
        )
        price = _api_value(
            raw,
            "issPricePerUnit", "issuePricePerUnit", "issuePricePerShare",
            "issuePrice", "minimumIssuePricePerSecurity", "minIssuePricePerSecurity",
            "minIssPricePerUnit", "minimumIssuePrice", "minIssuePrice",
            "minimumIssuePricePerShare", "minIssuePricePerSecurity",
        )
        value_entry = _api_value_entry(
            raw,
            "finalAmountOfIssueSize", "finalIssueSize", "finalIssueSizeInCrores",
            "finalIssueSizeInINRCrores", "totalAmtOfIssueSize", "estimatedTotalIssueAmount",
            "amountInINRCrores", "amountINRCrores", "amountOfIssueInCrores",
            "issueSize", "amountRaised", "amountOfIssue", "amountToBeRaised",
            "securityTypeAmountToBeRaised", "issueSizeAppliedForInCrores",
            "issueSizeAppliedForINRCrores", "issueSizeAppliedFor",
        )
        value = value_entry[1] if value_entry else None
        allottee_count = _api_value(raw, "noOfAllottees", "numberOfAllottees", "allotteeCount")
        security_type = _api_value(raw, "issue_type", "issueType", "securityType", "typeOfSecurity")
    else:
        company = _api_value(raw, "nameOfTheCompany", "companyName", "nameOfListedEntity", "company")
        symbol = _api_value(raw, "nseSymbol", "nsesymbol", "symbol")
        allotment = _api_value(raw, "dateOfAllotmentOfShares", "dtOfAllotmentOfShares", "allotmentDate")
        board = _api_value(raw, "dateBrdResoln", "boardResDate", "boardResolutionDate")
        submission = _api_value(raw, "dateOfSubmission", "submissionDate")
        quantity = _api_value(
            raw,
            "totalNumOfSharesAllotted", "totalNumberOfSharesAllotted",
            "noOfSharesAllotted", "numberOfSharesAllotted",
            "numberOfEquitySharesListed", "noOfEquitySharesListed", "totalNoOfEquitySharesListed",
            "totalNumberOfWarrantsAllotted", "totalNumOfWarrantsAllotted",
            "numberOfWarrantsAllotted", "numberOfWarrants",
        )
        price = _api_value(
            raw,
            "offerPricePerSecurity", "offerPricePerShare", "offerPrice",
            "issuePrice", "pricePerSecurity", "issuePricePerShare", "allotmentPrice",
        )
        value_entry = _api_value_entry(
            raw,
            "amountRaisedRsInLakhs", "amountRaisedInLakhs", "amountRaised",
            "totalAmtRaised", "totalConsideration", "considerationAmount", "consideration",
        )
        value = value_entry[1] if value_entry else None
        allottee_count = _api_value(raw, "noOfAllottees", "numberOfAllottees", "allotteeCount")
        security_type = _api_value(raw, "typeOfSecurity", "securityType", "issueType", "issue_type", "instrumentType")

    if not company and not symbol:
        return None
    actual_date = parse_datetime(allotment)
    board_date = parse_datetime(board)
    filing_date = parse_datetime(submission)
    event_date = actual_date or board_date or filing_date
    date_basis = "allotment_date" if actual_date else "board_resolution_date" if board_date else "filing_date_fallback" if filing_date else None
    if event_date is None:
        return None
    source_url = _api_document_url(raw, QIP_URL if category == "QIP" else PREF_URL)

    # NSE's published PREF template labels Amount Raised in Rs. Lakhs.
    # QIP pre-allotment forms use Amount (INR in Crores); only apply a
    # multiplier when the raw API key itself declares that unit.
    value_number = parse_number(value)
    value_field_name = value_entry[0] if value_entry else ""
    value_key_norm = norm(value_field_name)
    value_text_norm = norm(value)
    if value_number is not None and not any(unit in value_text_norm for unit in ("lakh", "lakhs", "lac", "crore", "crores")):
        if category != "QIP" and (
            value_key_norm in {
                "amountraised", "totalamtraised", "amountraisedrsinlakhs", "amountraisedinlakhs",
            }
            or "lakhs" in value_key_norm or "lakh" in value_key_norm
        ):
            value = value_number * 100_000
        elif category == "QIP" and "crore" in value_key_norm:
            value = value_number * 10_000_000

    return {
        "company": clean_text(company) or clean_text(symbol),
        "symbol": clean_text(symbol),
        "isin": _api_value(raw, "isin", "isinCode"),
        "date": event_date,
        "date_basis": date_basis,
        "disclosure_date": filing_date,
        "allotment_date": actual_date,
        "board_resolution_date": board_date,
        "submission_date": filing_date,
        "quantity": quantity,
        "price": price,
        "value": value,
        "value_field": value_field_name,
        "allottee_count": allottee_count,
        "allottee_category": _api_value(
            raw, "categoryOfAllottee", "allotteeCategory", "categoryOfInvestor", "allotteeType",
        ),
        "security_type": clean_text(security_type),
        "stage": clean_text(_api_value(raw, "stage")) or stage,
        "detail_url": source_url,
        "raw": raw,
        "listing_source": f"nse_{category.casefold()}_{stage.casefold().replace('-', '_').replace(' ', '_')}_api",
    }


def _issue_api_fallback_event(category: str, item: dict[str, Any], listing_url: str) -> dict[str, Any] | None:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    company = item.get("company") or item.get("symbol") or "Unknown"
    activity_type = "QIP" if category == "QIP" else "PREFERENTIAL_WARRANT"
    security = norm(item.get("security_type"))
    subtype = None if category == "QIP" else "WARRANT" if "warrant" in security else "PREFERENTIAL"
    actual_date = item.get("allotment_date")
    event_date = parse_datetime(actual_date or item.get("date"))
    if not event_date:
        return None
    date_basis = item.get("date_basis") or "unknown"
    action = "Allotted" if actual_date else normalize_issue_action(item.get("stage"), default="In-Principle")
    quantity = item.get("quantity")
    price = item.get("price")
    value = item.get("value")
    source_url = item.get("detail_url") or listing_url
    fields = {
        **raw,
        "Company Name": company,
        "Symbol": item.get("symbol"),
        "Stage": item.get("stage"),
        "Allottee Category": item.get("allottee_category"),
        "Number of Allottees": item.get("allottee_count"),
    }
    popup_text = "; ".join(f"{key}: {clean_text(val)}" for key, val in fields.items() if clean_text(val))
    if not popup_text:
        popup_text = f"Company: {company}; Stage: {item.get('stage') or 'not disclosed'}"

    quantity_number = parse_number(quantity)
    price_number = parse_number(price)
    value_number = parse_number(value)
    reported_value_number = value_number
    value_basis = "exchange_disclosed" if value_number is not None else None
    price_basis = "exchange_disclosed" if price_number is not None else None
    if actual_date and quantity_number is not None and quantity_number > 0:
        if price_number is None and value_number is not None and value_number > 0:
            price_number = value_number / quantity_number
            price = price_number
            price_basis = "derived_from_value_and_quantity"
        elif price_number is not None and price_number > 0:
            calculated_value = quantity_number * price_number
            if value_number is None or value_number <= 0:
                value_number = calculated_value
                value = value_number
                value_basis = "derived_from_quantity_times_price"
            elif abs(value_number - calculated_value) > max(1.0, calculated_value * 0.02):
                value_number = calculated_value
                value = value_number
                value_basis = "reconciled_from_quantity_times_price"

    detailed = bool(
        actual_date and quantity_number is not None and quantity_number > 0
        and ((price_number is not None and price_number > 0)
             or (value_number is not None and value_number > 0))
    )
    person = (
        item.get("allottee_category")
        or (f"{item.get('allottee_count')} allottees" if item.get("allottee_count") not in (None, "") else None)
        or _api_value(raw, "allotteeName", "nameOfAllottee", "allottee")
    )
    return normalized_event(
        exchange="NSE", activity_type=activity_type, activity_sub_type=subtype,
        company_name=company, symbol=item.get("symbol"), isin=item.get("isin"),
        event_date=event_date, disclosure_date=item.get("disclosure_date"),
        person=person, action=action, quantity=quantity, price=price, value=value,
        source_url=source_url,
        details={
            "company_popup_facts": fields,
            "popup_text": popup_text[:16000],
            "listing_row": raw,
            "aggregate_issue": {
                "quantity": quantity, "price": price,
                "reported_value_inr": reported_value_number,
                "final_value_inr": value_number,
                "value_basis": value_basis,
                "price_basis": price_basis,
            },
            "source_page": listing_url,
            "extraction_method": "NSE further-issues JSON API fallback",
        },
        identity=[
            "nse_issue_event", category, item.get("symbol") or item.get("isin") or norm(company),
            source_url, clean_text(item.get("stage")),
            event_date.date().isoformat() if source_url == listing_url else None,
            norm(person), 0,
        ],
        status="DETAILED" if detailed else "PARTIAL",
        source_metadata={
            "extraction_method": "nse_further_issues_json_api",
            "api_stage": item.get("stage"),
            "event_date_basis": date_basis,
            "allotment_date": str(actual_date) if actual_date else None,
            "board_resolution_date": str(item.get("board_resolution_date")) if item.get("board_resolution_date") else None,
            "submission_date": str(item.get("submission_date")) if item.get("submission_date") else None,
            "filing_link_kind": "xbrl_api_link" if item.get("detail_url") else "listing_page_only_no_standalone_filing_url",
            "filing_document_url": item.get("detail_url"),
            "allottee_name_disclosed_in_listing": bool(_api_value(raw, "allotteeName", "nameOfAllottee", "allottee")),
            "allottee_category": item.get("allottee_category"),
            "allottee_count": item.get("allottee_count"),
            "raw_api_fields": raw,
        },
    )


async def _fetch_issue_api_candidates(
    client: Any, category: str, listing_url: str, start: date, end: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    api_url = QIP_API if category == "QIP" else PREF_API
    stage_codes = ISSUE_API_STAGES[category]
    async def fetch_stage(stage: str, index: str):
        request_url = _with_query(
            api_url, index=index,
            from_date=start.strftime("%d-%m-%Y"),
            to_date=end.strftime("%d-%m-%Y"),
        )
        rows, diagnostic = await _fetch_json_rows_diagnostic(
            client, request_url, listing_url, source=category, stage=stage,
        )
        return stage, rows, diagnostic
    results = await asyncio.gather(
        *(fetch_stage(stage, index) for stage, index in stage_codes.items()),
        return_exceptions=True,
    )
    candidates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    errors: list[str] = []
    for result in results:
        if isinstance(result, Exception):
            errors.append(f"{type(result).__name__}: {str(result)[:240]}")
            diagnostics.append({"source": category, "status": "ERROR", "error": errors[-1]})
            continue
        stage, rows, diagnostic = result
        diagnostics.append(diagnostic)
        for raw in rows:
            candidate = _issue_api_candidate(category, raw, stage)
            if candidate:
                candidates.append(candidate)
    unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for item in candidates:
        key = (norm(item.get("symbol")), norm(item.get("stage")),
               norm(item.get("detail_url")), norm(item.get("date")))
        unique[key] = item
    candidates = list(unique.values())
    valid_feeds = len([item for item in diagnostics if item.get("payload_shape") in {"rows", "valid_empty_rows"}])
    api_complete = valid_feeds == len(stage_codes)
    if errors:
        logger.warning(
            "NSE_REGULATORY_API_INCOMPLETE category=%s errors=%s diagnostics=%s",
            category, errors, diagnostics,
        )
    return candidates, diagnostics, api_complete

def _insider_csv_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in rows:
        company = _csv_value(raw, "company name", "company", "security name", "issuer name")
        symbol = _csv_value(raw, "symbol", "nse symbol")
        disclosure = _csv_value(raw, "broadcast date/time", "broadcast date", "date of filing", "filing date", "date")
        detail_url = None
        for key, value in raw.items():
            if any(term in norm(key) for term in ("detail", "xbrl", "document url", "filing url", "attachment", "link")):
                candidate = _valid_nse_url(value, INSIDER_URL)
                if candidate and urlparse(candidate).path.casefold().endswith((".html", ".htm", ".xml", ".xhtml")):
                    detail_url = candidate
                    break
        if company and detail_url:
            output.append({"company": company, "symbol": symbol, "date": disclosure,
                           "detail_url": detail_url, "raw": raw, "listing_source": "csv"})
    return output


async def _csv_rows_from_url(client: Any, url: str, referer: str) -> list[dict[str, Any]]:
    try:
        content, content_type = await client.get_bytes(url, referer=referer, accept="text/csv,text/plain,*/*")
        decoded = content.decode("utf-8-sig", errors="replace")
        if "<html" in decoded[:500].casefold() or "<!doctype" in decoded[:500].casefold():
            return []
        reader = csv.DictReader(io.StringIO(decoded))
        headers = norm(" ".join(reader.fieldnames or []))
        if not any(token in headers for token in ("company", "symbol")):
            return []
        return [dict(row) for row in reader if any(clean_text(value) for value in row.values())]
    except Exception as exc:
        logger.debug("NSE regulatory CSV link unavailable url=%s error=%s", url, str(exc)[:160])
        return []


async def _collect_insider_http(client: Any, start: date, end: date) -> dict[str, Any]:
    listing_url = _date_window_url(INSIDER_URL, start, end)
    diagnostics: dict[str, Any] = {"extraction_method": "NSE PIT JSON API + HTML/XML/iXBRL"}
    candidates: list[dict[str, Any]] = []
    api_url = _with_query(
        PIT_API, index="equities",
        from_date=start.strftime("%d-%m-%Y"), to_date=end.strftime("%d-%m-%Y"),
    )
    api_complete = False
    try:
        raw_rows, api_diagnostic = await _fetch_json_rows_diagnostic(
            client, api_url, INSIDER_URL, source="INSIDER_TRADING",
        )
        diagnostics["pit_api"] = api_diagnostic
        candidates = _insider_api_candidates(raw_rows)
        api_complete = True
    except Exception as exc:
        diagnostics["pit_api_error"] = f"{type(exc).__name__}: {str(exc)[:320]}"
        logger.warning("NSE_INSIDER_PIT_API_FAILED url=%s error=%s", api_url, diagnostics["pit_api_error"])

    # Keep the server-rendered HTML/CSV route as a fallback for NSE feed changes.
    page_diagnostics: dict[str, Any] = {"pages_read": 0, "pagination_complete": False}
    if not candidates:
        try:
            tables, page_diagnostics = await _fetch_listing_pages(client, INSIDER_URL, start, end)
            candidates = _extract_company_candidates(tables, "INSIDER_TRADING", INSIDER_URL)
            if not candidates:
                csv_urls = [url for page in page_diagnostics.get("listing_pages", []) for url in page.get("download_urls", [])]
                for csv_url in csv_urls:
                    candidates.extend(_insider_csv_candidates(await _csv_rows_from_url(client, csv_url, INSIDER_URL)))
                    if candidates:
                        break
        except Exception as exc:
            page_diagnostics["html_fallback_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        diagnostics["html_fallback"] = page_diagnostics

    # RSS is only a final latest-disclosure fallback; it is never considered a
    # complete date-range source. Filter entries to the requested window.
    rss_fallback_used = False
    if not candidates:
        try:
            rss, _ = await client.get_bytes(
                INSIDER_RSS_URL, referer=INSIDER_URL,
                accept="application/rss+xml,application/xml,text/xml,*/*",
            )
            rss_soup = BeautifulSoup(rss, "xml")
            for entry in rss_soup.find_all(["item", "entry"]):
                link = entry.find("link")
                detail_url = _valid_nse_url(
                    (link.get("href") if link and link.get("href") else link.get_text(strip=True) if link else ""),
                    INSIDER_RSS_URL,
                )
                title_node = entry.find("title")
                title = clean_text(title_node.get_text(" ", strip=True) if title_node else "")
                date_node = entry.find("pubDate") or entry.find("updated")
                pubdate = clean_text(date_node.get_text(" ", strip=True) if date_node else "")
                parsed_pubdate = parse_datetime(pubdate)
                if detail_url and title and parsed_pubdate and start <= parsed_pubdate.date() <= end:
                    candidates.append({
                        "company": title, "date": pubdate, "detail_url": detail_url,
                        "raw": {"rss_title": title, "rss_pubdate": pubdate}, "listing_source": "rss",
                    })
            rss_fallback_used = bool(candidates)
        except Exception as exc:
            diagnostics["rss_fallback_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    if not candidates:
        # A valid empty data array is different from an unrecognized response.
        # Return EMPTY only when the actual PIT API response shape was valid.
        if api_complete and diagnostics.get("pit_api", {}).get("payload_shape") == "valid_empty_rows":
            return {
                "status": "EMPTY", "events": [], "discovered": 0,
                "details_ok": 0, "details_failed": 0,
                "diagnostics": diagnostics,
            }
        raise RuntimeError(
            "NSE Insider PIT discovery failed: no usable JSON rows, HTML table rows, CSV rows, or in-range RSS records; "
            f"diagnostics={diagnostics}"
        )

    async def process(item: dict[str, Any]):
        filing_url, link_kind = _source_link_for_candidate(item, INSIDER_URL)
        if not item.get("detail_url"):
            return [_failed_event(
                "INSIDER_TRADING", item, filing_url,
                "NSE PIT JSON row did not expose an ixbrl/Details HTML/XML URL",
            )], False, {"reason": "missing_ixbrl_url"}
        try:
            detail, content_type = await _fetch_document(client, item["detail_url"], INSIDER_URL)
            events, diag = _parse_insider_detail_events(item, item["detail_url"], detail)
            accepted = []
            for event in events:
                event_date = parse_datetime(event.get("event_date"))
                disclosure_date = parse_datetime(event.get("disclosure_date"))
                if (event_date and start <= event_date.date() <= end) or (disclosure_date and start <= disclosure_date.date() <= end):
                    event["source_metadata"].update({
                        "filing_content_type": content_type,
                        "source_link_kind": link_kind,
                        "discovery_source": item.get("listing_source"),
                        "api_submission_type": _api_value(item.get("raw", {}), "typeOfSubmission"),
                        "api_regulation": _api_value(item.get("raw", {}), "regulation"),
                    })
                    accepted.append(event)
            if accepted:
                return accepted, True, diag
            failed = _failed_event(
                "INSIDER_TRADING", item, filing_url,
                "Insider iXBRL document contained no transaction rows in the requested window",
                item.get("date"),
            )
            return [failed], False, diag
        except Exception as exc:
            failed = _failed_event(
                "INSIDER_TRADING", item, filing_url,
                f"{type(exc).__name__}: {str(exc)[:320]}", item.get("date"),
            )
            return [failed], False, None

    results = await asyncio.gather(*(process(item) for item in candidates), return_exceptions=True)
    events: list[dict[str, Any]] = []
    details_ok = details_failed = 0
    for item, result in zip(candidates, results):
        if isinstance(result, Exception):
            details_failed += 1
            failed = _failed_event("INSIDER_TRADING", item, item.get("detail_url") or INSIDER_URL, str(result), item.get("date"))
            if failed:
                events.append(failed)
            continue
        item_events, ok, _diag = result
        events.extend(event for event in item_events if event)
        details_ok += int(ok)
        details_failed += int(not ok)
    pagination_complete = api_complete if diagnostics.get("pit_api") else page_diagnostics.get("pagination_complete", False)
    complete = bool(pagination_complete) and details_failed == 0 and not rss_fallback_used
    diagnostics.update({
        "api_records_discovered": diagnostics.get("pit_api", {}).get("records", 0),
        "candidate_records": len(candidates),
        "detail_pages_parsed": details_ok,
        "detail_pages_failed": details_failed,
        "rss_latest_fallback_used": rss_fallback_used,
        "pagination_complete": bool(pagination_complete),
    })
    return {
        "status": "SUCCESS" if complete else "PARTIAL", "events": events,
        "discovered": len(candidates), "details_ok": details_ok, "details_failed": details_failed,
        "diagnostics": diagnostics,
    }

async def collect_insider(start: date, end: date, client: Any = None) -> dict[str, Any]:
    if client is not None:
        return await _collect_insider_http(client, start, end)
    async with NSEHttpSession() as owned:
        return await _collect_insider_http(owned, start, end)


async def _collect_issue_page(category: str, url: str, start: date, end: date, client: Any = None) -> dict[str, Any]:
    if client is None:
        async with NSEApiClient() as owned:
            return await _collect_issue_page(category, url, start, end, owned)

    candidates, stage_diagnostics, api_complete = await _fetch_issue_api_candidates(
        client, category, url, start, end,
    )
    html_diagnostics: dict[str, Any] = {"pages_read": 0, "pagination_complete": False}
    if not candidates:
        # HTML parsing remains as a fallback if NSE changes the JSON endpoint.
        try:
            listing_tables, html_diagnostics = await _fetch_listing_pages(client, url, start, end)
            candidates = _extract_company_candidates(listing_tables, category, url)
        except Exception as exc:
            html_diagnostics["fallback_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"

    if not candidates:
        valid_stages = [item for item in stage_diagnostics if item.get("payload_shape") in {"rows", "valid_empty_rows"}]
        if api_complete and len(valid_stages) == len(ISSUE_API_STAGES[category]):
            return {
                "status": "EMPTY", "events": [], "discovered": 0,
                "details_ok": 0, "details_failed": 0,
                "diagnostics": {
                    "extraction_method": "NSE further-issues stage JSON APIs",
                    "stage_results": stage_diagnostics,
                    "html_fallback": html_diagnostics,
                    "pagination_complete": True,
                    "valid_empty_api_response": True,
                },
            }
        raise RuntimeError(
            f"NSE {category} discovery failed: neither stage JSON feeds nor HTML fallback contained company records; "
            f"stage_diagnostics={stage_diagnostics}; html_diagnostics={html_diagnostics}"
        )

    async def process(item: dict[str, Any]):
        filing_url, link_kind = _source_link_for_candidate(item, url)
        if not item.get("detail_url"):
            fallback_event = _issue_api_fallback_event(category, item, url)
            if fallback_event:
                fallback_event["source_metadata"].update({
                    "filing_link_kind": link_kind,
                    "discovery_source": item.get("listing_source"),
                })
                return [fallback_event], fallback_event.get("status") == "DETAILED"
            return [_failed_event(category, item, filing_url, "NSE issue API row had no usable HTML/XML/XBRL URL", item.get("date"))], False
        try:
            detail_text, content_type = await _fetch_document(client, item["detail_url"], url)
            parsed = _parse_issue_detail(
                category=category, row=item, popup_html=detail_text,
                page_url=url, company_name=item.get("company") or item.get("symbol") or "Unknown",
                source_url=filing_url,
                source_metadata={
                    "filing_link_kind": link_kind,
                    "filing_document_url": item.get("detail_url"),
                    "filing_content_type": content_type,
                    "discovery_source": item.get("listing_source"),
                    "api_stage": item.get("stage"),
                    "extraction_method": "direct_http_xbrl_html_xml",
                    "api_listing_row": item.get("raw") or {},
                },
            )
            kept = []
            for event in parsed:
                event_dt = parse_datetime(event.get("event_date"))
                disclosure_dt = parse_datetime(event.get("disclosure_date"))
                if (event_dt and start <= event_dt.date() <= end) or (disclosure_dt and start <= disclosure_dt.date() <= end):
                    kept.append(event)
            if kept:
                return kept, all(event.get("status") == "DETAILED" for event in kept)
            # NSE listing JSON contains useful allotment facts even when its XBRL
            # template changes. Preserve those values as PARTIAL rather than losing
            # the entire company disclosure.
            fallback_event = _issue_api_fallback_event(category, item, url)
            if fallback_event:
                fallback_event["source_metadata"].update({
                    "filing_link_kind": link_kind,
                    "filing_document_url": item.get("detail_url"),
                    "filing_content_type": content_type,
                    "xbrl_parser_no_events": True,
                })
                return [fallback_event], False
            raise RuntimeError("XBRL/HTML document parsed but no in-range allotment event was available")
        except Exception as exc:
            fallback_event = _issue_api_fallback_event(category, item, url)
            if fallback_event:
                fallback_event["source_metadata"].update({
                    "filing_link_kind": link_kind,
                    "filing_document_url": item.get("detail_url"),
                    "detail_fetch_error": f"{type(exc).__name__}: {str(exc)[:240]}",
                })
                return [fallback_event], False
            return [_failed_event(category, item, filing_url, f"{type(exc).__name__}: {str(exc)[:320]}", item.get("date"))], False

    results = await asyncio.gather(*(process(item) for item in candidates), return_exceptions=True)
    events: list[dict[str, Any]] = []
    details_ok = details_failed = 0
    for item, result in zip(candidates, results):
        if isinstance(result, Exception):
            details_failed += 1
            failed = _failed_event(category, item, item.get("detail_url") or url, str(result), item.get("date"))
            if failed:
                events.append(failed)
        else:
            item_events, success = result
            events.extend(event for event in item_events if event)
            details_ok += int(success)
            details_failed += int(not success)
    pagination_complete = api_complete or html_diagnostics.get("pagination_complete", False)
    status = "SUCCESS" if pagination_complete and details_failed == 0 else "PARTIAL"
    return {
        "status": status, "events": events, "discovered": len(candidates),
        "details_ok": details_ok, "details_failed": details_failed,
        "diagnostics": {
            "extraction_method": "NSE further-issues JSON APIs + HTTP XBRL/HTML/XML",
            "stage_results": stage_diagnostics,
            "html_fallback": html_diagnostics,
            "candidate_records": len(candidates),
            "detail_pages_parsed": details_ok,
            "detail_pages_failed": details_failed,
            "pagination_complete": bool(pagination_complete),
        },
    }


async def collect_qip(start: date, end: date, client: Any = None) -> dict[str, Any]:
    return await _collect_issue_page("QIP", QIP_URL, start, end, client)


async def collect_preferential_warrants(start: date, end: date, client: Any = None) -> dict[str, Any]:
    return await _collect_issue_page("PREFERENTIAL_WARRANT", PREF_URL, start, end, client)

async def _fetch_deals_range(client: Any, deal_type: str, start: date, end: date, *, depth: int = 0) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Fetch NSE CSV slices without request deadlines; split ranges/capped replies."""
    span_days = (end - start).days + 1
    if span_days > 365:
        midpoint = start + timedelta(days=(end - start).days // 2)
        parent = {"from": start.isoformat(), "to": end.isoformat(), "status": "SPLIT",
                  "reason": "NSE API maximum request window is 365 days", "superseded_by_date_slices": True, "split_depth": depth}
        left, left_diag, left_ok = await _fetch_deals_range(client, deal_type, start, midpoint, depth=depth + 1)
        right, right_diag, right_ok = await _fetch_deals_range(client, deal_type, midpoint + timedelta(days=1), end, depth=depth + 1)
        return left + right, [parent] + left_diag + right_diag, left_ok and right_ok
    url = f"{DEALS_API}?optionType={deal_type}&from={start:%d-%m-%Y}&to={end:%d-%m-%Y}&csv=true"
    content, content_type = await client.get_bytes(url, referer=DEALS_PAGE_URL, accept="text/csv,text/plain,application/json,*/*")
    rows, parse_status = _deal_rows_from_response(content, content_type)
    diagnostics = [{"url": url, "from": start.isoformat(), "to": end.isoformat(), "status": parse_status,
                    "content_type": content_type, "received": len(rows), "split_depth": depth}]
    if len(rows) < 1000:
        return rows, diagnostics, True
    if start >= end:
        diagnostics[0].update({"status": "SATURATED", "error": "1000-row ceiling for a single-day range"})
        return rows, diagnostics, False
    diagnostics[0]["superseded_by_date_slices"] = True
    midpoint = start + timedelta(days=(end - start).days // 2)
    left, left_diag, left_ok = await _fetch_deals_range(client, deal_type, start, midpoint, depth=depth + 1)
    right, right_diag, right_ok = await _fetch_deals_range(client, deal_type, midpoint + timedelta(days=1), end, depth=depth + 1)
    return left + right, diagnostics + left_diag + right_diag, left_ok and right_ok


async def _collect_one_deal_type(client: Any, deal_type: str, subtype: str, start: date, end: date) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int]:
    events: list[dict[str, Any]] = []
    rows, diagnostics, complete = await _fetch_deals_range(client, deal_type, start, end)
    invalid_rows = 0
    seen: set[tuple[str, ...]] = set()
    for raw in rows:
        symbol = _csv_value(raw, "symbol", "scrip code")
        company = _csv_value(raw, "security name", "company name", "company") or symbol
        client_name = _csv_value(raw, "client name", "client")
        raw_action = _csv_value(raw, "buy/sell", "buy sell", "action", "transaction type")
        action = normalize_buy_sell(raw_action) or clean_text(raw_action)
        quantity = _csv_value(raw, "quantity traded", "quantity", "qty")
        price = _csv_value(raw, "trade price", "weighted average price", "price")
        date_raw = _csv_value(raw, "date", "trade date", "date of trade")
        event_date = parse_datetime(date_raw)
        identity = tuple(norm(value) for value in (symbol, date_raw, client_name, raw_action, quantity, price))
        if identity in seen:
            continue
        seen.add(identity)
        in_window = bool(event_date and start <= event_date.date() <= end)
        valid = bool(in_window and symbol and company and client_name and normalize_buy_sell(raw_action)
                     and parse_number(quantity) is not None and parse_number(price) is not None)
        details = {str(key): clean_text(value) for key, value in raw.items() if clean_text(value)}
        if not valid:
            invalid_rows += 1
            if company and event_date:
                failed = normalized_event(
                    exchange="NSE", activity_type="BULK_DEAL", activity_sub_type=subtype,
                    company_name=company, symbol=symbol, isin=_csv_value(raw, "isin"),
                    event_date=event_date, disclosure_date=event_date, person=client_name,
                    action=action, quantity=quantity, price=price, source_url=DEALS_PAGE_URL,
                    status="FAILED", details={"raw_row": details, "failure_reason": "Missing/invalid trade fields"},
                    identity=[subtype, identity, "failed"], source_metadata={"report_page": DEALS_PAGE_URL},
                )
                if failed:
                    events.append(failed)
            continue
        event = normalized_event(
            exchange="NSE", activity_type="BULK_DEAL", activity_sub_type=subtype,
            company_name=company, symbol=symbol, isin=_csv_value(raw, "isin"),
            event_date=event_date, disclosure_date=event_date, person=client_name,
            action=action, quantity=quantity, price=price, source_url=DEALS_PAGE_URL,
            details={"raw_row": details},
            identity=[subtype, symbol, event_date.isoformat(), client_name, action, quantity, price],
            status="DETAILED", source_metadata={"report_page": DEALS_PAGE_URL, "extraction_method": "NSE HTTP CSV API"},
        )
        if event:
            events.append(event)
    return events, [{"type": subtype, **item} for item in diagnostics], complete, invalid_rows


async def collect_bulk_block(start: date, end: date, client: Any = None) -> dict[str, Any]:
    """Fetch Bulk and Block CSV datasets in parallel, then normalize all rows."""
    if client is None:
        async with NSEApiClient() as owned:
            return await collect_bulk_block(start, end, owned)
    specs = (("bulk_deals", "BULK"), ("block_deals", "BLOCK"))
    results = await asyncio.gather(*(
        _collect_one_deal_type(client, deal_type, subtype, start, end)
        for deal_type, subtype in specs
    ), return_exceptions=True)
    events: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    invalid_rows = 0
    incomplete = False
    for (deal_type, subtype), result in zip(specs, results):
        if isinstance(result, Exception):
            incomplete = True
            diagnostics.append({"type": subtype, "status": "FAILED", "error": f"{type(result).__name__}: {str(result)[:300]}"})
            logger.error("NSE_%s_HTTP_CSV_FAILED error=%s", subtype, result)
            continue
        source_events, source_diag, source_complete, invalid = result
        events.extend(source_events)
        diagnostics.extend(source_diag)
        incomplete = incomplete or not source_complete
        invalid_rows += invalid
    by_type = {kind: [item for item in diagnostics if item.get("type") == kind] for kind in ("BULK", "BLOCK")}
    complete = all(items and all(item.get("status") in {"SUCCESS", "EMPTY", "SPLIT"} for item in items if not item.get("superseded_by_date_slices")) for items in by_type.values()) and not incomplete and invalid_rows == 0
    status = "SUCCESS" if complete and events else "EMPTY_UNVERIFIED" if complete else "PARTIAL" if events else "FAILED"
    return {"status": status, "events": events, "diagnostics": diagnostics,
            "discovered": sum(int(item.get("received") or 0) for item in diagnostics if not item.get("superseded_by_date_slices")),
            "details_ok": sum(1 for event in events if event.get("status") != "FAILED"), "details_failed": invalid_rows}


async def collect_all(start: date, end: date) -> dict[str, dict[str, Any]]:
    """Collect four source families simultaneously via one non-browser HTTP session."""
    async with NSEHttpSession(concurrency=20, retries=5) as client:
        jobs = {
            "INSIDER_TRADING": collect_insider(start, end, client),
            "BULK_DEAL": collect_bulk_block(start, end, client),
            "QIP": collect_qip(start, end, client),
            "PREFERENTIAL_WARRANT": collect_preferential_warrants(start, end, client),
        }
        names = list(jobs)
        results = await asyncio.gather(*(jobs[name] for name in names), return_exceptions=True)
    output: dict[str, dict[str, Any]] = {}
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            output[name] = {"status": "FAILED", "events": [], "error": f"{type(result).__name__}: {str(result)[:1500]}"}
            logger.error("REGULATORY_ACTIVITY_SOURCE_FAILED source=%s error=%s", name, result)
        else:
            output[name] = result
        source = output[name]
        logger.info(
            "REGULATORY_ACTIVITY_SOURCE_RESULT source=%s status=%s discovered=%s events=%s details_ok=%s details_failed=%s",
            name, source.get("status"), source.get("discovered"), len(source.get("events") or []),
            source.get("details_ok"), source.get("details_failed"),
        )
    return output
