from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4
from pydantic import BaseModel, Field

class EventType(str,Enum):
    NEWS_INGEST_REQUESTED="NEWS_INGEST_REQUESTED"; NEW_ARTICLE="NEW_ARTICLE"; ARTICLE_PROCESSED="ARTICLE_PROCESSED"; ARTICLE_ALERT="ARTICLE_ALERT"
    NEW_FILING="NEW_FILING"; TRANSACTION_DETECTED="TRANSACTION_DETECTED"; SIGNAL_GENERATED="SIGNAL_GENERATED"
    NEW_MARKET_DATA="NEW_MARKET_DATA"; DATA_NORMALIZED="DATA_NORMALIZED"
    NEW_AUDIO="NEW_AUDIO"; TRANSCRIPTION_COMPLETE="TRANSCRIPTION_COMPLETE"; NOTES_GENERATED="NOTES_GENERATED"
    PROCESSING_FAILED="PROCESSING_FAILED"; PROCESSING_RETRIED="PROCESSING_RETRIED"

class EventMetadata(BaseModel):
    request_id:str=Field(default_factory=lambda:str(uuid4())); trace_id:str=Field(default_factory=lambda:str(uuid4()))
    user_id:str|None=None; source_system:str; retry_count:int=0
    created_at:datetime=Field(default_factory=lambda:datetime.now(timezone.utc))

class Event(BaseModel):
    event_id:str=Field(default_factory=lambda:str(uuid4())); event_type:EventType
    timestamp:datetime=Field(default_factory=lambda:datetime.now(timezone.utc))
    source:str; payload:dict[str,Any]; metadata:EventMetadata
