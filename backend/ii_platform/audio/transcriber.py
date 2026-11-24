from __future__ import annotations

from ii_platform.storage.factory import get_object_storage


class GeminiTranscriber:
    """Transcribe real audio bytes through Gemini, optionally resolving an object URI."""
    def __init__(self, gateway, storage=None):
        self.gateway = gateway
        self.storage = storage or get_object_storage()

    async def transcribe(self, audio: bytes | str, mime_type: str = "audio/mpeg") -> str:
        if isinstance(audio, bytes):
            data = audio
        else:
            key = audio.removeprefix("s3://") if audio.startswith("s3://") else audio
            data = await self.storage.get(key)
        if not data:
            raise ValueError("Audio payload is empty")
        return await self.gateway.transcribe_audio(
            data, mime_type,
            prompt=("Transcribe this earnings-call audio faithfully. Preserve speaker changes "
                    "when identifiable, do not summarize, do not invent words, and return only the transcript.")
        )
