from __future__ import annotations

"""Evidence-constrained Groq assessment for PE, warrant and preferential-issue events."""

import json
import logging
import re
from typing import Any

import httpx
from sqlalchemy import select

from infrastructure.config.settings import get_settings
from ii_platform.database.models import CapitalActionAssessment, CapitalActionSignal

logger = logging.getLogger(__name__)
CAPITAL_ASSESSMENT_VERSION = "capital-assessment-v2"


def _fallback(event: CapitalActionSignal) -> dict[str, Any]:
    event_type = str(event.event_type or "").upper()
    title = str(event.title or "").lower()
    details = str(event.details or "").lower()
    facts = " ".join(x for x in (event_type, title, details) if x)
    missing = []
    if not event.source_url:
        missing.append("source document URL")
    if event.quantity is None:
        missing.append("quantity / number of securities")
    if event.price is None and event.amount_inr is None:
        missing.append("issue price or consideration")
    # Never treat AI-generated values as official terms. Review is required whenever
    # a required numeric term is missing or the source lacks a document reference.
    known_event = any(x in event_type for x in ("PE", "WARRANT", "PREFERENTIAL", "STRATEGIC_ACQUISITION"))
    return {
        "provider": "rules",
        "model": None,
        "confidence": 70 if known_event and not missing else 35,
        "review_required": bool(missing) or not known_event,
        "assessment": "POTENTIAL_OPPORTUNITY" if known_event and not missing else "REVIEW",
        "rationale": "Deterministic source-field assessment; no legal terms inferred from missing data.",
        "missing_information": ", ".join(missing) or None,
    }


async def assess_capital_event(db, event: CapitalActionSignal) -> CapitalActionAssessment:
    existing = await db.scalar(
        select(CapitalActionAssessment).where(
            CapitalActionAssessment.capital_action_id == event.id,
            CapitalActionAssessment.assessment_version == CAPITAL_ASSESSMENT_VERSION,
        ).order_by(CapitalActionAssessment.created_at.desc()).limit(1)
    )
    if existing:
        return existing
    settings = get_settings()
    result = _fallback(event)
    api_key = str(settings.groq_api_key or "").strip()
    model = (settings.groq_model or "openai/gpt-oss-120b").strip()
    if api_key:
        prompt = (
            "Assess this Indian-listed-company capital ownership event for analyst triage. "
            "Allowed event classes are STRATEGIC_ACQUISITION, WARRANT, PREFERENTIAL_ISSUE. Use only disclosed evidence; "
            "do not invent terms, infer legal approval, or produce a buy recommendation. "
            "If evidence is insufficient, assessment=REVIEW and list missing_information. "
            "Return JSON fields confidence integer 0-100, review_required boolean, assessment "
            "(POTENTIAL_OPPORTUNITY/NO_CLEAR_OPPORTUNITY/REVIEW), rationale max 400 chars, missing_information max 300 chars.\n"
            f"Company: {event.stock_name}\nEvent type: {event.event_type}\nTitle: {event.title or ''}\n"
            f"Action: {event.action or ''}\nInvestor: {event.investor_name or ''}\n"
            f"Event date: {event.event_date}\nQuantity: {event.quantity}\nIssue price: {event.price}\n"
            f"Amount INR: {event.amount_inr}\nSource URL: {event.source_url or ''}\nDetails: {(event.details or '')[:6000]}"
        )
        try:
            async with httpx.AsyncClient(timeout=max(8.0, min(25.0, float(settings.groq_timeout_seconds)))) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "temperature": 0.0, "max_tokens": 350,
                          "messages": [{"role": "system", "content": "Return valid JSON only."},
                                       {"role": "user", "content": prompt}]},
                )
                response.raise_for_status()
                raw = response.json()["choices"][0]["message"]["content"]
                match = re.search(r"\{.*\}", str(raw), flags=re.S)
                if match:
                    parsed = json.loads(match.group(0))
                    assessment = str(parsed.get("assessment") or "REVIEW").upper()
                    if assessment not in {"POTENTIAL_OPPORTUNITY", "NO_CLEAR_OPPORTUNITY", "REVIEW"}:
                        assessment = "REVIEW"
                    result = {
                        "provider": "groq", "model": model,
                        "confidence": max(0, min(100, int(float(parsed.get("confidence", 0))))),
                        "review_required": bool(parsed.get("review_required", True)),
                        "assessment": assessment,
                        "rationale": " ".join(str(parsed.get("rationale") or "").split())[:2000],
                        "missing_information": " ".join(str(parsed.get("missing_information") or "").split())[:1000] or None,
                    }
        except Exception as exc:
            logger.warning("CAPITAL_GROQ_ASSESSMENT_FALLBACK event_id=%s error=%s", event.id, str(exc)[:300])
    record = CapitalActionAssessment(
        capital_action_id=event.id, provider=result["provider"], model=result.get("model"),
        assessment_version=CAPITAL_ASSESSMENT_VERSION, confidence=result["confidence"], review_required=result["review_required"],
        assessment=result["assessment"], rationale=result["rationale"],
        missing_information=result.get("missing_information"),
    )
    db.add(record)
    await db.flush()
    return record


async def assess_unreviewed_capital_events(db, limit: int = 25) -> dict[str, int]:
    query = (
        select(CapitalActionSignal)
        .outerjoin(CapitalActionAssessment, (CapitalActionAssessment.capital_action_id == CapitalActionSignal.id) & (CapitalActionAssessment.assessment_version == CAPITAL_ASSESSMENT_VERSION))
        .where(CapitalActionAssessment.id.is_(None), CapitalActionSignal.source == "NSE")
        .order_by(CapitalActionSignal.event_date.desc(), CapitalActionSignal.id.desc())
        .limit(max(1, min(int(limit), 100)))
    )
    events = list((await db.execute(query)).scalars().all())
    for event in events:
        await assess_capital_event(db, event)
    if events:
        await db.commit()
    return {"assessed": len(events)}
