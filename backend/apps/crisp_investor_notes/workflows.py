from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from apps.crisp_investor_notes.ollama_summarizer import OllamaInvestorSummarizer
from ii_platform.database.models import TranscriptNote
from ii_platform.events.schemas import Event, EventMetadata, EventType
from ii_platform.events.outbox import stage_event


async def build_investor_notes(transcript, gateway=None):
    """Generate Crisp investor notes with the configured local Ollama/Qwen model."""
    return await OllamaInvestorSummarizer().summarize(
        transcript,
        context={"style": "concise investor notes", "include_risks": True},
    )


async def process_audio_event(event: Event, db: AsyncSession, bus):
    p = event.payload
    row = TranscriptNote(
        source_audio_uri=p["source_audio_uri"],
        transcript=p.get("transcript"),
        notes=p.get("notes"),
        diarization=p.get("diarization", []),
    )
    db.add(row)
    await db.flush()
    await stage_event(
        db,
        Event(
            event_type=EventType.NOTES_GENERATED,
            source="crisp-notes",
            payload={"note_id": row.id},
            metadata=EventMetadata(source_system="crisp-notes"),
        ),
    )
    return row
