from __future__ import annotations

import logging

from ii_platform.database.connection import session_scope
from ii_platform.events.schemas import Event, EventType
from apps.newsalert.workflows import process_article_event, process_news_ingest_event
from apps.regulatory_activity.workflows import process_filing_event
from apps.crisp_investor_notes.workflows import process_audio_event

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    def __init__(self, bus):
        self.bus = bus
        self.subscriptions = []
        self.routes = {
            EventType.NEWS_INGEST_REQUESTED: ("newsalert-ingest", process_news_ingest_event),
            EventType.NEW_ARTICLE: ("newsalert", process_article_event),
            EventType.NEW_FILING: ("regulatory", process_filing_event),
            EventType.NEW_AUDIO: ("crisp-notes", process_audio_event),
        }

    async def start(self) -> None:
        for event_type, (name, handler) in self.routes.items():
            async def wrapped(event: Event, _handler=handler, _name=name):
                async with session_scope() as db:
                    try:
                        await _handler(event, db, self.bus)
                    except Exception:
                        logger.exception(
                            "Pipeline handler failed",
                            extra={
                                "application": _name,
                                "event_id": event.event_id,
                            },
                        )
                        raise

            self.subscriptions.append(
                await self.bus.subscribe(
                    event_type,
                    application=name,
                    handler=wrapped,
                )
            )

    async def stop(self) -> None:
        await self.bus.close()
        self.subscriptions.clear()
