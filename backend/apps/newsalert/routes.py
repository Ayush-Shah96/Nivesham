from __future__ import annotations

import asyncio
import json
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urlparse
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from types import SimpleNamespace

from bs4 import BeautifulSoup

import logging 

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, case, delete, func, not_, or_, select

from infrastructure.config.settings import get_settings
from infrastructure.security.permissions import require_role
from ii_platform.database.connection import get_db, get_sessionmaker
from ii_platform.database.models import (
    ApprovalStatus,
    Article,
    Company,
    TargetCompany,
    TrackMode,
    User,
    UserRole,
    ProcessingJob,
    NewsSourceRun,
    NewsArticleAnalysis,
    NewsAlertItem,
    NewsAlertSource,
)
from ii_platform.web.collectors.financial_news_collector import FinancialNewsCollector
from ii_platform.web.collectors.news_source_registry import enabled_sources
from ii_platform.web.collectors.youtube_news_collector import YouTubeNewsCollector
from ii_platform.web.collectors.x_posts_collector import XPostsCollector
from ii_platform.entity_matching.coverage_matching import (
    TRACK_PRIORITY,
    match_headline,
    normalize_text,
    significant_tokens,
)
from apps.newsalert.workflows import persist_article_item, public_news_url
from apps.newsalert.classification import NEWS_TYPES, classify_topic, TYPE_LABELS
from apps.newsalert.fundamental_relevance import assess_fundamental_news
from apps.newsalert.pipeline import (
    run_newsalert_pipeline,
    _existing_x_headline_is_author,
    _guess_interview_company_label,
    _x_post_headline,
    _x_post_text,
)
from ii_platform.events.schemas import Event, EventMetadata, EventType
from ii_platform.events.outbox import stage_event

router = APIRouter(prefix="/news", tags=["NewsAlert"])
_NEWS_INGEST_LOCK = asyncio.Lock()


def _clean_headline_text(value: object) -> str:
    text = BeautifulSoup(str(value or ""), "lxml").get_text(" ", strip=True)
    return " ".join(text.split()).strip(" -–—|•")


def _headline_is_company_only(title: object, match) -> bool:
    normalized_title = normalize_text(title)
    if not normalized_title:
        return True
    company = normalize_text(match.company_name)
    matched_term = normalize_text(match.matched_term)
    if normalized_title in {company, matched_term}:
        return True
    company_tokens = set(significant_tokens(match.company_name))
    title_tokens = set(normalized_title.split())
    legal_only = {
        "limited", "ltd", "private", "pvt", "inc", "llp", "corporation",
        "corp", "company", "co", "bank", "industries", "industry", "holdings",
    }
    return bool(title_tokens) and title_tokens.issubset(company_tokens | legal_only)


def _headline_contains_match(title: object, match) -> bool:
    normalized_title = normalize_text(title)
    if not normalized_title:
        return False
    # Never treat an individual generic token (for example "clean") as a company
    # identity. Revalidate only complete company/matched phrases.
    for term in (match.company_name, match.matched_term):
        normalized_term = normalize_text(term)
        if normalized_term and re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])",
            normalized_title,
        ):
            return True
    return False


def _headline_from_text(text: object, match, current_title: object = "") -> str | None:
    clean = _clean_headline_text(text)
    if not clean:
        return None

    current = normalize_text(current_title)
    chunks = []
    for line in re.split(r"[\r\n]+", clean):
        line = line.strip()
        if line:
            chunks.append(line)
    if not chunks:
        chunks = [clean]

    expanded = []
    for chunk in chunks:
        expanded.extend([part.strip() for part in re.split(r"\s+[|•]\s+", chunk) if part.strip()])
        expanded.extend([part.strip() for part in re.split(r"(?<=[.!?])\s+", chunk) if part.strip()])
    candidates = chunks + expanded

    for candidate in candidates:
        candidate = _clean_headline_text(candidate)
        normalized = normalize_text(candidate)
        if len(normalized) < 20 or len(candidate) > 1000 or normalized == current:
            continue
        if _headline_is_company_only(candidate, match):
            continue
        if _headline_contains_match(candidate, match):
            return candidate[:1000]
    return None


def _display_news_title(article: Article, match) -> str | None:
    """Return only the stored publisher/video/post headline.

    NewsAlert never promotes a body/summary sentence into a headline. A Coverage
    company must already be identifiable from this exact title.
    """
    title = _clean_headline_text(article.title)
    if not title or _headline_is_company_only(title, match):
        return None
    if not _headline_contains_match(title, match):
        return None
    return title


_TRADING_NOISE_PATTERNS = (
    "chart pattern", "chart patterns", "technical analysis", "technical view",
    "technical setup", "technical indicator", "technical indicators", "technical signals", "chart analysis",
    "chart signals", "candlestick", "candle stick", "candle-stick", "heikin ashi", "renko chart", "doji",
    "bollinger bands", "fibonacci levels", "relative strength index", "rsi", "macd", "moving average",
    "supertrend", "ichimoku", "pivot point", "stochastic oscillator", "stop loss", "take profit",
    "price action", "trading view", "trade setup", "stock setup", "trading strategy",
    "swing trade", "swing trading", "intraday", "day trading", "scalping", "breakout stocks",
    "support and resistance", "support/resistance", "support resistance", "weekly chart",
    "daily chart", "pattern analysis", "option chain", "options trading", "f&o ban", "f&o strategy",
    "stock recommendation", "stock recommendations", "share recommendation", "share recommendations",
    "trading recommendation", "recommended stocks", "stock picks", "top stocks to buy", "top stocks",
    "top stock", "stocks to buy", "stocks to sell", "stocks to watch", "best stocks", "buy or sell",
    "buy/sell/hold", "target price", "price target", "multibagger", "buy rating", "sell rating",
    "brokerage upgrade", "brokerage downgrade", "top gainers", "top losers", "market wrap",
    "market round up", "market roundup", "market outlook", "market today", "market live updates",
    "sensex today", "nifty today", "muhurat picks", "horoscope", "astro", "gold rate today",
    "gold price today", "option buying", "option selling", "options strategy", "futures strategy",
    "derivatives strategy", "trade idea", "trading calls", "technical calls", "momentum trading",
)


def _headline_has_trading_noise(title: object) -> bool:
    clean = normalize_text(title or "")
    if not clean:
        return False
    return any(pattern in clean for pattern in _TRADING_NOISE_PATTERNS)


_PULSE_MARKET_MOVEMENT_PATTERNS = (
    re.compile(r"\b(?:stocks?|shares?)\b.{0,70}\b(?:rally|rallies|surge|soar|soared|jump|jumps|rose|rise|rises|gain|gains|gained|fall|falls|fell|drop|drops|slip|slips|decline|declines|plunge|plunges|tumble|tumbles|zoom|zooms|climb|climbs|sink|sinks|hit (?:a )?(?:high|low)|touch(?:ed)? (?:a )?(?:high|low))\b", re.I),
    re.compile(r"\b(?:rally|rallies|surge|soar|jump|rose|rise|gain|fall|fell|drop|slip|decline|plunge|tumble|zoom|climb|sink)\b.{0,70}\b(?:stocks?|shares?)\b", re.I),
    re.compile(r"\b(?:stock|share)\s+price\b|\bprice\s+(?:rallies|surges|jumps|soars|slumps|falls|drops)\b", re.I),
    re.compile(r"\b(?:sensex|nifty)\b.{0,80}\b(?:rally|rallies|surge|surges|jump|jumps|soar|soars|rose|rise|rises|fall|falls|fell|slip|slips|dip|dips|slide|slides|drop|drops|decline|declines|closes higher|closes lower|top gainer|top loser)\b", re.I),
    re.compile(r"\b(?:market live|market wrap|market outlook|top gainers|top losers|stocks in focus|shares in focus|stock market today|pre-market|closing bell)\b", re.I),
)

def _pulse_headline_is_market_noise(title: object) -> bool:
    clean = _clean_headline_text(title)
    if not clean:
        return True
    if _headline_has_trading_noise(clean):
        return True
    return any(pattern.search(clean) for pattern in _PULSE_MARKET_MOVEMENT_PATTERNS)


def _is_regulatory_news_source(source: object) -> bool:
    value = str(source or "").casefold()
    return any(marker in value for marker in (
        "nse corporate announcements", "bse corporate announcements",
        "nse board meetings", "nse corporate filings",
        "regulatory announcements (nse", "regulatory announcements (bse",
        "nse/bse regulatory announcements",
    ))


