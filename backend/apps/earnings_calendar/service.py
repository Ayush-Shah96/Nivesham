from __future__ import annotations

import asyncio
import hashlib
import json
import random
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ii_platform.database.models import (
    ApprovalStatus,
    Company,
    EarningsCalendarEvent,
    TargetCompany,
    User,
)

logger = logging.getLogger("earnings-calendar")

# Screener provides separate, purpose-built calendars for upcoming concalls and
# upcoming quarterly results. Keep these as independent sources so a failure or
# login wall on one page does not prevent the other event type from refreshing.
SOURCES = (
    ("Screener Concalls", "https://www.screener.in/concalls/upcoming/", "Concall"),
    ("Screener Results", "https://www.screener.in/upcoming-results/", "Results"),
)
MAX_SCREENER_PAGES = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
LEGAL_WORDS = {"limited", "ltd", "private", "pvt", "company", "co", "inc", "corporation", "corp", "india"}


@dataclass(frozen=True)
class CoverageCompany:
    company_id: int
    name: str
    nse_symbol: str | None
    bse_code: str | None
    analyst_id: int | None
    analyst: str | None
    aliases: tuple[str, ...]


def _norm(value: object) -> str:
    text = str(value or "").casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [token for token in text.split() if token not in LEGAL_WORDS]
    return " ".join(tokens).strip()


def _coverage_aliases(company: Company) -> tuple[str, ...]:
    values = [company.legal_name, company.nse_symbol, company.bse_code]
    if isinstance(company.aliases, list):
        values.extend(company.aliases)
    cleaned = []
    seen = set()
    for value in values:
        normalized = _norm(value)
        if normalized and normalized not in seen and len(normalized) >= 3:
            seen.add(normalized)
            cleaned.append(normalized)
    return tuple(cleaned)


async def _active_coverage(db: AsyncSession) -> list[CoverageCompany]:
    query = (
        select(Company, TargetCompany, User)
        .join(TargetCompany, TargetCompany.company_id == Company.id)
        .outerjoin(User, TargetCompany.analyst_id == User.id)
        .where(
            TargetCompany.active.is_(True),
            TargetCompany.approval_status == ApprovalStatus.APPROVED,
        )
    )
    rows = (await db.execute(query)).all()
    result = []
    for company, target, analyst in rows:
        result.append(CoverageCompany(
            company_id=company.id,
            name=company.legal_name,
            nse_symbol=company.nse_symbol,
            bse_code=company.bse_code,
            analyst_id=target.analyst_id,
            analyst=analyst.username if analyst else None,
            aliases=_coverage_aliases(company),
        ))
    return result


def _match_company(text: str, coverage: list[CoverageCompany]) -> CoverageCompany | None:
    """Return the unique company with the longest whole-token alias match.

    When two covered companies share an equally strong alias, do not randomly bind
    the event to whichever record happened to be first in the SQL result.
    """
    normalized_text = _norm(text)
    if not normalized_text:
        return None
    padded = f" {normalized_text} "
    best_length = 0
    best_ids: dict[int, CoverageCompany] = {}
    for company in coverage:
        for alias in company.aliases:
            if alias and f" {alias} " in padded:
                length = len(alias)
                if length > best_length:
                    best_length = length
                    best_ids = {company.company_id: company}
                elif length == best_length:
                    best_ids[company.company_id] = company
    return next(iter(best_ids.values())) if len(best_ids) == 1 else None


