"""Deterministic fundamental-news quality gate and relevance scoring.

Non-YouTube news keeps the six-factor NewsAlert score.

YouTube videos use a separate admission gate. A video is admitted only when:
  * its company is unambiguously on the Coverage List, and
  * one of these holds:
      1. it comes from a company-owned channel and is a corporate event
         (concall, AGM, results, investor meet), at least 15 minutes long;
      2. a named insider from the roster appears in the title or description
         and the video is an interview or corporate event, at least 5 minutes long;
      3. a publisher video whose TITLE announces a corporate event
         (concall, AGM, results, investor meet), at least 15 minutes long.
  * Role words alone (CEO, promoter, ...) never admit an interview unless
    NEWSALERT_YOUTUBE_ALLOW_ROLE_ONLY_INTERVIEWS=true.

The YouTube score is informational (ranking and display). The gate decides.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

MIN_RELEVANCE_SCORE = 62
MIN_MATERIALITY_SCORE = 14

YOUTUBE_MIN_DURATION_SECONDS = 120
YOUTUBE_MIN_INTERVIEW_SECONDS = 300
YOUTUBE_MIN_CORPORATE_EVENT_SECONDS = 900


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


ALLOW_PUBLISHER_CORPORATE_EVENTS = _env_flag("NEWSALERT_YOUTUBE_ALLOW_PUBLISHER_EVENTS", True)
ALLOW_ROLE_ONLY_INTERVIEWS = _env_flag("NEWSALERT_YOUTUBE_ALLOW_ROLE_ONLY_INTERVIEWS", False)


# ---------------------------------------------------------------- term matching
@lru_cache(maxsize=8192)
def _term_regex(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term.strip())}(?![a-z0-9])", re.IGNORECASE)


def _term_in(text: str, term: str) -> bool:
    """Whole-term match. Replaces raw substring tests, which matched 'import' in 'important'."""
    return bool(term.strip()) and _term_regex(term).search(text) is not None


def _has(text: str, markers: tuple[str, ...]) -> bool:
    return any(_term_in(text, marker) for marker in markers)


def _squash(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _coerce_seconds(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


# ---------------------------------------------------------------- trading / stock-tip noise
_TRADING_NOISE = tuple(re.compile(p, re.I) for p in (
    r"\btechnical (?:analysis|view|setup|call|indicator|indicators)\b",
    r"\bchart (?:pattern|patterns|analysis|setup|reading)\b",
    r"\b(?:candle\s*stick|candlestick)(?:s| pattern| patterns)?\b",
    r"\b(?:doji|heikin ashi|renko chart|fibonacci levels?|bollinger bands|macd|rsi|relative strength index|stochastic oscillator|moving averages?|supertrend|ichimoku|pivot points?)\b",
    r"\b(?:technical signals?|chart signals?|entry levels?|exit levels?|stop loss|take profit|risk.reward ratio)\b",
    r"\b(?:breakout|breakdown) (?:pattern|setup|signal|level|trade)\b",
    r"\b(?:intraday|swing trade|day trading|trading strategy|trade setup|scalping|scalper|btst|stbt)\b",
    r"\b(?:support and resistance|support/resistance|option chain|options trading|f\s*&\s*o strategy|futures and options strategy)\b",
    r"\b(?:stock tips?|stock picks?|stock recommendations?|share recommendations?|recommended stocks?|trading recommendations?)\b",
    r"\b(?:stocks? to (?:buy|sell|watch)|shares? to (?:buy|sell)|buy or sell|buy/sell/hold)\b",
    r"\b(?:top gainers|top losers|market wrap|market roundup|market round up|muhurat picks)\b",
    r"\b(?:price target|target price|brokerage (?:upgrade|downgrade|call)|buy rating|sell rating)\b",
    r"\b(?:analysts?|brokers?) (?:recommend|recommends|recommendation|maintain|assign|give) (?:a )?(?:buy|sell|hold|overweight|underweight)\b",
    r"\b(?:buy|sell|hold) (?:call|recommendation|rating)\b",
    r"\b(?:multibagger|breakout stocks?|horoscope|gold rate today|gold price today)\b",
    r"\b(?:top|best|hot|multibagger) stocks? (?:to buy|for today|for tomorrow|for this week|for long term)\b",
    r"\b(?:stock|share) price targets?\b|\btarget price for (?:the )?(?:stock|share)\b",
    # Bare "buy"/"sell" are deliberately absent: they rejected ordinary corporate
    # events such as "to sell stake", "to buy 51% stake" and "buy-back".
    r"\b(?:buy|sell|hold|accumulate) (?:the )?(?:stock|share|shares)\b",
    r"\b(?:stock picks?|share picks?|investment tips?|trading tips?|stock ideas?|equity picks?|"
    r"stocks? to watch|shares? to watch|stocks? in focus|top picks?|stock calls?|calls on stocks?|"
    r"buy ratings?|sell ratings?|hold ratings?)\b",
    r"\b(?:stocks? likely to rise|stocks? that could rally|best stocks? for today)\b",
    r"\b(?:technical calls?|chartists?|momentum trading|options strategy|option buying|option selling|trade idea|trading calls?)\b",
))

_STOCK_ADVICE_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"\b(?:stock|share)s?\s+(?:tips?|picks?|recommendations?|calls?)\b",
    r"\b(?:stocks?|shares?)\s+to\s+(?:buy|sell|watch)\b",
    r"\b(?:top|best|hot|multibagger)\s+stocks?\b",
    r"\b(?:investment|trading)\s+tips?\b",
    r"\b(?:buy|sell|hold)\s+(?:call|recommendation|rating)\b",
    r"\b(?:price|target)\s+targets?\b|\btarget price\b",
    r"\b(?:buy or sell|buy/sell/hold|should you buy|time to buy|time to sell)\b",
    r"\b(?:technical analysis|intraday|swing trade|day trading|scalping|options? strategy|f\s*&\s*o strategy|trading calls?)\b",
    r"\bwhich stocks? (?:to buy|to sell|will rally)\b",
))


def _stock_advice_reason(text: str) -> str | None:
    return "stock_recommendation_or_trading_content" if any(p.search(text) for p in _STOCK_ADVICE_PATTERNS) else None


# ---------------------------------------------------------------- lexicons
_HIGH_EVENTS = (
    "financial results", "quarterly results", "annual results", "earnings results", "earnings",
    "revenue", "ebitda", "operating profit", "net profit", "profit warning", "loss widens",
    "order win", "wins order", "bags order", "contract worth", "wins contract", "awarded contract",
    "order received", "receives order", "order worth", "gets order", "order book", "large order", "letter of award",
    "merger", "acquisition", "acquires", "acquired", "demerger", "divestment", "stake sale",
    "capital expenditure", "capex", "capacity expansion", "new plant", "plant shutdown",
    "production halt", "credit rating", "rating downgrade", "rating upgrade", "debt default",
    "loan default", "debt restructuring", "regulatory approval", "usfda approval", "cdsco approval",
    "drug approval", "fda approval", "clinical trial", "product recall", "sebi order", "sebi penalty",
    "cci order", "nclt", "insolvency", "bankruptcy", "fraud", "forensic audit",
    "investigation", "search and seizure", "tax demand", "material litigation", "promoter pledge",
    "pledged shares", "pledge release", "auditor resignation", "ceo resigns", "cfo resigns",
    "resignation of managing director", "dividend", "dividend declared", "interim dividend", "special dividend",
    "bonus issue", "stock split", "share buyback", "fund raising", "fundraise", "rights issue",
    "preferential issue", "qualified institutional placement", "qip", "related party transaction",
    "material subsidiary", "data breach", "cyber attack", "cybersecurity breach", "license revoked",
    "licence revoked", "patent infringement", "import ban", "export ban", "recall notice",
)

_MEDIUM_EVENTS = (
    "new product", "product launch", "launches product", "new customer", "customer agreement",
    "contract", "supply agreement", "long term agreement", "joint venture", "strategic partnership",
    "expands into", "expansion plan", "enters a new market", "manufacturing facility",
    "commercial production", "production capacity", "price increase", "price hike", "price cut",
    "management change", "appointed as ceo", "appointed as cfo", "new ceo", "new cfo",
    "appointment of director", "appointment of ceo", "appointment of cfo", "independent director",
    "resignation of director", "resignation of ceo", "resignation of cfo", "director resigns",
    "director appointed", "auditor appointed", "show cause notice",
    "regulatory notice", "court order", "lawsuit", "litigation", "credit outlook",
    "rating reaffirmed", "downgraded by", "upgraded by", "promoter holding", "shareholding pattern",
    "export restriction", "import duty", "tariff", "government tender", "bid awarded",
    "factory closure", "workforce reduction", "layoffs", "restructuring", "recall",
    "patent granted", "patent application", "environmental clearance", "land acquisition",
)

_THESIS_SIGNALS = (
    "revenue", "profit", "ebitda", "margin", "cash flow", "working capital", "debt", "default",
    "order book", "capacity", "capex", "market share", "customer", "supplier", "contract",
    "regulatory", "approval", "license", "licence", "clinical", "patent", "governance",
    "fraud", "investigation", "acquisition", "merger", "divestment", "promoter", "pledge",
    "litigation", "tax demand", "production", "shutdown", "recall", "export", "import",
)

_INTERVIEW_TOPIC_SIGNALS = (
    "business", "company", "management", "ceo", "cfo", "founder", "leadership",
    "results", "earnings", "revenue", "profit", "margin", "growth", "demand",
    "strategy", "outlook", "industry", "sector", "market share", "capex", "capacity",
    "order book", "orders", "operations", "manufacturing", "expansion", "competition",
    "customer", "supply chain", "cash flow", "debt", "balance sheet", "working capital",
    "regulation", "regulatory", "policy", "merger", "acquisition", "investment",
    "valuation", "capital allocation", "economy", "inflation", "interest rates",
    "export", "import", "technology", "product", "financial", "business model",
    "clinical", "drug", "approval", "governance", "promoter", "annual report", "concall",
    "agm", "egm", "annual general meeting", "extraordinary general meeting",
    "shareholders meeting", "shareholder meeting", "investor meet", "analyst meet",
    "earnings call", "conference call", "results call", "quarterly results",
    "financial results", "investor presentation", "investor day", "capital markets day",
    "management discussion", "management commentary", "promoter interview",
)

_X_FINANCIAL_SIGNALS = (
    "earnings", "results", "revenue", "profit", "ebitda", "margin", "cash flow",
    "free cash flow", "working capital", "balance sheet", "debt", "annual report",
    "concall", "management", "corporate governance", "fraud", "accounting", "valuation",
    "business model", "moat", "market share", "industry", "sector", "demand", "capex",
    "order book", "order win", "regulatory", "regulation", "policy", "rbi", "sebi",
    "inflation", "interest rates", "gdp", "economy", "tax", "budget", "exports", "imports",
    "manufacturing", "supply chain", "promoter", "insider", "dividend", "buyback",
    "acquisition", "merger", "investment thesis", "company", "business", "startup",
    "capital allocation", "roce", "roic", "return on capital", "inventory", "pricing power",
    "competition", "competitor", "growth", "credit rating", "default", "loan", "banking",
    "currency", "rupee", "crude oil", "commodity", "trade deficit", "monetary policy",
    "fiscal policy", "government spending", "interest expense", "cash conversion",
)

_OFFICIAL_SOURCE_MARKERS = (
    "nse corporate", "national stock exchange", "nse india", "bse india", "bombay stock exchange",
    "nse corporate announcements", "nse board meetings", "nse corporate filings",
    "bse corporate announcements", "regulatory announcements",
    "sebi", "reserve bank of india", "rbi", "ministry of corporate affairs", "mca",
    "competition commission", "cci", "nclt", "cdsco", "dgft", "crisil", "icra",
    "company investor relations", "investor relations", "company website", "official filing",
    "company press release", "regulatory filing", "exchange filing",
)

_TIER_ONE_MEDIA = (
    "reuters", "bloomberg", "the economic times", "economic times", "livemint", "mint",
    "business standard", "cnbc-tv18", "cnbc tv18", "moneycontrol",
    "the hindu businessline", "business today", "ndtv profit", "ndtvprofit",
)


def _flatten(value: Any, depth: int = 0) -> str:
    if depth > 4 or value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, (dict, list)):
                    return _flatten(parsed, depth + 1)
            except Exception:
                pass
        return text
    if isinstance(value, dict):
        preferred = ("text", "summary", "description", "announcement_subject", "announcement_details",
                     "subject", "details", "body", "content", "headline", "title", "nse")
        pieces = []
        for key in preferred:
            if key in value:
                part = _flatten(value[key], depth + 1)
                if part:
                    pieces.append(part)
        if not pieces:
            for child in list(value.values())[:30]:
                part = _flatten(child, depth + 1)
                if part:
                    pieces.append(part)
        return " ".join(pieces)
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(item, depth + 1) for item in list(value)[:50])
    return str(value)


_MARKET_ONLY_PHRASES = ("sensex today", "nifty today", "market outlook", "market live", "stocks in focus")
_MARKET_MOVE_RE = re.compile(r"\b(?:shares?|stocks?)\s+(?:surge|soar|jump|rise|rally|gain|fall|drop|slip|decline)\b", re.I)


def _is_market_only_headline(title: str) -> bool:
    return _has(title, _MARKET_ONLY_PHRASES) or bool(_MARKET_MOVE_RE.search(title))


def _rejection_reason(title: str, *, include_market_noise: bool = True) -> str | None:
    for pattern in _TRADING_NOISE:
        if pattern.search(title):
            return "trading_or_stock_recommendation"
    if include_market_noise and _is_market_only_headline(title) and not _has(title, _HIGH_EVENTS + _MEDIUM_EVENTS):
        return "generic_market_commentary"
    return None


def _source_reliability(source: str, content_type: str) -> int:
    name = source.casefold()
    if any(marker in name for marker in _OFFICIAL_SOURCE_MARKERS):
        return 10
    # Checked before publisher names: a YouTube channel is a video host, not a
    # newswire, so its reliability must not inherit tier-one publisher status.
    if "youtube" in name or content_type == "INTERVIEW":
        return 5
    if any(marker in name for marker in _TIER_ONE_MEDIA):
        return 8
    if "x / twitter" in name or "twitter" in name or "x.com" in name or content_type == "POST":
        return 4
    return 6


@dataclass(frozen=True)
class FundamentalAssessment:
    score: int
    eligible: bool
    is_material: bool
    factors: dict[str, int]
    reason: str | None = None
    matched_events: tuple[str, ...] = ()


# ---------------------------------------------------------------- X posts (unchanged policy)
def assess_x_post(title: Any, content: Any = "", source: Any = "", *, is_new: bool = True) -> FundamentalAssessment:
    """Score an allowlisted public X post without requiring a Coverage List match."""
    title_text = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    body_text = re.sub(r"\s+", " ", _flatten(content)).strip().casefold()[:12000]
    all_text = f"{title_text} {body_text}".strip()
    source_text = str(source or "")
    rejection = _rejection_reason(title_text, include_market_noise=False) or _stock_advice_reason(all_text)
    if rejection:
        return FundamentalAssessment(0, False, False, {
            "financial_relevance": 0, "event_significance": 0,
            "source_reliability": _source_reliability(source_text, "POST"),
            "novelty": 10 if is_new else 0,
        }, rejection, ())

    hits = tuple(dict.fromkeys(signal for signal in _X_FINANCIAL_SIGNALS if _term_in(all_text, signal)))
    strong = _has(all_text, (
        "earnings", "results", "revenue", "profit", "ebitda", "cash flow", "annual report",
        "concall", "valuation", "market share", "order book", "capex", "regulatory",
        "rbi", "sebi", "inflation", "interest rates", "gdp", "governance", "acquisition",
        "merger", "credit rating", "default", "dividend", "buyback", "roce", "roic",
        "balance sheet", "working capital", "business model", "capital allocation",
    ))
    generic_only = {"company", "business", "industry", "sector", "growth", "startup", "competitor"}
    specific_hits = tuple(hit for hit in hits if hit not in generic_only)
    if not specific_hits or (not strong and len(specific_hits) < 2) or (len(title_text) < 30 and not strong):
        return FundamentalAssessment(0, False, False, {
            "financial_relevance": 0, "event_significance": 0,
            "source_reliability": _source_reliability(source_text, "POST"),
            "novelty": 10 if is_new else 0,
        }, "x_post_has_no_clear_financial_or_business_signal", hits[:12])

    event_hits = tuple(m for m in (_HIGH_EVENTS + _MEDIUM_EVENTS) if _term_in(all_text, m))
    factors = {
        "financial_relevance": min(60, 45 + min(15, len(specific_hits) * 3)),
        "event_significance": 15 if event_hits else 5,
        "source_reliability": _source_reliability(source_text, "POST"),
        "novelty": 10 if is_new else 0,
    }
    score = min(100, sum(factors.values()))
    return FundamentalAssessment(
        score=score, eligible=score >= MIN_RELEVANCE_SCORE,
        is_material=bool(event_hits), factors=factors,
        reason=None if score >= MIN_RELEVANCE_SCORE else "below_relevance_threshold",
        matched_events=tuple(dict.fromkeys(event_hits + hits))[:12],
    )


# ---------------------------------------------------------------- non-YouTube news
def assess_fundamental_news(
    title: Any,
    content: Any = "",
    source: Any = "",
    *,
    content_type: Any = "NEWS",
    portfolio_connection: bool = True,
    is_new: bool = False,
) -> FundamentalAssessment:
    """Evaluate a publisher or filing candidate using the six weighted factors."""
    title_text = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    body_text = re.sub(r"\s+", " ", _flatten(content)).strip().casefold()[:50000]
    source_text = str(source or "").strip()
    ctype = str(content_type or "NEWS").strip().upper()
    all_text = f"{title_text} {body_text}".strip()

    rejection = _rejection_reason(title_text, include_market_noise=True)
    if ctype in {"INTERVIEW", "POST"} and not rejection:
        rejection = _stock_advice_reason(all_text)
    if rejection:
        return FundamentalAssessment(0, False, False, {
            "portfolio_connection": 30 if portfolio_connection else 0,
            "fundamental_materiality": 0, "event_significance": 0,
            "source_reliability": _source_reliability(source_text, ctype),
            "novelty": 10 if is_new else 0, "investment_thesis_relevance": 0,
        }, rejection, ())

    high_title = tuple(m for m in _HIGH_EVENTS if _term_in(title_text, m))
    high_body = tuple(m for m in _HIGH_EVENTS if _term_in(body_text, m))
    medium_title = tuple(m for m in _MEDIUM_EVENTS if _term_in(title_text, m))
    medium_body = tuple(m for m in _MEDIUM_EVENTS if _term_in(body_text, m))
    matched = tuple(dict.fromkeys(high_title + high_body + medium_title + medium_body))

    if high_title:
        materiality, significance = 25, 15
    elif high_body:
        materiality, significance = 21, 12
    elif medium_title:
        materiality, significance = 18, 10
    elif medium_body:
        materiality, significance = 14, 7
    else:
        materiality, significance = 0, 0

    if _has(title_text, _THESIS_SIGNALS):
        thesis = 10
    elif _has(body_text, _THESIS_SIGNALS):
        thesis = 7
    elif materiality:
        thesis = 5
    else:
        thesis = 0

    factors = {
        "portfolio_connection": 30 if portfolio_connection else 0,
        "fundamental_materiality": materiality,
        "event_significance": significance,
        "source_reliability": _source_reliability(source_text, ctype),
        "novelty": 10 if is_new else 0,
        "investment_thesis_relevance": thesis,
    }
    score = min(100, sum(factors.values()))
    eligible = portfolio_connection and materiality >= MIN_MATERIALITY_SCORE and score >= MIN_RELEVANCE_SCORE
    reason = None
    if not portfolio_connection:
        reason = "no_portfolio_connection"
    elif materiality < MIN_MATERIALITY_SCORE:
        reason = "no_fundamental_event"
    elif score < MIN_RELEVANCE_SCORE:
        reason = "below_relevance_threshold"
    return FundamentalAssessment(
        score=score, eligible=eligible,
        is_material=eligible and materiality >= 18 and significance >= 10,
        factors=factors, reason=reason, matched_events=matched[:12],
    )


# ---------------------------------------------------------------- YouTube
# Event kinds are matched on whole terms. Titles are authoritative for what a
# video is; descriptions can add context but cannot by themselves admit a video.
_YT_EVENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("agm", re.compile(
        r"\b(?:agms?|egms?|annual general meetings?|extraordinary general meetings?|shareholders'? meetings?)\b",
        re.IGNORECASE)),
    ("concall", re.compile(
        r"\b(?:con[- ]?calls?|conference calls?|earnings calls?|earnings conference(?: calls?)?|investor calls?)\b",
        re.IGNORECASE)),
    ("results", re.compile(
        r"\b(?:quarterly results?|annual results?|financial results?|results? (?:highlights|review|analysis|discussion|announcement)|"
        r"q[1-4](?:\s*fy\s*\d{2,4})?\s*(?:results?|earnings)|fy\s*\d{2,4}\s*(?:q[1-4]\s*)?results?)\b",
        re.IGNORECASE)),
    ("investor_meet", re.compile(
        r"\b(?:investor (?:meets?|presentations?|days?|conferences?)|analyst meets?|capital markets? days?)\b",
        re.IGNORECASE)),
    ("interview", re.compile(
        r"\b(?:interviews?|in conversation|conversation with|fireside chat|one[- ]on[- ]one|q\s*&\s*a|"
        r"talks? (?:to|with)|speaks? (?:to|with)|exclusive interaction|podcast|dialogue with|interacts? with|"
        r"chat with|promoter interaction|management interaction|ask me anything)\b",
        re.IGNORECASE)),
)

CORPORATE_EVENT_KINDS = frozenset({"agm", "concall", "results", "investor_meet"})

# Role words identify the kind of person, not who is speaking. "Manager", "director"
# and "executive" are excluded because they admit fund managers, analysts and
# regulators' staff. Non-executive directors are not insiders; the alternation
# still matches "executive director", so keep the roster for precision.
_YT_ROLE_RE = re.compile(
    r"\b(?:promoters?|co-?founders?|founders?|chairman|chairperson|chairwoman|"
    r"managing directors?|whole[- ]time directors?|executive directors?|executive chairman|"
    r"ceo|cfo|coo|cto|chief executive(?: officer)?|chief financial officer|chief operating officer)\b"
    r"|(?-i:\bMD\b)",
    re.IGNORECASE,
)

_YT_LOW_VALUE_TITLE = re.compile(
    r"\b(?:teasers?|trailers?|song launch|music video|shorts|celebrity gossip|movie review|film review|"
    r"cricket highlights|football highlights|horoscope|live blogs?|opening bell|closing bell|market wrap|"
    r"stocks? in news|stocks? to watch|top gainers|top losers|morning brief|pre[- ]market)\b",
    re.IGNORECASE,
)
_YT_OFF_TOPIC_TITLE = re.compile(
    r"\b(?:protests?|demonstrations?|politics|political|election campaign|campaign rally|murder|rape|"
    r"kidnapping|stampede|crime)\b",
    re.IGNORECASE,
)
# Narrow on purpose: "target price" is common brokerage boilerplate in genuine
# results-call descriptions, so only explicit stock-tip phrases are checked here.
_YT_DESCRIPTION_NOISE = re.compile(
    r"\b(?:stock tips?|stocks? to buy|buy or sell|intraday calls?|trading calls?|multibagger picks?|"
    r"top stocks to buy|stock picks?|share picks?)\b",
    re.IGNORECASE,
)


def youtube_event_kinds(text: str) -> frozenset[str]:
    return frozenset(kind for kind, pattern in _YT_EVENT_PATTERNS if pattern.search(text))


def youtube_candidate_cue(title: Any, description: Any = "") -> bool:
    """Cheap pre-filter used before metadata enrichment. Not an admission decision."""
    text = f"{_squash(title)} {_squash(_flatten(description))[:5000]}"
    return bool(youtube_event_kinds(text)) or bool(_YT_ROLE_RE.search(text))


def youtube_static_reject_reason(title: Any, description: Any = "", duration_seconds: Any = None) -> str | None:
    """Content rules that need no company context. Returns a reject reason or None."""
    title_text = _squash(title)
    desc_text = _squash(_flatten(description))[:50000]
    if not title_text:
        return "missing_title"
    duration = _coerce_seconds(duration_seconds)
    if duration is not None and duration < YOUTUBE_MIN_DURATION_SECONDS:
        return "short_or_clip"
    title_low = title_text.casefold()
    if _stock_advice_reason(title_low):
        return "stock_recommendation_or_trading_content"
    reason = _rejection_reason(title_low, include_market_noise=False)
    if reason:
        return reason
    if _YT_LOW_VALUE_TITLE.search(title_text):
        return "youtube_low_value_or_market_commentary"
    if _YT_OFF_TOPIC_TITLE.search(title_text):
        return "off_topic_title"
    if _YT_DESCRIPTION_NOISE.search(desc_text):
        return "stock_recommendation_in_description"
    if _is_market_only_headline(title_low) and not youtube_event_kinds(f"{title_text} {desc_text}") \
            and not _YT_ROLE_RE.search(title_text):
        return "generic_market_commentary"
    return None


def assess_youtube_promoter_relevance(
    title: Any,
    description: Any = "",
    source: Any = "YouTube",
    *,
    portfolio_connection: bool,
    is_new: bool = False,
    duration_seconds: Any = None,
    from_company_channel: bool = False,
    named_insiders: tuple[str, ...] | list[str] = (),
    channel_title: Any = None,
) -> FundamentalAssessment:
    """Admission gate for a YouTube video. See the module docstring for the rules.

    The weighted score is returned for ranking and display only; `eligible` is set
    by the evidence path that matched, not by the score.
    """
    title_text = _squash(title)
    desc_text = _squash(_flatten(description))[:50000]
    combined = f"{title_text} {desc_text}"
    combined_low = combined.casefold()
    title_low = title_text.casefold()
    duration = _coerce_seconds(duration_seconds)
    novelty = 5 if is_new else 0

    def rejected(reason: str, *, speaker: int = 0) -> FundamentalAssessment:
        factors = {
            "portfolio_connection": 25 if portfolio_connection else 0,
            "speaker_role_confirmation": speaker,
            "fundamental_materiality": 0,
            "event_significance": 0,
            "source_reliability": 5,
            "novelty": novelty,
            "investment_thesis_relevance": 0,
        }
        return FundamentalAssessment(sum(factors.values()), False, False, factors, reason, ())

    static_reason = youtube_static_reject_reason(title_text, desc_text, duration)
    if static_reason:
        return rejected(static_reason)
    if not portfolio_connection:
        return rejected("no_coverage_company_match")
    # Unknown duration is not a rejection by itself. Atom feeds and some yt-dlp
    # failures omit it, and rejecting on it emptied the whole YouTube feed. It only
    # blocks the publisher path, where a clip could otherwise pass as an event.
    # Verified-speaker paths keep the speaker evidence and are marked as unverified.
    duration_known = duration is not None
    def long_enough(minimum: int) -> bool:
        return duration is None or duration >= minimum

    title_kinds = youtube_event_kinds(title_text)
    desc_kinds = youtube_event_kinds(desc_text)
    corp_title = bool(title_kinds & CORPORATE_EVENT_KINDS)
    corp_any = corp_title or bool(desc_kinds & CORPORATE_EVENT_KINDS)
    interview_any = "interview" in (title_kinds | desc_kinds)
    role_hit = bool(_YT_ROLE_RE.search(combined))
    named = tuple(name for name in named_insiders if _term_in(combined_low, name))

    path: str | None = None
    speaker = 0
    if from_company_channel and corp_any and long_enough(YOUTUBE_MIN_CORPORATE_EVENT_SECONDS):
        path, speaker = "company_channel_corporate_event", 25
    elif named and (corp_any or interview_any) and long_enough(YOUTUBE_MIN_INTERVIEW_SECONDS):
        path, speaker = "named_insider", 25
    elif ALLOW_PUBLISHER_CORPORATE_EVENTS and corp_title and duration_known \
            and duration >= YOUTUBE_MIN_CORPORATE_EVENT_SECONDS:
        path, speaker = "publisher_corporate_event", 8
    elif ALLOW_ROLE_ONLY_INTERVIEWS and interview_any and role_hit and duration_known \
            and duration >= YOUTUBE_MIN_INTERVIEW_SECONDS:
        path, speaker = "role_word_interview_unverified", 12

    if path is None:
        if not duration_known and corp_title:
            reason = "publisher_event_needs_known_duration"
        elif corp_any and duration_known and duration < YOUTUBE_MIN_CORPORATE_EVENT_SECONDS:
            reason = "corporate_event_under_15_minutes"
        elif interview_any and not named:
            reason = "interview_without_named_insider"
        else:
            reason = "no_company_event_or_insider_evidence"
        return rejected(reason, speaker=12 if role_hit else 0)

    all_kinds = title_kinds | desc_kinds
    if all_kinds & {"agm", "concall", "results"}:
        materiality = 20
    elif "investor_meet" in all_kinds:
        materiality = 16
    else:
        materiality = 14
    high_title = _has(title_low, _HIGH_EVENTS)
    if high_title:
        event_significance = 15
    elif _has(title_low, _MEDIUM_EVENTS):
        event_significance = 8
    else:
        event_significance = 5
    thesis = 5 if _has(combined_low, _THESIS_SIGNALS) else 0

    factors = {
        "portfolio_connection": 25,
        "speaker_role_confirmation": speaker,
        "fundamental_materiality": materiality,
        "event_significance": event_significance,
        "source_reliability": 5,
        "novelty": novelty,
        "investment_thesis_relevance": thesis,
    }
    matched: list[str] = [path]
    matched.extend(f"kind:{kind}" for kind in sorted(all_kinds))
    matched.extend(f"insider:{name}" for name in named)
    if from_company_channel:
        matched.append("company_owned_channel")
    if not duration_known:
        matched.append("duration_unverified")
    return FundamentalAssessment(
        score=min(100, sum(factors.values())),
        eligible=True,
        is_material=bool(corp_any or high_title),
        factors=factors,
        reason=None,
        matched_events=tuple(matched)[:12],
    )


# ---------------------------------------------------------------- roster configuration
# JSON file (path in NEWSALERT_YOUTUBE_ROSTER_PATH), keyed by Coverage List company name:
#   {"Acme Ltd": {"insiders": ["Full Name", ...], "channels": ["UC..."]}}
# insiders: full names of promoters, MDs, CEOs, CFOs, chairmen (2+ words required).
# channels: YouTube channel IDs the company itself publishes from (IR channels).
_ROSTER_CACHE: dict[str, dict[str, Any]] | None = None


def _norm_company(name: Any) -> str:
    return _squash(name).casefold()


def _load_roster() -> dict[str, dict[str, Any]]:
    global _ROSTER_CACHE
    if _ROSTER_CACHE is not None:
        return _ROSTER_CACHE
    data: dict[str, dict[str, Any]] = {}
    path = os.getenv("NEWSALERT_YOUTUBE_ROSTER_PATH", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning("YOUTUBE_ROSTER_UNREADABLE path=%s error=%s", path, type(exc).__name__)
            raw = {}
        if isinstance(raw, dict):
            for company, cfg in raw.items():
                if not isinstance(cfg, dict):
                    continue
                insiders: list[str] = []
                for name in cfg.get("insiders") or []:
                    clean = _squash(name)
                    if len(clean.split()) >= 2:
                        insiders.append(clean)
                    elif clean:
                        logger.warning("YOUTUBE_ROSTER_SKIP_SINGLE_TOKEN company=%s name=%r", company, clean)
                channels = frozenset(_squash(c) for c in (cfg.get("channels") or []) if _squash(c))
                data[_norm_company(company)] = {"insiders": tuple(insiders), "channels": channels}
    _ROSTER_CACHE = data
    return data


def youtube_insiders_for(company_name: Any) -> tuple[str, ...]:
    entry = _load_roster().get(_norm_company(company_name))
    return tuple(entry["insiders"]) if entry else ()


def youtube_company_channels_for(company_name: Any) -> frozenset[str]:
    entry = _load_roster().get(_norm_company(company_name))
    return frozenset(entry["channels"]) if entry else frozenset()


def youtube_company_channel_ids() -> frozenset[str]:
    ids: set[str] = set()
    for entry in _load_roster().values():
        ids.update(entry["channels"])
    return frozenset(ids)