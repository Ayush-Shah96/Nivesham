from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import and_, func, or_, select

from ii_platform.web.collectors.nse_corporate_announcements_collector import clean_investor_name

from ii_platform.database.models import (
    Article, CapitalAction, CapitalActionSignal, Company, InsiderSignal,
    InsiderTransaction, NewsAlertItem, RegulatoryMarketPrice, TargetCompany, User,
    ApprovalStatus, TrackMode,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _norm_url(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.casefold().startswith("utm_") and k.casefold() not in {"fbclid", "gclid", "ref", "source"}]
        return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), urlencode(sorted(query)), ""))
    except Exception:
        return raw


def _dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _window_start(days: int) -> datetime:
    return datetime.combine((_now().date() - timedelta(days=max(1, int(days)) - 1)), datetime.min.time(), tzinfo=timezone.utc)


def _analyst_query(company_id: int):
    return select(User.username).select_from(TargetCompany).join(
        User, and_(User.id == TargetCompany.analyst_id, User.is_active.is_(True))
    ).where(
        TargetCompany.company_id == company_id,
        TargetCompany.active.is_(True),
        TargetCompany.approval_status == ApprovalStatus.APPROVED,
    ).limit(1)


def _track_query(company_id: int):
    return select(TargetCompany.track_mode).where(
        TargetCompany.company_id == company_id,
        TargetCompany.active.is_(True),
        TargetCompany.approval_status == ApprovalStatus.APPROVED,
    ).limit(1)


