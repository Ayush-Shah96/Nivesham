from __future__ import annotations

"""Deterministic-only NewsAlert relevance assessment.

Groq scoring is intentionally disabled for NewsAlert while the collection and
fundamental-news filters are being validated. This module preserves the prior
function signature so existing callers continue working, but it performs no
network requests and never reads a Groq API key.
"""

from typing import Any

from apps.newsalert.fundamental_relevance import assess_fundamental_news

PROMPT_VERSION = "fundamental-rules-v2"


def _fallback(title: str, content: str, source: str, content_type: str) -> dict[str, Any]:
    result = assess_fundamental_news(
        title,
        content,
        source,
        content_type=content_type,
        portfolio_connection=True,
    )
    materiality = "HIGH" if result.is_material else "MEDIUM" if result.eligible else "LOW"
    return {
        "provider": "heuristic",
        "model": None,
        "score": int(max(0, min(100, result.score))),
        "materiality": materiality,
        "event_type": content_type if content_type in {"NEWS", "INTERVIEW", "POST"} else "NEWS",
        "rationale": (result.reason or ", ".join(result.matched_events) or "No fundamental event matched")[:1200],
        "eligible": bool(result.eligible),
    }


async def score_news_relevance(
    title: str,
    content: str,
    source: str,
    content_type: str,
    *,
    company_name: str = "",
    allow_groq: bool = False,
) -> dict[str, Any]:
    """Return deterministic fundamental relevance; never call an LLM provider."""
    del company_name, allow_groq  # retained only for backwards-compatible callers
    return _fallback(title, content, source, content_type)