from __future__ import annotations

"""Deterministic NewsAlert topic classifier.

The classifier deliberately runs without an LLM so the 4-hour worker can classify
large NSE announcement batches cheaply and reproducibly.  YouTube and X source
channels are handled separately as INTERVIEW/POST.
"""

from typing import Any
import re

NEWS_TYPES = (
    "DIVIDEND_ISSUE",
    "MERGER_DEMERGER",
    "FINANCIAL_RESULTS",
    "BONUS_STOCK_SPLIT",
    "GOVERNANCE",
    "INSIDE_BUYING",
    "SEBI_ORDERS",
    "CREDIT_DEBT",
    "NEWS",
    "INTERVIEW",
    "POST",
)

TYPE_LABELS = {
    "DIVIDEND_ISSUE": "Dividend Issue",
    "MERGER_DEMERGER": "Merger / Demerger",
    "FINANCIAL_RESULTS": "Financial Results",
    "BONUS_STOCK_SPLIT": "Bonus / Stock Split",
    "GOVERNANCE": "Governance",
    "INSIDE_BUYING": "Inside Buying",
    "SEBI_ORDERS": "SEBI Orders",
    "CREDIT_DEBT": "Credit / Debt",
    "NEWS": "News",
    "INTERVIEW": "Interview",
    "POST": "Post",
}

TYPE_COLORS = {
    "DIVIDEND_ISSUE": "emerald",
    "MERGER_DEMERGER": "violet",
    "FINANCIAL_RESULTS": "indigo",
    "BONUS_STOCK_SPLIT": "amber",
    "GOVERNANCE": "slate",
    "INSIDE_BUYING": "rose",
    "SEBI_ORDERS": "red",
    "CREDIT_DEBT": "orange",
    "NEWS": "black",
    "INTERVIEW": "red",
    "POST": "blue",
}

_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "SEBI_ORDERS",
        (
            "sebi order", "order passed by sebi", "sebi adjudication", "sebi penalty",
            "securities and exchange board of india", "whole time member", "wtm order",
            "settlement order", "adjudicating officer", "final order of sebi",
            "sebi settlement", "sebi proceedings", "sebi interim order",
        ),
    ),
    (
        "INSIDE_BUYING",
        (
            "pit disclosure", "pits disclosure", "regulation 7(2)", "regulation 7 2",
            "regulation 7", "insider trading", "prohibition of insider trading",
            "promoter purchase", "promoter buying", "promoter acquisition",
            "promoter sale", "promoter sold", "acquisition by promoter",
            "designated person", "connected person", "acquisition of shares by promoter",
            "sale of shares by promoter", "change in shareholding of promoter",
        ),
    ),
    (
        "MERGER_DEMERGER",
        (
            "merger", "amalgamation", "demerger", "scheme of arrangement",
            "scheme of amalgamation", "scheme of merger", "arrangement with shareholders",
            "arrangement with creditors", "slump sale", "business transfer agreement",
            "composite scheme of arrangement", "spin-off", "spin off",
        ),
    ),
    (
        "FINANCIAL_RESULTS",
        (
            "financial results", "financial result", "quarterly results", "audited results",
            "unaudited results", "financial statement", "results for the quarter",
            "results for the year", "financial performance", "standalone financial results",
            "consolidated financial results", "results conference call", "earnings call",
            "earnings release", "limited review report", "financial highlights",
        ),
    ),
    (
        "DIVIDEND_ISSUE",
        (
            "dividend", "interim dividend", "final dividend", "special dividend",
            "dividend declared", "dividend recommendation", "dividend payment",
            "record date for dividend", "dividend record date", "declaration of dividend",
        ),
    ),
    (
        "BONUS_STOCK_SPLIT",
        (
            "bonus issue", "bonus shares", "bonus allotment", "bonus issue of equity shares",
            "stock split", "split of shares", "sub-division", "sub division",
            "subdivision of shares", "face value split", "split from rs",
            "sub-division of equity", "record date for bonus", "record date for stock split",
        ),
    ),
    (
        "CREDIT_DEBT",
        (
            "credit rating", "rating reaffirmed", "rating action", "downgrade in rating",
            "upgrade in rating", "debenture", "non convertible debenture", "ncd",
            "debt", "borrowing", "loan facility", "lender", "refinancing",
            "default", "commercial paper", "credit watch", "debt securities",
            "credit assessment", "rating rationale",
        ),
    ),
    (
        "GOVERNANCE",
        (
            "outcome of board meeting", "board meeting", "board of directors",
            "appointment of director", "resignation of director", "resignation",
            "appointment", "independent director", "additional director", "auditor",
            "company secretary", "key managerial personnel", "agm",
            "annual general meeting", "extraordinary general meeting", "shareholders meeting",
            "scrutinizer", "trading window", "code of conduct", "compliance",
            "corporate governance", "material subsidiary", "related party transaction",
            "change in director", "change in auditor", "secretarial audit",
            "postal ballot", "investor meet", "investor meeting",
        ),
    ),
)


def _normalize(value: Any) -> str:
    text = str(value or "").casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/%&+.-]", " ", text)).strip()


def classify_topic(title: Any, summary: Any = "", *, source_type: str | None = None) -> str:
    """Classify corporate/publisher items into the canonical NewsAlert taxonomy.

    For NSE Corporate Announcements the classifier uses subject + details, with
    specific corporate-event categories taking precedence over the generic NEWS
    fallback. Channel semantics remain fixed: YouTube=INTERVIEW, X=POST.
    """
    source = _normalize(source_type)
    if "youtube" in source:
        return "INTERVIEW"
    if "x / twitter" in source or "twitter" in source or "x.com" in source:
        return "POST"

    subject = _normalize(title)
    details = _normalize(summary)
    haystack = " ".join(part for part in (subject, details) if part).strip()
    if not haystack:
        return "NEWS"

    # Subject is stronger evidence than body text. This prevents generic body terms
    # such as "board" from overriding an explicit "financial results" subject.
    for topic, markers in _PATTERNS:
        if any(marker in subject for marker in markers):
            return topic

    # Then inspect details, but use the whole event taxonomy. Generic NEWS remains
    # the final fallback for ordinary announcements.
    for topic, markers in _PATTERNS:
        if any(marker in details for marker in markers):
            return topic

    return "NEWS"


def type_label(value: Any) -> str:
    key = str(value or "NEWS").upper()
    return TYPE_LABELS.get(key, TYPE_LABELS["NEWS"])
