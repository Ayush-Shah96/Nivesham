from __future__ import annotations

import asyncio
import logging
import json
import os
import shutil
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from starlette.datastructures import UploadFile as StarletteUploadFile, Headers

from infrastructure.config.settings import get_settings
from infrastructure.security.auth import get_current_user
from apps.crisp_investor_notes.ollama_summarizer import OllamaInvestorSummarizer
from ii_platform.audio.deepgram_transcriber import DeepgramTranscriber
from ii_platform.database.connection import get_db
from ii_platform.database.models import TranscriptNote, User, UserRole
from ii_platform.storage.factory import get_object_storage

router = APIRouter(prefix="/crisp-notes", tags=["Crisp Investor Notes"])


log = logging.getLogger("crisp-notes")
log.setLevel(logging.INFO)
# Keep Crisp Notes diagnostics visible even when the application's root logger
# is configured at WARNING. Avoid adding duplicate handlers on module reload.
if not any(getattr(handler, "_crisp_notes_handler", False) for handler in log.handlers):
    _crisp_handler = logging.StreamHandler()
    _crisp_handler.setLevel(logging.INFO)
    _crisp_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _crisp_handler._crisp_notes_handler = True
    log.addHandler(_crisp_handler)
log.propagate = False




TITLE_MAX_LENGTH = 255
# Keep each request comfortably below Microsoft Dev Tunnels' 16 MiB forwarding cap.
CHUNK_SIZE = 8 * 1024 * 1024
UPLOAD_SESSION_TTL_SECONDS = 24 * 60 * 60
UPLOAD_SESSION_ROOT = Path(tempfile.gettempdir()) / "crisp_notes_chunk_uploads"

# Audio goes to Deepgram in its original container. Video containers are accepted too;
# their audio track is extracted to WAV in-process with PyAV (no ffmpeg.exe).
_ALLOWED = {
    ".mp3": "audio/mpeg", ".mp2": "audio/mpeg", ".mpga": "audio/mpeg",
    ".m4a": "audio/mp4", ".wav": "audio/wav", ".wave": "audio/wav",
    ".webm": "audio/webm", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/opus", ".flac": "audio/flac", ".aac": "audio/aac",
    ".amr": "audio/amr", ".aif": "audio/aiff", ".aiff": "audio/aiff",
    ".caf": "audio/x-caf", ".wma": "audio/x-ms-wma",
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".mpeg": "video/mpeg", ".mpg": "video/mpeg", ".wmv": "video/x-ms-wmv",
    ".3gp": "video/3gpp",
}
_VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".mpeg", ".mpg", ".wmv", ".3gp",
}


_SUMMARY_CONTEXT: dict[str, Any] = {
    "style": "concise finance and investment research meeting notes",
    "include_risks": True,
    "include_management_commentary": True,
    "source": "finance_related_meeting_or_recording",
    "multilingual_source": True,
    "summary_language": "English only (translate non-English speech faithfully into English)",
}


class RenameRequest(BaseModel):
    title: str | None = Field(default=None, max_length=TITLE_MAX_LENGTH)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=5000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=3000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)


def _clean_title(value: str | None) -> str | None:
    """Collapse whitespace; blank means 'no custom name'."""
    if value is None:
        return None
    cleaned = " ".join(str(value).split()).strip()
    if len(cleaned) > TITLE_MAX_LENGTH:
        raise HTTPException(status_code=422, detail=f"Title must be {TITLE_MAX_LENGTH} characters or fewer")
    return cleaned or None


def _summary_status(note: TranscriptNote) -> str:
    return "ready" if note.notes and note.notes.strip() else "not_requested"


def _download_filename(note: TranscriptNote) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", note.title or "").strip("-.")[:80]
    return f"{slug}-transcript.txt" if slug else f"crisp-note-{note.id}-transcript.txt"


async def _get_accessible_note(db, note_id: int, user: User) -> TranscriptNote | None:
    """Return a note owned by this user, or a legacy pre-ownership note.

    New notes are private to their creator. Rows created before user ownership
    was introduced have user_id=NULL and remain visible for backward compatibility.
    """
    result = await db.execute(
        select(TranscriptNote).where(
            TranscriptNote.id == note_id,
            or_(TranscriptNote.user_id == user.id, TranscriptNote.user_id.is_(None)),
        )
    )
    return result.scalar_one_or_none()