def _parse_date(text: str, today: date) -> date | None:
    text = re.sub(r"\s+", " ", text.strip())
    patterns = (
        (r"\b(\d{4}-\d{2}-\d{2})\b", "%Y-%m-%d"),
        (r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b", None),
        (r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b", None),
        (r"\b([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b", None),
        (r"\b(\d{1,2}\s+[A-Za-z]{3,9})\b", None),
    )
    month_formats = ("%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y")
    for pattern, fmt in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        raw = match.group(1).replace(",", "")
        if fmt:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        for candidate_fmt in month_formats:
            try:
                parsed = datetime.strptime(raw, candidate_fmt).date()
                return parsed
            except ValueError:
                pass
        try:
            parsed = datetime.strptime(f"{raw} {today.year}", "%d %b %Y").date()
            if parsed < today - timedelta(days=180):
                parsed = parsed.replace(year=today.year + 1)
            return parsed
        except ValueError:
            pass
    return None


def _parse_time(text: str) -> str | None:
    """Parse Screener's HH:MM:SS AM/PM cells as well as simpler time strings."""
    value = re.sub(r"\s+", " ", str(text or "").strip()).upper()
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*(AM|PM)\b", value)
    if match:
        hour, minute, _second, meridiem = match.groups()
        try:
            parsed = datetime.strptime(f"{int(hour):02d}:{minute or '00'} {meridiem}", "%I:%M %p")
            return parsed.strftime("%I:%M %p").lstrip("0") + " IST"
        except ValueError:
            return None
    match = re.search(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\b", value)
    if match:
        try:
            parsed = datetime.strptime(f"{int(match.group(1)):02d}:{match.group(2)}", "%H:%M")
            return parsed.strftime("%H:%M") + " IST"
        except ValueError:
            return None
    return None


def _event_type(text: str) -> str | None:
    value = text.casefold()
    if any(term in value for term in ("concall", "con-call", "conference call", "earnings call", "investor call")):
        return "Concall"
    if any(term in value for term in ("result", "results", "reports", "reporting", "earnings", "financial outcome", "quarterly numbers")):
        return "Results"
    return None


def _safe_url(base: str, href: str | None) -> str:
    url = urljoin(base, href or "")
    if url.startswith(("https://", "http://")):
        return url[:1500]
    return base[:1500]


def _parse_page(
    html: str,
    base_url: str,
    source: str,
    coverage: list[CoverageCompany],
    today: date,
    default_event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Parse tables, cards, generic result rows, and JSON-LD event entries.

    Some providers render calendar rows inside divs rather than table rows. The old
    parser silently returned zero rows for those HTTP-200 pages because it only
    inspected tables/articles/lists and a small set of class names.
    """
    soup = BeautifulSoup(html, "lxml")
    candidates: list[tuple[str, str]] = []
    seen_texts: set[tuple[str, str]] = set()

    def add_candidate(text: str, node: Any = None, explicit_url: str | None = None) -> None:
        row_text = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(row_text) < 12 or len(row_text) > 1200:
            return
        # Parent containers can include two listed companies. Never infer which
        # date belongs to which company from a multi-company block.
        normalized = f" {_norm(row_text)} "
        matched_company_ids = {
            company.company_id
            for company in coverage
            if any(f" {alias} " in normalized for alias in company.aliases)
        }
        if len(matched_company_ids) != 1 or _match_company(row_text, coverage) is None:
            return
        # A result-calendar row also needs a plausible date or an explicit pending label.
        if _parse_date(row_text, today) is None and not re.search(
            r"\b(tbc|date pending|pending date|further out)\b", row_text, re.I
        ):
            return
        href = explicit_url
        if not href and node is not None:
            link = node.find("a", href=True) if hasattr(node, "find") else None
            href = link.get("href") if link else None
        url = _safe_url(base_url, href)
        key = (row_text, url)
        if key not in seen_texts:
            seen_texts.add(key)
            candidates.append(key)

    # Conventional structures are fast and preserve source-specific links.
    for row in soup.select("tr, article, li, [class*=event], [class*=result], [class*=company-card], [class*=calendar-item]"):
        add_candidate(" ".join(row.stripped_strings), row)

    # Fallback for providers with table-like cards using generic div/span/section markup.
    # Restrict to small nodes containing a known covered-company alias and date so the
    # page's entire container isn't mistaken for one event.
    for node in soup.find_all(["div", "span", "p", "section", "td", "time"]):
        row_text = " ".join(node.stripped_strings)
        if len(row_text) <= 1200:
            add_candidate(row_text, node)

    # Structured data and common JS application-state blobs can retain rows even
    # when the provider's client-side renderer has not populated visible markup.
    def scalar_strings(value: Any, depth: int = 0) -> list[str]:
        if depth > 3:
            return []
        if isinstance(value, (str, int, float)):
            return [str(value)]
        found: list[str] = []
        if isinstance(value, dict):
            for child in value.values():
                found.extend(scalar_strings(child, depth + 1))
                if len(found) >= 60:
                    break
        elif isinstance(value, list):
            for child in value[:30]:
                found.extend(scalar_strings(child, depth + 1))
                if len(found) >= 60:
                    break
        return found

    def walk_json(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk_json(item)
        elif isinstance(value, dict):
            # Include nested company.name + event.date values by flattening a small
            # object. A JSON-LD Event often nests the company inside organizer/about.
            text_parts = scalar_strings(value)
            if text_parts:
                raw_url = value.get("url")
                url = raw_url if isinstance(raw_url, str) else None
                add_candidate(" ".join(text_parts), None, url)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk_json(child)

    for script in soup.find_all("script"):
        script_type = str(script.get("type") or "").casefold()
        script_id = str(script.get("id") or "").casefold()
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        # Only try JSON / application state blocks; ignore libraries and ordinary JS.
        looks_like_state = (
            "json" in script_type
            or script_id in {"__next_data__", "__nuxt_data__", "initial-state", "app-state"}
            or "resultsCalendar" in raw
            or "resultCalendar" in raw
            or "upcomingResults" in raw
        )
        if not looks_like_state:
            continue
        parsed_json = None
        try:
            parsed_json = json.loads(raw)
        except (ValueError, TypeError):
            # Some apps wrap a JSON object in `window.__STATE__ = ...;`.
            left, right = raw.find("{"), raw.rfind("}")
            if left >= 0 and right > left:
                try:
                    parsed_json = json.loads(raw[left:right + 1])
                except (ValueError, TypeError):
                    parsed_json = None
        if parsed_json is not None:
            walk_json(parsed_json)

    records: list[dict[str, Any]] = []
    for text, url in candidates:
        company = _match_company(text, coverage)
        if company is None:
            continue
        kind = _event_type(text) or default_event_type
        if kind is None:
            continue
        event_date = _parse_date(text, today)
        pending_label = None
        if event_date is None:
            if not re.search(r"\b(tbc|date pending|pending date|further out)\b", text, re.I):
                continue
            pending_label = "Date pending"
        time_value = _parse_time(text) if kind == "Concall" else None
        records.append({
            "company": company,
            "event_date": event_date,
            "pending_label": pending_label,
            "event_type": kind,
            "call_time": time_value,
            "event_title": text[:1000],
            "source": source,
            "source_url": url,
        })

    # One provider can expose the same event through a table row, nested card, and
    # embedded JSON state. Collapse those duplicates before they reach persistence.
    records_by_key: dict[tuple[int, date | None, str, str | None], dict[str, Any]] = {}
    for record in records:
        key = (
            record["company"].company_id,
            record.get("event_date"),
            record["event_type"],
            record.get("pending_label"),
        )
        previous = records_by_key.get(key)
        if previous is None:
            records_by_key[key] = record
            continue
        if previous.get("source_url") == base_url and record.get("source_url") != base_url:
            previous["source_url"] = record["source_url"]
        if record.get("call_time") and not previous.get("call_time"):
            previous["call_time"] = record["call_time"]
        if len(record.get("event_title") or "") > len(previous.get("event_title") or ""):
            previous["event_title"] = record["event_title"]
    return list(records_by_key.values())



def _parse_screener_page(
    html: str,
    base_url: str,
    source: str,
    coverage: list[CoverageCompany],
    today: date,
    default_event_type: str,
) -> tuple[list[dict[str, Any]], int, int]:
    """Parse Screener's tabular calendar rows without relying on broad parent divs.

    Upcoming Concalls currently exposes Company / Date / Time columns. The company
    anchor points directly to the underlying BSE/NSE invite announcement. The results
    calendar is parsed using the same row-first strategy, with its event type supplied
    by the configured source rather than guessed from row copy.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict[str, Any]] = []
    table_rows = soup.select("table tr")
    for row in table_rows:
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            continue
        cell_texts = [re.sub(r"\s+", " ", " ".join(cell.stripped_strings)).strip() for cell in cells]
        row_text = " | ".join(part for part in cell_texts if part)
        if not row_text:
            continue
        company = _match_company(row_text, coverage)
        if company is None:
            continue

        # Screener's concall table is Company | Date | Time. Results pages may have
        # extra columns, so date/time parsing also checks the complete row text.
        event_date = None
        for cell_text in cell_texts[1:]:
            event_date = _parse_date(cell_text, today)
            if event_date:
                break
        if event_date is None:
            event_date = _parse_date(row_text, today)
        if event_date is None:
            if not re.search(r"\b(tbc|date pending|pending date)\b", row_text, re.I):
                continue
            pending_label = "Date pending"
        else:
            pending_label = None

        call_time = None
        if default_event_type == "Concall":
            for cell_text in cell_texts[2:]:
                call_time = _parse_time(cell_text)
                if call_time:
                    break
            if not call_time:
                call_time = _parse_time(row_text)

        company_link = cells[0].find("a", href=True) if cells else None
        href = company_link.get("href") if company_link else None
        source_url = _safe_url(base_url, href)
        records.append({
            "company": company,
            "event_date": event_date,
            "pending_label": pending_label,
            "event_type": default_event_type,
            "call_time": call_time,
            "event_title": f"Upcoming {default_event_type.lower()} — {company.name}" + (f" at {call_time}" if call_time else ""),
            "source": source,
            "source_url": source_url,
        })

    # A Screener login/register redirect is an access failure, not a valid empty
    # results calendar. Never silently report it as a successful zero-row scrape.
    visible = soup.get_text(" ", strip=True).casefold()
    title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
    register_wall = (
        "/register/" in base_url.casefold()
        or ("register" in title and ("create your free account" in visible or "already have an account" in visible))
    )
    if register_wall:
        raise RuntimeError(
            "Screener redirected this calendar to its registration/login page. "
            "Set SCREENER_SESSIONID in the backend environment for an authenticated session."
        )

    # Pagination links on Screener use ?p=2, ?p=3, ... . Return the highest page
    # observed so the caller can walk all currently published upcoming entries.
    max_page = 1
    for link in soup.select("a[href]"):
        href = str(link.get("href") or "")
        query = parse_qs(urlsplit(urljoin(base_url, href)).query)
        for key in ("p", "page"):
            for value in query.get(key, []):
                try:
                    max_page = max(max_page, int(value))
                except (TypeError, ValueError):
                    pass
    return records, len(table_rows), min(max_page, MAX_SCREENER_PAGES)


def _screener_page_url(base_url: str, page: int) -> str:
    if page <= 1:
        return base_url
    parts = urlsplit(base_url)
    query = parse_qs(parts.query)
    # Screener's current concall page uses `p`; retain a page key if the source
    # itself ever advertises that convention, otherwise use `p` for both routes.
    key = "page" if "page" in query and "p" not in query else "p"
    query[key] = [str(page)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))

def _event_key(company_id: int, event_date: date | None, event_type: str) -> str:
    # Cross-source dedup: one result event and one concall per covered company/day.
    date_key = event_date.isoformat() if event_date else "DATE_PENDING"
    raw = f"{company_id}|{date_key}|{event_type.casefold()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _get_page(client: httpx.AsyncClient, url: str) -> str:
    """Fetch one publisher with bounded retries and jitter for temporary throttling."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            # ConnectTimeout is a TCP establishment failure, not a parser failure.
            # Allow a slower handshake and fewer attempts, with the scheduler taking
            # over retries between runs instead of hammering a failing host in-loop.
            response = await client.get(url, timeout=httpx.Timeout(28.0, connect=10.0, pool=10.0))
            if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                response.raise_for_status()
            response.raise_for_status()
            final_url = str(response.url)
            if "screener.in" in urlsplit(final_url).netloc.casefold() and "/register/" in urlsplit(final_url).path.casefold():
                raise RuntimeError(
                    "Screener redirected this calendar to its registration/login page. "
                    "Set SCREENER_SESSIONID in the backend environment for an authenticated session."
                )
            content_type = response.headers.get("content-type", "text/html").casefold()
            if "text/html" not in content_type and "application/xhtml" not in content_type:
                raise RuntimeError(f"Unexpected content type {content_type!r} for {url}")
            return response.text
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_error = exc
            if attempt < 2:
                delay = min(6.0, 1.25 * (2 ** attempt)) + random.uniform(0.0, 0.5)
                logger.warning(
                    "EARNINGS_CALENDAR_SOURCE_RETRY url=%s attempt=%s/3 error=%s status=%s delay_seconds=%.2f",
                    url, attempt + 1, type(exc).__name__,
                    getattr(getattr(exc, "response", None), "status_code", None), delay,
                )
                await asyncio.sleep(delay)
    detail = str(last_error).strip() if last_error else "no response"
    raise RuntimeError(f"{type(last_error).__name__ if last_error else 'FetchError'}: {detail or 'request failed'}") from last_error


async def collect_earnings_calendar(
    db: AsyncSession,
    *,
    now: date | None = None,
    source_names: set[str] | None = None,
) -> dict[str, Any]:
    """Scrape Screener's upcoming concall and results calendars for approved coverage.

    The caller owns the six-day scheduling cadence. Concalls and results are collected
    independently, and a failed/login-gated source does not block the healthy source.
    """
    today = now or date.today()
    coverage = await _active_coverage(db)
    if not coverage:
        return {"status": "skipped", "reason": "no active approved coverage companies", "coverage_companies": 0, "inserted": 0, "updated": 0}

    timeout = httpx.Timeout(28.0, connect=10.0, pool=10.0)
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    # The upcoming-results route currently redirects anonymous visitors to /register/.
    # A locally configured Screener session cookie can be supplied without putting
    # credentials in source control or exposing them in logs.
    screener_cookies = {}
    if os.getenv("SCREENER_SESSIONID"):
        screener_cookies["sessionid"] = os.environ["SCREENER_SESSIONID"].strip()
    if os.getenv("SCREENER_CSRFTOKEN"):
        screener_cookies["csrftoken"] = os.environ["SCREENER_CSRFTOKEN"].strip()
    parsed_records: list[dict[str, Any]] = []
    source_stats: list[dict[str, Any]] = []
    selected_sources = [item for item in SOURCES if source_names is None or item[0] in source_names]
    if source_names is not None and not selected_sources:
        return {"status": "skipped", "reason": "no configured sources selected for retry", "coverage_companies": len(coverage), "inserted": 0, "updated": 0, "unchanged": 0, "source_stats": []}

    async with httpx.AsyncClient(
        headers=headers, cookies=screener_cookies, timeout=timeout,
        limits=limits, follow_redirects=True,
    ) as client:
        async def scrape_one(source: str, url: str, default_type: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            try:
                all_rows: list[dict[str, Any]] = []
                total_table_rows = 0
                max_page = 1
                pages_fetched = 0
                total_html_bytes = 0
                page_title = None
                coverage_aliases_seen = 0
                for page_number in range(1, MAX_SCREENER_PAGES + 1):
                    page_url = _screener_page_url(url, page_number)
                    if page_number > 1:
                        await asyncio.sleep(0.45 + random.uniform(0.0, 0.25))
                    html = await _get_page(client, page_url)
                    page_rows, table_count, discovered_max_page = _parse_screener_page(
                        html, page_url, source, coverage, today, default_type
                    )
                    soup = BeautifulSoup(html, "lxml")
                    visible_text = soup.get_text(" ", strip=True)
                    normalized_visible = f" {_norm(visible_text)} "
                    coverage_aliases_seen = max(
                        coverage_aliases_seen,
                        sum(1 for company in coverage if any(f" {alias} " in normalized_visible for alias in company.aliases)),
                    )
                    page_title = soup.title.get_text(" ", strip=True)[:160] if soup.title else page_title
                    total_table_rows += table_count
                    total_html_bytes += len(html.encode("utf-8", errors="ignore"))
                    pages_fetched += 1
                    all_rows.extend(page_rows)
                    max_page = max(max_page, discovered_max_page)
                    if page_number >= max_page:
                        break

                # Use the legacy generic parser only when the source is not tabular;
                # this protects against Screener changing the results page markup.
                if not all_rows and pages_fetched == 1:
                    html = await _get_page(client, url)
                    all_rows = _parse_page(html, url, source, coverage, today, default_event_type=default_type)

                # Deduplicate repeated table rows/pages by company/date/type.
                rows_by_key: dict[tuple[int, date | None, str], dict[str, Any]] = {}
                for record in all_rows:
                    key = (record["company"].company_id, record.get("event_date"), record["event_type"])
                    previous = rows_by_key.get(key)
                    if previous is None:
                        rows_by_key[key] = record
                    elif record.get("call_time") and not previous.get("call_time"):
                        previous["call_time"] = record["call_time"]
                rows = list(rows_by_key.values())
                parse_suspicious = bool(not rows and coverage_aliases_seen and total_table_rows > 1)
                stat = {
                    "source": source, "status": "ok" if rows else "empty",
                    "rows": len(rows), "pages_fetched": pages_fetched,
                    "max_page_discovered": max_page, "url": url,
                    "html_bytes": total_html_bytes, "title": page_title,
                    "table_rows": total_table_rows,
                    "coverage_aliases_seen": coverage_aliases_seen,
                    "parse_suspicious": parse_suspicious,
                }
                if not rows:
                    logger.warning(
                        "EARNINGS_CALENDAR_SOURCE_EMPTY_PARSE source=%s html_bytes=%s title=%r table_rows=%s coverage_aliases_seen=%s parse_suspicious=%s",
                        source, total_html_bytes, page_title, total_table_rows,
                        coverage_aliases_seen, parse_suspicious,
                    )
                return rows, stat
            except Exception as exc:
                error_text = str(exc).strip() or exc.__class__.__name__
                logger.warning("EARNINGS_CALENDAR_SOURCE_FAILED source=%s url=%s error=%s", source, url, error_text[:300])
                return [], {"source": source, "status": "failed", "rows": 0, "url": url, "error": error_text[:500]}

        outcomes = []
        for index, spec in enumerate(selected_sources):
            if index:
                await asyncio.sleep(0.75 + random.uniform(0.0, 0.35))
            outcomes.append(await scrape_one(*spec))
        for rows, stats in outcomes:
            parsed_records.extend(rows)
            source_stats.append(stats)

    # Discard non-coverage results defensively and coalesce repeated observations.
    unique: dict[str, dict[str, Any]] = {}
    active_ids = {c.company_id for c in coverage}
    for record in parsed_records:
        company = record["company"]
        if company.company_id not in active_ids:
            continue
        key = _event_key(company.company_id, record.get("event_date"), record["event_type"])
        previous = unique.get(key)
        if previous is None:
            record["event_key"] = key
            unique[key] = record
        else:
            # Prefer detailed event links and populated call times over calendar overview links.
            if record.get("call_time") and not previous.get("call_time"):
                previous["call_time"] = record["call_time"]
            if len(record.get("source_url") or "") > len(previous.get("source_url") or ""):
                previous["source_url"] = record["source_url"]
                previous["source"] = record["source"]
            if record.get("event_title") and len(record["event_title"]) > len(previous.get("event_title") or ""):
                previous["event_title"] = record["event_title"]

    inserted = updated = unchanged = 0
    for key, record in unique.items():
        statement = select(EarningsCalendarEvent).where(EarningsCalendarEvent.event_key == key)
        existing = await db.scalar(statement)
        payload = dict(
            event_key=key,
            company_id=record["company"].company_id,
            company_name=record["company"].name,
            event_date=record.get("event_date"),
            pending_label=record.get("pending_label"),
            event_type=record["event_type"],
            call_time=record.get("call_time"),
            event_title=record.get("event_title"),
            source=record["source"],
            source_url=record["source_url"],
        )
        if existing is None:
            db.add(EarningsCalendarEvent(**payload))
            inserted += 1
        else:
            changed = False
            for field, value in payload.items():
                if field == "event_key":
                    continue
                if getattr(existing, field) != value:
                    setattr(existing, field, value)
                    changed = True
            if changed:
                updated += 1
            else:
                unchanged += 1
    # Remove event rows for companies no longer on the active, approved Coverage List.
    if active_ids:
        await db.execute(delete(EarningsCalendarEvent).where(EarningsCalendarEvent.company_id.not_in(active_ids)))
    await db.flush()
    source_failures = sum(1 for item in source_stats if item["status"] == "failed")
    suspicious_empty_sources = sum(1 for item in source_stats if item.get("parse_suspicious"))
    sources_with_rows = sum(1 for item in source_stats if int(item.get("rows") or 0) > 0)
    if unique and not source_failures and not suspicious_empty_sources:
        overall_status = "ok"
    elif unique:
        overall_status = "partial"
    elif source_failures or suspicious_empty_sources:
        overall_status = "failed"
    else:
        overall_status = "empty"
    result = {
        "status": overall_status,
        "source_failures": source_failures,
        "suspicious_empty_sources": suspicious_empty_sources,
        "sources_with_rows": sources_with_rows,
        "schedule_interval_seconds": 518400,
        "coverage_companies": len(coverage),
        "source_stats": source_stats,
        "parsed_source_rows": len(parsed_records),
        "deduplicated_events": len(unique),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "empty_sources": sum(1 for item in source_stats if item["status"] == "empty"),
        "requested_sources": sorted(source_names) if source_names is not None else "all",
        "retained_only_coverage_companies": True,
    }
    logger.info("EARNINGS_CALENDAR_COLLECTION_RESULT %s", result)
    return result
