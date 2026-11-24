"""Normalize supported audio/video uploads for Deepgram.

Small, commonly supported compressed audio files are passed through unchanged.
Video, WAV, uncommon containers, and large files are normalized to mono 16 kHz
Opus. FFmpeg is therefore required for video, uncommon formats, WAV, and large
uploads; ordinary MP3/M4A/OGG/WEBM/FLAC/AAC files do not need it when small.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("crisp-notes.audio")

TRANSCODE_OVER_BYTES = 200 * 1024 * 1024
TRANSCODE_TIMEOUT_SECONDS = 12 * 60 * 60
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".3gp", ".3g2",
    ".mpeg", ".mpg", ".ts", ".mts", ".m2ts",
}
NATIVE_AUDIO_EXTENSIONS = {
    ".mp3", ".mp2", ".mpga", ".m4a", ".wav", ".wave", ".webm",
    ".ogg", ".oga", ".opus", ".flac", ".aac", ".amr",
}


@dataclass(frozen=True)
class PreparedAudio:
    path: Path
    mime_type: str
    transcoded: bool


async def prepare_audio_for_transcription(source: Path, suffix: str, mime_type: str) -> PreparedAudio:
    suffix = suffix.lower()
    size = source.stat().st_size
    is_video = suffix in VIDEO_EXTENSIONS
    needs_transcode = (
        is_video
        or suffix in {".wav", ".wave"}
        or suffix not in NATIVE_AUDIO_EXTENSIONS
        or size > TRANSCODE_OVER_BYTES
    )
    if not needs_transcode:
        return PreparedAudio(path=source, mime_type=mime_type, transcoded=False)

    configured_ffmpeg = os.getenv("FFMPEG_PATH", "").strip()
    ffmpeg = configured_ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg is required to process this media format or file size. "
            "Install FFmpeg, add it to PATH, or set FFMPEG_PATH to ffmpeg.exe."
        )

    output = source.with_name(f"{source.stem}-transcode.ogg")
    command = [
        ffmpeg, "-y", "-v", "error", "-i", str(source), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "32k",
        "-f", "ogg", str(output),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=TRANSCODE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("FFmpeg transcoding timed out")

        if process.returncode != 0:
            detail = (stderr or b"").decode(errors="replace")[:1000]
            raise RuntimeError(detail or "FFmpeg failed")
        if not output.exists() or output.stat().st_size <= 0:
            raise RuntimeError("FFmpeg produced an empty audio file")
    except Exception as exc:
        await asyncio.to_thread(output.unlink, missing_ok=True)
        if is_video:
            raise RuntimeError(
                "Could not extract audio from this video. Confirm it contains an audio track "
                f"and that FFmpeg supports its codec: {exc}"
            ) from exc
        raise RuntimeError(f"Could not normalize this audio file with FFmpeg: {exc}") from exc

    log.info(
        "CRISP_TRANSCODED source_bytes=%s output_bytes=%s suffix=%s",
        size, output.stat().st_size, suffix,
    )
    return PreparedAudio(path=output, mime_type="audio/ogg", transcoded=True)