async def _run_summary(transcript: str, diagnostic_id: str | None = None) -> tuple[str, str]:
    """Return an English summary and the local Qwen model name, with timings."""
    started = time.perf_counter()
    summarizer = OllamaInvestorSummarizer()
    log.info(
        "CRISP_SUMMARY_PIPELINE_START request_id=%s model=%s transcript_chars=%s base_url=%s",
        diagnostic_id or "-", summarizer.model, len(transcript or ""), summarizer.base_url,
    )
    try:
        text = await summarizer.summarize(transcript, context=dict(_SUMMARY_CONTEXT), diagnostic_id=diagnostic_id)
    except Exception:
        log.exception(
            "CRISP_SUMMARY_PIPELINE_FAILED request_id=%s model=%s transcript_chars=%s elapsed=%.2fs",
            diagnostic_id or "-", summarizer.model, len(transcript or ""), time.perf_counter() - started,
        )
        raise
    log.info(
        "CRISP_SUMMARY_PIPELINE_DONE request_id=%s model=%s output_chars=%s elapsed=%.2fs",
        diagnostic_id or "-", summarizer.model, len(text or ""), time.perf_counter() - started,
    )
    return text, summarizer.model


def _extract_video_audio(source: Path, destination: Path) -> None:
    """Decode a video's first audio stream to mono 16 kHz PCM WAV using bundled PyAV libs."""
    try:
        import av
    except ImportError as exc:
        raise RuntimeError(
            "Video uploads require PyAV. Install the backend requirements; no separate ffmpeg.exe is needed."
        ) from exc

    import wave

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with av.open(str(source), mode="r") as container:
            audio_stream = next((stream for stream in container.streams if stream.type == "audio"), None)
            if audio_stream is None:
                raise ValueError("This video does not contain an audio track")

            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            with wave.open(str(destination), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                frames_written = 0

                def write_resampled(frames) -> None:
                    nonlocal frames_written
                    for output_frame in frames or []:
                        pcm = output_frame.to_ndarray().tobytes()
                        if pcm:
                            wav_file.writeframesraw(pcm)
                            frames_written += len(pcm)

                for decoded_frame in container.decode(audio_stream):
                    write_resampled(resampler.resample(decoded_frame))
                write_resampled(resampler.resample(None))

            if frames_written <= 0:
                raise ValueError("The video audio track could not be decoded")
    except Exception as exc:
        destination.unlink(missing_ok=True)
        message = str(exc).strip() or exc.__class__.__name__
        if isinstance(exc, (ValueError, RuntimeError)):
            raise
        raise RuntimeError(f"Could not extract audio from this video: {message}") from exc


def _timestamp_label(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _transcript_segments(note: TranscriptNote) -> list[str]:
    segments: list[str] = []
    diarization = note.diarization if isinstance(note.diarization, list) else []
    for item in diarization:
        if not isinstance(item, dict):
            continue
        text = str(item.get("transcript") or "").strip()
        if not text:
            continue
        start = _timestamp_label(item.get("start"))
        end = _timestamp_label(item.get("end"))
        speaker = item.get("speaker")
        prefix = f"[{start}" + (f"–{end}" if end else "") + "] " if start else ""
        if speaker is not None:
            prefix += f"Speaker {speaker}: "
        segments.append(prefix + text)

    if segments:
        return segments

    transcript = str(note.transcript or "").strip()
    if not transcript:
        return []
    # Deepgram can return a transcript without utterance boundaries. Split it into
    # readable passages, but do not fabricate timestamps.
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", transcript) if part.strip()]
    pieces: list[str] = []
    for paragraph in paragraphs or [transcript]:
        while len(paragraph) > 1500:
            boundary = max(paragraph.rfind(". ", 0, 1500), paragraph.rfind("।", 0, 1500))
            boundary = boundary + 1 if boundary > 500 else 1500
            pieces.append(paragraph[:boundary].strip())
            paragraph = paragraph[boundary:].strip()
        if paragraph:
            pieces.append(paragraph)
    return pieces


def _retrieve_transcript_evidence(note: TranscriptNote, query: str) -> str:
    """Small lexical retrieval keeps long recordings inside the local model context."""
    segments = _transcript_segments(note)
    if not segments:
        return ""
    joined = "\n\n".join(segments)
    if len(joined) <= 18000:
        return joined

    stop_words = {
        "about", "after", "again", "also", "and", "are", "because", "been", "being",
        "between", "could", "does", "from", "have", "into", "just", "more", "most",
        "other", "should", "some", "such", "than", "that", "their", "them", "then",
        "there", "these", "they", "this", "those", "through", "what", "when", "where",
        "which", "with", "would", "your", "का", "की", "के", "को", "से", "में", "और",
        "है", "हैं", "था", "थी", "थे", "क्या", "कैसे", "क्यों", "पर", "एक", "यह", "वह",
    }
    terms = [
        term for term in re.findall(r"[a-zA-Z0-9_]+|[\u0900-\u097f]+", query.lower())
        if len(term) > 1 and term not in stop_words
    ]
    if not terms:
        return "\n\n".join(segments[:8])[:15000]

    ranked: list[tuple[float, int]] = []
    for index, segment in enumerate(segments):
        lower = segment.lower()
        score = sum(min(4, lower.count(term)) * (1.0 + min(len(term), 12) / 12) for term in terms)
        if score:
            ranked.append((score / max(1.0, len(segment) ** 0.25), index))
    if not ranked:
        return "\n\n".join(segments[:5] + segments[-3:])[:15000]

    chosen: set[int] = set()
    for _, index in sorted(ranked, reverse=True)[:9]:
        chosen.update(i for i in (index - 1, index, index + 1) if 0 <= i < len(segments))
    excerpt = "\n\n".join(segments[index] for index in sorted(chosen))
    return excerpt[:15000]


@router.get("/health")
async def health(user: User = Depends(get_current_user)):
    settings = get_settings()
    return {
        "status": "ok",
        "workflow": "crisp_investor_notes",
        "transcription": "deepgram_nova3_multilingual",
        "transcription_language": settings.deepgram_language,
        "summarization": "ollama",
        "chat": "ollama_transcript_grounded",
        "ollama_model": settings.ollama_model,
        "ollama_base_url": settings.ollama_base_url,
        "deepgram_model": settings.deepgram_model,
        "deepgram_configured": bool(settings.deepgram_api_key),
        "deepgram_timeout_seconds": settings.deepgram_timeout_seconds,
    }


@router.get("")
async def list_notes(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db=Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return compact metadata; transcript/diarization are loaded on demand."""
    rows = (
        await db.execute(
            select(
                TranscriptNote.id,
                TranscriptNote.title,
                TranscriptNote.source_audio_uri,
                TranscriptNote.notes,
                TranscriptNote.created_at,
            )
            .where(or_(TranscriptNote.user_id == user.id, TranscriptNote.user_id.is_(None)))
            .order_by(TranscriptNote.created_at.desc(), TranscriptNote.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [{
        "id": note_id,
        "title": title,
        "source_audio_uri": source_audio_uri,
        "created_at": created_at,
        "summary_status": "ready" if notes and notes.strip() else "not_requested",
        "transcript_txt_url": f"/api/v1/crisp-notes/{note_id}/transcript.txt",
    } for note_id, title, source_audio_uri, notes, created_at in rows]


@router.get("/{note_id}")
async def get_note(
    note_id: int,
    db=Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return one full transcript/summary only when the user opens a note."""
    note = await _get_accessible_note(db, note_id, user)
    if note is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return {
        "id": note.id,
        "title": note.title,
        "source_audio_uri": note.source_audio_uri,
        "transcript": note.transcript,
        "notes": note.notes,
        "diarization": note.diarization,
        "created_at": note.created_at,
        "transcript_txt_url": f"/api/v1/crisp-notes/{note.id}/transcript.txt",
        "summary_status": _summary_status(note),
    }


@router.patch("/{note_id}")
async def rename_note(
    note_id: int,
    body: RenameRequest,
    db=Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Save a user-chosen name for a transcript. An empty name clears it."""
    note = await _get_accessible_note(db, note_id, user)
    if note is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    note.title = _clean_title(body.title)
    await db.commit()
    await db.refresh(note)
    return {
        "id": note.id,
        "title": note.title,
        "source_audio_uri": note.source_audio_uri,
        "created_at": note.created_at,
        "summary_status": _summary_status(note),
    }


@router.get("/{note_id}/transcript.txt", response_class=PlainTextResponse)
async def download_transcript(
    note_id: int,
    db=Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return the saved transcript as a real text-file download.

    The transcript is served from the database record created immediately after
    Deepgram finishes. Local Qwen is never invoked by this endpoint.
    """
    note = await _get_accessible_note(db, note_id, user)
    if note is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    transcript = (note.transcript or "").strip()
    if not transcript:
        raise HTTPException(status_code=422, detail="This recording has no transcript")

    response = PlainTextResponse(transcript + "\n", media_type="text/plain; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{_download_filename(note)}"'
    response.headers["Cache-Control"] = "private, no-store"
    return response




class ChunkedUploadInit(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    file_size: int = Field(gt=0)
    content_type: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=TITLE_MAX_LENGTH)
    summarize: bool = False


def _chunk_session_dir(upload_id: str, user_id: int) -> Path:
    """Resolve a session directory using the same UUID format used at init.

    Init creates directories with ``uuid4().hex`` (32 hexadecimal characters).
    UUID.__str__ inserts hyphens, so use UUID.hex here to preserve the exact
    directory name across init, chunk, complete, and cancel requests.
    """
    try:
        safe_id = UUID(str(upload_id)).hex
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=404, detail="Upload session not found") from exc
    return UPLOAD_SESSION_ROOT / str(user_id) / safe_id


def _read_upload_metadata(session_dir: Path, user_id: int) -> dict[str, Any]:
    metadata_path = session_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Upload session not found or expired")
    if metadata.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Upload session not found")
    if time.time() - float(metadata.get("created_at", 0)) > UPLOAD_SESSION_TTL_SECONDS:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(status_code=410, detail="Upload session expired. Please upload again.")
    return metadata


def _cleanup_expired_upload_sessions() -> None:
    cutoff = time.time() - UPLOAD_SESSION_TTL_SECONDS
    if not UPLOAD_SESSION_ROOT.exists():
        return
    for metadata_path in UPLOAD_SESSION_ROOT.glob("*/*/metadata.json"):
        try:
            if metadata_path.stat().st_mtime < cutoff:
                shutil.rmtree(metadata_path.parent, ignore_errors=True)
        except OSError:
            continue


@router.post("/upload/chunked/init")
async def init_chunked_upload(
    body: ChunkedUploadInit,
    user=Depends(get_current_user),
):
    """Create a user-bound temporary upload session for small tunnel-safe chunks."""
    suffix = Path(body.filename).suffix.lower()
    if suffix not in _ALLOWED:
        raise HTTPException(status_code=400, detail=f"Unsupported media type '{suffix or 'unknown'}'. Supported audio/video extensions: {', '.join(sorted(_ALLOWED))}.")
    _cleanup_expired_upload_sessions()
    upload_id = uuid4().hex
    session_dir = _chunk_session_dir(upload_id, user.id)
    session_dir.mkdir(parents=True, exist_ok=False)
    total_chunks = (body.file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    metadata = {
        "upload_id": upload_id,
        "user_id": int(user.id),
        "filename": Path(body.filename).name,
        "file_size": body.file_size,
        "content_type": body.content_type,
        "title": _clean_title(body.title),
        "summarize": bool(body.summarize),
        "total_chunks": total_chunks,
        "chunk_size": CHUNK_SIZE,
        "created_at": time.time(),
    }
    (session_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    log.info("CRISP_CHUNKED_UPLOAD_INIT upload_id=%s user_id=%s bytes=%s chunks=%s", upload_id, user.id, body.file_size, total_chunks)
    return {"upload_id": upload_id, "chunk_size": CHUNK_SIZE, "total_chunks": total_chunks}


@router.put("/upload/chunked/{upload_id}/chunks/{chunk_index}")
async def put_upload_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request,
    user=Depends(get_current_user),
):
    """Receive one raw binary chunk; no multipart overhead and no whole-file body."""
    chunk_started = time.perf_counter()
    session_dir = _chunk_session_dir(upload_id, user.id)
    metadata = _read_upload_metadata(session_dir, user.id)
    total_chunks = int(metadata["total_chunks"])
    if chunk_index < 0 or chunk_index >= total_chunks:
        raise HTTPException(status_code=400, detail="Invalid chunk index")
    expected_size = min(int(metadata["chunk_size"]), int(metadata["file_size"]) - chunk_index * int(metadata["chunk_size"]))
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > CHUNK_SIZE:
        raise HTTPException(status_code=413, detail="Chunk exceeds the 8 MiB upload chunk limit")
    payload = await request.body()
    if len(payload) != expected_size:
        raise HTTPException(status_code=400, detail=f"Chunk {chunk_index} has {len(payload)} bytes; expected {expected_size}")
    target = session_dir / f"chunk-{chunk_index:08d}.part"
    temp_target = session_dir / f"chunk-{chunk_index:08d}.tmp"
    temp_target.write_bytes(payload)
    os.replace(temp_target, target)
    log.info("CRISP_UPLOAD_CHUNK_RECEIVED upload_id=%s chunk=%s/%s bytes=%s elapsed_ms=%.1f", upload_id, chunk_index + 1, total_chunks, len(payload), (time.perf_counter() - chunk_started) * 1000)
    return {"upload_id": upload_id, "chunk_index": chunk_index, "received_bytes": len(payload)}


@router.post("/upload/chunked/{upload_id}/complete")
async def complete_chunked_upload(
    upload_id: str,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    """Assemble verified chunks, then invoke the unchanged Crisp Notes pipeline."""
    session_dir = _chunk_session_dir(upload_id, user.id)
    metadata = _read_upload_metadata(session_dir, user.id)
    chunk_paths = [session_dir / f"chunk-{i:08d}.part" for i in range(int(metadata["total_chunks"]))]
    missing = [i for i, path in enumerate(chunk_paths) if not path.is_file()]
    if missing:
        raise HTTPException(status_code=409, detail=f"Upload is incomplete; missing chunks: {missing[:20]}")
    assembled_path = session_dir / f"assembled{Path(metadata['filename']).suffix.lower()}"
    total_written = 0
    complete_started = time.perf_counter()
    diagnostic_id = uuid4().hex[:12]
    log.info(
        "CRISP_PIPELINE_START request_id=%s upload_id=%s filename=%s file_size_bytes=%s chunks=%s summarize=%s",
        diagnostic_id, upload_id, metadata.get("filename"), metadata.get("file_size"), len(chunk_paths), metadata.get("summarize"),
    )
    log.info(
        "CRISP_CHUNKED_COMPLETE_START request_id=%s upload_id=%s chunks=%s expected_bytes=%s summarize=%s",
        diagnostic_id, upload_id, len(chunk_paths), metadata.get("file_size"), metadata.get("summarize"),
    )
    try:
        with assembled_path.open("wb") as output:
            for index, chunk_path in enumerate(chunk_paths):
                expected = min(int(metadata["chunk_size"]), int(metadata["file_size"]) - index * int(metadata["chunk_size"]))
                if chunk_path.stat().st_size != expected:
                    raise HTTPException(status_code=400, detail=f"Chunk {index} failed size verification")
                with chunk_path.open("rb") as chunk_file:
                    shutil.copyfileobj(chunk_file, output, length=1024 * 1024)
                total_written += expected
        if total_written != int(metadata["file_size"]):
            raise HTTPException(status_code=400, detail="Assembled file size does not match original upload")
        log.info(
            "CRISP_CHUNKED_ASSEMBLY_DONE request_id=%s upload_id=%s bytes=%s chunks=%s elapsed=%.2fs",
            diagnostic_id, upload_id, total_written, len(chunk_paths), time.perf_counter() - complete_started,
        )
        declared_mime = str(metadata.get("content_type") or "").split(";")[0].strip().lower()
        mime = declared_mime if declared_mime.startswith(("audio/", "video/")) else _ALLOWED[Path(metadata["filename"]).suffix.lower()]
        with assembled_path.open("rb") as assembled_file:
            upload = StarletteUploadFile(
                file=assembled_file,
                filename=metadata["filename"],
                headers=Headers({"content-type": mime}),
            )
            result = await upload_audio(
                file=upload,
                title=metadata.get("title"),
                summarize=bool(metadata.get("summarize")),
                db=db,
                user=user,
                diagnostic_id=diagnostic_id,
            )
        log.info(
            "CRISP_PIPELINE_COMPLETE request_id=%s upload_id=%s note_id=%s total_elapsed=%.2fs processing_seconds=%s summary_status=%s",
            diagnostic_id, upload_id, result.get("id"), time.perf_counter() - complete_started, result.get("processing_seconds"), result.get("summary_status"),
        )
        log.info(
            "CRISP_CHUNKED_UPLOAD_COMPLETE request_id=%s upload_id=%s user_id=%s bytes=%s processing_seconds=%s total_elapsed=%.2fs",
            diagnostic_id, upload_id, user.id, total_written, result.get("processing_seconds"), time.perf_counter() - complete_started,
        )
        return result
    except Exception:
        log.exception(
            "CRISP_CHUNKED_COMPLETE_FAILED request_id=%s upload_id=%s elapsed=%.2fs",
            diagnostic_id, upload_id, time.perf_counter() - complete_started,
        )
        raise
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)


@router.delete("/upload/chunked/{upload_id}")
async def cancel_chunked_upload(upload_id: str, user=Depends(get_current_user)):
    """Discard an incomplete upload belonging to the current user."""
    session_dir = _chunk_session_dir(upload_id, user.id)
    if session_dir.exists():
        _read_upload_metadata(session_dir, user.id)
        shutil.rmtree(session_dir, ignore_errors=True)
    return {"status": "cancelled"}


@router.post("/upload")
async def upload_audio(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    summarize: bool = Form(default=False),
    db=Depends(get_db),
    user: User = Depends(get_current_user),
    diagnostic_id: str | None = None,
):
    """Store the original media, transcribe with Deepgram, then optionally summarize locally.

    Audio-only files are sent directly to Deepgram. For video containers, PyAV extracts
    the audio track to a temporary WAV using bundled libraries; a separate ffmpeg.exe
    is never invoked. The transcript is committed before Qwen runs, so a local model
    failure never discards the transcription.
    """
    started = time.perf_counter()
    settings = get_settings()
    cleaned_title = _clean_title(title)
    diagnostic_id = diagnostic_id or uuid4().hex[:12]
    if not (settings.deepgram_api_key or "").strip():
        raise HTTPException(
            status_code=503,
            detail="Audio transcription is not configured: set DEEPGRAM_API_KEY on the backend.",
        )
    suffix = Path(file.filename or "audio.bin").suffix.lower()
    incoming_mime = (file.content_type or "").split(";")[0].strip().lower()
    log.info(
        "CRISP_UPLOAD_START request_id=%s filename=%s content_type=%s extension=%s summarize=%s",
        diagnostic_id, file.filename, file.content_type, suffix, summarize,
    )

    if suffix not in _ALLOWED:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported media type '{suffix or 'unknown'}'. Supported audio/video extensions: "
                f"{', '.join(sorted(_ALLOWED))}."
            ),
        )

    mime_type = incoming_mime if incoming_mime.startswith(("audio/", "video/")) else _ALLOWED[suffix]
    is_video = suffix in _VIDEO_EXTENSIONS or mime_type.startswith("video/")
    key = f"media/{uuid4().hex}{suffix}"
    storage = None
    tmp_path: Path | None = None
    extracted_audio_path: Path | None = None

    try:
        try:
            storage = get_object_storage()
        except Exception as storage_exc:
            log.exception("CRISP_STORAGE_INIT_FAILED error=%s", storage_exc)
            raise HTTPException(
                status_code=503,
                detail=f"Audio storage is not configured correctly: {storage_exc}",
            ) from storage_exc

        file_stage_started = time.perf_counter()
        with tempfile.NamedTemporaryFile(prefix="crisp-", suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            while True:
                chunk = await file.read(8 * 1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
            tmp.flush()

        size = tmp_path.stat().st_size
        if size <= 0:
            raise HTTPException(status_code=400, detail="Audio file is empty")

        log.info(
            "CRISP_UPLOAD_STAGED request_id=%s bytes=%s mime=%s file_read_elapsed=%.2fs total_elapsed=%.2fs",
            diagnostic_id, size, mime_type, time.perf_counter() - file_stage_started, time.perf_counter() - started,
        )

        stage_started = time.perf_counter()
        await storage.put_file(key, tmp_path, mime_type)
        log.info(
            "CRISP_STORAGE_SAVED request_id=%s key=%s bytes=%s stage_elapsed=%.2fs total_elapsed=%.2fs",
            diagnostic_id, key, size, time.perf_counter() - stage_started, time.perf_counter() - started,
        )

        transcription_path = tmp_path
        transcription_mime = mime_type
        audio_extracted = False
        if is_video:
            stage_started = time.perf_counter()
            extracted_audio_path = tmp_path.with_name(f"{tmp_path.stem}-audio.wav")
            log.info("CRISP_VIDEO_AUDIO_EXTRACT_START request_id=%s bytes=%s", diagnostic_id, size)
            try:
                await asyncio.to_thread(_extract_video_audio, tmp_path, extracted_audio_path)
            except Exception as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Could not read an audio track from this video: {exc}",
                ) from exc
            transcription_path = extracted_audio_path
            transcription_mime = "audio/wav"
            audio_extracted = True
            log.info(
                "CRISP_VIDEO_AUDIO_EXTRACT_DONE request_id=%s audio_bytes=%s stage_elapsed=%.2fs total_elapsed=%.2fs",
                diagnostic_id, extracted_audio_path.stat().st_size, time.perf_counter() - stage_started, time.perf_counter() - started,
            )

        log.info(
            "CRISP_DEEPGRAM_START request_id=%s bytes=%s mime=%s video_audio_extracted=%s elapsed=%.2fs",
            diagnostic_id, transcription_path.stat().st_size, transcription_mime, audio_extracted, time.perf_counter() - started,
        )
        deepgram_started = time.perf_counter()
        try:
            dg = await DeepgramTranscriber.transcribe_file(transcription_path, transcription_mime, diagnostic_id=diagnostic_id)
        except Exception:
            log.exception(
                "CRISP_DEEPGRAM_FAILED request_id=%s bytes=%s mime=%s stage_elapsed=%.2fs total_elapsed=%.2fs",
                diagnostic_id, transcription_path.stat().st_size, transcription_mime,
                time.perf_counter() - deepgram_started, time.perf_counter() - started,
            )
            raise
        transcript = (dg.transcript or "").strip()
        log.info(
            "CRISP_DEEPGRAM_DONE request_id=%s chars=%s words=%s language=%s duration=%s stage_elapsed=%.2fs total_elapsed=%.2fs",
            diagnostic_id, len(transcript), len(transcript.split()), dg.language, dg.duration_seconds,
            time.perf_counter() - deepgram_started, time.perf_counter() - started,
        )

        if not transcript:
            raise HTTPException(
                status_code=422,
                detail="Deepgram returned an empty transcript. Please try a clearer recording.",
            )

        note = TranscriptNote(
            user_id=user.id,
            title=cleaned_title,
            source_audio_uri=key,
            transcript=transcript,
            notes=None,
            diarization=dg.diarization,
        )
        db_stage_started = time.perf_counter()
        db.add(note)
        await db.commit()
        await db.refresh(note)
        log.info(
            "CRISP_NOTE_PERSISTED request_id=%s note_id=%s transcript_chars=%s transcript_words=%s db_elapsed=%.2fs total_elapsed=%.2fs",
            diagnostic_id, note.id, len(transcript), len(transcript.split()), time.perf_counter() - db_stage_started, time.perf_counter() - started,
        )

        summary_status = "not_requested"
        summary_error = None
        summarization_model = settings.ollama_model
        if summarize:
            summary_started = time.perf_counter()
            log.info(
                "CRISP_SUMMARY_AFTER_UPLOAD_START request_id=%s note_id=%s transcript_chars=%s model=%s",
                diagnostic_id, note.id, len(transcript), settings.ollama_model,
            )
            try:
                note.notes, summarization_model = await _run_summary(transcript, diagnostic_id=diagnostic_id)
                db_commit_started = time.perf_counter()
                await db.commit()
                await db.refresh(note)
                log.info(
                    "CRISP_SUMMARY_DB_SAVE_DONE request_id=%s note_id=%s output_chars=%s db_elapsed=%.2fs",
                    diagnostic_id, note.id, len(note.notes or ""), time.perf_counter() - db_commit_started,
                )
                summary_status = "ready"
                log.info(
                    "CRISP_SUMMARY_AFTER_UPLOAD_DONE request_id=%s note_id=%s model=%s stage_elapsed=%.2fs total_elapsed=%.2fs",
                    diagnostic_id, note.id, summarization_model, time.perf_counter() - summary_started, time.perf_counter() - started,
                )
            except Exception as exc:
                log.exception(
                    "CRISP_SUMMARY_AFTER_UPLOAD_FAILED request_id=%s note_id=%s stage_elapsed=%.2fs total_elapsed=%.2fs error=%s",
                    diagnostic_id, note.id, time.perf_counter() - summary_started, time.perf_counter() - started, exc,
                )
                summary_status = "failed"
                summary_error = f"Qwen summary failed; the transcript is saved. ({exc})"

        total = time.perf_counter() - started
        log.info(
            "CRISP_TRANSCRIBED request_id=%s note_id=%s bytes=%s duration_seconds=%s summary=%s total_elapsed=%.2fs",
            diagnostic_id, note.id, size, dg.duration_seconds, summary_status, total,
        )

        return {
            "id": note.id,
            "title": note.title,
            "source_audio_uri": key,
            "transcript": transcript,
            "notes": note.notes,
            "diarization": dg.diarization,
            "status": "summarized" if summary_status == "ready" else "transcribed",
            "summary_status": summary_status,
            "summary_error": summary_error,
            "duration_seconds": dg.duration_seconds,
            "processing_seconds": round(total, 2),
            "language_mode": dg.language or "auto",
            "transcription_provider": "deepgram",
            "transcription_model": settings.deepgram_model,
            "audio_transcoded": audio_extracted,
            "media_kind": "video" if is_video else "audio",
            "summarization_provider": "ollama",
            "summarization_model": summarization_model,
        }

    except HTTPException:
        try:
            if storage is not None:
                await storage.delete(key)
        except Exception:
            log.warning("CRISP_STORAGE_CLEANUP_FAILED key=%s", key, exc_info=True)
        raise
    except Exception as exc:
        try:
            if storage is not None:
                await storage.delete(key)
        except Exception:
            log.warning("CRISP_STORAGE_CLEANUP_FAILED key=%s", key, exc_info=True)
        log.exception(
            "CRISP_TRANSCRIPTION_FAILED request_id=%s elapsed=%.2fs error=%s",
            diagnostic_id, time.perf_counter() - started,
            exc,
        )
        message = str(exc).strip() or exc.__class__.__name__
        raise HTTPException(
            status_code=502,
            detail=f"Audio transcription failed: {message}",
        ) from exc
    finally:
        for temporary_path in (extracted_audio_path, tmp_path):
            if temporary_path is not None:
                try:
                    await asyncio.to_thread(temporary_path.unlink, missing_ok=True)
                except Exception:
                    log.warning("CRISP_TEMP_CLEANUP_FAILED path=%s", temporary_path, exc_info=True)


@router.post("/{note_id}/summarize")
async def summarize_note(
    note_id: int,
    force: bool = Query(default=False),
    db=Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate an English investor note with local Qwen on user action."""
    note = await _get_accessible_note(db, note_id, user)
    if note is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    transcript = (note.transcript or "").strip()
    if not transcript:
        raise HTTPException(status_code=422, detail="Transcript not found or empty")

    if note.notes and note.notes.strip() and not force:
        return {
            "id": note.id,
            "title": note.title,
            "transcript": note.transcript,
            "notes": note.notes,
            "status": "already_summarized",
            "summary_status": "ready",
            "diarization": note.diarization,
            "summarization_provider": "ollama",
            "summarization_model": get_settings().ollama_model,
        }

    started = time.perf_counter()
    settings = get_settings()
    log.info(
        "CRISP_QWEN_START note_id=%s chars=%s model=%s elapsed=%.2fs",
        note.id, len(transcript), settings.ollama_model, time.perf_counter() - started,
    )
    try:
        notes, summarization_model = await _run_summary(transcript)
        note.notes = notes
        await db.commit()
        await db.refresh(note)
        log.info(
            "CRISP_QWEN_DONE note_id=%s elapsed=%.2fs",
            note.id, time.perf_counter() - started,
        )
        return {
            "id": note.id,
            "title": note.title,
            "transcript": note.transcript,
            "notes": note.notes,
            "status": "summarized",
            "summary_status": "ready",
            "diarization": note.diarization,
            "summarization_provider": "ollama",
            "summarization_model": summarization_model,
        }
    except Exception as exc:
        log.exception(
            "CRISP_SUMMARY_FAILED note_id=%s elapsed=%.2fs error=%s",
            note.id, time.perf_counter() - started, exc,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "Local Qwen summary is temporarily unavailable. Your transcript is saved; "
                f"please try again. ({exc})"
            ),
        ) from exc

@router.post("/{note_id}/chat")
async def chat_about_note(
    note_id: int,
    body: ChatRequest,
    db=Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Answer follow-up questions using only the selected recording's transcript."""
    note = await _get_accessible_note(db, note_id, user)
    if note is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    transcript = (note.transcript or "").strip()
    if not transcript:
        raise HTTPException(status_code=422, detail="This recording does not have a transcript yet")

    history = [
        {"role": item.role, "content": item.content.strip()[:4000]}
        for item in body.history[-8:]
        if item.content.strip()
    ]
    retrieval_query = " ".join(
        [item["content"] for item in history if item["role"] == "user"][-3:]
        + [body.question.strip()]
    )
    evidence = _retrieve_transcript_evidence(note, retrieval_query)

    summarizer = OllamaInvestorSummarizer()
    try:
        answer = await summarizer.answer_question(
            body.question,
            evidence,
            summary=note.notes or "",
            history=history,
        )
    except Exception as exc:
        log.exception("CRISP_CHAT_FAILED note_id=%s error=%s", note_id, exc)
        raise HTTPException(
            status_code=502,
            detail=(
                "Local Qwen could not answer this question. Check that Ollama is running "
                f"with model '{summarizer.model}'. ({exc})"
            ),
        ) from exc

    return {
        "note_id": note.id,
        "answer": answer,
        "provider": "ollama",
        "model": summarizer.model,
    }
