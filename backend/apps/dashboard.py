from __future__ import annotations

import asyncio
import logging

from datetime import datetime, timedelta, timezone
import re


def _youtube_interview_is_valid(title: object) -> bool:
    text = str(title or "").strip().casefold()
    if not re.search(r"\binterview\b", text):
        return False
    return not any(
        marker in text
        for marker in (
            "stock recommendation", "stocks to buy", "stock to buy",
            "buying opportunity", "top stocks", "best stocks", "multibagger",
            "target price", "buy or sell", "buy/sell", "should you buy",
            "should you sell", "stock picks", "stock pick", "sell the stock",
        )
    )

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from infrastructure.security.permissions import require_role
from ii_platform.database.connection import get_db, safe_rollback
from ii_platform.database.models import (
    Article,
    ArticleCompanyMention,
    AuditLog,
    User,
    UserRole,
    UserSession,
    InsiderSignal,
    CapitalActionSignal,
    Company,
    TargetCompany,
    TrackMode,
    ApprovalStatus,
    MarketMetric,
)
from ii_platform.cache.snapshot import get_snapshot, set_snapshot
from ii_platform.database.session_usage import as_utc, credit_usage_interval, normalise_app_usage, REPORTING_TZ

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Dashboard"])


class ActivityPing(BaseModel):
    app: str = Field(min_length=1, max_length=120)
    action: str = Field(default="view", max_length=120)
    route_path: str | None = Field(default=None, max_length=500)
    visible: bool = True


class DashboardInsights(BaseModel):
    news: str
    regulatory: str = ""
    market: str = ""


def _headline_has_trading_noise(title: object) -> bool:
    text = str(title or "").casefold()
    return any(
        marker in text
        for marker in (
            "stock recommendation", "stocks to buy", "stock to buy",
            "buy or sell", "buy/sell", "target price", "stock pick",
            "stock picks", "sell the stock", "multibagger",
        )
    )


@router.post("/activity", status_code=202)
async def record_activity(
    data: ActivityPing,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    """Record app activity in the existing user_sessions row.

    The JSON app_usage column contains compact per-app and per-day totals. Only
    visible-tab intervals with a nearby heartbeat are credited; gaps over 90s
    are treated as sleep/network loss rather than fabricated usage.
    """
    now = datetime.now(timezone.utc)
    app_name = data.app.strip()[:120] or "Workspace"
    action = data.action.strip().casefold()[:120] or "view"
    route_path = (data.route_path or "")[:500]
    quiet_actions = {"heartbeat", "view", "visibilitychange", "focus", "pagehide", "blur"}

    async def _record():
        session = None
        if user.session_id:
            session = await db.scalar(
                select(UserSession)
                .where(
                    UserSession.session_id == user.session_id,
                    UserSession.user_id == user.id,
                )
                .with_for_update()
            )

        if session is None:
            # The login audit insert is best-effort and may still be pending. Use
            # the signed token's session id as the same durable key when possible.
            import uuid
            new_session_id = str(user.session_id or uuid.uuid4().hex)
            session = UserSession(
                session_id=new_session_id,
                user_id=user.id,
                login_at=min(as_utc(getattr(user, "issued_at", None)) or now, now),
                last_seen_at=now,
                active_seconds=0,
                app_usage={},
            )
            db.add(session)
            await db.flush()

        if session.logout_at is not None:
            return {"status": "closed_session", "tracked": False, "reason": "session_already_logged_out"}

        usage = normalise_app_usage(session.app_usage, session.active_seconds)
        tracking = dict(usage.get("_tracking") or {})
        previous_visible = bool(tracking.get("visible", True))
        previous_app = str(session.last_app or app_name)[:120]
        last_seen = as_utc(session.last_seen_at) or now
        delta = (now - last_seen).total_seconds()
        credited = 0

        # A visibility-change event is sent as soon as a tab is hidden. The short
        # interval before that event is still counted if it was visible. No time
        # after a hidden state or a long browser/network pause is credited.
        if previous_visible and 0 < delta <= 90:
            credited = int(delta)
            credit_usage_interval(usage, previous_app, last_seen, credited)
            if credited:
                session.active_seconds = max(0, int(session.active_seconds or 0)) + credited

        usage["_tracking"] = {
            "visible": bool(data.visible),
            "route_path": route_path,
            "app": app_name,
            "updated_at": now.isoformat(),
        }
        session.app_usage = usage
        if data.visible:
            session.last_seen_at = now
            session.last_app = app_name

        if action not in quiet_actions:
            db.add(AuditLog(
                user_id=user.id,
                action=action,
                resource=app_name,
                resource_id=session.session_id,
                detail={"app": app_name, "route_path": route_path, "session_id": session.session_id},
                created_at=now,
            ))

        await db.commit()
        return {
            "status": "recorded",
            "tracked": True,
            "session_id": session.session_id,
            "seconds_credited": credited,
            "last_seen_at": session.last_seen_at,
        }

    try:
        result = await asyncio.wait_for(_record(), timeout=2.5)
        if result.get("tracked") is False:
            logger.info("APP_USAGE_NOT_TRACKED user_id=%s reason=%s", user.id, result.get("reason"))
        return result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await safe_rollback(db)
        logger.warning("APP_USAGE_TRACK_FAILED user_id=%s action=%s error=%s", user.id, action, type(exc).__name__)
        return {"status": "accepted", "tracked": False, "reason": type(exc).__name__}


@router.get("/dashboard/recent-activity")
async def recent_activity(
    limit: int = 12,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    stmt = (
        select(AuditLog, User)
        .join(User, User.id == AuditLog.user_id)
        .order_by(AuditLog.created_at.desc())
        .limit(min(max(int(limit), 1), 50))
    )
    if user.role != UserRole.ADMIN:
        stmt = stmt.where(AuditLog.user_id == user.id)

    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": log.id,
            "username": actor.username,
            "app": log.resource,
            "action": log.action,
            "timestamp": log.created_at,
        }
        for log, actor in rows
    ]


