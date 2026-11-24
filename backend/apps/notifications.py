from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from infrastructure.security.permissions import require_role
from ii_platform.database.connection import get_db
from ii_platform.database.models import Notification, User, UserRole

router = APIRouter(prefix="/notifications", tags=["Notifications"])


class NotificationRead(BaseModel):
    read: bool = True


@router.get("")
async def list_notifications(
    unread_only: bool = False,
    limit: int = 30,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    # Notification materialization is owned by the background worker. Keep this
    # endpoint read-only so opening any page never performs bulk inserts or a
    # Coverage List synchronization.
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    rows = (await db.execute(stmt.order_by(Notification.created_at.desc()).limit(min(max(limit, 1), 100)))).scalars().all()
    return [
        {
            "id": row.id,
            "type": row.notification_type,
            "title": row.title,
            "message": row.message,
            "company_id": row.company_id,
            "article_id": row.article_id,
            "signal_id": row.signal_id,
            "read": row.read_at is not None,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.patch("/{notification_id}")
async def mark_notification(
    notification_id: int,
    data: NotificationRead,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    row = await db.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user.id,
        )
    )
    if not row:
        raise HTTPException(404, "Notification not found")
    row.read_at = datetime.now(timezone.utc) if data.read else None
    await db.commit()
    return {"id": row.id, "read": row.read_at is not None}
