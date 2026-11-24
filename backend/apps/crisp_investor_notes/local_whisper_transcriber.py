from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.crisp_investor_notes import config

log = logging.getLogger("crisp-notes.whisper")

_SAMPLE_RATE = 16_000
_MODEL: Any | None = None
_MODEL_LOCK = threading.Lock()
# A single upload owns the CPU worker pool at a time. This avoids two simultaneous
# uploads each starting four CTranslate2 workers and oversubscribing the CPU.
_TRANSCRIPTION_SLOT = threading.BoundedSemaphore(value=1)

# This is a transcription hint, not a translation instruction. Whisper is allowed
# to detect the language on every chunk so Hindi/English code-switching is retained.
_INITIAL_PROMPT = (
    "यह recording Hindi और English दोनों में हो सकती है। Hindi को देवनागरी में "
    "और English business तथा financial terms को Latin script में transcribe करें। "
    "Company names, ticker symbols, numbers, EBITDA, EPS, revenue, margins, "
    "guidance और अन्य technical terms को spoken form के अनुसार रखें। "
    "Do not translate the speech."
)


@dataclass(frozen=True)
class _ChunkSpec:
    index: int
    start_sample: int
    end_sample: int
    owned_start_seconds: float
    owned_end_seconds: float


@dataclass(frozen=True)
class WhisperTranscriptionResult:
    transcript: str
    duration_seconds: float
    language: str | None
    detected_languages: list[str]
    model: str
    chunk_count: int
    worker_count: int
    segments: list[dict[str, Any]]