def _is_nse_document_url(value: object) -> bool:
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").casefold()
    return parsed.scheme in {"http", "https"} and (host == "nseindia.com" or host.endswith(".nseindia.com"))


def _regulatory_source_clause():
    """Official NSE/BSE filing rows, matched to Coverage or not."""
    return or_(
        func.lower(NewsAlertItem.source).contains("nse corporate announcements"),
        func.lower(NewsAlertItem.source).contains("nse board meetings"),
        func.lower(NewsAlertItem.source).contains("nse corporate filings"),
        func.lower(NewsAlertItem.source).contains("bse corporate announcements"),
        func.lower(NewsAlertItem.source).contains("regulatory announcements"),
    )


NSE_STOCK_QUOTE_PAGE = "https://www.nseindia.com/get-quotes/equity"


def _is_pdf_nse_document(value: object) -> bool:
    """True only for an NSE-hosted PDF. XBRL/XML/HTML attachments are not shown."""
    if not _is_nse_document_url(value):
        return False
    return urlparse(str(value).strip()).path.casefold().endswith(".pdf")


def _is_nse_announcement_row(item: NewsAlertItem, metadata: dict[str, Any]) -> bool:
    source = str(item.source or "").casefold()
    if str(metadata.get("source_key") or "").casefold() == "nse_india" or metadata.get("official_nse_announcement"):
        return True
    if "nse" in source and _is_regulatory_news_source(source):
        return True
    # Board-meeting and filing-action rows may carry an NSE page URL under any label.
    return _is_nse_document_url(item.url)


def _nse_symbol_for_item(
    item: NewsAlertItem,
    metadata: dict[str, Any],
    symbol_by_name: dict[str, str] | None = None,
) -> str | None:
    """The NSE symbol of the company the announcement is about.

    Tried in order: the symbol column, stored metadata, the `symbol` in the row's
    NSE page URL, then the NSE master list (Company table) matched by company name.
    Some NSE feeds carry only the company name, so the last step is what makes those
    rows reach the stock page.
    """
    symbol = item.nse_symbol or metadata.get("nse_symbol")
    if not symbol:
        query = dict(parse_qsl(urlparse(str(item.url or "")).query))
        symbol = query.get("symbol")
    if not symbol and symbol_by_name:
        symbol = symbol_by_name.get(_name_key(item.company_name))
    return symbol


def _name_key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _nse_stock_page_url(symbol: object) -> str | None:
    clean = re.sub(r"\s+", "", str(symbol or "")).upper()
    if not clean:
        return None
    return f"{NSE_STOCK_QUOTE_PAGE}?{urlencode({'symbol': clean})}"


def _valid_bse_stock_page_url(value: object) -> str | None:
    """Accept only a canonical BSE issuer quote page, never a generic BSE URL."""
    candidate = str(value or "").strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme in {"http", "https"}
        and (host == "bseindia.com" or host.endswith(".bseindia.com"))
        and parsed.path.casefold().startswith("/stock-share-price/")
    ):
        return candidate
    return None


def _bse_stock_page_url(company_name: object, bse_code: object, bse_symbol: object = None) -> str | None:
    """Build BSE's canonical issuer URL when metadata has a code but no saved URL."""
    code = str(bse_code or "").strip()
    if re.fullmatch(r"\d+\.0+", code):
        code = code.split(".", 1)[0]
    if not re.fullmatch(r"\d{5,7}", code):
        return None

    company = re.sub(r"\s+", " ", str(company_name or "company").strip().casefold())
    company = re.sub(r"\bprivate\s+limited\b", "pvt ltd", company)
    company = re.sub(r"\blimited\b", "ltd", company)
    company = re.sub(r"\bincorporated\b", "inc", company)
    company_slug = re.sub(r"[^a-z0-9]+", "-", company).strip("-") or "company"

    ticker = str(bse_symbol or "").strip().casefold().removeprefix("bse:")
    ticker = re.sub(r"-(?:a|b|t|x|m|xt|mt|st|z|zp|p|e|be)$", "", ticker, flags=re.I)
    ticker_slug = re.sub(r"[^a-z0-9]+", "-", ticker).strip("-")
    if not ticker_slug:
        base = re.sub(r"\b(?:pvt|private|ltd|limited|inc|incorporated|corp|corporation|company|co)\b", " ", company)
        ticker_slug = re.sub(r"[^a-z0-9]+", "", base).strip() or company_slug
    return f"https://www.bseindia.com/stock-share-price/{company_slug}/{ticker_slug}/{code}/"


def _public_article_url(item: NewsAlertItem, symbol_by_name: dict[str, str] | None = None) -> str:
    """Return the correct exchange document or issuer page for a NewsAlert row.

    For NSE corporate announcements, a valid NSE PDF always wins. When no PDF is
    present, use the BSE quote page stored by the coverage-code fallback. Only
    companies without a usable BSE code should fall back to their NSE quote page.
    `item.url` remains the stable identity used for deduplication.
    """
    metadata = item.metadata_json if isinstance(item.metadata_json, dict) else {}
    if not _is_nse_announcement_row(item, metadata):
        return item.url

    for candidate in (item.url, metadata.get("attachment_url"), metadata.get("source_url")):
        if _is_pdf_nse_document(candidate):
            return str(candidate).strip()

    # This field is populated from the BSE code in Coverage_List.xlsx. It must
    # take precedence over the NSE quote page for no-PDF / board-meeting rows.
    for candidate in (metadata.get("bse_stock_url"), metadata.get("fallback_stock_url")):
        valid_bse_url = _valid_bse_stock_page_url(candidate)
        if valid_bse_url:
            return valid_bse_url

    company_name = (
        item.company_name
        or metadata.get("issuer_name_from_source")
        or metadata.get("company_name")
    )
    generated_bse_url = _bse_stock_page_url(
        company_name, metadata.get("bse_code"), metadata.get("bse_symbol")
    )
    if generated_bse_url:
        return generated_bse_url

    # NSE is the intended route only for the six companies explicitly marked
    # without a BSE code (or when no valid BSE identifier is available).
    nse_url = metadata.get("nse_stock_url")
    if nse_url and str(nse_url).strip().startswith("https://www.nseindia.com/"):
        return str(nse_url).strip()
    return _nse_stock_page_url(_nse_symbol_for_item(item, metadata, symbol_by_name)) or item.url


def _social_match_text(item: dict[str, Any]) -> str:
    """Return the full human-authored social/video text used for matching.

    X API v2 exposes cashtags separately from the post text. Coverage matching
    already understands NSE symbols, so append cashtags as explicit identifiers
    to catch posts such as "$HDFCBANK" even when the company name is omitted.
    """
    title = _clean_headline_text(item.get("title"))
    summary = _clean_headline_text(item.get("summary"))
    parts = [title or summary]
    if summary and normalize_text(summary) != normalize_text(title):
        parts.append(summary)
    cashtags = item.get("x_cashtags") or []
    if isinstance(cashtags, (list, tuple, set)):
        parts.extend(str(tag).strip().lstrip("$") for tag in cashtags if str(tag).strip())
    else:
        parts.extend(
            token.upper()
            for token in re.findall(r"\$([A-Za-z][A-Za-z0-9_]{2,14})", f"{title} {summary}")
        )
    return " ".join(part for part in parts if part).strip()


async def _repair_collected_headline(
    item: dict[str, Any],
    match,
    collector: FinancialNewsCollector,
) -> bool:
    """Replace an issuer-only/broken feed title with the publisher's real headline.

    The old NewsAlert pipeline called this helper without defining it. That is a
    runtime error and prevented affected feeds from being persisted. We only accept
    a structured article-page headline that still contains a strong company match.
    """
    url = str(item.get("url") or "").strip()
    if not url:
        return False
    candidate = await collector.extract_article_headline(url)
    candidate = _clean_headline_text(candidate)
    if not candidate or _headline_is_company_only(candidate, match):
        return False
    if _headline_has_trading_noise(candidate):
        return False
    if not _headline_contains_match(candidate, match):
        return False
    item["title"] = candidate[:1000]
    return True


