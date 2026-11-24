from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from ii_platform.database.connection import get_sessionmaker
from ii_platform.database.models import EventOutbox
from ii_platform.events.schemas import Event


async def stage_event(db, event: Event) -> EventOutbox:
    row = EventOutbox(
        event_id=event.event_id,
        event_type=event.event_type.value,
        subject=event.event_type.value.lower(),
        payload=event.model_dump(mode="json"),
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


async def publish_outbox_once(
    bus,
    limit: int = 100,
) -> int:
    published = 0

    async with get_sessionmaker()() as db:
        rows = (
            await db.execute(
                select(EventOutbox)
                .where(EventOutbox.status == "pending")
                .order_by(EventOutbox.id)
                .limit(limit)
            )
        ).scalars().all()

        for row in rows:
            try:
                event = Event.model_validate(row.payload)
                await bus.publish(event)
                row.status = "published"
                row.published_at = datetime.now(timezone.utc)
                row.attempts += 1
                row.last_error = None
                published += 1
            except Exception as exc:
                row.attempts += 1
                row.last_error = str(exc)

        await db.commit()

    return published


async def outbox_worker(
    bus,
    stop_event: asyncio.Event,
    interval: float = 1.0,
) -> None:
    while not stop_event.is_set():
        try:
            await publish_outbox_once(bus)
        except Exception:
            # Keep the worker alive; failed rows remain pending for retry.
            pass

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
