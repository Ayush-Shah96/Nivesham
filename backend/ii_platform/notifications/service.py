from __future__ import annotations

from datetime import datetime, timedelta, timezone

from types import SimpleNamespace

from sqlalchemy import select

from ii_platform.entity_matching.coverage_matching import match_headline
from typing import Any


from ii_platform.database.models import (
    ApprovalStatus,
    Article,
    Company,
    InsiderSignal,
    Notification,
    TargetCompany,
    TrackMode,
    User,
)


LOOKBACK_HOURS = 24


def _norm(value: str | None) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in (value or "")).split())



async def ensure_recent_ht_notifications(db) -> dict[str, Any]:
    """Materialize recent HT alerts without scanning unrelated companies.

    This function is worker-only. The HTTP notification endpoint is read-only.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    ht_rows = (
        await db.execute(
            select(TargetCompany, Company)
            .join(Company, TargetCompany.company_id == Company.id)
            .where(
                TargetCompany.active.is_(True),
                TargetCompany.approval_status == ApprovalStatus.APPROVED,
                TargetCompany.track_mode == TrackMode.HT,
            )
        )
    ).all()
    if not ht_rows:
        return {"created": 0, "articles": 0, "signals": 0}

    users = (
        await db.execute(
            select(User.id).where(User.is_active.is_(True))
        )
    ).scalars().all()
    if not users:
        return {"created": 0, "articles": 0, "signals": 0}

    ht_company_ids = {int(company.id) for _, company in ht_rows}

    article_rows = (
        await db.execute(
            select(Article, Company)
            .join(Company, Company.id == Article.matched_company_id)
            .where(
                Article.matched_company_id.in_(ht_company_ids),
                Article.processing_status.in_(["ingested", "processed"]),
                Article.published_at >= since,
            )
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(300)
        )
    ).all()

    signal_rows = (
        await db.execute(
            select(InsiderSignal, Company)
            .join(Company, Company.id == InsiderSignal.company_id)
            .where(
                InsiderSignal.company_id.in_(ht_company_ids),
                InsiderSignal.transaction_date >= since,
            )
            .order_by(InsiderSignal.transaction_date.desc(), InsiderSignal.id.desc())
            .limit(300)
        )
    ).all()

    candidate_keys = [
        *(f"ht-news:{article.id}:user:{user_id}" for article, _ in article_rows for user_id in users),
        *(f"ht-signal:{signal.id}:user:{user_id}" for signal, _ in signal_rows for user_id in users),
    ]
    existing: set[str] = set()
    for offset in range(0, len(candidate_keys), 1000):
        chunk = candidate_keys[offset:offset + 1000]
        existing.update(
            (await db.execute(
                select(Notification.dedupe_key).where(Notification.dedupe_key.in_(chunk))
            )).scalars().all()
        )

    created = 0
    now = datetime.now(timezone.utc)

    for article, company in article_rows:
        for user_id in users:
            key = f"ht-news:{article.id}:user:{user_id}"
            if key in existing:
                continue
            db.add(
                Notification(
                    user_id=user_id,
                    notification_type="HT_NEWS",
                    title=f"HT news: {company.legal_name}",
                    message=article.title,
                    company_id=company.id,
                    article_id=article.id,
                    dedupe_key=key,
                    created_at=now,
                )
            )
            existing.add(key)
            created += 1

    for signal, company in signal_rows:
        action = signal.action or "Holding activity"
        qty = (
            f"{signal.quantity:g}"
            if isinstance(signal.quantity, (int, float))
            else str(signal.quantity or "")
        )
        message = f"{signal.person_name} — {action} — quantity {qty}".strip(" —")
        for user_id in users:
            key = f"ht-signal:{signal.id}:user:{user_id}"
            if key in existing:
                continue
            db.add(
                Notification(
                    user_id=user_id,
                    notification_type="HT_HOLDING",
                    title=f"HT holding change: {company.legal_name}",
                    message=message,
                    company_id=company.id,
                    signal_id=signal.id,
                    dedupe_key=key,
                    created_at=now,
                )
            )
            existing.add(key)
            created += 1

    if created:
        await db.flush()
    return {"created": created, "articles": len(article_rows), "signals": len(signal_rows)}