async def _match_and_repair_collected_item(
    item: dict[str, Any],
    coverage_rows,
    collector: FinancialNewsCollector | None = None,
) -> list[Any]:
    """Match an article to Coverage using its real headline.

    NSE/BSE announcement feeds occasionally expose only the issuer name as the
    feed title and place the actual announcement subject in the summary/details.
    In that specific case we promote a matching subject to the stored title. Social
    posts use their full post text because the post itself is the headline-equivalent.
    """
    title = _clean_headline_text(item.get("title"))
    item["title"] = title
    if not title or _headline_has_trading_noise(title):
        return []

    source_key = str(item.get("source_key") or "").casefold()
    source_name = str(item.get("source") or "").casefold()
    is_social = bool(item.get("content_type") in {"POST", "INTERVIEW"}) or "x / twitter" in source_name or "youtube" in source_name

    matches = match_headline(title, coverage_rows)
    if matches:
        return matches

    # X posts: title contains the first 1000 chars while summary contains the full
    # text. A company may legitimately occur after the first 1000 chars.
    if is_social and source_key in {"x_posts", "youtube"}:
        social_text = _social_match_text(item)
        if social_text and social_text != title:
            return match_headline(social_text, coverage_rows)

    # Exchange announcement feeds: use structured details only when the title itself
    # is exactly the company name / issuer. Never turn arbitrary article bodies into
    # headlines.
    if collector is not None:
        source_is_exchange = any(token in source_name for token in ("nse", "bse", "exchange"))
        if source_is_exchange:
            for field in ("summary", "description", "details", "content"):
                candidate = _headline_from_text(item.get(field), matches[0] if matches else None) if matches else None
                if not matches:
                    candidate_matches = match_headline(_clean_headline_text(item.get(field)), coverage_rows)
                else:
                    candidate_matches = matches
                if candidate_matches and _headline_is_company_only(title, candidate_matches[0]):
                    repaired = _headline_from_text(item.get(field), candidate_matches[0], current_title=title)
                    if repaired:
                        item["title"] = repaired
                        return candidate_matches

            if matches:
                repaired = await _repair_collected_headline(item, matches[0], collector)
                if repaired:
                    return matches

    # Some genuine company stories name the issuer in the feed summary/body but not
    # in the publisher's headline. Match against a bounded amount of source text so
    # the company can be attached and prefixed to the displayed headline.
    if not matches:
        context_parts = [title]
        for field in ("summary", "description", "raw_content", "content", "details"):
            value = _clean_headline_text(item.get(field))
            if value:
                context_parts.append(value[:5000])
        context = " ".join(context_parts).strip()
        if context and context != title:
            matches = match_headline(context, coverage_rows)

    return matches


def _headline_with_company(title: object, company_name: object) -> str:
    """Prefix a headline with the matched issuer name if the publisher omitted it."""
    clean_title = _clean_headline_text(title)
    clean_company = " ".join(str(company_name or "").split()).strip()
    if not clean_title:
        return clean_company[:1000]
    normalized_title = normalize_text(clean_title)
    normalized_company = normalize_text(clean_company)
    if normalized_company and re.search(
        rf"(?<![a-z0-9]){re.escape(normalized_company)}(?![a-z0-9])",
        normalized_title,
    ):
        return clean_title[:1000]
    return f"{clean_company}: {clean_title}"[:1000] if clean_company else clean_title[:1000]


def _prepare_fundamental_item(item: dict[str, Any], matches, content_type: str) -> bool:
    """Reject trading noise and only persist issuer-matched fundamental events."""
    if not matches:
        return False
    raw_title = _clean_headline_text(item.get("title"))
    source_key = str(item.get("source_key") or "").casefold()
    if not raw_title or _headline_has_trading_noise(raw_title):
        return False
    if source_key == "zerodha_pulse" and _pulse_headline_is_market_noise(raw_title):
        return False
    match = matches[0]
    source = str(item.get("source") or "Unknown")
    content = str(item.get("raw_content") or item.get("summary") or item.get("description") or "")
    assessment = assess_fundamental_news(
        raw_title,
        content,
        source,
        content_type=content_type,
        portfolio_connection=True,
        is_new=False,
    )
    if not assessment.eligible:
        return False

    item["title"] = _headline_with_company(raw_title, match.company_name)
    item["matched_company_id"] = match.company_id
    item["matched_nse_symbol"] = match.nse_symbol or ""
    item["companies_mentioned"] = [m.company_name for m in matches[:5]]
    item["relevance_score"] = assessment.score
    item["is_material"] = bool(assessment.is_material)
    item["news_alert_eligible"] = True
    item["fundamental_rationale"] = assessment.reason or ", ".join(assessment.matched_events)
    item["content_type"] = content_type
    item["is_interview"] = content_type == "INTERVIEW"
    return True


def _content_type_for_article(article: Article) -> str:
    source = str(article.source or "").casefold()
    # Content type follows the collection source. Do not use the legacy
    # is_interview flag for publisher articles because older versions inferred it
    # from the word "interview" in a headline/body.
    if source.startswith("youtube") or "youtube ·" in source:
        return "INTERVIEW"
    stored_type = str(getattr(article, "content_type", "") or "").upper()
    if stored_type == "POST" or source.startswith("x /") or any(token in source for token in ("twitter", "x.com")):
        return "POST"
    return "NEWS"


class NewsItem(BaseModel):
    id: int
    company: str
    title: str
    content_type: str = "NEWS"
    is_interview: bool = False
    analyst: str | None = None
    track_mode: str
    priority: int = 3
    timestamp: datetime
    source: str
    url: str
    summary: str | None = None
    relevance_score: int | None = None
    materiality: str | None = None
    relevance_rationale: str | None = None
    news_alert_eligible: bool = False


