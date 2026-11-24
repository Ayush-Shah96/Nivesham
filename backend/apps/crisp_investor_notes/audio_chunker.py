from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class AudioChunk:
    start_seconds: float
    end_seconds: float

def chunk_ranges(duration_seconds: float, chunk_seconds: float = 120, overlap_seconds: float = 20) -> list[tuple[float,float]]:
    if duration_seconds <= 0 or chunk_seconds <= 0 or overlap_seconds < 0 or overlap_seconds >= chunk_seconds:
        return []
    step = chunk_seconds - overlap_seconds
    chunks=[]
    start=0.0
    while start < duration_seconds:
        end=min(start+chunk_seconds, duration_seconds)
        chunks.append((start,end))
        if end >= duration_seconds: break
        start += step
    return chunks
