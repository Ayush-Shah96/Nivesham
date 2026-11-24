from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from infrastructure.config.settings import get_settings

log = logging.getLogger("crisp-notes.deepgram")


@dataclass(frozen=True)
class DeepgramResult:
    transcript: str
    diarization: list[dict[str, Any]]
    duration_seconds: float | None
    language: str | None
    confidence: float | None


class DeepgramTranscriber:
    """Deepgram pre-recorded STT with byte and streamed-file support."""

    @staticmethod
    def _params() -> list[tuple[str, str]]:
        settings = get_settings()
        params: list[tuple[str, str]] = [
            ("model", settings.deepgram_model),
            ("smart_format", "true"),
            ("utterances", "true"),
            (
                "diarize_model",
                (settings.deepgram_diarize_model or "latest").strip().lower()
                if (settings.deepgram_diarize_model or "").strip().lower() in {"latest", "v1", "v2"}
                else "latest",
            ),
        ]

        language = (settings.deepgram_language or "multi").strip()
        if language and language.casefold() not in {"auto", "detect"}:
            params.append(("language", language))
        else:
            params.append(("detect_language", "true"))

        # Nova-3 multilingual accepts repeated language_hint parameters.
        if language.casefold() == "multi" and (settings.deepgram_model or "").casefold().startswith("nova-3"):
            for hint in (getattr(settings, "deepgram_language_hints", "") or "").split(","):
                if hint.strip():
                    params.append(("language_hint", hint.strip()))

        for term in (settings.deepgram_keyterms or "").split(","):
            term = " ".join(term.split()).strip()
            if term:
                params.append(("keyterm", term))
        return params

    @staticmethod
    async def _async_chunks(
        path: str | Path,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> AsyncIterator[bytes]:
        """Yield file bytes asynchronously for httpx.AsyncClient streaming uploads.

        Passing a synchronous iterator to AsyncClient(content=...) causes
        `Attempted to send an sync request with an AsyncClient instance`.
        Use asyncio.to_thread for file reads so disk I/O does not block the event loop.
        """
        with Path(path).open("rb") as fh:
            while True:
                chunk = await asyncio.to_thread(fh.read, chunk_size)
                if not chunk:
                    break
                yield chunk

    @staticmethod
    async def _transcribe_request(
        content: bytes | AsyncIterator[bytes],
        mime_type: str,
        content_length: int | None = None,
        diagnostic_id: str | None = None,
    ) -> DeepgramResult:
        request_started = time.perf_counter()
        settings = get_settings()
        api_key = (settings.deepgram_api_key or "").strip()
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY is not configured")
        if isinstance(content, bytes) and not content:
            raise ValueError("Deepgram transcription payload is empty")

        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": mime_type or "application/octet-stream",
        }
        if content_length is not None:
            headers["Content-Length"] = str(content_length)

        # The configured timeout is the real limit for long recordings; no hidden floor or cap.
        transport_timeout = float(getattr(settings, "deepgram_timeout_seconds", 3600) or 3600)
        timeout = httpx.Timeout(
            connect=20.0,
            read=transport_timeout,
            write=transport_timeout,
            pool=30.0,
        )

        log.info("CRISP_DEEPGRAM_HTTP_START request_id=%s mime=%s content_length=%s timeout_seconds=%s", diagnostic_id or "-", mime_type, content_length, transport_timeout)
        http_started = time.perf_counter()
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                "https://api.deepgram.com/v1/listen",
                params=DeepgramTranscriber._params(),
                headers=headers,
                content=content,
            )
        log.info("CRISP_DEEPGRAM_HTTP_DONE request_id=%s status=%s response_bytes=%s http_elapsed=%.2fs total_elapsed=%.2fs", diagnostic_id or "-", response.status_code, len(response.content), time.perf_counter() - http_started, time.perf_counter() - request_started)

        if response.status_code >= 400:
            detail = response.text[:3000]
            raise RuntimeError(f"Deepgram transcription failed with HTTP {response.status_code}: {detail}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Deepgram returned a non-JSON response") from exc

        parse_started = time.perf_counter()
        results = payload.get("results") or {}
        channels = results.get("channels") or []
        first_channel = channels[0] if channels else {}
        alternatives = first_channel.get("alternatives") or []
        alternative = alternatives[0] if alternatives else {}
        transcript = (alternative.get("transcript") or "").strip()
        utterances = results.get("utterances") or []

        diarization: list[dict[str, Any]] = []
        for item in utterances:
            text = (item.get("transcript") or "").strip()
            if not text:
                continue
            diarization.append({
                "speaker": item.get("speaker"),
                "start": item.get("start"),
                "end": item.get("end"),
                "confidence": item.get("confidence"),
                "channel": item.get("channel", 0),
                "transcript": text,
                "languages": item.get("languages") or [],
            })

        metadata = payload.get("metadata") or {}
        duration = metadata.get("duration")
        language = alternative.get("languages") or alternative.get("detected_language") or alternative.get("language") or metadata.get("language")
        confidence = alternative.get("confidence")

        if diarization:
            speaker_lines = []
            for item in diarization:
                speaker = item.get("speaker")
                text = str(item.get("transcript") or "").strip()
                if not text:
                    continue
                prefix = f"[Speaker {speaker}] " if speaker is not None else ""
                speaker_lines.append(prefix + text)
            utterance_transcript = "\n\n".join(speaker_lines).strip()
            if utterance_transcript:
                transcript = utterance_transcript

        if not transcript:
            raise RuntimeError("Deepgram returned an empty transcript")

        log.info("CRISP_DEEPGRAM_PARSE_DONE request_id=%s transcript_chars=%s words=%s utterances=%s language=%s parse_elapsed=%.2fs total_elapsed=%.2fs", diagnostic_id or "-", len(transcript), len(transcript.split()), len(diarization), language, time.perf_counter() - parse_started, time.perf_counter() - request_started)
        return DeepgramResult(
            transcript=transcript,
            diarization=diarization,
            duration_seconds=float(duration) if duration is not None else None,
            language=", ".join(language) if isinstance(language, list) else (str(language) if language else None),
            confidence=float(confidence) if confidence is not None else None,
        )

    @staticmethod
    async def transcribe(data: bytes, mime_type: str) -> DeepgramResult:
        return await DeepgramTranscriber._transcribe_request(data, mime_type, len(data))

    @staticmethod
    async def transcribe_file(path: str | Path, mime_type: str, diagnostic_id: str | None = None) -> DeepgramResult:
        file_started = time.perf_counter()
        file_path = Path(path)
        size = file_path.stat().st_size
        log.info("CRISP_DEEPGRAM_FILE_STREAM_START request_id=%s bytes=%s mime=%s", diagnostic_id or "-", size, mime_type)
        if size <= 0:
            raise ValueError("Deepgram transcription payload is empty")
        result = await DeepgramTranscriber._transcribe_request(
            DeepgramTranscriber._async_chunks(file_path),
            mime_type,
            size,
            diagnostic_id=diagnostic_id,
        )
        log.info("CRISP_DEEPGRAM_FILE_STREAM_DONE request_id=%s bytes=%s transcript_chars=%s elapsed=%.2fs", diagnostic_id or "-", size, len(result.transcript), time.perf_counter() - file_started)
        return result