from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select

from ii_platform.database.models import Company
from apps.newsalert.workflows import persist_article_item

logger = logging.getLogger(__name__)

BSE_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_ANNOUNCEMENTS_URL = "https://www.bseindia.com/corporates/ann.html"
BSE_ATTACHMENT_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
IST = ZoneInfo("Asia/Kolkata")
MAX_WINDOW_DAYS = 14
DEFAULT_MAX_PAGES = 360


def _text(row: dict[str, Any], *keys: str) -> str:
    lowered = {str(k).casefold(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(key.casefold())
        if value is not None and str(value).strip():
            return " ".join(str(value).split()).strip()
    return ""


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        parsed = None
        for candidate in (text, text.replace("Z", "+00:00")):
            try:
                parsed = datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue
        if parsed is None:
            for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y%m%d", "%d-%m-%Y %H:%M:%S"):
                try:
                    parsed = datetime.strptime(text[:19], fmt)
                    break
                except ValueError:
                    pass
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc)


def _attachment_url(row: dict[str, Any]) -> str | None:
    raw = _text(row, "NSURL", "ATTACHMENTNAME", "ATTACHMENT", "PDFURL")
    if not raw:
        return None
    if raw.startswith(("https://", "http://")):
        host = (urlparse(raw).hostname or "").lower()
        return raw if host.endswith("bseindia.com") else None
    filename = raw.lstrip("/\\")
    if not filename or ".." in filename.split("/"):
        return None
    return BSE_ATTACHMENT_BASE + quote(filename, safe="/%._-()")


async def _get_or_create_company(db, *, bse_code: str, name: str) -> Company | None:
    clean_code = str(bse_code or "").strip()
    clean_name = " ".join(str(name or "").split()).strip()
    if clean_code:
        company = await db.scalar(select(Company).where(Company.bse_code == clean_code))
        if company is not None:
            if clean_name and company.legal_name == clean_code and clean_name != clean_code:
                company.legal_name = clean_name[:255]
            return company
    if clean_name:
        company = await db.scalar(select(Company).where(func.lower(Company.legal_name) == clean_name.casefold()))
        if company is not None:
            if clean_code and not company.bse_code:
                company.bse_code = clean_code[:40]
            return company
        # Avoid making a duplicate name if the case-folded exact match lookup missed
        # because of surrounding spaces or Unicode normalization differences.
        company = await db.scalar(select(Company).where(Company.legal_name.ilike(clean_name[:255])))
        if company is not None:
            if clean_code and not company.bse_code:
                company.bse_code = clean_code[:40]
            return company
    if not clean_name and not clean_code:
        return None
    company = Company(
        legal_name=(clean_name or clean_code)[:255],
        bse_code=clean_code[:40] or None,
        aliases=[],
    )
    db.add(company)
    await db.flush()
    return company


def _announcement_url(row: dict[str, Any]) -> str:
    news_id = _text(row, "NEWSID", "NEWS_ID", "ANN_ID", "ID")
    code = _text(row, "SCRIP_CD", "SCRIPCODE", "SCRIP_CODE")
    if news_id:
        return f"{BSE_ANNOUNCEMENTS_URL}?NEWSID={quote(news_id)}&SCRIP_CD={quote(code)}&type=A"
    attachment = _attachment_url(row)
    if attachment:
        return attachment
    stable = "|".join((_text(row, "SCRIP_CD"), _text(row, "NEWS_DT", "DT_TM"), _text(row, "NEWSSUB", "HEADLINE")))
    return f"{BSE_ANNOUNCEMENTS_URL}?notice={quote(stable)}"


async def ingest_bse_corporate_announcements_all(
    db,
    *,
    history_days: int = 2,
    start_date: date | None = None,
    end_date: date | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """Collect exchange-wide BSE equity announcements into the existing Article store.

    Articles are deduplicated by the existing canonical URL/content-hash upsert. The
    source is intentionally not limited to Coverage List companies; company metadata
    is attached from the existing BSE-code/company master or created when needed.
    """
    if start_date is not None or end_date is not None:
        end = end_date or datetime.now(IST).date()
        days = max(1, min(int(history_days), MAX_WINDOW_DAYS))
        start = start_date or end - timedelta(days=days - 1)
        if end < start:
            raise ValueError("end_date cannot be earlier than start_date")
        if (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise ValueError(f"BSE announcement window cannot exceed {MAX_WINDOW_DAYS} days")
    else:
        days = max(1, min(int(history_days), MAX_WINDOW_DAYS))
        end = datetime.now(IST).date()
        start = end - timedelta(days=days - 1)

    all_rows: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    pages_fetched = 0
    reported_total = 0
    truncated = False
    timeout = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://www.bseindia.com",
        "Referer": BSE_ANNOUNCEMENTS_URL,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/134.0.0.0 Safari/537.36"
        ),
    }
    params_base = {
        "strCat": "-1",
        "subcategory": "-1",
        "strPrevDate": start.strftime("%Y%m%d"),
        "strScrip": "",
        "strSearch": "P",
        "strToDate": end.strftime("%Y%m%d"),
        "strType": "C",
    }

    logger.info("BSE_NEWS_INGEST_START from=%s to=%s max_pages=%s", start, end, max_pages)
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for page in range(1, max(1, int(max_pages)) + 1):
            params = {**params_base, "pageno": page}
            try:
                response = await client.get(BSE_API_URL, params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError(f"unexpected BSE payload type: {type(payload).__name__}")
                rows = payload.get("Table") or payload.get("table") or []
                if not isinstance(rows, list):
                    raise ValueError("BSE response Table field is not a list")
                table1 = payload.get("Table1") or payload.get("table1") or []
                if table1 and isinstance(table1, list) and isinstance(table1[0], dict):
                    try:
                        reported_total = int(table1[0].get("ROWCNT") or table1[0].get("rowcnt") or reported_total or 0)
                    except (TypeError, ValueError):
                        pass
                pages_fetched += 1
                logger.info("BSE_NEWS_API_PAGE page=%s rows=%s total=%s", page, len(rows), reported_total)
                if not rows:
                    break
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    published = _parse_datetime(_text(raw, "NEWS_DT", "DT_TM", "News_submission_dt", "DissemDT"))
                    if published is None or not (start <= published.astimezone(IST).date() <= end):
                        continue
                    subject = _text(raw, "HEADLINE", "NEWSSUB", "SUBJECT", "ANNOUNCEMENT_TYPE")
                    if not subject:
                        continue
                    code = _text(raw, "SCRIP_CD", "SCRIPCODE", "SCRIP_CODE")
                    company_name = _text(raw, "SLONGNAME", "SCRIP_NAME", "COMPANY_NAME", "SCRIP_NAME_LONG") or code
                    link = _announcement_url(raw)
                    news_id = _text(raw, "NEWSID", "NEWS_ID", "ANN_ID", "ID")
                    identity = news_id or "|".join((code, published.isoformat(), subject.casefold(), _text(raw, "ATTACHMENTNAME")))
                    all_rows[identity] = {
                        "raw": raw,
                        "published_at": published,
                        "subject": subject[:1000],
                        "company_name": company_name[:255],
                        "bse_code": code,
                        "url": link,
                        "attachment_url": _attachment_url(raw),
                        "news_id": news_id,
                        "details": _text(raw, "MORE", "DETAILS", "DESCRIPTION", "HEADLINE")[:10000],
                    }
                if reported_total and len(all_rows) >= reported_total:
                    break
                await asyncio.sleep(0.12)
                # Use the row-count metadata when available. An empty final partial
                # page is a valid end-of-feed; don't convert it into a hard error.
                if not reported_total and len(rows) < 10:
                    break
            except Exception as exc:
                errors.append(f"page {page}: {type(exc).__name__}: {exc}")
                logger.warning("BSE_NEWS_API_PAGE_FAILED page=%s error=%s", page, exc)
                break
        else:
            truncated = True

    created = skipped = 0
    await db.rollback()  # End any stale/read transaction before external rows are persisted.
    ordered = sorted(all_rows.values(), key=lambda item: item["published_at"], reverse=True)
    logger.info("BSE_NEWS_PERSIST_START rows=%s", len(ordered))
    for item in ordered:
        company = await _get_or_create_company(
            db, bse_code=item["bse_code"], name=item["company_name"]
        )
        if company is None:
            skipped += 1
            continue
        title = item["subject"]
        details = item["details"]
        row = {
            "url": item["url"],
            "title": title,
            "source": "BSE Corporate Announcements",
            "source_url": item["attachment_url"] or item["url"],
            "published_at": item["published_at"],
            "raw_content": details or title,
            "summary": details or None,
            "content_type": "NEWS",
            "companies_mentioned": [company.legal_name],
            "matched_company_id": company.id,
            "news_alert_eligible": True,
            "is_material": True,
            "is_interview": False,
            "bse_announcement_id": item["news_id"],
            "bse_scrip_code": item["bse_code"],
            "bse_raw_row": item["raw"],
        }
        _, was_created = await persist_article_item(db, row, source_user_id=None)
        created += int(was_created)
        skipped += int(not was_created)
    await db.commit()

    status = "partial" if errors or truncated else "ok"
    result = {
        "status": status,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "pages": pages_fetched,
        "reported_rows": reported_total,
        "unique_rows": len(all_rows),
        "created": created,
        "skipped_or_existing": skipped,
        "errors": errors,
        "truncated": truncated,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "transport": "bse-public-api",
    }
    logger.info("BSE_NEWS_INGEST_DONE %s", result)
    return result
