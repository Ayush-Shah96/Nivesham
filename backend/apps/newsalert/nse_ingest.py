from __future__ import annotations

"""Worker-only all-company NSE Corporate Announcements ingestion.

This module deliberately uses the exchange-wide NSE corporate-announcements API
without a symbol parameter. The worker can therefore collect announcements for
companies outside Coverage List, while Coverage List remains only the source of
track/analyst metadata in the UI.
"""

import hashlib
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

from sqlalchemy import select

from infrastructure.config.settings import get_settings
from ii_platform.database.models import ApprovalStatus, Article, Company, TargetCompany, User
from ii_platform.nse import NSEApiClient, NSEApiError
from apps.newsalert.classification import classify_topic, NEWS_TYPES
from apps.newsalert.workflows import persist_article_item

logger = logging.getLogger(__name__)
settings = get_settings()

NSE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
NSE_HOSTS = {"www.nseindia.com", "nseindia.com"}
DEFAULT_LOOKBACK_DAYS = 7
MAX_WINDOW_DAYS = 14
WINDOW_DAYS = 2


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).casefold()).strip()


def _field(row: dict[str, Any], *names: str) -> Any:
    lowered = {str(k).casefold(): v for k, v in row.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if value not in (None, ""):
            return value
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # NSE broadcasts are India local time.
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = _clean(value)
    if not text:
        return None
    formats = (
        "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%B-%Y %H:%M:%S",
        "%d-%B-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%d/%m/%Y",
        "%Y-%m-%d",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _valid_nse_url(value: Any) -> str | None:
    raw = _clean(value).replace("\\/", "/")
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return None
    if (parsed.hostname or "").casefold() not in NSE_HOSTS:
        return None
    return raw


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    candidates: list[tuple[int, int, list[dict[str, Any]]]] = []

    def visit(value: Any, depth: int = 0) -> None:
        if isinstance(value, list):
            rows = [x for x in value if isinstance(x, dict)]
            if rows:
                score = 0
                for row in rows[:50]:
                    keys = {str(k).casefold() for k in row.keys()}
                    if keys & {
                        "symbol", "company name", "companyname", "sm_name", "subject", "desc",
                        "attchmntfile", "attchmnttext", "broadcastdate", "an_dt", "seq_id", "details",
                    }:
                        score += 1
                if score:
                    candidates.append((score * 1000 + min(len(rows), 1000), depth, rows))
            for child in value[:20]:
                visit(child, depth + 1)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child, depth + 1)

    visit(payload)
    if not candidates:
        return []
    candidates.sort(key=lambda x: (x[0], -x[1]), reverse=True)
    return candidates[0][2]


def _announcement_url(symbol: str, attachment: str | None, detail: str | None, xbrl: str | None, announcement_id: str = "") -> str:
    for candidate in (attachment, detail, xbrl):
        valid = _valid_nse_url(candidate)
        if valid:
            return valid
    seq_id = announcement_id or "row"
    return f"{NSE_ANNOUNCEMENTS_URL}?{urlencode({'symbol': symbol, 'tabIndex': 'equity', 'seq_id': seq_id})}"


def _dedupe_key(symbol: str, row: dict[str, Any], event_date: datetime | None, title: str, url: str) -> str:
    announcement_id = _clean(_field(row, "seq_id", "seqId", "id", "newsId", "ANN_ID", "ann_id"))
    if announcement_id:
        return f"NSE|{symbol}|{announcement_id}"
    basis = "|".join((symbol, _norm(title), str(event_date or ""), url))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _target_index(rows: list[tuple[TargetCompany, Company, User | None]]) -> tuple[dict[str, tuple[TargetCompany, Company, User | None]], dict[str, Company]]:
    by_symbol: dict[str, tuple[TargetCompany, Company, User | None]] = {}
    companies: dict[str, Company] = {}
    for target, company, analyst in rows:
        if company.nse_symbol:
            by_symbol[str(company.nse_symbol).strip().upper()] = (target, company, analyst)
        if company.legal_name:
            companies[_norm(company.legal_name)] = company
    return by_symbol, companies


async def _ensure_company(db, *, symbol: str, company_name: str) -> Company:
    clean_symbol = symbol.strip().upper()
    existing = None
    if clean_symbol:
        existing = await db.scalar(select(Company).where(Company.nse_symbol == clean_symbol))
    if existing is not None:
        if company_name and existing.legal_name == clean_symbol and company_name != clean_symbol:
            existing.legal_name = company_name[:255]
        return existing

    if company_name:
        existing = await db.scalar(select(Company).where(Company.legal_name == company_name[:255]))
        if existing is not None:
            if clean_symbol and not existing.nse_symbol:
                existing.nse_symbol = clean_symbol
            return existing

    company = Company(
        legal_name=(company_name or clean_symbol or "Unknown Company")[:255],
        nse_symbol=clean_symbol[:40] or None,
        aliases=[],
    )
    db.add(company)
    await db.flush()
    return company


async def ingest_nse_corporate_announcements_all(
    db,
    *,
    history_days: int = DEFAULT_LOOKBACK_DAYS,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    """Collect NSE announcements into SQL. Explicit historical windows are capped at 14 days."""
    if start_date is not None or end_date is not None:
        end = end_date or datetime.now(timezone.utc).date()
        start = start_date or end - timedelta(days=max(1, min(int(history_days), MAX_WINDOW_DAYS)) - 1)
        if end < start:
            raise ValueError("end_date cannot be earlier than start_date")
        if (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise ValueError(f"NSE announcement window cannot exceed {MAX_WINDOW_DAYS} days")
    else:
        days = max(1, min(int(history_days), MAX_WINDOW_DAYS))
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days - 1)

    # Load track/analyst metadata once. This does NOT constrain ingestion.
    targets = (
        await db.execute(
            select(TargetCompany, Company, User)
            .outerjoin(User, User.id == TargetCompany.analyst_id)
            .join(Company, Company.id == TargetCompany.company_id)
            .where(TargetCompany.active.is_(True), TargetCompany.approval_status == ApprovalStatus.APPROVED)
        )
    ).all()
    target_by_symbol, companies_by_name = _target_index(targets)

    all_rows: dict[str, dict[str, Any]] = {}
    api_errors: list[str] = []
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=WINDOW_DAYS - 1))
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)

    logger.info(
        "NSE_NEWS_INGEST_START from=%s to=%s api_windows=%s", start, end, len(windows)
    )
    fetch_started = time.monotonic()
    async with NSEApiClient() as client:
        for window_index, (window_start, window_end) in enumerate(windows, start=1):
            window_started = time.monotonic()
            logger.info(
                "NSE_NEWS_API_WINDOW_START index=%s/%s from=%s to=%s",
                window_index, len(windows), window_start, window_end,
            )
            try:
                payload = await client.get_json(
                    "/api/corporate-announcements",
                    params={
                        "index": "equities",
                        "from_date": window_start.strftime("%d-%m-%Y"),
                        "to_date": window_end.strftime("%d-%m-%Y"),
                    },
                    referer=NSE_ANNOUNCEMENTS_URL,
                )
            except NSEApiError as exc:
                api_errors.append(f"{window_start.isoformat()}..{window_end.isoformat()}: {exc}")
                logger.warning("NSE_NEWS_BULK_WINDOW_FAILED from=%s to=%s error=%s", window_start, window_end, exc)
                continue

            raw_rows = _extract_rows(payload)
            logger.info(
                "NSE_NEWS_API_WINDOW_DONE index=%s/%s from=%s to=%s rows=%s elapsed_seconds=%.2f",
                window_index, len(windows), window_start, window_end, len(raw_rows),
                time.monotonic() - window_started,
            )
            for raw in raw_rows:
                symbol = _clean(_field(raw, "symbol", "nse_symbol", "securitySymbol")).upper()
                company_name = _clean(_field(raw, "companyName", "company name", "sm_name", "company"))
                subject = _clean(_field(raw, "subject", "desc", "attchmntText", "announcementText"))
                details = _clean(_field(raw, "details", "description", "attchmntFileText", "detail"))
                event_date = _parse_datetime(_field(raw, "broadcastDate", "broadcast_date", "an_dt", "date", "sort_date"))
                if event_date is None:
                    event_date = datetime.combine(window_end, datetime.min.time(), tzinfo=timezone.utc)
                if event_date.date() < start or event_date.date() > end:
                    continue
                if not symbol and not company_name:
                    continue

                attachment = _valid_nse_url(_field(raw, "attchmntFile", "attachment", "attachmentFile", "fileUrl", "attachment_url"))
                detail_url = _valid_nse_url(_field(raw, "detail_url", "detailsUrl", "details_url"))
                xbrl_url = _valid_nse_url(_field(raw, "xbrl", "xbrlUrl"))
                # Resolve this row's identifier before building its fallback URL.
                # It must not depend on `item`, which belongs to the later persistence loop.
                announcement_id = _clean(_field(
                    raw,
                    "seq_id", "seqId", "id", "newsId", "ANN_ID", "ann_id",
                    "announcement_id", "announcementId",
                ))
                url = _announcement_url(symbol, attachment, detail_url, xbrl_url, announcement_id)
                key = _dedupe_key(symbol or _norm(company_name), raw, event_date, subject, url)
                all_rows[key] = {
                    "raw": raw,
                    "symbol": symbol,
                    "company_name": company_name or symbol,
                    "subject": subject or company_name or "NSE Corporate Announcement",
                    "details": details,
                    "event_date": event_date,
                    "attachment_url": attachment,
                    "detail_url": detail_url,
                    "xbrl_url": xbrl_url,
                    "announcement_id": announcement_id,

                    "url": url,
                }

    logger.info(
        "NSE_NEWS_FETCH_DONE windows=%s unique_rows=%s api_errors=%s elapsed_seconds=%.2f",
        len(windows), len(all_rows), len(api_errors), time.monotonic() - fetch_started,
    )
    created = 0
    skipped = 0
    categorized: dict[str, int] = {}
    companies_touched: set[str] = set()
    # Persist every discovered exchange row to the relational article table.
    # The NewsAlert UI queries these records directly; relevance only affects scoring.
    # Keep DB transaction bounded; this is background work, so slower persistence is
    # preferable to holding a connection while NSE/network work happens.
    await db.rollback()

    ordered_items = sorted(all_rows.values(), key=lambda x: x["event_date"], reverse=True)
    logger.info("NSE_NEWS_PERSIST_START unique_rows=%s", len(ordered_items))
    persist_started = time.monotonic()
    for index, item in enumerate(ordered_items, start=1):
        item_started = time.monotonic()
        symbol = str(item["symbol"] or "").upper()
        company_name = str(item["company_name"] or symbol)
        logger.info(
            "NSE_NEWS_PERSIST_ITEM_START index=%s/%s symbol=%s company=%s",
            index, len(ordered_items), symbol or "-", company_name[:100],
        )
        company = target_by_symbol.get(symbol, (None, None, None))[1] if symbol in target_by_symbol else None
        if company is None:
            company = companies_by_name.get(_norm(company_name))
        if company is None:
            company = await _ensure_company(db, symbol=symbol, company_name=company_name)
            companies_by_name[_norm(company_name)] = company
        elif symbol and not company.nse_symbol:
            company.nse_symbol = symbol
        if company.nse_symbol:
            symbol = company.nse_symbol.upper()

        category = classify_topic(
            item["subject"],
            item["details"],
            source_type="NSE Corporate Announcements",
        )
        if category not in NEWS_TYPES:
            category = "NEWS"
        categorized[category] = categorized.get(category, 0) + 1
        companies_touched.add(company.legal_name)

        payload = {
            "nse_source": "corporate-announcements",
            "nse_symbol": symbol,
            "announcement_id": item["announcement_id"],
            "announcement_subject": item["subject"],
            "announcement_details": item["details"],
            "attachment_url": item["attachment_url"],
            "detail_url": item["detail_url"],
            "xbrl_url": item["xbrl_url"],
            "raw_nse_row": item["raw"],
        }
        row = {
            "url": item["url"],
            "title": item["subject"][:1000],
            "source": "NSE Corporate Announcements",
            "published_at": item["event_date"],
            "raw_content": item["details"][:200000],
            "summary": item["details"][:6000] or None,
            "content_type": category,
            "companies_mentioned": [company.legal_name],
            "matched_company_id": company.id,
            "news_alert_eligible": True,
            "is_material": category in {
                "DIVIDEND_ISSUE", "MERGER_DEMERGER", "FINANCIAL_RESULTS",
                "BONUS_STOCK_SPLIT", "GOVERNANCE", "INSIDE_BUYING",
                "SEBI_ORDERS", "CREDIT_DEBT",
            },
            "is_interview": False,
            "nse_announcement_id": payload["announcement_id"],
        }
        # Keep raw_content as readable text. Structured NSE fields are normalized into
        # dedicated columns, and original supporting-document URLs are stored separately.
        row["source_url"] = item.get("attachment_url") or item.get("detail_url") or item.get("xbrl_url") or item.get("url")

        _, was_created = await persist_article_item(db, row, source_user_id=None)
        logger.info(
            "NSE_NEWS_PERSIST_ITEM_DONE index=%s/%s result=%s elapsed_seconds=%.2f",
            index, len(ordered_items), "created" if was_created else "existing",
            time.monotonic() - item_started,
        )
        if was_created:
            created += 1
        else:
            skipped += 1

        if (created + skipped) % 25 == 0:
            logger.info(
                "NSE_NEWS_PERSIST_PROGRESS processed=%s/%s created=%s skipped=%s elapsed_seconds=%.2f",
                created + skipped, len(ordered_items), created, skipped,
                time.monotonic() - persist_started,
            )
            await db.commit()

    await db.commit()
    logger.info(
        "NSE_NEWS_PERSIST_DONE processed=%s created=%s skipped=%s elapsed_seconds=%.2f",
        len(ordered_items), created, skipped, time.monotonic() - persist_started,
    )
    return {
        "status": "degraded" if api_errors and all_rows else ("failed" if api_errors and not all_rows else "ok"),
        "rows": len(all_rows),
        "created": created,
        "skipped": skipped,
        "companies": len(companies_touched),
        "categories": categorized,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "windows": len(windows),
        "source_errors": api_errors,
        "transport": "nse-public-api-bulk",
        # all_rows is keyed by the stable announcement identity, so this count
        # represents unique accepted source records after in-run deduplication.
        "collected_count": len(all_rows),
    }