class CompanyCreate(BaseModel):
    legal_name: str = Field(min_length=2, max_length=255)
    nse_symbol: str | None = Field(default=None, max_length=40)
    bse_code: str | None = Field(default=None, max_length=40)
    isin: str | None = Field(default=None, max_length=20)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    website: str | None = Field(default=None, max_length=500)

    @field_validator("legal_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split()).strip()
        if not value:
            raise ValueError("Company name is required")
        return value

    @field_validator("nse_symbol", "bse_code", "isin", "website", mode="before")
    @classmethod
    def clean_optional(cls, value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    @field_validator("aliases", mode="before")
    @classmethod
    def clean_aliases(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            value = [x.strip() for x in value.split(",")]
        return list(dict.fromkeys(x.strip() for x in value if str(x).strip()))


class TargetCreate(BaseModel):
    company_id: int
    track_mode: TrackMode = TrackMode.LT
    start_date: datetime | None = None
    end_date: datetime | None = None


class CompanyWithTrackingCreate(CompanyCreate):
    create_target: bool = False
    analyst_id: int | None = None
    track_mode: TrackMode = TrackMode.LT
    start_date: datetime | None = None
    end_date: datetime | None = None


class CompanyTrackCreate(BaseModel):
    analyst_id: int
    track_mode: TrackMode = TrackMode.LT
    start_date: datetime | None = None
    end_date: datetime | None = None


def _display_news_headline(item: NewsAlertItem) -> str:
    """Display X post text, repairing legacy rows whose title is an author name."""
    headline = str(item.headline or "").strip()
    content_type = str(item.content_type or "NEWS").upper()
    if content_type == "POST":
        metadata = item.metadata_json if isinstance(item.metadata_json, dict) else {}
        source_match = re.search(r"@([A-Za-z0-9_]{1,50})", str(item.source or ""))
        source_handle = source_match.group(1) if source_match else ""
        raw = {
            **metadata,
            "x_account": metadata.get("x_account") or metadata.get("author_handle") or source_handle or item.author or "",
            "author_handle": metadata.get("author_handle") or metadata.get("x_account") or source_handle or item.author or "",
            "author": item.author or metadata.get("author") or "",
            "display_name": metadata.get("display_name") or metadata.get("author_name") or "",
            "author_name": metadata.get("author_name") or "",
            "title": headline,
            "summary": item.summary or metadata.get("summary") or "",
            "description": metadata.get("description") or "",
            "raw_content": metadata.get("raw_content") or metadata.get("post_text") or "",
            "full_text": metadata.get("full_text") or "",
            "tweet_text": metadata.get("tweet_text") or "",
            "text": metadata.get("text") or "",
        }
        post_text = _x_post_text(raw)
        if post_text:
            return _x_post_headline(post_text) or "Post text unavailable"
        # Never show the user's profile name as though it were the post.
        if _existing_x_headline_is_author(item):
            return "Post text unavailable — refresh collection"
    return headline or "Untitled"


def _display_news_company(item: NewsAlertItem) -> str:
    """Return exactly one primary company for YouTube interviews."""
    content_type = str(item.content_type or "NEWS").upper()
    stored = str(item.company_name or "").strip()
    metadata = item.metadata_json if isinstance(item.metadata_json, dict) else {}
    if content_type == "INTERVIEW":
        source = str(item.source or "").strip()
        channel = str(metadata.get("channel_title") or "").strip()
        if not channel and "·" in source:
            channel = source.split("·", 1)[1].strip()
        wrong_labels = {
            value.casefold() for value in (channel, str(item.author or "").strip(), source)
            if value
        }
        # New rows carry only the selected company. Older rows may carry a list;
        # use the first valid company in title order, never the YouTube publisher.
        candidates = metadata.get("interview_companies")
        if isinstance(candidates, (list, tuple)):
            for value in candidates:
                candidate = str(value or "").strip()
                if candidate and candidate.casefold() not in wrong_labels:
                    return candidate[:255]
        if stored and stored.casefold() not in wrong_labels:
            # The old ingestion path joined multiple mentioned companies with
            # comma-space. The visible Company column must contain a single one.
            return stored.split(", ", 1)[0][:255]
        inferred = _guess_interview_company_label(item.headline)
        if inferred and inferred.casefold() not in wrong_labels:
            return inferred[:255]
        return "Company not identified"
    # Preserve the monitored-account label in the Company column for X; only the
    # headline is the post text.
    return stored or str(item.nse_symbol or item.author or "Unknown").strip()


@router.get("", response_model=list[NewsItem])
async def list_news(
    analyst: str = "",
    track_mode: str = "",
    content_type: str = "",
    type: str = "",
    company: str = "",
    date_from: str = "",
    date_to: str = "",
    source: str = "",
    include_regulatory: bool = False,
    limit: int = 100,
    offset: int = 0,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    """Serve only visible canonical rows; apply every filter before SQL pagination."""
    def csv_values(value: str) -> list[str]:
        return [part.strip() for part in str(value or "").split(",") if part.strip()]

    selected_types = {value.upper() for value in csv_values(content_type or type)}
    selected_tracks = {value.upper() for value in csv_values(track_mode)}
    selected_analysts = [value.casefold() for value in csv_values(analyst)]
    if selected_types - {"NEWS", "POST", "INTERVIEW"}:
        raise HTTPException(400, "NewsAlert type must be News, Post, or Interview")
    if selected_tracks - {"PO", "HT", "LT", "NT", "OTHERS"}:
        raise HTTPException(400, "track_mode must be PO, HT, LT, NT, or Others")
    try:
        start_date = date.fromisoformat(date_from[:10]) if date_from else None
        end_date = date.fromisoformat(date_to[:10]) if date_to else None
    except ValueError as exc:
        raise HTTPException(400, "date_from and date_to must be YYYY-MM-DD") from exc
    if start_date and end_date and start_date > end_date:
        raise HTTPException(400, "date_from cannot be after date_to")

    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    if start_date is not None:
        start_at = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    elif end_date is not None:
        start_at = datetime.combine(end_date - timedelta(days=6), datetime.min.time(), tzinfo=timezone.utc)
    else:
        # Match the ingestion and pruning policy: a rolling seven-day window.
        start_at = now_utc - timedelta(days=7)
    end_at = datetime.combine((end_date or today) + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    regulatory_sources = _regulatory_source_clause()
    # YouTube items must be Coverage List linked; only allowlisted X posts may be companyless.
    visible_content = or_(
        NewsAlertItem.company_id.is_not(None),
        func.upper(NewsAlertItem.content_type) == "POST",
    )
    social_cutoff = now_utc - timedelta(days=7)
    youtube_cutoff = now_utc - timedelta(days=7)
    fresh_social = or_(
        func.upper(NewsAlertItem.content_type) == "NEWS",
        (func.upper(NewsAlertItem.content_type) == "POST") & (NewsAlertItem.published_at >= social_cutoff),
        (func.upper(NewsAlertItem.content_type) == "INTERVIEW") & (NewsAlertItem.published_at >= youtube_cutoff),
    )
    stmt = select(NewsAlertItem).where(
        NewsAlertItem.published_at >= start_at,
        NewsAlertItem.published_at < end_at,
        NewsAlertItem.processing_status == "VISIBLE",
        visible_content,
        fresh_social,
    )

    # X collection is shared, but each user's feed is private to the handles
    # they added. Reuse NewsAlertSource.created_by; no per-user table/migration.
    selected_x_values = (await db.scalars(
        select(NewsAlertSource.value).where(
            NewsAlertSource.source_type == "x",
            NewsAlertSource.created_by == user.id,
            NewsAlertSource.enabled.is_(True),
        )
    )).all()
    selected_x_handles = {
        str(value or "").strip().lstrip("@").casefold()
        for value in selected_x_values
        if str(value or "").strip()
    }
    x_account_expr = func.lower(func.trim(func.replace(func.coalesce(
        NewsAlertItem.metadata_json["x_account"].as_string(),
        NewsAlertItem.author,
        "",
    ), "@", "")))
    is_x_post_expr = or_(
        func.lower(NewsAlertItem.source).like("x /%"),
        NewsAlertItem.metadata_json["x_account"].as_string().is_not(None),
    )
    if selected_x_handles:
        stmt = stmt.where(or_(not_(is_x_post_expr), x_account_expr.in_(selected_x_handles)))
    else:
        # No handles selected means no X posts, while all other NewsAlert
        # sources (NSE/BSE/RSS/YouTube) remain unaffected.
        stmt = stmt.where(not_(is_x_post_expr))
    if not include_regulatory:
        stmt = stmt.where(not_(regulatory_sources))
    if selected_types:
        stmt = stmt.where(func.upper(NewsAlertItem.content_type).in_(selected_types))
    if selected_analysts:
        stmt = stmt.where(func.lower(func.coalesce(NewsAlertItem.analyst, "")).in_(selected_analysts))
    if source:
        stmt = stmt.where(func.lower(NewsAlertItem.source).contains(source.casefold()))
    if company:
        needle = f"%{company.strip().casefold()}%"
        stmt = stmt.where(or_(func.lower(NewsAlertItem.company_name).like(needle),
                              func.lower(func.coalesce(NewsAlertItem.nse_symbol, "")).like(needle),
                              func.lower(NewsAlertItem.headline).like(needle)))

    # Track mode lives in JSON metadata. Filter it in SQL before LIMIT/OFFSET;
    # otherwise a selected track can result in short/empty pages despite matches.
    track_expr = func.upper(func.coalesce(NewsAlertItem.metadata_json["track_mode"].as_string(), "OTHERS"))
    if selected_tracks:
        selected = set(selected_tracks)
        include_other_tracks = bool(selected & {"NT", "OTHERS"})
        selected -= {"NT", "OTHERS"}
        track_clauses = []
        if selected:
            track_clauses.append(track_expr.in_(selected))
        if include_other_tracks:
            track_clauses.append(track_expr.in_(["NT", "OTHERS"]))
        if track_clauses:
            stmt = stmt.where(or_(*track_clauses))

    bounded_limit = min(max(int(limit), 1), 100)
    bounded_offset = max(int(offset), 0)
    rows = (await db.execute(
        stmt.order_by(NewsAlertItem.published_at.desc(), NewsAlertItem.id.desc())
        .offset(bounded_offset).limit(bounded_limit)
    )).scalars().all()

    # One batched lookup of NSE symbols by company name, for rows whose feed gave no symbol.
    names_needing_symbol = {
        _name_key(item.company_name) for item in rows
        if not item.nse_symbol and _name_key(item.company_name)
        and _is_nse_announcement_row(item, item.metadata_json if isinstance(item.metadata_json, dict) else {})
    }
    symbol_by_name: dict[str, str] = {}
    if names_needing_symbol:
        company_rows = (await db.execute(
            select(Company.legal_name, Company.nse_symbol).where(Company.nse_symbol.is_not(None))
        )).all()
        for legal_name, nse_symbol in company_rows:
            key = _name_key(legal_name)
            if key in names_needing_symbol and nse_symbol and key not in symbol_by_name:
                symbol_by_name[key] = str(nse_symbol).strip().upper()

    response: list[NewsItem] = []
    for item in rows:
        metadata = item.metadata_json if isinstance(item.metadata_json, dict) else {}
        raw_track = str(metadata.get("track_mode") or "OTHERS").upper()
        item_track = raw_track if raw_track in {"PO", "HT", "LT", "NT"} else "OTHERS"
        display_track = "Others" if item_track in {"OTHERS", "NT"} else item_track
        response.append(NewsItem(
            id=int(item.id), company=_display_news_company(item),
            title=_display_news_headline(item), content_type=item.content_type or "NEWS",
            is_interview=(str(item.content_type or "").upper() == "INTERVIEW"), analyst=item.analyst,
            track_mode=display_track, priority={"PO": 1, "HT": 2, "LT": 3, "NT": 4}.get(item_track, 5),
            timestamp=item.published_at, source=item.source or "News source", url=_public_article_url(item, symbol_by_name),
            summary=item.summary, relevance_score=int(item.relevance_score) if item.relevance_score is not None else None,
            materiality=item.materiality, relevance_rationale=item.relevance_rationale,
            news_alert_eligible=True,
        ))
    return response


@router.get("/coverage")
async def news_coverage(
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    rows = (await db.execute(
        select(Company.id, Company.legal_name, Company.nse_symbol, Company.bse_code, TargetCompany.track_mode, User.username)
        .join(TargetCompany, TargetCompany.company_id == Company.id)
        .outerjoin(User, User.id == TargetCompany.analyst_id)
        .where(TargetCompany.active.is_(True), TargetCompany.approval_status == ApprovalStatus.APPROVED)
        .order_by(Company.legal_name)
    )).all()
    return {"count": len(rows), "companies": [
        {"company_id": r.id, "company_name": r.legal_name, "nse_symbol": r.nse_symbol,
         "bse_code": r.bse_code, "track_mode": getattr(r.track_mode, "value", str(r.track_mode)), "analyst": r.username}
        for r in rows
    ]}


@router.get("/archive-status")
async def news_archive_status(
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    latest = await db.scalar(
        select(NewsSourceRun).where(NewsSourceRun.source_key == "newsalert-v2")
        .order_by(NewsSourceRun.id.desc()).limit(1)
    )
    recent_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    visible_content = or_(
        NewsAlertItem.company_id.is_not(None),
        func.upper(NewsAlertItem.content_type) == "POST",
    )
    social_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    youtube_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    fresh_social = or_(
        func.upper(NewsAlertItem.content_type) == "NEWS",
        (func.upper(NewsAlertItem.content_type) == "POST") & (NewsAlertItem.published_at >= social_cutoff),
        (func.upper(NewsAlertItem.content_type) == "INTERVIEW") & (NewsAlertItem.published_at >= youtube_cutoff),
    )
    count = int((await db.scalar(select(func.count(NewsAlertItem.id)).where(
        NewsAlertItem.published_at >= recent_cutoff,
        NewsAlertItem.processing_status == "VISIBLE",
        visible_content,
        fresh_social,
    ))) or 0)
    return {
        "count": count, "generated_at": latest.completed_at if latest else None,
        "lookback_days": 7, "source_statuses": [],
        "last_run": ({"id": latest.id, "source": latest.source_key, "status": latest.status,
                      "items_fetched": latest.items_fetched, "articles_created": latest.articles_created,
                      "articles_skipped": latest.articles_skipped, "errors_count": latest.errors_count,
                      "started_at": latest.started_at, "completed_at": latest.completed_at,
                      "error_summary": latest.error_summary} if latest else None),
    }


@router.get("/sources")
async def sources(
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    settings = get_settings()
    rows = []
    for source in enabled_sources():
        urls = list(source.feed_urls) + list(source.discovery_urls) + list(source.article_urls)
        if source.key == "bse_india":
            if settings.bse_feed_url:
                urls.insert(0, settings.bse_feed_url)
            elif settings.bse_api_url:
                urls.insert(0, settings.bse_api_url)
        source_url = getattr(source, "homepage_url", None) or next((url for url in urls if url), f"https://{source.domains[0]}")
        rows.append({
            "key": source.key,
            "source": source.name,
            "url": source_url,
        })
    if settings.youtube_enabled:
        # YouTube RSS works without an API key; configured API remains an optional fallback.
        # Keep one stable "YouTube" filter plus configured display names; the filter
        # is substring-based so both work when channel titles differ in case.
        rows.append({
            "key": "youtube",
            "source": "YouTube",
            "url": "https://www.youtube.com/",
        })
        configured_names = [
            value.strip()
            for value in settings.youtube_channel_names.split(",")
            if value.strip()
        ]
        for index, channel in enumerate(configured_names):
            rows.append({
                "key": f"youtube_channel_{index + 1}",
                "source": f"YouTube · {channel}",
                "url": channel if channel.startswith(("http://", "https://")) else "https://www.youtube.com/",
            })
    x_feeds = [
        x.strip()
        for x in os.getenv(
            "X_POST_FEED_URLS",
            "",
        ).split(",")
        if x.strip()
    ]
    
    x_accounts = [
        x.strip()
        for x in settings.x_allowed_accounts.split(",")
        if x.strip()
    ] or [
        "unseenvalue",
        "Nigel__DSouza",
        "ResearchSOIC",
        "JoshiEien",
        "HemindraHazari",
    ]
    
    rows.append({
        "key": "x_posts",
        "source": "X / Twitter Posts",
        "url": (
            f"https://x.com/{x_accounts[0].lstrip('@')}"
            if x_accounts
            else "https://x.com/"
        ),
        "configured": bool(
            x_accounts or x_feeds
        ),
    })
    
    return rows


@router.get("/source-status")
async def source_status(
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    """Return source-level results from the latest canonical pipeline run."""
    latest = await db.scalar(
        select(NewsSourceRun).where(NewsSourceRun.source_key == "newsalert-v2")
        .order_by(NewsSourceRun.id.desc()).limit(1)
    )
    if latest is None:
        # No canonical worker run has completed yet. Do not substitute stale
        # status values from the retired legacy collectors.
        return []
    try:
        parsed = json.loads(latest.error_summary or "{}")
        source_map = parsed.get("sources", {}) if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return [{
            "key": "pipeline", "source": "News Alert ingestion", "status": latest.status or "unknown",
            "items_found": int(latest.items_fetched or 0), "created": int(latest.articles_created or 0),
            "errors": [str(latest.error_summary or "Unable to parse source diagnostics")[:500]],
            "diagnostics": {}, "last_attempted_at": latest.started_at,
            "last_success_at": latest.completed_at if latest.status in {"ok", "partial"} else None,
        }]
    if not isinstance(source_map, dict):
        source_map = {}
    labels = {"rss": "Financial News RSS", "google_news": "Google News", "nse": "NSE Announcements",
              "youtube": "YouTube Interviews", "x": "X / Twitter Posts"}
    rows = [
        {"key": key, "source": labels.get(key, key), "status": value.get("status", "unknown"),
         "items_found": int(value.get("fetched") or 0), "created": int(value.get("created") or 0),
         "errors": value.get("errors") or [], "diagnostics": value.get("diagnostics") or {},
         "last_attempted_at": latest.started_at,
         "last_success_at": latest.completed_at if value.get("status") in {"ok", "partial", "empty"} else None}
        for key, value in source_map.items() if isinstance(value, dict)
    ]
    fallback_counts = parsed.get("fallback_counts") or {}
    if isinstance(fallback_counts, dict) and any(int(value or 0) > 0 for value in fallback_counts.values()):
        rows.append({
            "key": "relevance_fallback", "source": "Deterministic relevance fallback", "status": "partial",
            "items_found": sum(int(value or 0) for value in fallback_counts.values()), "created": 0,
            "errors": parsed.get("processing_errors") or [], "diagnostics": {"fallback_counts": fallback_counts},
            "last_attempted_at": latest.started_at, "last_success_at": latest.completed_at,
        })
    if not rows:
        rows.append({
            "key": "pipeline", "source": "News Alert ingestion", "status": latest.status or parsed.get("cycle_status") or "unknown",
            "items_found": int(latest.items_fetched or 0), "created": int(latest.articles_created or 0),
            "errors": parsed.get("processing_errors") or [], "diagnostics": {"fallback_counts": fallback_counts},
            "last_attempted_at": latest.started_at,
            "last_success_at": latest.completed_at if latest.status in {"ok", "partial"} else None,
        })
    return rows


@router.get("/analysts")
async def list_analysts(
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    rows = (
        await db.execute(
            select(User).where(
                User.is_active.is_(True),
                User.role == UserRole.ANALYST,
            ).order_by(User.username)
        )
    ).scalars().all()
    if user.role == UserRole.ANALYST and not rows:
        rows = [user]
    return [{"id": x.id, "username": x.username, "role": x.role.value} for x in rows]


@router.get("/companies")
async def list_companies(
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    rows = (await db.execute(select(Company).order_by(Company.legal_name))).scalars().all()
    return [
        {
            "id": x.id,
            "legal_name": x.legal_name,
            "nse_symbol": x.nse_symbol,
            "bse_code": x.bse_code,
            "isin": x.isin,
            "aliases": x.aliases or [],
            "website": x.website,
        }
        for x in rows
    ]


@router.post("/companies/{company_id}/track")
async def track_company(
    company_id: int,
    data: CompanyTrackCreate,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN)),
):
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    analyst = await db.get(User, data.analyst_id)
    if not analyst or not analyst.is_active or analyst.role != UserRole.ANALYST:
        raise HTTPException(400, "Selected analyst is not an active Analyst user")

    existing_result = await db.execute(
        select(TargetCompany).where(TargetCompany.company_id == company_id)
    )
    target = existing_result.scalar_one_or_none()

    if target:
        if target.approval_status == ApprovalStatus.APPROVED and target.active:
            raise HTTPException(409, "Company is already an active approved target")

        target.analyst_id = analyst.id
        target.track_mode = data.track_mode
        target.start_date = data.start_date
        target.end_date = data.end_date
    else:
        target = TargetCompany(
            company_id=company.id,
            analyst_id=analyst.id,
            track_mode=data.track_mode,
            start_date=data.start_date,
            end_date=data.end_date,
        )
        db.add(target)

    if user.role == UserRole.ADMIN:
        target.approval_status = ApprovalStatus.APPROVED
        target.approval_by = user.id
        target.approval_at = datetime.now(timezone.utc)
        target.active = True
    else:
        target.approval_status = ApprovalStatus.PENDING_AASHIL_APPROVAL
        target.approval_by = None
        target.approval_at = None
        target.active = False

    await db.commit()
    await db.refresh(target)

    return {
        "company_id": company.id,
        "company_name": company.legal_name,
        "target_id": target.id,
        "analyst_id": target.analyst_id,
        "track_mode": target.track_mode.value,
        "approval_status": target.approval_status.value,
        "active": target.active,
    }


@router.delete("/companies/{company_id}")
async def delete_company(
    company_id: int,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    await db.execute(
        delete(TargetCompany).where(TargetCompany.company_id == company_id)
    )
    await db.delete(company)
    await db.commit()

    return {
        "status": "deleted",
        "company_id": company_id,
    }


@router.post("/companies")
async def create_company(
    data: CompanyWithTrackingCreate,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN)),
):
    existing = await db.execute(
        select(Company).where(Company.legal_name.ilike(data.legal_name))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Company with this name already exists")

    for field_name, field_value in (
        ("nse_symbol", data.nse_symbol),
        ("bse_code", data.bse_code),
        ("isin", data.isin),
    ):
        if field_value:
            column = getattr(Company, field_name)
            duplicate = await db.execute(select(Company).where(column == field_value))
            if duplicate.scalar_one_or_none():
                raise HTTPException(409, f"Company with {field_name}={field_value} already exists")

    if data.create_target and user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only Admin can modify the shared Coverage List.")

    company = Company(
        legal_name=data.legal_name,
        nse_symbol=data.nse_symbol.upper() if data.nse_symbol else None,
        bse_code=data.bse_code,
        isin=data.isin.upper() if data.isin else None,
        aliases=data.aliases,
        website=data.website,
    )
    db.add(company)
    await db.flush()

    target = None
    if data.create_target:
        analyst_id = data.analyst_id or user.id
        analyst = await db.get(User, analyst_id)
        if not analyst or not analyst.is_active or analyst.role != UserRole.ANALYST:
            raise HTTPException(400, "Selected analyst is not an active Analyst user")
        approval = ApprovalStatus.APPROVED if user.role == UserRole.ADMIN else ApprovalStatus.PENDING_AASHIL_APPROVAL
        target = TargetCompany(
            company_id=company.id,
            analyst_id=analyst.id,
            track_mode=data.track_mode,
            start_date=data.start_date,
            end_date=data.end_date,
            approval_status=approval,
            approval_by=user.id if user.role == UserRole.ADMIN else None,
            approval_at=datetime.now(timezone.utc) if user.role == UserRole.ADMIN else None,
        )
        db.add(target)

    await db.commit()
    return {
        "company": {
            "id": company.id,
            "legal_name": company.legal_name,
            "nse_symbol": company.nse_symbol,
            "bse_code": company.bse_code,
            "isin": company.isin,
            "aliases": company.aliases or [],
            "website": company.website,
        },
        "target": None if target is None else {
            "id": target.id,
            "analyst_id": target.analyst_id,
            "track_mode": target.track_mode.value,
            "approval_status": target.approval_status.value,
        },
    }


@router.get("/targets")
async def list_targets(
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    rows = (
        await db.execute(
            select(TargetCompany, Company, User)
            .join(Company, TargetCompany.company_id == Company.id)
            .join(User, TargetCompany.analyst_id == User.id)
            .order_by(TargetCompany.id)
        )
    ).all()
    return [
        {
            "id": target.id,
            "company_id": company.id,
            "company_name": company.legal_name,
            "analyst_id": analyst.id,
            "analyst": analyst.username,
            "track_mode": target.track_mode.value,
            "active": target.active,
            "approval_status": target.approval_status.value,
            "start_date": target.start_date,
            "end_date": target.end_date,
        }
        for target, company, analyst in rows
    ]


@router.post("/targets")
async def create_target(
    data: TargetCreate,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN)),
):
    company = await db.get(Company, data.company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    existing = await db.execute(select(TargetCompany).where(TargetCompany.company_id == company.id))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Company is already configured as a NewsAlert target")

    status = ApprovalStatus.APPROVED if user.role == UserRole.ADMIN else ApprovalStatus.PENDING_AASHIL_APPROVAL
    row = TargetCompany(
        company_id=data.company_id,
        analyst_id=user.id,
        track_mode=data.track_mode,
        start_date=data.start_date,
        end_date=data.end_date,
        approval_status=status,
        approval_by=user.id if user.role == UserRole.ADMIN else None,
        approval_at=datetime.now(timezone.utc) if user.role == UserRole.ADMIN else None,
    )
    db.add(row)
    await db.commit()
    return {
        "id": row.id,
        "company_id": row.company_id,
        "analyst_id": row.analyst_id,
        "track_mode": row.track_mode.value,
        "approval_status": row.approval_status.value,
    }


@router.patch("/targets/{target_id}")
async def update_target(
    target_id: int,
    data: TargetCreate,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN)),
):
    row = await db.get(TargetCompany, target_id)
    if not row:
        raise HTTPException(404, "Target not found")
    if row.company_id != data.company_id:
        existing = await db.execute(select(TargetCompany).where(TargetCompany.company_id == data.company_id, TargetCompany.id != target_id))
        if existing.scalar_one_or_none():
            raise HTTPException(409, "Selected company is already a NewsAlert target")
        row.company_id = data.company_id
    row.track_mode = data.track_mode
    row.start_date = data.start_date
    row.end_date = data.end_date
    if user.role == UserRole.ANALYST:
        row.analyst_id = user.id
        row.approval_status = ApprovalStatus.PENDING_AASHIL_APPROVAL
        row.approval_by = None
        row.approval_at = None
    else:
        row.approval_status = ApprovalStatus.APPROVED
        row.approval_by = user.id
        row.approval_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "id": row.id,
        "company_id": row.company_id,
        "analyst_id": row.analyst_id,
        "track_mode": row.track_mode.value,
        "approval_status": row.approval_status.value,
    }


@router.post("/targets/{target_id}/approve")
async def approve_target(
    target_id: int,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    row = await db.get(TargetCompany, target_id)
    if not row:
        raise HTTPException(404, "Target not found")
    if row.approval_status != ApprovalStatus.PENDING_AASHIL_APPROVAL:
        raise HTTPException(409, "Target is not pending approval")
    row.approval_status = ApprovalStatus.APPROVED
    row.approval_by = admin.id
    row.approval_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": row.id, "approval_status": row.approval_status.value, "approved_by": admin.id}


@router.post("/targets/{target_id}/reject")
async def reject_target(
    target_id: int,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    row = await db.get(TargetCompany, target_id)
    if not row:
        raise HTTPException(404, "Target not found")
    row.approval_status = ApprovalStatus.REJECTED
    row.approval_by = admin.id
    row.approval_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": row.id, "approval_status": row.approval_status.value}


class IngestRequest(BaseModel):
    feed_urls: list[str] | None = None
    limit_per_feed: int = Field(default=30, ge=1, le=200)
    max_items_per_source: int | None = Field(default=None, ge=1, le=200)
    source_ids: list[str] | None = None
    include_youtube: bool = True
    include_x_posts: bool = True
    include_stockscan: bool = False
    include_regulatory: bool = False


def _coverage_rows_from_targets(target_rows):
    rows = []
    for target, company in target_rows:
        rows.append(
            SimpleNamespace(
                id=target.id,
                company_id=company.id,
                legal_name=company.legal_name,
                aliases=company.aliases or [],
                nse_symbol=company.nse_symbol,
                bse_code=company.bse_code,
                track_mode=target.track_mode.value,
                analyst_name=None,
            )
        )
    return rows


async def _persist_collected_source(db, source_result, coverage_rows, user_id, limit_per_feed, collector):
    created = 0
    skipped = 0
    for item in source_result.items[:limit_per_feed]:
        if str(item.get("source_key") or "").casefold() == "zerodha_pulse" and _pulse_headline_is_market_noise(item.get("title")): 
            skipped += 1
            continue
        matches = await _match_and_repair_collected_item(item, coverage_rows, collector)
        if not _prepare_fundamental_item(item, matches, "NEWS"):
            skipped += 1
            continue
        _, was_created = await persist_article_item(db, item, source_user_id=user_id)
        created += int(was_created)
        skipped += int(not was_created)
    return created, skipped


async def _ingest_regulatory_news_sources(db, *, history_days: int = 7) -> dict[str, Any]:
    """Collect NSE and BSE official announcements into the existing news tables.

    Each source commits its own idempotent Article upserts. A commit before starting
    protects already-collected publisher/social rows from the collectors' transaction
    reset used to avoid stale read transactions during external HTTP requests.
    """
    await db.commit()
    results: dict[str, Any] = {}
    try:
        from apps.newsalert.nse_ingest import ingest_nse_corporate_announcements_all
        results["nse"] = await ingest_nse_corporate_announcements_all(db, history_days=history_days)
    except Exception as exc:
        logger.exception("NEWSALERT_NSE_REGULATORY_INGEST_FAILED")
        results["nse"] = {"status": "failed", "error": str(exc)[:1000]}
        await db.rollback()
    try:
        from apps.newsalert.bse_ingest import ingest_bse_corporate_announcements_all
        results["bse"] = await ingest_bse_corporate_announcements_all(db, history_days=min(history_days, 6))
    except Exception as exc:
        logger.exception("NEWSALERT_BSE_REGULATORY_INGEST_FAILED")
        results["bse"] = {"status": "failed", "error": str(exc)[:1000]}
        await db.rollback()
    # Exchange-specific legacy Article collection is retained for compatibility.
    # The NewsAlert v2 pipeline is the only default writer to news_alert_items.
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    logger.info("NEWSALERT_REGULATORY_MANUAL_INGEST_RESULT result=%s", results)
    return results


async def _ingest_configured_news_unlocked(
    db,
    feed_urls: list[str] | None = None,
    *,
    user_id: int | None = None,
    limit_per_feed: int = 30,
    source_ids: list[str] | None = None,
    include_rss: bool = True,
    include_google_news: bool = True,
    include_nse: bool = True,
    include_youtube: bool = True,
    include_x_posts: bool = True,
    include_stockscan: bool = False,
    include_regulatory: bool = False,
) -> dict[str, Any]:
    """Run the single NewsAlert v2 ingestion pipeline into news_alert_items."""
    return await run_newsalert_pipeline(
        db,
        feed_urls=feed_urls,
        user_id=user_id,
        limit_per_feed=limit_per_feed,
        source_ids=source_ids,
        include_rss=include_rss,
        include_google_news=include_google_news,
        include_nse=include_nse,
        include_youtube=include_youtube,
        include_x_posts=include_x_posts,
        include_stockscan=include_stockscan,
        include_regulatory=include_regulatory,
    )


async def ingest_configured_news(*args, **kwargs) -> dict[str, Any]:
    """Serialize ingestion and record one audit row; caller owns transaction commit."""
    db = args[0] if args else kwargs.get("db")
    started = datetime.now(timezone.utc)
    async with _NEWS_INGEST_LOCK:
        try:
            result = await _ingest_configured_news_unlocked(*args, **kwargs)
            if db is not None:
                errors = result.get("errors") or []
                source_audit = {}
                for source_key, details in (result.get("sources") or {}).items():
                    if not isinstance(details, dict):
                        continue
                    # Keep the audit row compact but preserve the per-source and
                    # per-X-account outcomes required for diagnosing empty cycles.
                    diagnostics = details.get("diagnostics") or {}
                    compact_diagnostics = {}
                    if source_key == "x":
                        compact_diagnostics = {
                            key: diagnostics.get(key) for key in
                            ("transport", "transports_attempted", "feed_provider_configured", "feed_provider_warning",
                             "feeds_checked", "accounts_checked", "accounts_succeeded", "posts_found",
                             "account_results", "search_engine_details", "errors")
                            if diagnostics.get(key) is not None
                        }
                    elif source_key == "rss":
                        compact_diagnostics = {"feeds": diagnostics.get("feeds", {})}
                    elif source_key == "youtube":
                        compact_diagnostics = {
                            "resolved_channels": diagnostics.get("resolved_channels"),
                            "channels": diagnostics.get("channels", {}),
                            "company_interview_search": diagnostics.get("company_interview_search", {}),
                            "transport_attempts": diagnostics.get("transport_attempts", []),
                            "collector_errors": diagnostics.get("collector_errors", []),
                            "search_discoveries": diagnostics.get("search_discoveries", []),
                            "metadata_requests": diagnostics.get("metadata_requests", 0),
                            "metadata_cache_hits": diagnostics.get("metadata_cache_hits", 0),
                        }
                    source_audit[source_key] = {
                        "status": details.get("status", "unknown"),
                        "fetched": int(details.get("fetched") or 0),
                        "created": int(details.get("created") or 0),
                        "errors": [str(value)[:250] for value in (details.get("errors") or [])[:3]],
                        "diagnostics": compact_diagnostics,
                    }
                audit_text = json.dumps({
                    "sources": source_audit,
                    "cycle_status": result.get("status"),
                    "fallback_counts": result.get("fallback_counts") or {},
                    "processing_errors": [str(value)[:250] for value in errors[:15]],
                }, ensure_ascii=False, default=str)
                run = NewsSourceRun(
                    source_key="newsalert-v2",
                    status=str(result.get("status") or ("partial" if errors else "ok")),
                    started_at=started,
                    completed_at=datetime.now(timezone.utc),
                    items_fetched=int(result.get("items_fetched") or 0),
                    articles_created=int(result.get("created") or 0),
                    articles_skipped=sum(int(v or 0) for v in (result.get("skipped") or {}).values()) if isinstance(result.get("skipped"), dict) else int(result.get("skipped") or 0),
                    errors_count=len(errors),
                    error_summary=audit_text or None,
                    lookback_days=7,
                    window_start=datetime.now(timezone.utc) - timedelta(days=7),
                    window_end=datetime.now(timezone.utc),
                )
                db.add(run)
                await db.flush()
            return result
        except Exception as exc:
            if db is not None:
                # The caller may roll back the ingestion transaction after this
                # exception. Record a fatal run in an independent short-lived
                # session so that the audit row survives that rollback, without
                # committing or otherwise taking ownership of the caller's DB
                # transaction.
                try:
                    session_factory = get_sessionmaker()
                    async with session_factory() as audit_db:
                        audit_db.add(NewsSourceRun(
                            source_key="newsalert-v2", status="failed", started_at=started,
                            completed_at=datetime.now(timezone.utc), errors_count=1,
                            error_summary=str(exc)[:2000], lookback_days=7,
                            window_start=started - timedelta(days=7),
                            window_end=datetime.now(timezone.utc),
                        ))
                        await audit_db.commit()
                except Exception:
                    logger.exception("NEWSALERT_RUN_AUDIT_WRITE_FAILED")
            raise


@router.post("/ingest", status_code=202)
async def ingest_news(
    data: IngestRequest,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST)),
):
    """Queue a NewsAlert scrape and return immediately.

    The dedicated worker performs all external HTTP/browser work. This endpoint only
    creates durable job state plus an outbox event.
    """
    import uuid

    request_id = str(uuid.uuid4())
    trace_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    payload = {
        "user_id": user.id,
        "feed_urls": data.feed_urls,
        "limit_per_feed": int(data.max_items_per_source or data.limit_per_feed),
        "source_ids": data.source_ids,
        "include_youtube": bool(data.include_youtube),
        "include_x_posts": bool(data.include_x_posts),
        "include_stockscan": bool(data.include_stockscan),
        "include_regulatory": bool(data.include_regulatory),
        "lookback_days": 7,
    }
    job = ProcessingJob(
        request_id=request_id,
        trace_id=trace_id,
        event_id=event_id,
        application="newsalert",
        workflow_stage="ingest",
        status="queued",
    )
    db.add(job)
    event = Event(
        event_id=event_id,
        event_type=EventType.NEWS_INGEST_REQUESTED,
        source="newsalert-api",
        payload=payload,
        metadata=EventMetadata(
            request_id=request_id,
            trace_id=trace_id,
            user_id=str(user.id),
            source_system="newsalert-api",
        ),
    )
    await stage_event(db, event)
    await db.commit()
    return {"status": "queued", "run_id": request_id, "job_id": job.id}


@router.get("/ingest/latest")
async def latest_ingest_job(
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    job = await db.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.application == "newsalert",
            ProcessingJob.workflow_stage == "ingest",
        )
        .order_by(ProcessingJob.id.desc())
        .limit(1)
    )
    if job is None:
        return None
    return {
        "run_id": job.request_id,
        "job_id": job.id,
        "status": job.status,
        "result": job.result,
        "error": job.error_message,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


@router.get("/ingest/status/{run_id}")
async def ingest_job_status(
    run_id: str,
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    job = await db.scalar(
        select(ProcessingJob).where(
            ProcessingJob.application == "newsalert",
            ProcessingJob.workflow_stage == "ingest",
            ProcessingJob.request_id == run_id,
        )
    )
    if job is None:
        raise HTTPException(404, "NewsAlert run not found")
    return {
        "run_id": job.request_id,
        "job_id": job.id,
        "status": job.status,
        "result": job.result,
        "error": job.error_message,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }

# User-managed NewsAlert source registry. The UI calls these endpoints for the
# Manage Sources panel; previously the routes were missing, producing HTTP 404.
class CustomNewsSourceCreate(BaseModel):
    source_type: str = Field(min_length=1, max_length=20)
    label: str | None = Field(default=None, max_length=255)
    value: str = Field(min_length=1, max_length=1500)
    enabled: bool = True

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, value: str) -> str:
        value = value.strip().casefold().replace("-", "_")
        aliases = {"rss_feed": "rss", "website": "rss", "news_website": "rss",
                   "youtube_channel": "youtube", "youtube_channels": "youtube",
                   "x_handle": "x", "twitter": "x", "twitter_handle": "x"}
        value = aliases.get(value, value)
        if value not in {"rss", "youtube", "x"}:
            raise ValueError("source_type must be rss, youtube, or x")
        return value


def _normalize_custom_source_value(source_type: str, value: str) -> str:
    value = value.strip()
    if source_type == "x":
        value = value.rstrip("/")
        if value.startswith("https://x.com/"):
            value = "@" + value.split("/", 3)[-1].split("?", 1)[0].lstrip("@")
        elif value.startswith("https://twitter.com/"):
            value = "@" + value.split("/", 3)[-1].split("?", 1)[0].lstrip("@")
        else:
            value = "@" + value.lstrip("@")
    elif source_type == "youtube":
        # Keep channel URLs/IDs intact; normalize only surrounding whitespace.
        value = value.strip()
    else:
        value = value.rstrip()
    return value


@router.get("/custom-sources")
async def list_custom_sources(
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    rows = (await db.scalars(
        select(NewsAlertSource)
        .where(or_(NewsAlertSource.source_type != "x", NewsAlertSource.created_by == user.id))
        .order_by(NewsAlertSource.source_type, NewsAlertSource.label)
    )).all()
    return [{"id": row.id, "source_type": row.source_type, "type": row.source_type,
             "label": row.label, "value": row.value, "url": row.value,
             "enabled": row.enabled, "is_default": bool(row.created_by is None),
             "created_at": row.created_at, "updated_at": row.updated_at} for row in rows]


@router.post("/custom-sources", status_code=201)
async def create_custom_source(
    data: CustomNewsSourceCreate,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    if data.source_type != "x" and user.role not in {UserRole.ADMIN, UserRole.ANALYST}:
        raise HTTPException(status_code=403, detail="Only ADMIN or ANALYST users can add shared RSS/YouTube sources")
    value = _normalize_custom_source_value(data.source_type, data.value)
    if data.source_type == "rss" and not re.match(r"^https?://", value, re.I):
        raise HTTPException(status_code=422, detail="Enter a valid website or RSS URL beginning with http:// or https://")
    if data.source_type == "youtube" and not (re.match(r"^https?://(www\\.)?youtube\\.com/", value, re.I) or re.match(r"^UC[A-Za-z0-9_-]{20,}$", value)):
        raise HTTPException(status_code=422, detail="Enter a YouTube channel URL or channel ID")
    if data.source_type == "x" and not re.match(r"^@[A-Za-z0-9_]{1,15}$", value):
        raise HTTPException(status_code=422, detail="Enter an X handle such as @ETNOWlive")
    normalized_handle = value.casefold().rstrip("/")
    # The existing table has a global unique key on (source_type, normalized_value).
    # Namespace X values by user id so two users can add the same handle without
    # sharing ownership or requiring a uniqueness migration.
    normalized = f"user:{user.id}:{normalized_handle}" if data.source_type == "x" else normalized_handle
    existing_query = select(NewsAlertSource).where(
        NewsAlertSource.source_type == data.source_type,
    )
    if data.source_type == "x":
        # Match by handle as well as owner so older, pre-isolation X rows
        # created before this change are reused instead of duplicated.
        existing_query = existing_query.where(
            NewsAlertSource.created_by == user.id,
            func.lower(NewsAlertSource.value) == normalized_handle,
        )
    else:
        existing_query = existing_query.where(NewsAlertSource.normalized_value == normalized)
    existing = await db.scalar(existing_query)
    if existing:
        if not existing.enabled:
            existing.enabled = data.enabled
            await db.commit()
        return {"id": existing.id, "source_type": existing.source_type, "type": existing.source_type,
                "label": existing.label, "value": existing.value, "url": existing.value,
                "enabled": existing.enabled, "already_exists": True}
    row = NewsAlertSource(source_type=data.source_type, label=(data.label or value)[:255], value=value,
                          normalized_value=normalized, enabled=data.enabled, created_by=getattr(user, "id", None),
                          created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
    db.add(row)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        # A concurrent duplicate insertion should return the existing record.
        retry_query = select(NewsAlertSource).where(
            NewsAlertSource.source_type == data.source_type,
        )
        if data.source_type == "x":
            retry_query = retry_query.where(
                NewsAlertSource.created_by == user.id,
                func.lower(NewsAlertSource.value) == normalized_handle,
            )
        else:
            retry_query = retry_query.where(NewsAlertSource.normalized_value == normalized)
        existing = await db.scalar(retry_query)
        if existing:
            return {"id": existing.id, "source_type": existing.source_type, "type": existing.source_type,
                    "label": existing.label, "value": existing.value, "url": existing.value,
                    "enabled": existing.enabled, "already_exists": True}
        raise
    await db.refresh(row)
    return {"id": row.id, "source_type": row.source_type, "type": row.source_type,
            "label": row.label, "value": row.value, "url": row.value, "enabled": row.enabled,
            "already_exists": False}


@router.delete("/custom-sources/{source_id}")
async def delete_custom_source(
    source_id: int,
    db=Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    row = await db.get(NewsAlertSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if row.source_type == "x":
        if row.created_by != user.id:
            raise HTTPException(status_code=404, detail="X account not found for this user")
    else:
        if user.role not in {UserRole.ADMIN, UserRole.ANALYST}:
            raise HTTPException(status_code=403, detail="Only ADMIN or ANALYST users can remove shared RSS/YouTube sources")
        if row.created_by is None:
            raise HTTPException(status_code=403, detail="Built-in default sources cannot be removed; disable them instead")
    await db.delete(row)
    await db.commit()
    return {"ok": True, "id": source_id}