async def _latest_prices(db, symbols: set[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    latest = select(
        RegulatoryMarketPrice.nse_symbol.label("symbol"),
        func.max(RegulatoryMarketPrice.trade_date).label("day"),
    ).where(RegulatoryMarketPrice.nse_symbol.in_(symbols)).group_by(RegulatoryMarketPrice.nse_symbol).subquery()
    rows = (await db.execute(select(RegulatoryMarketPrice).join(latest, and_(
        RegulatoryMarketPrice.nse_symbol == latest.c.symbol,
        RegulatoryMarketPrice.trade_date == latest.c.day,
    )))).scalars().all()
    return {r.nse_symbol.upper(): {"cmp": r.current_price, "daily_change": r.change_percent,
            "trade_date": r.trade_date.isoformat() if r.trade_date else None} for r in rows}


def _upsert_values(row, values: dict[str, Any]) -> None:
    for key, value in values.items():
        setattr(row, key, value)
    row.last_seen_at = _now()


async def sync_news_alert_items(db, *, days: int = 6) -> dict[str, int]:
    """Legacy Article-to-NewsAlert projection; disabled unless explicitly opted in.

    NewsAlert v2 writes directly to ``news_alert_items``. Leaving this utility
    callable preserves compatibility, but callers cannot overwrite or reintroduce
    legacy items into the active UI table unless the operator explicitly enables
    NEWSALERT_LEGACY_PROJECTION_ENABLED=true.
    """
    enabled = str(os.getenv("NEWSALERT_LEGACY_PROJECTION_ENABLED", "false")).strip().casefold() in {"1", "true", "yes", "on"}
    if not enabled:
        logger.info("PRIMARY_NEWS_SYNC_SKIPPED reason=newsalert-v2-is-canonical")
        return {"scanned": 0, "inserted": 0, "updated": 0, "unchanged": 0, "disabled": 1}
    start = _window_start(days)
    stmt = (
        select(Article, Company, TargetCompany, User)
        .join(Company, Company.id == Article.matched_company_id)
        .outerjoin(TargetCompany, and_(TargetCompany.company_id == Company.id,
            TargetCompany.active.is_(True), TargetCompany.approval_status == ApprovalStatus.APPROVED))
        .outerjoin(User, and_(User.id == TargetCompany.analyst_id, User.is_active.is_(True)))
        .where(
            Article.matched_company_id.is_not(None),
            Article.news_alert_eligible.is_(True),
            Article.published_at >= start,
            Article.processing_status.in_(["ingested", "processed"]),
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
    )
    rows = (await db.execute(stmt)).all()
    inserted = updated = unchanged = 0
    seen: set[str] = set()
    for article, company, target, analyst in rows:
        published = _dt(article.published_at) or _now()
        content_type = str(getattr(article, "content_type", "NEWS") or "NEWS").upper()
        source_lower = str(article.source or "").casefold()
        if content_type not in {"NEWS", "POST", "INTERVIEW"}:
            content_type = "INTERVIEW" if "youtube" in source_lower else "POST" if "x /" in source_lower or "twitter" in source_lower else "NEWS"
        canonical_url = _norm_url(article.url or article.source_url)
        # Use a stable source identity when URLs are missing. Do not fall back to
        # the legacy auto-increment article ID, because the same scraped item may
        # be re-created under another ID by a different source runner.
        stable_title = re.sub(r"\s+", " ", str(article.title or "").casefold()).strip()
        stable_day = published.date().isoformat()
        identity = (
            f"NEWS|{company.id}|{content_type}|{canonical_url}"
            if canonical_url
            else f"NEWS|{company.id}|{str(article.source or '').casefold()}|{content_type}|{stable_day}|{stable_title}"
        )
        key = _hash(identity)
        if key in seen:
            continue
        seen.add(key)
        content_hash = _hash({"headline": article.title, "summary": article.summary,
                              "raw_content": article.raw_content, "source": article.source,
                              "published_at": published.isoformat()})
        metadata = {
            "legacy_article_id": article.id,
            "source_url": article.source_url,
            "is_interview": bool(article.is_interview),
            "is_material": bool(article.is_material),
            "news_alert_eligible": bool(article.news_alert_eligible),
            "track_mode": getattr(target.track_mode, "value", str(target.track_mode)) if target else "Others",
        }
        values = dict(
            company_id=company.id, company_name=company.legal_name, nse_symbol=company.nse_symbol,
            headline=(article.title or "Untitled")[:1000], url=(canonical_url or article.source_url or article.url or "")[:1500],
            source=(article.source or "Unknown")[:160], analyst=analyst.username if analyst else None,
            content_type=content_type, published_at=published, author=None,
            summary=article.summary or (article.raw_content or "")[:4000] or None,
            relevance_score=article.relevance_score, materiality="HIGH" if article.is_material else "MEDIUM",
            relevance_rationale=(article.raw_content or "")[:1000] or None,
            source_item_id=str(article.id), content_hash=content_hash, dedupe_key=key,
            metadata_json=metadata, processing_status="VISIBLE",
        )
        existing = await db.scalar(select(NewsAlertItem).where(NewsAlertItem.dedupe_key == key))
        if existing is None:
            db.add(NewsAlertItem(**values, first_seen_at=_now(), last_seen_at=_now()))
            inserted += 1
        elif existing.content_hash != content_hash or existing.analyst != values["analyst"]:
            _upsert_values(existing, values)
            updated += 1
        else:
            existing.last_seen_at = _now()
            unchanged += 1
    await db.flush()
    result = {"scanned": len(rows), "inserted": inserted, "updated": updated, "unchanged": unchanged}
    logger.info("PRIMARY_NEWS_SYNC %s", result)
    return result


async def sync_insider_transactions(db, *, days: int = 6) -> dict[str, int]:
    """Project recent NSE PIT disclosures and enrich them with stored daily prices.

    NSE's feed window is based on when the disclosure was broadcast, not necessarily
    when the underlying trade occurred. A promoter may report a trade several days
    after it happened, so filter by ``broadcast_at`` and only fall back to the trade
    date for legacy rows that lack a disclosure timestamp.
    """
    start = _window_start(days)
    end = start + timedelta(days=max(1, int(days)))
    rows = (await db.execute(
        select(InsiderSignal, Company)
        .outerjoin(Company, Company.id == InsiderSignal.company_id)
        .where(
            InsiderSignal.source == "NSE",
            or_(
                and_(InsiderSignal.broadcast_at.is_not(None),
                     InsiderSignal.broadcast_at >= start, InsiderSignal.broadcast_at < end),
                and_(InsiderSignal.broadcast_at.is_(None),
                     InsiderSignal.transaction_date >= start, InsiderSignal.transaction_date < end),
            ),
        )
        .order_by(func.coalesce(InsiderSignal.broadcast_at, InsiderSignal.transaction_date).desc(), InsiderSignal.id.desc())
    )).all()
    symbols = {str((company.nse_symbol if company else None) or signal.nse_symbol or "").strip().upper()
               for signal, company in rows}
    prices = await _latest_prices(db, symbols)
    inserted = updated = unchanged = 0
    seen: set[str] = set()
    for signal, company in rows:
        symbol = str((company.nse_symbol if company else None) or signal.nse_symbol or "").strip().upper() or None
        person = (signal.person_name or "Undisclosed person").strip()
        tx_date = _dt(signal.transaction_date) or _now()
        identity = signal.source_key or _hash({
            "symbol": symbol, "person": person.casefold(), "date": tx_date.date().isoformat(),
            "action": (signal.action or "").casefold(), "quantity": signal.quantity,
            "price": signal.price, "post_holding": signal.post_transaction_holding,
            "source_url": _norm_url(signal.source_url),
        })
        key = _hash(f"INSIDER|{identity}")
        if key in seen:
            continue
        seen.add(key)
        quote = prices.get(symbol or "", {})
        cmp = signal.current_price if signal.current_price is not None else quote.get("cmp")
        daily_change = signal.current_price_change_percent if signal.current_price_change_percent is not None else quote.get("daily_change")
        avg = signal.price
        cmp_vs_avg = ((float(cmp) / float(avg)) - 1.0) * 100.0 if cmp not in (None, 0) and avg not in (None, 0) else None
        total_value = signal.amount_inr
        if total_value is None and signal.quantity is not None and avg is not None:
            total_value = float(signal.quantity) * float(avg)
        analyst = await db.scalar(_analyst_query(int(signal.company_id))) if signal.company_id else None
        values = dict(
            legacy_signal_id=signal.id, company_id=signal.company_id,
            stock_name=(signal.stock_name or (company.legal_name if company else symbol or "Unknown"))[:255],
            nse_symbol=symbol, client_name=person[:255], category=signal.client_category,
            action=signal.action, transaction_date=tx_date, broadcast_at=_dt(signal.broadcast_at), quantity=signal.quantity,
            holding_before=signal.holding_before, holding_after=signal.post_transaction_holding,
            holding_change_pct=signal.traded_percent, average_price=avg,
            current_market_price=cmp, cmp_vs_avg_transaction_price_pct=cmp_vs_avg,
            daily_stock_change_pct=daily_change, total_transaction_value=total_value,
            analyst=analyst, source=signal.source or "NSE_PIT", source_url=signal.source_url,
            source_filing_id=signal.nse_pid or signal.nse_did or signal.source_key,
            source_hash=_hash({"action": signal.action, "quantity": signal.quantity, "price": signal.price,
                               "holding": signal.post_transaction_holding, "revision": signal.revision_remarks}),
            dedupe_key=key,
            metadata_json={"legacy_signal_id": signal.id, "regulation": signal.regulation,
                "submission_type": signal.submission_type, "revision_remarks": signal.revision_remarks,
                "broadcast_at": _dt(signal.broadcast_at).isoformat() if signal.broadcast_at else None,
                "period": signal.period, "transaction_mode": signal.transaction_mode,
                "nse_pid": signal.nse_pid, "nse_did": signal.nse_did,
                "source_key": signal.source_key,
                "holding_before_percent": signal.holding_before_percent,
                "current_price_fetched_at": _dt(signal.current_price_fetched_at).isoformat() if signal.current_price_fetched_at else None,
                "market_cap": company.market_cap if company else None},
        )
        existing = await db.scalar(select(InsiderTransaction).where(InsiderTransaction.dedupe_key == key))
        if existing is None:
            existing_by_legacy = await db.scalar(select(InsiderTransaction).where(InsiderTransaction.legacy_signal_id == signal.id))
            existing = existing_by_legacy
        if existing is None:
            db.add(InsiderTransaction(**values, first_seen_at=_now(), last_seen_at=_now()))
            inserted += 1
        elif any(getattr(existing, k) != v for k, v in values.items() if k not in {"legacy_signal_id", "company_id", "metadata_json"}):
            _upsert_values(existing, values)
            updated += 1
        else:
            existing.last_seen_at = _now()
            unchanged += 1
    await db.flush()
    result = {"scanned": len(rows), "inserted": inserted, "updated": updated, "unchanged": unchanged}
    logger.info("PRIMARY_INSIDER_SYNC %s", result)
    return result


async def sync_capital_actions(db, *, days: int = 6) -> dict[str, int]:
    """Project normalized capital-event records into the canonical table."""
    start = _window_start(days)
    rows = (await db.execute(
        select(CapitalActionSignal, Company)
        .outerjoin(Company, Company.id == CapitalActionSignal.company_id)
        .where(CapitalActionSignal.event_date >= start, CapitalActionSignal.source == "NSE")
        .order_by(CapitalActionSignal.event_date.desc(), CapitalActionSignal.id.desc())
    )).all()
    symbols = {str((company.nse_symbol if company else None) or "").strip().upper() for _, company in rows}
    prices = await _latest_prices(db, symbols)
    inserted = updated = unchanged = 0
    seen: set[str] = set()
    for signal, company in rows:
        symbol = str((company.nse_symbol if company else None) or "").strip().upper() or None
        event_date = _dt(signal.event_date)
        identity = signal.source_key or _hash({
            "symbol": symbol, "event": signal.event_type, "date": event_date.date().isoformat() if event_date else None,
            "investor": (signal.investor_name or "").casefold(), "subtype": signal.event_sub_type,
            "quantity": signal.quantity, "price": signal.price, "url": _norm_url(signal.source_url),
        })
        key = _hash(f"CAPITAL|{identity}")
        if key in seen:
            continue
        seen.add(key)
        quote = prices.get(symbol or "", {})
        analyst = await db.scalar(_analyst_query(int(signal.company_id))) if signal.company_id else None
        payload = {
            "legacy_signal_id": signal.id, "investor_category": signal.investor_category,
            "action": signal.action, "pre_holding_pct": signal.pre_holding_pct,
            "post_holding_pct": signal.post_holding_pct, "holding_change_pct": signal.holding_change_pct,
            "threshold_status": signal.threshold_status, "purpose": signal.purpose,
            "severity": signal.severity, "document_url": signal.document_url,
            "document_status": signal.document_status, "document_summary": signal.document_summary,
            "document_sha256": signal.document_sha256, "document_bytes": signal.document_bytes,
            "announcement_subject": signal.announcement_subject, "announcement_details": signal.announcement_details,
            "transaction_status": signal.transaction_status, "classification_status": signal.classification_status,
            "review_reason": signal.review_reason, "isin": signal.isin,
            "event_chain_key": signal.event_chain_key, "promoter_or_pac": signal.promoter_or_pac,
        }
        values = dict(
            legacy_signal_id=signal.id, company_id=signal.company_id,
            event=signal.event_type or "REVIEW", company_name=(signal.stock_name or (company.legal_name if company else "Unknown"))[:255],
            nse_symbol=symbol, investor_allottee=clean_investor_name(signal.investor_name), subtype=signal.event_sub_type,
            shares_warrants=signal.quantity, issue_transaction_price=signal.price,
            value_issue_size=signal.amount_inr, ownership_pct=(signal.holding_change_pct if signal.holding_change_pct is not None else signal.post_holding_pct),
            cmp=quote.get("cmp"), importance=signal.severity, importance_score=signal.importance_score,
            event_date=event_date, details=signal.document_summary or signal.details or signal.announcement_details,
            analyst=analyst, source=signal.source or "NSE_CA", source_url=signal.document_url or signal.source_url,
            source_announcement_id=signal.source_key,
            transaction_status=signal.transaction_status or "REVIEW",
            classification_status=signal.classification_status or "REVIEW", review_reason=signal.review_reason,
            source_hash=_hash({"title": signal.title, "details": signal.details, "document_sha256": signal.document_sha256}),
            dedupe_key=key, metadata_json=payload,
        )
        existing = await db.scalar(select(CapitalAction).where(CapitalAction.dedupe_key == key))
        if existing is None:
            existing = await db.scalar(select(CapitalAction).where(CapitalAction.legacy_signal_id == signal.id))
        if existing is None:
            db.add(CapitalAction(**values, first_seen_at=_now(), last_seen_at=_now()))
            inserted += 1
        elif any(getattr(existing, k) != v for k, v in values.items() if k not in {"legacy_signal_id", "company_id", "metadata_json"}):
            _upsert_values(existing, values)
            updated += 1
        else:
            existing.last_seen_at = _now()
            unchanged += 1
    await db.flush()
    result = {"scanned": len(rows), "inserted": inserted, "updated": updated, "unchanged": unchanged}
    logger.info("PRIMARY_CAPITAL_SYNC %s", result)
    return result


async def sync_primary_tables(db, *, days: int = 6) -> dict[str, Any]:
    news = await sync_news_alert_items(db, days=days)
    insider = await sync_insider_transactions(db, days=days)
    capital = await sync_capital_actions(db, days=days)
    await db.commit()
    summary = {"news_alert_items": news, "insider_transactions": insider, "capital_actions": capital}
    logger.info("PRIMARY_DATA_SYNC_DONE %s", summary)
    return summary