async def _build_dashboard_summary(db, limit: int = 12, quote_client=None) -> dict:
    """Build the complete dashboard serving snapshot from persisted data only.

    This function performs no external HTTP calls, no scraping, and no AI work.
    It is owned by worker.py and is also safe as a bounded bootstrap fallback.
    """
    limit = min(max(int(limit), 1), 50)

    # -------------------------------
    # News / investment-alert stream
    # -------------------------------
    conditions = [
        Article.processing_status.in_(["ingested", "processed"]),
        or_(
            Article.content_type.is_(None),
            Article.content_type.in_(["NEWS", "INTERVIEW", "POST"]),
        ),
    ]
    stmt = (
        select(Article)
        .where(*conditions)
        .order_by(Article.published_at.desc(), Article.id.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    article_ids = [int(row.id) for row in rows]
    mentions_by_article: dict[int, list[str]] = {}
    if article_ids:
        mention_rows = (await db.execute(
            select(ArticleCompanyMention.article_id, ArticleCompanyMention.mention_name)
            .where(ArticleCompanyMention.article_id.in_(article_ids))
            .order_by(ArticleCompanyMention.id.asc())
        )).all()
        for article_id, mention_name in mention_rows:
            names = mentions_by_article.setdefault(int(article_id), [])
            normalized = str(mention_name).strip()
            if normalized and normalized.casefold() not in {name.casefold() for name in names}:
                names.append(normalized)
    news_payload = []
    for row in rows:
        companies = mentions_by_article.get(int(row.id), [])
        company_name = companies[0] if companies else None
        content_type = str(getattr(row, "content_type", None) or "NEWS").upper()
        is_youtube = bool(getattr(row, "source", "") and "youtube" in str(row.source).casefold())
        is_interview = bool(getattr(row, "is_interview", False)) or content_type == "INTERVIEW"
        title = str(row.title or "").strip()
        if content_type == "INTERVIEW" and is_youtube and "interview" not in title.casefold():
            title = f"Interview: {title}"
        news_payload.append({
            "id": row.id,
            "kind": "NEWS_ALERT",
            "title": title,
            "source": row.source,
            "company": company_name,
            "companies": companies,
            "timestamp": row.published_at.isoformat() if row.published_at else None,
            "summary": row.summary,
            "tone": "violet" if is_interview else "slate",
            "is_interview": is_interview,
            "content_type": content_type,
        })

    # -------------------------------
    # Regulatory insider alerts
    # -------------------------------
    reg_stmt = (
        select(InsiderSignal)
        .order_by(InsiderSignal.transaction_date.desc(), InsiderSignal.id.desc())
        .limit(limit)
    )
    reg_rows = (await db.execute(reg_stmt)).scalars().all()
    regulatory_payload = [
        {
            "id": row.id,
            "kind": "INSIDER_BUYING" if str(row.action or "").upper() not in {"SELL", "DISPOSAL"} else "INSIDER_SELLING",
            "title": f"{row.person_name or 'Insider'} {str(row.action or 'transaction').lower()} {row.stock_name}",
            "company": row.stock_name,
            "investor": row.person_name,
            "action": row.action,
            "quantity": row.quantity,
            "price": row.price,
            "value": row.amount_inr,
            "holding_change": row.traded_percent,
            "timestamp": row.transaction_date.isoformat() if row.transaction_date else None,
            "source_url": row.source_url,
            "tone": "red" if str(row.action or "").upper() in {"SELL", "DISPOSAL"} else "green",
        }
        for row in reg_rows
    ]

    # -------------------------------
    # Capital / ownership alerts
    # -------------------------------
    cap_stmt = (
        select(CapitalActionSignal)
        .order_by(CapitalActionSignal.event_date.desc(), CapitalActionSignal.id.desc())
        .limit(limit)
    )
    cap_rows = (await db.execute(cap_stmt)).scalars().all()
    capital_payload = [
        {
            "id": row.id,
            "kind": str(row.event_type or "CAPITAL_EVENT").upper(),
            "title": row.title or row.action or str(row.event_type or "Capital event"),
            "company": row.stock_name,
            "investor": row.investor_name,
            "action": row.action,
            "quantity": row.quantity,
            "price": row.price,
            "value": row.amount_inr,
            "timestamp": row.event_date.isoformat() if row.event_date else None,
            "source_url": row.source_url,
            "tone": "violet",
        }
        for row in cap_rows
    ]

    # -------------------------------
    # Market metrics — persisted only
    # -------------------------------
    metric_stmt = (
        select(MarketMetric)
        .order_by(MarketMetric.recorded_at.desc(), MarketMetric.id.desc())
        .limit(100)
    )
    metric_rows = (await db.execute(metric_stmt)).scalars().all()
    market = []
    seen_metric_names = set()
    preferred = ("nifty 50", "nifty50", "sensex", "bank nifty", "banknifty", "nifty bank")
    preferred_rows = []
    generic_rows = []
    for row in metric_rows:
        name = str(row.metric_name or "").strip()
        lowered = name.casefold()
        canonical = None
        if "sensex" in lowered:
            canonical = "SENSEX"
        elif "bank nifty" in lowered or "banknifty" in lowered or "nifty bank" in lowered:
            canonical = "BANK NIFTY"
        elif "nifty 50" in lowered or "nifty50" in lowered:
            canonical = "NIFTY 50"
        target = preferred_rows if canonical else generic_rows
        target.append((row, canonical))

    for row, canonical in preferred_rows:
        if canonical in seen_metric_names:
            continue
        seen_metric_names.add(canonical)
        market.append({
            "name": canonical,
            "value": row.metric_value,
            "unit": row.unit,
            "source": row.source_name,
            "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
        })
    if not market:
        seen_metric_names.clear()
        for row, _ in generic_rows:
            name = str(row.metric_name or "").strip()
            key = name.casefold()
            if not name or key in seen_metric_names:
                continue
            seen_metric_names.add(key)
            market.append({
                "name": name,
                "value": row.metric_value,
                "unit": row.unit,
                "source": row.source_name,
                "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
            })
            if len(market) >= 6:
                break

    # -------------------------------
    # Tracked / analyst-owned companies
    # -------------------------------
    target_stmt = (
        select(TargetCompany, Company)
        .join(Company, Company.id == TargetCompany.company_id)
        .where(
            TargetCompany.active.is_(True),
            TargetCompany.approval_status == ApprovalStatus.APPROVED,
        )
        .order_by(Company.legal_name.asc(), TargetCompany.id.asc())
    )
    target_rows = (await db.execute(target_stmt)).all()

    # First recover the most recently worker-captured NSE CMP from InsiderSignal
    # source_data. This adds no network traffic.
    company_ids = [company.id for _target, company in target_rows]
    persisted_quotes: dict[int, tuple[float | None, float | None]] = {}
    if company_ids:
        quote_stmt = (
            select(InsiderSignal.company_id, InsiderSignal.current_price, InsiderSignal.current_price_change_percent, InsiderSignal.transaction_date)
            .where(InsiderSignal.company_id.in_(company_ids))
            .order_by(InsiderSignal.transaction_date.desc(), InsiderSignal.id.desc())
            .limit(min(max(len(company_ids) * 12, 60), 500))
        )
        quote_rows = (await db.execute(quote_stmt)).all()
        for company_id, current_price, change_percent, _transaction_date in quote_rows:
            if int(company_id) in persisted_quotes:
                continue
            if current_price is not None:
                persisted_quotes[int(company_id)] = (current_price, change_percent)

    tracked = []
    for target, company in target_rows:
        cmp_price, cmp_change = persisted_quotes.get(company.id, (None, None))
        tracked.append({
            "company_id": target.id,
            "analyst_id": target.analyst_id,
            "name": company.legal_name,
            "symbol": company.nse_symbol,
            "bse_code": company.bse_code,
            "track_mode": target.track_mode.value if hasattr(target.track_mode, "value") else str(target.track_mode),
            "cmp": cmp_price,
            "change_percent": cmp_change,
            "quote_source": "persisted" if cmp_price is not None else None,
        })

    # Worker-only NSE CMP enrichment. API requests never call the quote client.
    # Fetch the full portfolio through the NSE client in one bulk archive request,
    # then opportunistically use live NSE quotes where the exchange allows them.
    if quote_client is not None and tracked:
        symbols = [row.get("symbol") for row in tracked if row.get("symbol")]
        try:
            quotes = await quote_client.get_equity_quotes(symbols, refresh_live=True)
            for row in tracked:
                symbol = str(row.get("symbol") or "").upper()
                quote = quotes.get(symbol) or {}
                if quote.get("last_price") is not None:
                    row["cmp"] = quote.get("last_price")
                    row["change_percent"] = quote.get("change_percent")
                    row["quote_source"] = quote.get("source") or "NSE"
                    row["quote_trade_date"] = quote.get("trade_date")
                    row["quote_is_live"] = bool(quote.get("is_live"))
        except Exception as exc:
            logger.warning("DASHBOARD_NSE_CMP_REFRESH_FAILED error=%s", type(exc).__name__)

    # Sector research is served by the unified Sector Screener API. The legacy
    # legacy sector dashboard JSON projection has been retired; Sector Screener serves sector research directly.
    market_kpis: list[dict] = []
    market_generated_at = None

    # Combined investment-alert stream, newest first.  The UI can therefore stay
    # useful even when NewsAlert has no rows in the current database snapshot.
    intelligence = sorted(
        [*news_payload, *regulatory_payload, *capital_payload],
        key=lambda item: str(item.get("timestamp") or ""),
        reverse=True,
    )[: max(12, limit)]

    return {
        "news": news_payload,
        "regulatory": regulatory_payload,
        "capital": capital_payload,
        "market": market,
        "market_kpis": market_kpis,
        "market_generated_at": market_generated_at,
        "tracked_companies": tracked,
        "intelligence": intelligence,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def warm_dashboard_snapshot(db, limit: int = 12, quote_client=None) -> dict:
    payload = await _build_dashboard_summary(db, limit=limit, quote_client=quote_client)
    await set_snapshot("dashboard:summary", payload)
    return {"count": len(payload.get("news") or []), "key": "dashboard:summary"}


@router.get("/dashboard/summary")
async def dashboard_summary(
    limit: int = 12,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    snapshot, meta = await get_snapshot("dashboard:summary", allow_stale=True)
    if not snapshot or not isinstance(snapshot, dict):
        logger.warning("DASHBOARD_SNAPSHOT_MISS source=%s; using persisted DB fallback", meta.get("source"))
        try:
            snapshot = await _build_dashboard_summary(db, limit=min(max(int(limit), 1), 50))
            await set_snapshot("dashboard:summary", snapshot)
            meta = {"source": "db-fallback"}
        except Exception as exc:
            logger.warning("DASHBOARD_DB_FALLBACK_FAILED error=%s", type(exc).__name__)
            return {"news": [], "regulatory": [], "capital": [], "market": [], "market_kpis": [], "market_generated_at": None, "tracked_companies": [], "intelligence": [], "generated_at": None}
    if "market_kpis" not in snapshot:
        try:
            rebuilt = await _build_dashboard_summary(db, limit=min(max(int(limit), 1), 50))
            await set_snapshot("dashboard:summary", rebuilt)
            snapshot = rebuilt
        except Exception as exc:
            logger.warning("DASHBOARD_LEGACY_SNAPSHOT_REBUILD_FAILED error=%s", type(exc).__name__)
    return {**snapshot, "news": list(snapshot.get("news") or [])[:min(max(int(limit), 1), 50)], "intelligence": list(snapshot.get("intelligence") or [])[:min(max(int(limit), 1), 50)]}


@router.get("/dashboard/insights", response_model=DashboardInsights)
async def dashboard_insights(
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    """Return a lightweight NewsAlert-only insight without Gemini or other desk reads."""
    conditions = [
        Article.processing_status.in_(["ingested", "processed"]),
    ]
    count = await db.scalar(
        select(func.count()).select_from(Article).where(*conditions)
    )
    count = int(count or 0)

    if count == 1:
        message = "1 NewsAlert record is currently available."
    else:
        message = f"{count} NewsAlert records are currently available."

    return DashboardInsights(
        news=message,
        regulatory="",
        market="",
    )