def _get_model():
    """Load one shared CTranslate2 model lazily and reuse it for all chunks."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL

    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL

        # Import lazily so unrelated backend routes can start even before the
        # optional transcription dependency has been installed.
        from faster_whisper import WhisperModel

        model_name = config.WHISPER_MODEL
        log.info(
            "CRISP_WHISPER_MODEL_LOADING model=%s device=%s compute_type=%s "
            "workers=%s cpu_threads=%s",
            model_name,
            config.WHISPER_DEVICE,
            config.WHISPER_COMPUTE_TYPE,
            config.WHISPER_WORKERS,
            config.WHISPER_CPU_THREADS,
        )
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "device": config.WHISPER_DEVICE,
            "compute_type": config.WHISPER_COMPUTE_TYPE,
            "cpu_threads": config.WHISPER_CPU_THREADS,
            "num_workers": config.WHISPER_WORKERS,
            "local_files_only": config.WHISPER_LOCAL_FILES_ONLY,
        }
        if config.WHISPER_DOWNLOAD_ROOT:
            kwargs["download_root"] = config.WHISPER_DOWNLOAD_ROOT

        _MODEL = WhisperModel(model_name, **kwargs)
        log.info(
            "CRISP_WHISPER_MODEL_READY model=%s load_seconds=%.2f",
            model_name,
            time.perf_counter() - started,
        )
        return _MODEL


def get_transcriber_info() -> dict[str, Any]:
    """Return configuration without triggering a potentially large model load."""
    return {
        "provider": "faster-whisper-local",
        "model": config.WHISPER_MODEL,
        "device": config.WHISPER_DEVICE,
        "compute_type": config.WHISPER_COMPUTE_TYPE,
        "parallel_workers": config.WHISPER_WORKERS,
        "cpu_threads_per_worker": config.WHISPER_CPU_THREADS,
        "chunk_seconds": config.WHISPER_CHUNK_SECONDS,
        "overlap_seconds": config.WHISPER_OVERLAP_SECONDS,
        "beam_size": config.WHISPER_BEAM_SIZE,
        "model_loaded": _MODEL is not None,
    }


def _make_chunk_specs(total_samples: int) -> list[_ChunkSpec]:
    chunk_samples = config.WHISPER_CHUNK_SECONDS * _SAMPLE_RATE
    overlap_samples = config.WHISPER_OVERLAP_SECONDS * _SAMPLE_RATE
    if chunk_samples <= 0 or overlap_samples < 0 or overlap_samples >= chunk_samples:
        raise ValueError("Invalid Whisper chunk/overlap configuration")

    specs: list[_ChunkSpec] = []
    start = 0
    index = 0
    while start < total_samples:
        end = min(total_samples, start + chunk_samples)
        window_duration = (end - start) / _SAMPLE_RATE
        is_first = start == 0
        is_last = end >= total_samples
        specs.append(
            _ChunkSpec(
                index=index,
                start_sample=start,
                end_sample=end,
                owned_start_seconds=0.0 if is_first else config.WHISPER_OVERLAP_SECONDS / 2,
                owned_end_seconds=(
                    window_duration
                    if is_last
                    else window_duration - config.WHISPER_OVERLAP_SECONDS / 2
                ),
            )
        )
        if is_last:
            break
        start = end - overlap_samples
        index += 1
    return specs


def _transcribe_chunk(model, audio_chunk, spec: _ChunkSpec) -> tuple[list[dict[str, Any]], str | None, float]:
    started = time.perf_counter()
    segments_iter, info = model.transcribe(
        audio_chunk,
        task="transcribe",
        language=None,  # Detect per chunk; do not force Hindi or English globally.
        beam_size=config.WHISPER_BEAM_SIZE,
        temperature=0.0,
        condition_on_previous_text=False,
        initial_prompt=_INITIAL_PROMPT,
        vad_filter=True,
        word_timestamps=False,
    )

    # faster-whisper returns a lazy generator. Consume it inside this worker so
    # all chunks actually execute concurrently in the ThreadPoolExecutor.
    local_segments: list[dict[str, Any]] = []
    window_duration = (spec.end_sample - spec.start_sample) / _SAMPLE_RATE
    for segment in segments_iter:
        midpoint = (float(segment.start) + float(segment.end)) / 2
        if midpoint < spec.owned_start_seconds or midpoint >= spec.owned_end_seconds:
            continue
        local_segments.append(
            {
                "start": spec.start_sample / _SAMPLE_RATE + float(segment.start),
                "end": spec.start_sample / _SAMPLE_RATE + float(segment.end),
                "text": str(segment.text or ""),
            }
        )

    elapsed = time.perf_counter() - started
    log.info(
        "CRISP_WHISPER_CHUNK_DONE chunk=%s language=%s speech_segments=%s "
        "audio_seconds=%.2f processing_seconds=%.2f",
        spec.index + 1,
        getattr(info, "language", None),
        len(local_segments),
        window_duration,
        elapsed,
    )
    return local_segments, getattr(info, "language", None), window_duration


def _join_segment_text(segments: list[dict[str, Any]]) -> str:
    pieces: list[str] = []
    for item in segments:
        value = str(item.get("text") or "")
        if not value:
            continue
        if pieces and not value[0].isspace() and not pieces[-1].endswith((" ", "\n", "\t")):
            pieces.append(" ")
        pieces.append(value)

    transcript = "".join(pieces)
    transcript = re.sub(r"[ \t]+", " ", transcript)
    transcript = re.sub(r"\s+([,.;:!?।])", r"\1", transcript)
    return transcript.strip()


def transcribe_audio_file(audio_path: str | Path) -> WhisperTranscriptionResult:
    """Decode an audio file and transcribe overlapping chunks in parallel.

    PyAV (a dependency of faster-whisper) bundles the required FFmpeg libraries,
    so this feature does not invoke or require a system ffmpeg.exe executable.
    """
    # Import lazily for a clear, actionable error if requirements have not yet
    # been installed into the backend virtual environment.
    try:
        from faster_whisper.audio import decode_audio
    except ImportError as exc:  # pragma: no cover - exercised on first deployment
        raise RuntimeError(
            "Local transcription dependency is missing. Install backend requirements "
            "or run: python -m pip install 'faster-whisper>=1.1.0,<2'."
        ) from exc

    wait_started = time.perf_counter()
    with _TRANSCRIPTION_SLOT:
        queue_wait = time.perf_counter() - wait_started
        started = time.perf_counter()
        log.info("CRISP_WHISPER_START file=%s queue_wait_seconds=%.2f", audio_path, queue_wait)

        try:
            # .webm can contain either audio-only streams or video. Keep support
            # for audio-only WebM while explicitly rejecting WebM video uploads.
            if Path(audio_path).suffix.lower() == ".webm":
                import av

                with av.open(str(audio_path)) as container:
                    if any(stream.type == "video" for stream in container.streams):
                        raise ValueError("Video uploads are not supported. Upload an audio-only file.")
                    if not any(stream.type == "audio" for stream in container.streams):
                        raise ValueError("The WebM file does not contain an audio stream")
            waveform = decode_audio(str(audio_path), sampling_rate=_SAMPLE_RATE)
        except ValueError:
            raise
        except Exception as exc:
            # Keep the underlying PyAV exception in the backend log. In particular,
            # recent PyAV releases removed an av.open() keyword still used by
            # faster-whisper 1.2.x; that is a dependency mismatch, not a bad M4A.
            log.exception(
                "CRISP_AUDIO_DECODE_FAILED path=%s exception_type=%s error=%s",
                audio_path, type(exc).__name__, exc,
            )
            raise ValueError(
                "Audio decoding failed. The recording may be unsupported or the "
                "local PyAV/faster-whisper dependencies may be incompatible. "
                "Check the backend log entry CRISP_AUDIO_DECODE_FAILED."
            ) from exc

        total_samples = int(getattr(waveform, "shape", [0])[0])
        if total_samples <= 0:
            raise ValueError("The uploaded audio contains no decodable audio samples")

        duration_seconds = total_samples / _SAMPLE_RATE
        specs = _make_chunk_specs(total_samples)
        model = _get_model()
        worker_count = min(config.WHISPER_WORKERS, len(specs))
        all_segments: list[dict[str, Any]] = []
        languages: Counter[str] = Counter()

        # Submit at most one worker-batch at a time. This bounds temporary
        # waveform slices and keeps memory use predictable for long recordings.
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="crisp-whisper") as pool:
            for batch_start in range(0, len(specs), worker_count):
                batch = specs[batch_start:batch_start + worker_count]
                futures = [
                    pool.submit(
                        _transcribe_chunk,
                        model,
                        waveform[spec.start_sample:spec.end_sample],
                        spec,
                    )
                    for spec in batch
                ]
                # Reading futures in submission order preserves deterministic
                # ordering; all jobs in the batch are nevertheless running at once.
                for future in futures:
                    chunk_segments, language, chunk_audio_seconds = future.result()
                    all_segments.extend(chunk_segments)
                    if language:
                        languages[language] += chunk_audio_seconds

        all_segments.sort(key=lambda item: (item["start"], item["end"]))
        transcript = _join_segment_text(all_segments)
        if not transcript:
            raise ValueError("Whisper did not detect any speech in this recording")

        detected_languages = [language for language, _ in languages.most_common()]
        primary_language = detected_languages[0] if detected_languages else None
        elapsed = time.perf_counter() - started
        log.info(
            "CRISP_WHISPER_DONE model=%s duration_seconds=%.2f chunks=%s workers=%s "
            "languages=%s transcript_chars=%s processing_seconds=%.2f real_time_factor=%.2f",
            config.WHISPER_MODEL,
            duration_seconds,
            len(specs),
            worker_count,
            ",".join(detected_languages),
            len(transcript),
            elapsed,
            duration_seconds / elapsed if elapsed > 0 else 0.0,
        )

        return WhisperTranscriptionResult(
            transcript=transcript,
            duration_seconds=duration_seconds,
            language=primary_language,
            detected_languages=detected_languages,
            model=config.WHISPER_MODEL,
            chunk_count=len(specs),
            worker_count=worker_count,
            segments=all_segments,
        )
