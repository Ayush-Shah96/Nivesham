from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ii_platform.database.models import ApprovalStatus, Company, InsiderSignal, TargetCompany, TrackMode
from ii_platform.events.schemas import Event


def _norm(value: str | None) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _allowed_category(value: str | None) -> bool:
    text = _norm(value)
    return bool(text) and "promoter" in text


def _action_key(value: str | None) -> str:
    text = _norm(value)
    if "pledge invocation" in text:
        return "pledge invocation"
    if "release" in text or "revok" in text:
        return "pledge released"
    if "pledge" in text:
        return "pledge"
    if any(x in text for x in ("sale", "sell", "disposal", "disposed")):
        return "sale"
    if any(x in text for x in ("buy", "purchase", "acquisition", "acquired")):
        return "buy"
    return text


async def process_filing_event(event: Event, db: AsyncSession, bus):
    payload = event.payload
    company_id = payload.get("company_id")
    if not company_id:
        return None

    market_cap = float(payload.get("market_cap") or 0)

    if not _allowed_category(payload.get("person_type") or payload.get("client_category")):
        return None

    # Event-driven promoter disclosures obey the same active Coverage List gate
    # as the historical backfill, including PO/HT/LT/NT.
    target = await db.scalar(
        select(TargetCompany).where(
            and_(
                TargetCompany.company_id == company_id,
                TargetCompany.active.is_(True),
                TargetCompany.approval_status == ApprovalStatus.APPROVED,
                TargetCompany.track_mode.in_([TrackMode.PO, TrackMode.HT, TrackMode.LT, TrackMode.NT]),
            )
        )
    )
    if target is None:
        return None

    transaction_date = payload.get("transaction_date") or datetime.now(timezone.utc)
    if isinstance(transaction_date, str):
        transaction_date = datetime.fromisoformat(transaction_date.replace("Z", "+00:00"))

    action = payload.get("action") or payload.get("transaction_type") or "—"
    person_name = payload.get("person_name") or payload.get("acquirer_name") or "—"
    quantity = payload.get("quantity")
    amount = payload.get("amount_inr")

    # Avoid duplicate event rows during retries/re-delivery.
    duplicate = await db.scalar(
        select(InsiderSignal.id).where(
            InsiderSignal.company_id == company_id,
            InsiderSignal.person_name == str(person_name)[:255],
            InsiderSignal.transaction_date == transaction_date,
            InsiderSignal.action == str(action)[:120],
            InsiderSignal.quantity == quantity,
            InsiderSignal.amount_inr == amount,
        ).limit(1)
    )
    if duplicate:
        return None

    company = await db.scalar(select(Company).where(Company.id == company_id))
    if company is None:
        return None

    signal = InsiderSignal(
        company_id=company_id,
        stock_name=payload.get("stock_name") or company.legal_name,
        person_name=str(person_name)[:255],
        client_category=str(payload.get("client_category") or payload.get("person_type") or "—")[:120],
        action=str(action)[:120],
        transaction_date=transaction_date,
        quantity=quantity,
        post_transaction_holding=payload.get("post_transaction_holding"),
        traded_percent=payload.get("traded_percent"),
        price=payload.get("price"),
        amount_inr=amount,
        period=payload.get("period") or "",
        transaction_mode=payload.get("transaction_mode") or payload.get("acq_mode") or "",
        market_cap=market_cap,
    )
    db.add(signal)
    await db.flush()
    return signal
