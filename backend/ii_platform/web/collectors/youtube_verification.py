from __future__ import annotations

"""Conservative verification gate for company-management YouTube videos.

Transcript retrieval is best-effort. A missing transcript is not classified as
irrelevance; it is recorded as unverified and must not enter the verified feed.
"""

import asyncio
import re
from typing import Any
from urllib.parse import parse_qs, urlparse


def video_id_from_url(url: str) -> str | None:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/", 1)[0] or None
    if parsed.path == "/watch":
        return parse_qs(parsed.query).get("v", [None])[0]
    match = re.search(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})", parsed.path)
    return match.group(1) if match else None


def _transcript_sync(video_id: str) -> str:
    """Fetch captions using youtube-transcript-api, supporting old/new APIs."""
    from youtube_transcript_api import YouTubeTranscriptApi  # optional dependency

    # Current releases expose an instance fetch() API; older releases expose
    # the static get_transcript() API.
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id)
        snippets = list(fetched)
    except (AttributeError, TypeError):
        snippets = YouTubeTranscriptApi.get_transcript(video_id)
    pieces: list[str] = []
    for snippet in snippets:
        if isinstance(snippet, dict):
            value = snippet.get("text", "")
        else:
            value = getattr(snippet, "text", "")
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if value:
            pieces.append(value)
    return " ".join(pieces)[:100_000]


async def fetch_transcript(url: str) -> tuple[str | None, str]:
    """Return (text, status); never turn retrieval errors into irrelevant-content."""
    video_id = video_id_from_url(url)
    if not video_id:
        return None, "invalid_video_id"
    try:
        text = await asyncio.to_thread(_transcript_sync, video_id)
        if not text.strip():
            return None, "empty_transcript"
        return text, "available"
    except Exception as exc:
        # Keep the error class, not potentially sensitive or noisy exception text.
        return None, f"unavailable:{type(exc).__name__}"


_ROLE = re.compile(
    r"\b(?:managing director|joint managing director|whole[- ]time director|"
    r"chairman|chairperson|chairwoman|chief executive officer|ceo|promoter|"
    r"founder|co[- ]founder|chief financial officer|cfo|chief operating officer|"
    r"coo|chief technology officer|cto|executive director|company secretary)\b",
    re.I,
)
_FORMAT = re.compile(
    r"\b(?:interview|in conversation|conversation with|fireside chat|discussion|"
    r"question and answer|q\s*&\s*a|annual general meeting|agm|shareholders meeting|"
    r"earnings call|conference call|investor meet|management interaction|"
    r"joining us|welcome .{0,80}\b(?:ceo|md|chairman|founder|promoter)\b|"
    r"our guest .{0,80}\b(?:ceo|md|chairman|founder|promoter)\b)\b",
    re.I,
)
_SELF_IDENTIFICATION = re.compile(
    r"\b(?:i am|i'm|my name is|as the|as a|serving as|currently the|"
    r"i serve as|i work as)\b.{0,100}"
    r"\b(?:managing director|chairman|chairperson|ceo|chief executive officer|"
    r"promoter|founder|co[- ]founder|cfo|chief financial officer|"
    r"executive director)\b",
    re.I,
)


def verify_transcript_speaker(
    title: str, description: str, transcript: str, company_name: str = ""
) -> tuple[bool, str]:
    """Require transcript evidence of both a representative role and speaking context.

    This is a conservative text heuristic, not biometric/voice identity proof.
    It deliberately does not accept a role mention alone.
    """
    text = re.sub(r"\s+", " ", f"{title} {description} {transcript}").strip()
    transcript_text = re.sub(r"\s+", " ", transcript or "").strip()
    if not transcript_text:
        return False, "transcript_missing"
    role_match = _ROLE.search(transcript_text)
    if not role_match:
        return False, "transcript_no_representative_role"
    # When a company name is known, require it to occur close to the role in
    # transcript text or in the title/description, reducing unrelated role mentions.
    company = re.escape(str(company_name or "").strip())
    if company:
        # The association must appear in transcript evidence itself. Title and
        # description keywords are discovery hints, not proof of who spoke.
        # Same-sentence association avoids joining unrelated names from recaps.
        sentences = re.split(r"(?<=[.!?;])\s+|\n+", transcript_text)
        company_in_context = any(
            re.search(company, sentence, re.I) and _ROLE.search(sentence)
            for sentence in sentences
        )
        if not company_in_context:
            return False, "transcript_role_not_linked_to_covered_company"
    if _SELF_IDENTIFICATION.search(transcript_text):
        return True, "transcript_self_identifies_representative"
    # The transcript must indicate a representative is a participant, not merely
    # mention a role in financial commentary.
    if _FORMAT.search(text) and re.search(
        r"\b(?:joining us|with us|our guest|speaking with|interview with|"
        r"conversation with|questions for|welcome|thank you for joining|"
        r"management says|md says|ceo says|chairman says|founder says)\b",
        text, re.I,
    ):
        return True, "transcript_role_and_interview_context"
    return False, "transcript_speaker_not_established"

