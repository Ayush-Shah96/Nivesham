from __future__ import annotations

"""Unified NewsAlert pipeline writing only to ``news_alert_items``.

Collectors run concurrently and return items, errors, and source diagnostics. This
module never commits the caller's SQLAlchemy transaction and never writes to the
legacy Article table.
"""

import asyncio
import csv
import io
import difflib
import hashlib
import json
import logging
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError

from apps.newsalert.fundamental_relevance import assess_fundamental_news

from infrastructure.config.settings import get_settings
from ii_platform.database.connection import get_sessionmaker
from ii_platform.database.models import (
    ApprovalStatus,
    Company,
    NewsAlertItem,
    NewsAlertSource,
    TargetCompany,
    User,
)
from ii_platform.web.collectors.financial_news_collector import FinancialNewsCollector, collect_nse_filing_items
from ii_platform.web.collectors.news_source_registry import NEWS_SOURCES
from ii_platform.web.collectors.x_posts_collector import XPostsCollector
from ii_platform.web.collectors.youtube_news_collector import YouTubeNewsCollector
from ii_platform.web.collectors.youtube_verification import fetch_transcript

logger = logging.getLogger(__name__)
UTC = timezone.utc
# Twelve default public RSS/Atom feeds; deployments can replace this list using
# NEWSALERT_RSS_FEEDS. These values are public source URLs, not credentials.
DEFAULT_RSS_FEEDS: tuple[str, ...] = (
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms",
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://www.business-standard.com/rss/companies-101.rss",
    "https://www.livemint.com/rss/companiesRSS",
    "https://www.livemint.com/rss/newsRSS",
    "https://www.livemint.com/rss/marketsRSS",
    "https://www.thehindubusinessline.com/markets/feeder/default.rss",
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://www.moneycontrol.com/rss/business.xml",
    "https://www.moneycontrol.com/rss/results.xml",
    "https://www.moneycontrol.com/rss/economy.xml",
)

_NOISE_MARKERS = (
    "technical analysis", "technical view", "chart pattern", "candlestick",
    "intraday trading", "day trading", "scalping", "stock picks", "top stocks to buy",
    "buy or sell", "buy/sell/hold", "target price", "price target", "market live",
    "market wrap", "market outlook", "top gainers", "top losers", "horoscope",
    "gold rate today", "options strategy", "f&o strategy", "trading calls",
    "sponsored content", "casino", "giveaway", "free money", "click here",
)
_STOP_WORDS = {
    "about", "after", "again", "also", "amid", "and", "are", "around", "as",
    "at", "been", "before", "being", "between", "but", "by", "for", "from",
    "has", "have", "into", "its", "more", "new", "not", "over", "says", "said",
    "than", "that", "the", "their", "this", "through", "to", "under", "was",
    "were", "will", "with", "without", "after", "india", "indian", "company",
}
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid",
    "ref", "ref_src", "ref_url", "source", "yclid", "cmpid", "sourceid",
}


@dataclass(frozen=True)
class CoverageCompany:
    company_id: int
    target_id: int
    name: str
    aliases: tuple[str, ...] = ()
    nse_symbol: str | None = None
    bse_code: str | None = None
    track_mode: str = "OTHERS"
    analyst: str | None = None
    extra_terms: tuple[str, ...] = ()

    @property
    def terms(self) -> tuple[str, ...]:
        legal_terms = (self.name, *self.aliases, self.nse_symbol or "", self.bse_code or "", *self.extra_terms)
        terms: list[str] = []
        for raw in legal_terms:
            term = str(raw or "").strip()
            if not term:
                continue
            terms.append(term)
            # Legal suffixes are commonly omitted in headlines (e.g. "Infosys"
            # for "Infosys Limited"). Add a clean core name without weakening
            # case-sensitive matching for short tickers/terms.
            core = re.sub(
                r"\s+(?:private|pvt|limited|ltd|incorporated|inc|corporation|corp|llp|plc|company|co\.?)+$",
                "", term, flags=re.IGNORECASE,
            ).strip()
            if core and core.casefold() != term.casefold():
                terms.append(core)
        return tuple(dict.fromkeys(terms))


@dataclass
class NormalizedItem:
    title: str
    url: str
    published_at: datetime
    source: str
    description: str = ""
    content_type: str = "NEWS"
    source_item_id: str | None = None
    company: CoverageCompany | None = None
    author: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    triage: dict[str, Any] | None = None
    score: dict[str, Any] | None = None

    @property
    def canonical_url(self) -> str:
        return canonicalize_url(self.url)

    @property
    def is_x(self) -> bool:
        return self.content_type == "POST" or bool(self.raw.get("is_x_post"))

    @property
    def source_handle(self) -> str:
        return str(self.raw.get("x_account") or self.raw.get("author_handle") or self.author or "").strip().lstrip("@")

    @property
    def dedupe_key(self) -> str:
        if self.is_x:
            identity = self.canonical_url or f"{self.source_handle}:{self.source_item_id or _normalise(self.title)}"
            return _sha256(f"x|{identity}")
        company_key = str(self.company.company_id) if self.company else "unmatched"
        identity = self.canonical_url or f"{self.source}|{self.source_item_id or _normalise(self.title)}"
        return _sha256(f"newsalert|{company_key}|{self.content_type}|{identity}")


@dataclass
class CollectorResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def tuple(self) -> tuple[list[dict[str, Any]], list[str]]:
        # Compatibility helper for any older callers that still expect a pair.
        return self.items, self.errors


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _is_dedupe_unique_violation(exc: IntegrityError) -> bool:
    """Distinguish an expected dedupe race from other integrity/schema failures."""
    details = f"{exc} {getattr(exc, 'orig', '')}".casefold()
    return "dedupe_key" in details and any(
        marker in details for marker in ("unique", "duplicate key", "uniqueviolation")
    )


def _normalise(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def canonicalize_url(raw_url: Any) -> str:
    """Remove fragments and common tracking parameters while retaining meaningful query keys."""
    raw = str(raw_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold().removeprefix("www.")
    # X migrated from twitter.com to x.com. They identify the same public post,
    # so canonicalise both hosts before calculating idempotency keys.
    if host == "twitter.com":
        host = "x.com"
    try:
        port = parsed.port
    except ValueError:
        return ""
    netloc = host
    if port and not ((parsed.scheme.casefold() == "https" and port == 443) or (parsed.scheme.casefold() == "http" and port == 80)):
        netloc = f"{host}:{port}"
    kept = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        low = key.casefold()
        if low.startswith("utm_") or low in _TRACKING_PARAMS:
            continue
        kept.append((key, value))
    kept.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, urlencode(kept, doseq=True), ""))


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        from email.utils import parsedate_to_datetime
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _youtube_lookback_days() -> int:
    try:
        return max(1, min(int(os.getenv("NEWSALERT_YOUTUBE_LOOKBACK_DAYS", "2")), 10))
    except (TypeError, ValueError):
        return 2


def item_age_allowed(item: NormalizedItem | dict[str, Any], *, now: datetime | None = None) -> bool:
    published = item.published_at if isinstance(item, NormalizedItem) else _aware_datetime(item.get("published_at") or item.get("published"))
    content_type = item.content_type if isinstance(item, NormalizedItem) else str(item.get("content_type") or "NEWS").upper()
    if published is None:
        return False
    current = now or datetime.now(UTC)
    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    # X posts remain 48-hour. YouTube backfill may widen INTERVIEW freshness for
    # this process; scheduled runs retain the default two-day window. Publisher
    # NEWS keeps its seven-day window.
    max_age = (timedelta(hours=48) if content_type == "POST" else
               timedelta(days=_youtube_lookback_days()) if content_type == "INTERVIEW" else
               timedelta(days=7))
    return current - max_age <= published <= current


def is_noise(title: Any, description: Any = "") -> bool:
    text = f"{title or ''} {description or ''}".casefold()
    return any(marker in text for marker in _NOISE_MARKERS)


def _tokens(value: Any) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", str(value or "").casefold()) if len(token) > 2 and token not in _STOP_WORDS}


def jaccard_similarity(left: Any, right: Any) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_same_story(left_title: Any, right_title: Any, *, threshold: float = 0.6) -> bool:
    return jaccard_similarity(left_title, right_title) >= threshold


def parse_youtube_atom(xml_text: str, channel_name: str) -> list[dict[str, Any]]:
    """Parse interview-like Atom entries using the configured YouTube lookback."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    label = str(channel_name or "").strip()
    if not label or re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", label):
        author_name = root.findtext("atom:author/atom:name", default="", namespaces=ns).strip()
        feed_title = root.findtext("atom:title", default="", namespaces=ns).strip()
        label = author_name or re.sub(r"^Uploads from\s+", "", feed_title, flags=re.I) or label or "YouTube channel"
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=_youtube_lookback_days())
    rows: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
        title = " ".join((entry.findtext("atom:title", default="", namespaces=ns) or "").split())
        # Do not substitute <updated> for the original <published> upload instant.
        published_raw = (entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
        # Require an explicit clock component; date-only data cannot prove exact age.
        published = _aware_datetime(published_raw) if re.search(r"\d{2}:\d{2}", published_raw) else None
        description = ""
        for tag in entry.findall("media:group/media:description", ns):
            description = " ".join((tag.text or "").split())
            if description:
                break
        if not video_id or not title or published is None:
            continue
        published = published.astimezone(UTC)
        if not cutoff <= published <= now:
            continue
        if not YouTubeNewsCollector._is_finance_interview(title, description, label):
            continue
        rows.append({
            "title": title[:1000], "url": f"https://www.youtube.com/watch?v={video_id}",
            "published_at": published, "source": f"YouTube · {label}",
            "summary": description[:10000], "description": description[:10000],
            "channel_title": label, "video_id": video_id, "source_item_id": video_id,
            "is_youtube": True, "is_interview": True, "content_type": "INTERVIEW",
            "_timestamp_exact": True,
        })
    return rows


def parse_google_news_publisher(xml_or_row: Any) -> str:
    """Extract the publisher name from a Google News RSS <source> element or parsed row."""
    if isinstance(xml_or_row, dict):
        return str(xml_or_row.get("publisher") or xml_or_row.get("source_name") or xml_or_row.get("source") or "Google News").strip()
    try:
        element = ET.fromstring(str(xml_or_row))
    except ET.ParseError:
        return "Google News"
    for candidate in element.iter():
        if candidate.tag.rsplit("}", 1)[-1].casefold() == "source":
            value = " ".join("".join(candidate.itertext()).split())
            if value:
                return value
    return "Google News"


def _configured_terms(raw_config: str) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for pair in str(raw_config or "").split(";"):
        if "=" not in pair:
            continue
        company_name, terms = pair.split("=", 1)
        key = _normalise(company_name)
        if key:
            result[key] = tuple(term.strip() for term in terms.split(",") if term.strip())
    return result


def build_company_matcher(companies: Iterable[CoverageCompany]) -> tuple[dict[str, Any], ...]:
    """Normalize company records once; terms <=4 chars are exact/case-sensitive."""
    return tuple({"company": company, "terms": company.terms} for company in companies)


def match_company(text: Any, matcher: Iterable[dict[str, Any]]) -> CoverageCompany | None:
    body = str(text or "")
    matched: dict[int, CoverageCompany] = {}
    for row in matcher:
        company: CoverageCompany = row["company"]
        for term in row["terms"]:
            term = str(term or "").strip()
            if not term:
                continue
            flags = 0 if len(term) <= 4 else re.IGNORECASE
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", flags)
            if pattern.search(body):
                matched[company.company_id] = company
                break
    return next(iter(matched.values())) if len(matched) == 1 else None


def _match_raw_company(raw: dict[str, Any], companies: list[CoverageCompany], matcher) -> CoverageCompany | None:
    """Match explicit issuer metadata against Coverage List using strict canonical equality."""
    raw_symbol = raw.get("nse_symbol") or raw.get("symbol") or raw.get("company_symbol")
    symbol = _normalise(raw_symbol).replace(" ", "").upper()
    if ":" in symbol:
        symbol = symbol.split(":", 1)[1]
    company_label = _normalise(
        raw.get("stockscans_company_name") or raw.get("company_name")
        or raw.get("companyName") or raw.get("company")
    )

    def legal_name_key(value: Any) -> str:
        # Treat common legal suffixes as equivalent (Ltd == Limited), but do not
        # use fuzzy similarity: the issuer must still exactly match a list entry.
        words = _normalise(value).split()
        suffixes = {"limited", "ltd", "private", "pvt", "incorporated", "inc",
                    "corporation", "corp", "llp", "plc", "company", "co"}
        while words and words[-1] in suffixes:
            words.pop()
        return " ".join(words)

    label_key = legal_name_key(company_label)
    matches: dict[int, CoverageCompany] = {}
    for company in companies:
        known_symbols = {
            _normalise(company.nse_symbol).replace(" ", "").upper(),
            _normalise(company.bse_code).replace(" ", "").upper(),
        } - {""}
        known_names = {_normalise(company.name), *(_normalise(alias) for alias in company.aliases)}
        known_name_keys = {legal_name_key(value) for value in (company.name, *company.aliases) if value}
        if ((symbol and symbol in known_symbols)
                or (company_label and company_label in known_names)
                or (label_key and label_key in known_name_keys)):
            matches[company.company_id] = company
    if len(matches) == 1:
        return next(iter(matches.values()))
    # Fall back to title/body matching only after explicit issuer metadata failed.
    # This remains a unique Coverage List match, never a fuzzy-name acceptance.
    text_value = " ".join(str(raw.get(key) or "") for key in (
        "title", "headline", "description", "summary", "company_name",
        "stockscans_company_name", "companyName", "nse_symbol", "company_symbol", "symbol"
    ))
    return match_company(text_value, matcher)


_X_ACCOUNT_DISPLAY_NAMES = {
    "nigel__dsouza": "Nigel D'Souza",
    "researchsoic": "ResearchSOIC",
    "joshieien": "Ajay Joshi Chemicals",
    "hemindrahazari": "Hemindra Hazari",
}

_LEGAL_NAME_WORDS = {
    "limited", "ltd", "private", "pvt", "incorporated", "inc", "corporation",
    "corp", "llp", "plc", "company", "co",
}


def _plain_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def _x_post_text(raw: dict[str, Any]) -> str:
    """Extract the actual X post body, never the account/profile label."""
    handle = _plain_text(
        raw.get("x_account") or raw.get("author_handle") or raw.get("username")
    ).lstrip("@").casefold()
    labels = {
        _normalise(handle),
        _normalise("@" + handle) if handle else "",
        _normalise(_X_ACCOUNT_DISPLAY_NAMES.get(handle, "")),
        _normalise(raw.get("author")),
        _normalise(raw.get("display_name")),
        _normalise(raw.get("author_name")),
        _normalise(raw.get("username")),
    }
    labels.discard("")
    # Some legacy records saved a card header (display name + @handle) instead
    # of the body. Treat these identity-only combinations as empty as well.
    identity_only = set(labels)
    if handle:
        identity_only.add(_normalise(f"{_X_ACCOUNT_DISPLAY_NAMES.get(handle, '')} @{handle}"))
        identity_only.add(_normalise(f"@{handle} {_X_ACCOUNT_DISPLAY_NAMES.get(handle, '')}"))

    def strip_profile_prefix(value: str) -> str:
        result = value.strip()
        display = _plain_text(_X_ACCOUNT_DISPLAY_NAMES.get(handle, ""))
        # Remove a profile header only when it is explicitly joined to the
        # handle or separated by a typical card delimiter; do not alter normal
        # post text that happens to begin with a person's name.
        prefixes = []
        if display and handle:
            prefixes.extend([
                rf"^{re.escape(display)}\s+@?{re.escape(handle)}\b\s*(?:[·|•—–:-]\s*)?",
                rf"^@?{re.escape(handle)}\s+{re.escape(display)}\b\s*(?:[·|•—–:-]\s*)?",
            ])
        for label in (display, _plain_text(raw.get("display_name")), _plain_text(raw.get("author_name"))):
            if label:
                prefixes.append(rf"^{re.escape(label)}\s*[|·•—–:-]\s*")
        for pattern in prefixes:
            stripped = re.sub(pattern, "", result, flags=re.IGNORECASE).strip()
            if stripped and _normalise(stripped) not in identity_only:
                result = stripped
                break
        return result

    # Inspect body-shaped fields before title because title is the field most
    # often contaminated by old/profile-card extractors.
    candidates = (
        raw.get("raw_content"), raw.get("full_text"), raw.get("tweet_text"),
        raw.get("post_text"), raw.get("post_body"), raw.get("status_text"),
        raw.get("content_text"), raw.get("body"), raw.get("tweet"), raw.get("article_text"),
        raw.get("text"), raw.get("summary"), raw.get("description"),
        raw.get("content"), raw.get("title"),
        raw.get("headline"),
    )
    for value in candidates:
        text_value = _plain_text(value)
        if not text_value:
            continue
        if _normalise(text_value) in identity_only:
            continue
        text_value = strip_profile_prefix(text_value)
        if not text_value or _normalise(text_value) in identity_only:
            continue
        # A raw profile-card line can combine display name and handle with no
        # delimiter. If no text remains after removing that header, skip it.
        normalized = _normalise(text_value)
        if normalized in identity_only:
            continue
        if len(normalized.split()) <= 2 and normalized in identity_only:
            continue
        return text_value
    return ""


def _x_post_headline(value: Any, *, limit: int = 420) -> str:
    """Return the first few lines/words of a post for the table headline."""
    text_value = _plain_text(value)
    if len(text_value) <= limit:
        return text_value
    cut = text_value.rfind(" ", 0, limit - 1)
    if cut < int(limit * 0.65):
        cut = limit - 1
    return text_value[:cut].rstrip(" ,;:-") + "…"


def _interview_company_matches(text: Any, companies: list[CoverageCompany]) -> list[CoverageCompany]:
    """Match interview subjects using legal names, aliases, acronyms and unique short names."""
    body = str(text or "")
    first_words = []
    for company in companies:
        tokens = [t for t in re.findall(r"[A-Za-z0-9]+", company.name) if t.casefold() not in _LEGAL_NAME_WORDS]
        first_words.append(tokens[0].casefold() if tokens else "")
    first_word_counts = {word: first_words.count(word) for word in set(first_words) if word}

    found: dict[int, tuple[int, int, CoverageCompany]] = {}
    for company, first_word in zip(companies, first_words):
        terms = list(company.terms)
        words = [t for t in re.findall(r"[A-Za-z0-9]+", company.name) if t.casefold() not in _LEGAL_NAME_WORDS]
        if len(words) >= 2:
            acronym = "".join(word[0] for word in words).upper()
            if len(acronym) >= 3:
                terms.append(acronym)
        # Only use a one-word shorthand when it uniquely identifies one active
        # Coverage List company. This handles titles such as "Crompton Q2 Earnings"
        # without confusing companies sharing a group name such as Bajaj.
        if first_word and len(first_word) >= 5 and first_word_counts.get(first_word) == 1:
            terms.append(first_word)
        best: tuple[int, int] | None = None
        for term in dict.fromkeys(str(t).strip() for t in terms if str(t).strip()):
            if len(term) < 3:
                continue
            match = re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", body, re.IGNORECASE)
            if match:
                candidate = (match.start(), -(match.end() - match.start()))
                if best is None or candidate < best:
                    best = candidate
        if best is not None:
            found[company.company_id] = (best[0], best[1], company)
    return [value[2] for value in sorted(found.values(), key=lambda value: (value[0], value[1], value[2].name.casefold()))]


def _guess_interview_company_label(title: Any) -> str:
    """Fallback display label extracted from a video title, never from its channel."""
    text_value = _plain_text(title)
    if not text_value:
        return ""
    text_value = re.sub(r"^\s*(?:\[[^\]]+\]\s*)?", "", text_value)
    # Remove result/earnings wrappers that appear before the actual issuer list.
    text_value = re.sub(
        r"^(?:Q[1-4]\s+)?(?:quarterly\s+)?(?:results?|earnings?)(?:\s+live)?\s*[:|–—-]\s*",
        "", text_value, flags=re.IGNORECASE,
    )
    # The company or issuer list is normally before the first colon/pipe/dash.
    segment = re.split(r"\s*(?:\||:|—|–)\s*", text_value, maxsplit=1)[0].strip()
    # Remove common quarter/event suffixes while preserving the company name.
    segment = re.sub(r"\s+(?:Q[1-4]\s*)?(?:results?|earnings?).*$", "", segment, flags=re.IGNORECASE).strip()
    segment = re.sub(r"\s+(?:share price|stock price|share market today)$", "", segment, flags=re.IGNORECASE).strip()
    if _normalise(segment) in {"live", "results", "earnings", "q1", "q2", "q3", "q4", "unknown"}:
        return ""
    # Keep a single issuer label even when the video title lists multiple
    # companies. Prefer the first company mentioned in title order.
    segment = re.split(r"\s*,\s*|\s+and\s+|\s*&\s*", segment, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    # A publisher/host intro isn't a company name. Do not surface it as one.
    if re.match(r"^(?:interview with|conversation with|talks? to|exclusive interview|business news)\b", segment, re.I):
        return ""
    return segment[:255]


def _x_row_handle(row: Any, metadata: dict[str, Any] | None = None) -> str:
    metadata = metadata if isinstance(metadata, dict) else getattr(row, "metadata_json", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    candidates = [metadata.get("x_account"), metadata.get("author_handle")]
    source = str(getattr(row, "source", "") or "")
    source_match = re.search(r"@([A-Za-z0-9_]{1,50})", source)
    if source_match:
        candidates.append(source_match.group(1))
    candidates.append(getattr(row, "author", ""))
    return _plain_text(next((value for value in candidates if _plain_text(value)), "")).lstrip("@").casefold()


def _existing_x_headline_is_author(row: Any) -> bool:
    metadata = getattr(row, "metadata_json", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    handle = _x_row_handle(row, metadata)
    title = _normalise(getattr(row, "headline", ""))
    candidates = {_normalise(handle), _normalise("@" + handle) if handle else ""}
    candidates.add(_normalise(_X_ACCOUNT_DISPLAY_NAMES.get(handle, "")))
    candidates.add(_normalise(getattr(row, "author", "")))
    for key in ("display_name", "author_name", "author"):
        candidates.add(_normalise(metadata.get(key)))
    candidates.discard("")
    return bool(title and title in candidates)


def _repair_existing_social_rows(rows: list[Any], companies: list[CoverageCompany]) -> int:
    """Repair already-saved legacy social headlines/company labels in-place."""
    repaired = 0
    for row in rows:
        content_type = str(getattr(row, "content_type", "") or "").upper()
        metadata = getattr(row, "metadata_json", None)
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        if content_type == "POST" and _existing_x_headline_is_author(row):
            post_text = _x_post_text({
                "x_account": _x_row_handle(row, metadata),
                "author_handle": _x_row_handle(row, metadata),
                "author": getattr(row, "author", ""),
                "display_name": metadata.get("display_name") or metadata.get("author_name"),
                "author_name": metadata.get("author_name"),
                "title": getattr(row, "headline", ""),
                "summary": getattr(row, "summary", ""),
                "raw_content": metadata.get("raw_content") or metadata.get("post_text"),
                "full_text": metadata.get("full_text"),
                "tweet_text": metadata.get("tweet_text"),
                "text": metadata.get("text"),
                "post_body": metadata.get("post_body"),
                "status_text": metadata.get("status_text"),
                "content_text": metadata.get("content_text"),
                "body": metadata.get("body"),
                "tweet": metadata.get("tweet"),
                "article_text": metadata.get("article_text"),
                "description": metadata.get("description"),
            })
            if post_text:
                row.headline = _x_post_headline(post_text)
                # The incoming candidate has a verified post body. Replace any
                # stale/author-only stored summary with that body as well.
                row.summary = post_text[:10000]
                metadata.update({"post_text": post_text[:10000], "raw_content": post_text[:10000]})
                row.metadata_json = metadata
                repaired += 1
        elif content_type == "INTERVIEW":
            source = _plain_text(getattr(row, "source", ""))
            channel = _plain_text(metadata.get("channel_title"))
            if not channel and "·" in source:
                channel = _plain_text(source.split("·", 1)[1])
            current_company = _plain_text(getattr(row, "company_name", ""))
            channel_names = {_normalise(channel), _normalise(getattr(row, "author", "")), _normalise(source)} - {""}
            old_company_labels = metadata.get("interview_companies")
            has_multiple_companies = (
                isinstance(old_company_labels, (list, tuple)) and len([v for v in old_company_labels if str(v or "").strip()]) > 1
            ) or ", " in current_company
            if not current_company or _normalise(current_company) in channel_names or has_multiple_companies:
                matches = _interview_company_matches(getattr(row, "headline", ""), companies)
                if matches:
                    # One interview row represents one primary company. Matches
                    # are ordered by the earliest/strongest title mention.
                    company = matches[0]
                    row.company_id = company.company_id
                    row.company_name = company.name[:255]
                    row.nse_symbol = company.nse_symbol
                    row.analyst = company.analyst
                    metadata.update({"track_mode": company.track_mode, "coverage_target_id": company.target_id,
                                     "interview_companies": [company.name]})
                else:
                    label = _guess_interview_company_label(getattr(row, "headline", ""))
                    row.company_id = None
                    row.company_name = (label or "Company not identified")[:255]
                    row.nse_symbol = None
                    row.analyst = None
                    metadata.pop("track_mode", None)
                    metadata.pop("coverage_target_id", None)
                row.metadata_json = metadata
                repaired += 1
    return repaired


def _official_content_identities(
    *, source_key: Any, filing_feed: Any, nse_symbol: Any, company_name: Any,
    subject: Any, published_at: Any,
) -> set[tuple[str, str, str, str]]:
    """Stable issuer/subject/minute keys for enriching previously saved filings.

    RSS GUIDs and NSE API sequence IDs can differ, and older deployments used a
    less specific listing-page URL. This secondary identity is intentionally exact
    (not fuzzy) and scoped by exchange + filing feed to avoid merging unrelated rows.
    """
    source = str(source_key or "").strip().casefold()
    if source not in {"nse_india", "bse_india"}:
        return set()
    feed = str(filing_feed or "").strip().casefold()
    if not feed:
        feed = "corporate_announcements" if source == "nse_india" else "bse_announcements"
    subject_key = _normalise(subject)
    published = _aware_datetime(published_at)
    if not subject_key or published is None:
        return set()
    minute = published.astimezone(ZoneInfo("Asia/Kolkata")).replace(second=0, microsecond=0).isoformat()
    identities: set[tuple[str, str, str, str]] = set()
    for issuer in (nse_symbol, company_name):
        issuer_key = _normalise(issuer)
        if issuer_key:
            identities.add((source, feed, issuer_key, f"{subject_key}|{minute}"))
    return identities


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in re.split(r"[,\n;]+", str(value or "")) if item.strip()]


def _source_name_for_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    labels = {
        "economictimes.indiatimes.com": "The Economic Times",
        "business-standard.com": "Business Standard",
        "livemint.com": "Mint",
        "thehindubusinessline.com": "The Hindu BusinessLine",
        "moneycontrol.com": "Moneycontrol",
        "sebi.gov.in": "SEBI",
    }
    for domain, name in labels.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or "RSS source"


def configured_rss_feeds(override: list[str] | None = None) -> list[str]:
    """Return URL seeds for configured RSS feeds; source fallbacks are handled later.

    The source registry—not this list—is authoritative for fallback discovery. This
    keeps the env override useful while making sure a configured RSS URL does not
    become a single point of failure for its publisher.
    """
    settings = get_settings()
    # Runtime RSS settings are additive seeds, not a global source allowlist.
    # The scheduled pipeline must keep polling all registered publishers even
    # when an operator has supplied one or more custom feed URLs. An explicit
    # ``override`` remains scoped for manual single-source refreshes.
    if override:
        values = list(override)
    else:
        values = _split_csv(settings.newsalert_rss_feeds)
        values.extend(DEFAULT_RSS_FEEDS)
        for profile in NEWS_SOURCES:
            values.extend(profile.feed_urls)
    unique: list[str] = []
    for value in values:
        canonical = canonicalize_url(value)
        if canonical and canonical not in unique:
            unique.append(canonical)
    return unique[:100]


async def _safe_collector(name: str, coro) -> tuple[str, CollectorResult]:
    try:
        result = await coro
        if isinstance(result, CollectorResult):
            return name, result
        if isinstance(result, tuple) and len(result) == 2:
            return name, CollectorResult(list(result[0] or []), [str(e) for e in result[1] or []])
        return name, CollectorResult(list(result or []), [])
    except Exception as exc:
        logger.exception("NEWSALERT_COLLECTOR_FAILED source=%s", name)
        return name, CollectorResult([], [f"{type(exc).__name__}: {str(exc)[:500]}"])


async def collect_rss_items(
    feed_urls: list[str],
    *,
    limit_per_feed: int = 30,
    source_ids: list[str] | None = None,
    include_all_sources: bool = False,
) -> CollectorResult:
    """Collect registered financial sources through their full fallback waterfall.

    This deliberately calls ``collect_source`` rather than ``collect(url)``. The
    former tries direct RSS/Atom, RSS discovery, public publisher pages, JSON-LD / a
    conservative card parser, and a public Google News RSS fallback where defined.
    A failure at one publisher never stops the others.
    """
    collector = FinancialNewsCollector()
    requested_urls = [canonicalize_url(url) for url in (feed_urls or [])]
    requested_urls = [url for url in requested_urls if url]
    requested_hosts = {(urlsplit(url).hostname or "").casefold().removeprefix("www.") for url in requested_urls}
    profiles = list(NEWS_SOURCES)
    all_registered_profiles = list(profiles)
    # Explicit custom RSS URLs are not discarded just because their hosts are not
    # present in the built-in registry. Such URLs are parsed by collector.collect().
    custom_feed_urls = []
    for url in requested_urls:
        host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
        registered = any(
            host == domain.casefold().removeprefix("www.") or host.endswith("." + domain.casefold().removeprefix("www."))
            for profile in all_registered_profiles for domain in profile.domains
        )
        if host and not registered:
            custom_feed_urls.append(url)
    if source_ids:
        wanted = {str(value).strip().casefold() for value in source_ids if str(value).strip()}
        selected = []
        for profile in profiles:
            host_match = any(
                host == domain.casefold().removeprefix("www.") or host.endswith("." + domain.casefold().removeprefix("www."))
                for host in requested_hosts for domain in profile.domains
            )
            if profile.key.casefold() in wanted or host_match:
                selected.append(profile)
        profiles = selected
    elif not include_all_sources:
        profiles = [profile for profile in profiles if any(
            host == domain.casefold().removeprefix("www.") or host.endswith("." + domain.casefold().removeprefix("www."))
            for host in requested_hosts for domain in profile.domains
        )]
    else:
        # NSE is collected through its dedicated official RSS method below; don't
        # run the exchange feed twice through the general publisher pool.
        profiles = [profile for profile in profiles if profile.key != "nse_india" and not profile.source_type.startswith("exchange")]

    # Inject caller-supplied URLs before the registry feeds for each selected source.
    # The built-in registry URLs remain as failover if a custom/primary feed is down.
    run_profiles = []
    for profile in profiles:
        custom_for_profile = [url for url in requested_urls if any(
            (urlsplit(url).hostname or "").casefold().removeprefix("www.") == domain.casefold().removeprefix("www.")
            or (urlsplit(url).hostname or "").casefold().removeprefix("www.").endswith("." + domain.casefold().removeprefix("www."))
            for domain in profile.domains
        )]
        feed_values = tuple(dict.fromkeys([*custom_for_profile, *profile.feed_urls]))
        run_profiles.append(replace(profile, feed_urls=feed_values))

    if not run_profiles and not custom_feed_urls:
        return CollectorResult([], ["No registered financial-news sources matched the configured RSS URLs."],
                               {"sources": {}, "requested_feeds": requested_urls})

    semaphore = asyncio.Semaphore(4)
    async def collect_one(profile):
        async with semaphore:
            try:
                result = await collector.collect_source(profile, limit=max(1, int(limit_per_feed)))
                rows = []
                for row in result.items:
                    item = dict(row)
                    item["url"] = canonicalize_url(item.get("url"))
                    item["source"] = str(item.get("source") or profile.name)
                    item["source_key"] = str(item.get("source_key") or profile.key)
                    item["content_type"] = "NEWS"
                    item["_collector_feed_url"] = str(item.get("feed_url") or (result.attempted_urls[0] if result.attempted_urls else profile.homepage_url))
                    if not item["url"] or not (item.get("published_at") or item.get("published")):
                        continue
                    rows.append(item)
                return profile.key, rows, list(result.errors), list(result.attempted_urls)
            except Exception as exc:
                return profile.key, [], [f"{type(exc).__name__}: {str(exc)[:350]}"], []

    results = await asyncio.gather(*(collect_one(profile) for profile in run_profiles), return_exceptions=True)
    items_by_url: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    stats: dict[str, Any] = {}
    for profile, result in zip(run_profiles, results):
        if isinstance(result, BaseException):
            error = f"{profile.key}: {type(result).__name__}: {str(result)[:300]}"
            errors.append(error)
            stats[profile.key] = {"status": "failed", "fetched": 0, "errors": [error], "attempted_urls": []}
            continue
        key, rows, source_errors, attempted = result
        for item in rows:
            url = item["url"]
            previous = items_by_url.get(url)
            if previous is None:
                items_by_url[url] = item
            elif len(str(item.get("description") or item.get("summary") or "")) > len(str(previous.get("description") or previous.get("summary") or "")):
                items_by_url[url] = item
        errors.extend(f"{key}: {error}" for error in source_errors)
        stats[key] = {
            "status": "failed" if source_errors and not rows else ("partial" if source_errors else ("ok" if rows else "empty")),
            "fetched": len(rows), "errors": [str(e)[:250] for e in source_errors[:3]],
            "attempted_urls": attempted[:12],
        }
        logger.info("NEWSALERT_FINANCIAL_SOURCE_RESULT source=%s status=%s fetched=%s attempted=%s errors=%s",
                    key, stats[key]["status"], len(rows), len(attempted), len(source_errors))
    if custom_feed_urls:
        custom_semaphore = asyncio.Semaphore(4)
        async def collect_custom_url(url: str):
            async with custom_semaphore:
                try:
                    rows = await collector.collect(url)
                    accepted = []
                    for row in rows[:max(1, int(limit_per_feed))]:
                        item = dict(row)
                        item["url"] = canonicalize_url(item.get("url") or item.get("link"))
                        if not item["url"] or not (item.get("published_at") or item.get("published") or item.get("date")):
                            continue
                        item["source"] = str(item.get("source") or _source_name_for_url(url))
                        item["source_key"] = str(item.get("source_key") or "custom_rss")
                        item["source_url"] = url
                        item["_collector_feed_url"] = url
                        item["content_type"] = "NEWS"
                        item["transport"] = str(item.get("transport") or "custom-public-rss")
                        accepted.append(item)
                    return url, accepted, []
                except Exception as exc:
                    return url, [], [f"{url}: {type(exc).__name__}: {str(exc)[:300]}"]
        custom_results = await asyncio.gather(*(collect_custom_url(url) for url in custom_feed_urls))
        for url, rows, custom_errors in custom_results:
            host = (urlsplit(url).hostname or url).casefold().removeprefix("www.")
            for item in rows:
                item_url = item["url"]
                previous = items_by_url.get(item_url)
                if previous is None or len(str(item.get("summary") or item.get("description") or "")) > len(str(previous.get("summary") or previous.get("description") or "")):
                    items_by_url[item_url] = item
            errors.extend(custom_errors)
            stats[f"custom:{host}"] = {
                "status": "failed" if custom_errors and not rows else ("partial" if custom_errors else ("ok" if rows else "empty")),
                "fetched": len(rows), "errors": custom_errors[:2], "attempted_urls": [url],
            }
            logger.info("NEWSALERT_CUSTOM_RSS_RESULT host=%s fetched=%s errors=%s", host, len(rows), len(custom_errors))

    return CollectorResult(list(items_by_url.values()), errors[:150], {"sources": stats, "source_count": len(run_profiles) + len(custom_feed_urls), "custom_feed_count": len(custom_feed_urls), "requested_feeds": requested_urls, "deduplicated_urls": len(items_by_url)})


def _log_youtube_candidate_rejection(
    row: dict[str, Any], reason: str, *, company_match: str | None = None,
) -> None:
    """Log candidate metadata and one deterministic rejection reason, never transcript text."""
    title = " ".join(str(row.get("title") or row.get("headline") or "").split())[:240]
    published = _aware_datetime(row.get("published_at") or row.get("published"))
    channel = " ".join(str(row.get("channel_title") or row.get("source") or "YouTube").split())[:100]
    video_id = str(row.get("video_id") or "")[:32]
    logger.info(
        "NEWSALERT_YOUTUBE_CANDIDATE decision=rejected video_id=%s title=%r upload_date=%s channel=%r company_match=%r reason=%s",
        video_id, title, published.isoformat() if published else "unknown", channel,
        (company_match or "not_checked")[:120], reason,
    )


async def collect_youtube_items(
    channel_specs: list[str],
    coverage_companies: list[CoverageCompany] | None = None,
) -> CollectorResult:
    # The existing collector's default configuration is five public finance
    # channels; explicit NEWSALERT_YOUTUBE_CHANNELS/YOUTUBE_CHANNEL_IDS replaces it.
    collector = YouTubeNewsCollector()
    errors: list[str] = []
    resolved: list[tuple[str, str | None]] = []
    try:
        # A previous hard cap of five meant the eight additional finance-publisher
        # channels were resolved (and logged) but never actually scraped. Resolve
        # configured and maintained defaults together, then scrape a bounded number
        # in each cycle. Operators can tune cost/coverage without changing code.
        try:
            max_channels = int(os.getenv("NEWSALERT_YOUTUBE_MAX_CHANNELS", "13"))
        except (TypeError, ValueError):
            max_channels = 13
        max_channels = max(5, min(max_channels, 20))
        resolved = (await collector.resolve_channel_ids(channel_specs or None))[:max_channels]
    except Exception as exc:
        message = f"channel resolution: {type(exc).__name__}: {str(exc)[:350]}"
        errors.append(message)
        resolved = []
    if not resolved:
        errors.append("No YouTube channel IDs could be resolved; company interview search may still run")
    semaphore = asyncio.Semaphore(5)
    async def fetch(channel_id: str, channel_name: str | None) -> tuple[str, list[dict[str, Any]], list[str]]:
        name = channel_name or channel_id
        async with semaphore:
            errors_for_channel: list[str] = []
            rows_by_url: dict[str, dict[str, Any]] = {}
            try:
                # 1) Direct Atom HTTP with BrowserClient/Playwright fallback inside collector.
                for row in await collector._fetch_rss(channel_id, name, days=_youtube_lookback_days()):
                    if isinstance(row.get("published_at"), datetime) and row.get("_timestamp_exact"):
                        rows_by_url[str(row.get("url"))] = row
            except Exception as exc:
                errors_for_channel.append(f"Atom feed: {type(exc).__name__}: {str(exc)[:220]}")

            # 2) Channel /videos HTML plus exact watch-page metadata, only when feed is thin.
            if len(rows_by_url) < 3:
                try:
                    page_rows = await collector._fetch_channel_videos_page(channel_id, name)
                    async def enrich_channel_candidate(candidate: dict[str, Any]):
                        url = str(candidate.get("url") or "")
                        if not url:
                            return None
                        exact = await collector.fetch_video_metadata(url)
                        if not exact or not collector._exact_recent(exact):
                            return None
                        if exact.get("channel_id") and exact["channel_id"] != channel_id:
                            return None
                        exact["channel_title"] = exact.get("channel_title") or name
                        exact["source"] = f"YouTube · {exact['channel_title']}"
                        exact["source_item_id"] = exact.get("video_id")
                        exact["transport"] = "channel-videos-page+watch-metadata"
                        return url, exact
                    exact_candidates = await asyncio.gather(
                        *(enrich_channel_candidate(candidate) for candidate in page_rows[:3]),
                        return_exceptions=True,
                    )
                    for exact_candidate in exact_candidates:
                        if isinstance(exact_candidate, tuple):
                            url, exact = exact_candidate
                            rows_by_url[url] = exact
                except Exception as exc:
                    errors_for_channel.append(f"Channel page fallback: {type(exc).__name__}: {str(exc)[:220]}")

            # 3) YouTube HTML -> yt-dlp -> Google/Bing indexed discovery, all in the collector.
            if len(rows_by_url) < 3:
                try:
                    search_rows = await collector._discover_recent_interviews(
                        f'"{name}" business interview management earnings in conversation podcast',
                        allowed_channel_ids={channel_id}, max_results=12,
                    )
                    for row in search_rows:
                        row.setdefault("source_item_id", row.get("video_id"))
                        rows_by_url.setdefault(str(row.get("url")), row)
                except Exception as exc:
                    errors_for_channel.append(f"Search waterfall: {type(exc).__name__}: {str(exc)[:220]}")

            # Enforce an original publication instant and configured YouTube lookback.
            now = datetime.now(UTC)
            cutoff = now - timedelta(days=_youtube_lookback_days())
            output = []
            for row in rows_by_url.values():
                published = row.get("published_at")
                if not isinstance(published, datetime) or not row.get("_timestamp_exact"):
                    continue
                if published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
                published = published.astimezone(UTC)
                if cutoff <= published <= now:
                    row["published_at"] = published
                    row.setdefault("content_type", "INTERVIEW")
                    row.setdefault("is_youtube", True)
                    row.setdefault("is_interview", True)
                    row.setdefault("source_item_id", row.get("video_id"))
                    output.append(row)
            output.sort(key=lambda item: item["published_at"], reverse=True)
            if not output and not errors_for_channel:
                # Empty is a legitimate result; diagnostics in collector explain method counts.
                logger.info("NEWSALERT_YOUTUBE_CHANNEL_EMPTY channel=%s methods=%s", name, collector.last_diagnostics.get("transport"))
            return channel_id, output, errors_for_channel
    results = await asyncio.gather(*(fetch(channel_id, name) for channel_id, name in resolved))
    items = [item for _channel, rows, _errors in results for item in rows]
    errors.extend(error for _channel, _rows, channel_errors in results for error in channel_errors)
    channels = {
        channel: {"status": "failed" if channel_errors and not rows else ("partial" if channel_errors else ("ok" if rows else "empty")),
                  "fetched": len(rows), "errors": [e[:300] for e in channel_errors[:2]]}
        for channel, rows, channel_errors in results
    }

    # Coverage-specific discovery supplements channel RSS/page collection. To keep
    # a ten-minute cycle bounded with a large coverage list, the list is rotated in
    # deterministic ten-minute slots; repeated cycles eventually search every issuer.
    companies = sorted(list(coverage_companies or []), key=lambda company: company.name.casefold())
    search_diag: dict[str, Any] = {"enabled": bool(companies), "companies_available": len(companies), "companies_searched": 0, "fetched": 0}
    if not companies:
        # Channel RSS / public video-page items remain eligible even when Coverage
        # List is empty; only the optional company-specific discovery is disabled.
        search_diag["reason"] = "no_coverage_companies_channel_collection_still_enabled"
    else:
        try:
            per_cycle = max(4, min(int(os.getenv("NEWSALERT_YOUTUBE_COMPANIES_PER_CYCLE", "12")), 35))
        except (TypeError, ValueError):
            per_cycle = 12
        cycle_slot = int(datetime.now(UTC).timestamp() // 600) % max(1, (len(companies) + per_cycle - 1) // per_cycle)
        batch = companies[cycle_slot * per_cycle:(cycle_slot + 1) * per_cycle]
        search_diag.update({"companies_per_cycle": per_cycle, "cycle_slot": cycle_slot, "companies_searched": len(batch)})
        company_semaphore = asyncio.Semaphore(3)
        async def search_company(company: CoverageCompany):
            async with company_semaphore:
                query = (
                    f'"{company.name}" promoter interview OR management interview OR '
                    f'earnings call OR concall OR AGM OR annual general meeting'
                )
                try:
                    rows = await collector._discover_recent_interviews(query, max_results=8)
                    accepted = []
                    for row in rows:
                        title = str(row.get("title") or "").strip()
                        summary = str(row.get("summary") or row.get("description") or "").strip()
                        published = row.get("published_at")
                        if not title or not isinstance(published, datetime) or not row.get("_timestamp_exact"):
                            continue
                        if not collector._exact_recent(row):
                            continue
                        # Search is discovery only. The final approval pass below
                        # checks the complete Coverage List against transcript evidence.
                        row["discovery_query_company"] = company.name
                        row["source"] = str(row.get("source") or f"YouTube · {row.get('channel_title') or 'Video'}")
                        row["source_key"] = "youtube"
                        row["content_type"] = "INTERVIEW"
                        row["is_youtube"] = True
                        row["is_interview"] = True
                        row["source_item_id"] = row.get("source_item_id") or row.get("video_id")
                        row["company_name"] = match.name
                        row["nse_symbol"] = match.nse_symbol
                        row["_youtube_coverage_match"] = True
                        accepted.append(row)
                    return company.name, accepted, []
                except Exception as exc:
                    return company.name, [], [f"{company.name}: {type(exc).__name__}: {str(exc)[:180]}"]
        search_results = await asyncio.gather(*(search_company(company) for company in batch), return_exceptions=True)
        company_found = 0
        for entry in search_results:
            if isinstance(entry, BaseException):
                errors.append(f"Coverage YouTube search: {type(entry).__name__}: {str(entry)[:180]}")
                continue
            company_name, rows, row_errors = entry
            errors.extend(row_errors)
            items.extend(rows)
            company_found += len(rows)
            logger.info("NEWSALERT_YOUTUBE_COVERAGE_SEARCH company=%s fetched=%s errors=%s", company_name, len(rows), len(row_errors))
        search_diag["fetched"] = company_found

    # Channel-scoped finance interviews are valid NewsAlert content even if the
    # title does not uniquely name a Coverage List issuer. Matching enriches the
    # company field, but must not erase otherwise valid interviews.
    matcher = build_company_matcher(companies)
    unique: dict[str, dict[str, Any]] = {}
    eligibility = {
        "received": len(items), "accepted": 0, "rejected_invalid_url": 0,
        "rejected_missing_or_inexact_timestamp": 0, "rejected_outside_lookback": 0,
        "rejected_not_finance_interview": 0, "rejected_stock_tip_or_noise": 0,
        "company_matched": 0, "company_unmatched_but_kept": 0,
        "transcript_available": 0, "transcript_missing": 0,
        "rejected_company_not_in_coverage": 0,
        "rejected_speaker_unverified": 0,
    }
    for row in items:
        title = " ".join(str(row.get("title") or row.get("headline") or "").split()).strip()
        summary = " ".join(str(row.get("summary") or row.get("description") or "").split()).strip()
        channel_title = str(row.get("channel_title") or row.get("source") or "YouTube").strip()
        url = canonicalize_url(row.get("url"))
        if not url or not ("youtube.com/watch" in url or "youtu.be/" in url):
            eligibility["rejected_invalid_url"] += 1
            _log_youtube_candidate_rejection(row, "invalid_or_non_youtube_url")
            continue
        published = _aware_datetime(row.get("published_at") or row.get("published"))
        if published is None or not row.get("_timestamp_exact"):
            eligibility["rejected_missing_or_inexact_timestamp"] += 1
            _log_youtube_candidate_rejection(row, "missing_or_inexact_upload_timestamp")
            continue
        # Acceptance intentionally has only two content filters: (1) a match to
        # the approved Coverage List, and (2) a management-role keyword anywhere
        # in title, description, or transcript. Date/noise/interview/speaker gates
        # are not applied here; discovery's configured lookback still bounds fetches.
        transcript, transcript_status = await fetch_transcript(url)
        row["transcript_status"] = transcript_status
        if transcript:
            eligibility["transcript_available"] += 1
        else:
            eligibility["transcript_missing"] += 1

        evidence_text = f"{title}\n{summary[:5000]}\n{(transcript or '')[:50000]}"
        match = match_company(evidence_text, matcher)
        if match is None:
            interview_matches = _interview_company_matches(evidence_text, companies)
            match = interview_matches[0] if interview_matches else None
        if match is None:
            eligibility["rejected_company_not_in_coverage"] += 1
            _log_youtube_candidate_rejection(row, "no_approved_coverage_company_match", company_match="none")
            continue

        role_pattern = re.compile(
            r"\b(?:md|m\.?d\.?|managing director|joint managing director|"
            r"whole[- ]time director|ceo|chief executive officer|chairman|"
            r"chairperson|chairwoman|promoter|founder|co[- ]founder|"
            r"executive director|cfo|chief financial officer|coo|"
            r"chief operating officer|cto|chief technology officer|"
            r"director|business head|president)\b", re.I,
        )
        if not role_pattern.search(evidence_text):
            eligibility["rejected_speaker_unverified"] += 1
            _log_youtube_candidate_rejection(row, "no_management_role_keyword_in_title_description_or_transcript", company_match=match.name)
            continue
        speaker_reason = "coverage_company_and_management_role_keyword_match"

        # Transcript is used transiently for filtering only; do not attach it to
        # downstream rows, logs, or persisted NewsAlert payloads.
        row["url"] = url
        row["published_at"] = published
        row["source"] = str(row.get("source") or f"YouTube · {channel_title}")
        row["source_key"] = "youtube"
        row["content_type"] = "INTERVIEW"
        row["is_youtube"] = True
        row["is_interview"] = True
        row["source_item_id"] = row.get("source_item_id") or row.get("video_id") or url
        row["channel_title"] = channel_title[:255]
        row["company_name"] = match.name
        row["nse_symbol"] = match.nse_symbol
        row["_youtube_coverage_match"] = True
        eligibility["company_matched"] += 1
        logger.info(
            "NEWSALERT_YOUTUBE_CANDIDATE decision=accepted video_id=%s title=%r upload_date=%s channel=%r company_match=%r reason=%s",
            str(row.get("video_id") or "")[:32], title[:240], published.isoformat(),
            channel_title[:100], match.name[:120], speaker_reason,
        )
        previous = unique.get(url)
        if previous is None or len(summary) > len(str(previous.get("summary") or previous.get("description") or "")):
            unique[url] = row
    items = list(unique.values())
    eligibility["accepted"] = len(items)
    logger.info(
        "NEWSALERT_YOUTUBE_RESULT channels=%s coverage_companies=%s fetched=%s atom_eligibility=%s channel_page_eligibility=%s search_page_eligibility=%s exact_eligibility=%s errors=%s",
        len(resolved), search_diag.get("companies_searched", 0), len(items),
        collector.last_diagnostics.get("atom_eligibility", {}),
        collector.last_diagnostics.get("channel_page_eligibility", {}),
        collector.last_diagnostics.get("search_page_eligibility", {}),
        collector.last_diagnostics.get("eligibility", {}), len(errors),
    )
    return CollectorResult(items, errors, {
        "resolved_channels": len(resolved), "channels": channels,
        "company_interview_search": search_diag,
        "transport_attempts": list(collector.last_diagnostics.get("transports_attempted") or []),
        "collector_errors": list(collector.last_diagnostics.get("errors") or [])[:30],
        "eligibility": dict(collector.last_diagnostics.get("eligibility") or {}),
        "atom_eligibility": dict(collector.last_diagnostics.get("atom_eligibility") or {}),
        "channel_page_eligibility": dict(collector.last_diagnostics.get("channel_page_eligibility") or {}),
        "search_page_eligibility": dict(collector.last_diagnostics.get("search_page_eligibility") or {}),
        "search_discoveries": list(collector.last_diagnostics.get("search_discoveries") or [])[:20],
        "invalid_channel_ids": list(collector.last_diagnostics.get("invalid_channel_ids") or []),
        "api_used": False, "lookback_hours": _youtube_lookback_days() * 24, "final_eligibility": eligibility,
    })


async def collect_google_news_items(
    collector: FinancialNewsCollector,
    companies: list[CoverageCompany],
    *,
    max_results_per_company: int = 12,
) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for offset in range(0, len(companies), 4):
        batch = companies[offset:offset + 4]
        async def fetch(company: CoverageCompany) -> tuple[list[dict[str, Any]], list[str]]:
            try:
                rows = await collector.collect_company_google_news(
                    company.name, nse_symbol=company.nse_symbol, max_results=max_results_per_company,
                )
                for row in rows:
                    row["content_type"] = "NEWS"
                    row["source"] = str(row.get("source") or "Google News")
                    row["source_item_id"] = row.get("source_item_id") or row.get("guid")
                return rows, []
            except Exception as exc:
                return [], [f"Google News {company.name}: {type(exc).__name__}: {str(exc)[:350]}"]
        results = await asyncio.gather(*(fetch(company) for company in batch))
        for rows, source_errors in results:
            items.extend(rows)
            errors.extend(source_errors)
    return items, errors


def _bse_company_name_keys(value: Any) -> tuple[str, ...]:
    """Return conservative exact-match keys for company/scrip-master names."""
    base = _normalise(value)
    if not base:
        return ()
    keys = [base]
    # Strip legal-form suffixes only. Terms such as "Industries" and
    # "Technology" are meaningful parts of issuer names and must not be
    # discarded into a potentially ambiguous match.
    suffixes = {
        "limited", "ltd", "private", "pvt", "incorporated", "inc", "corporation",
        "corp", "company", "co", "llp", "plc",
    }
    tokens = base.split()
    while tokens and tokens[-1] in suffixes:
        tokens.pop()
        candidate = " ".join(tokens).strip()
        if candidate and candidate not in keys:
            keys.append(candidate)
    return tuple(keys)


def _bse_pdf_url(value: Any) -> str | None:
    """Accept only a direct PDF URL hosted by BSE."""
    candidate = str(value or "").strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not (host == "bseindia.com" or host.endswith(".bseindia.com")):
        return None
    return candidate if parsed.path.rstrip("/").casefold().endswith(".pdf") else None


def _build_bse_stock_page_url(company_name: Any, bse_code: Any, bse_symbol: Any = None) -> str | None:
    """Build BSE's canonical quote URL from a validated equity scrip code."""
    code = re.sub(r"\D", "", str(bse_code or ""))
    if not code:
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


def _needs_bse_issuer_fallback(row: dict[str, Any]) -> bool:
    """Only identifiable board meetings and NSE rows without a valid PDF need BSE routing."""
    feed = str(row.get("nse_filing_feed") or "").strip().casefold()
    has_issuer = any(str(row.get(field) or "").strip() for field in ("company_name", "nse_symbol", "symbol", "isin"))
    if not has_issuer:
        return False
    if feed == "board_meetings":
        return True
    return not _valid_nse_pdf_attachment(
        row.get("attachment_url"), attachment_type=row.get("attachment_type")
    )


async def _collect_bse_announcements(
    *,
    days: int = 7,
    max_pages: int = 500,
    issuer_rows: list[dict[str, Any]] | None = None,
    db=None,
    collect_announcements: bool = True,
) -> CollectorResult:
    """Collect BSE announcements and enrich company BSE codes without manual maps.

    BSE's JSON endpoints are tried first using browser-compatible headers. The
    downloadable exchange-wide BSE CM symbol master is then used as an independent
    code-discovery fallback; the BSE public announcements XML feeds are the data
    fallback when the JSON announcement endpoint is denied.
    """
    from email.utils import parsedate_to_datetime
    from urllib.parse import quote

    api_url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    master_url = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    announcements_url = "https://www.bseindia.com/corporates/ann.html"
    attachment_base = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
    ist = ZoneInfo("Asia/Kolkata")
    end = datetime.now(ist).date()
    start = end - timedelta(days=max(1, min(int(days), 14)) - 1)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://www.bseindia.com",
        # BSE's API checks the site root as Referer; a deep-link referer can
        # produce 403s even when the same request works in a browser.
        "Referer": "https://www.bseindia.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
        "Sec-CH-UA": '"Google Chrome";v="155", "Chromium";v="155", "Not?A_Brand";v="99"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "DNT": "1",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }
    params_base = {
        "strCat": "-1", "subcategory": "-1", "strPrevDate": start.strftime("%Y%m%d"),
        # Keep BSE's documented parameter casing. The public client sends
        # `strScrip`; changing its case can silently return an empty Table.
        "strScrip": "", "strSearch": "P", "strToDate": end.strftime("%Y%m%d"), "strType": "C",
    }
    items: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    diagnostics: dict[str, Any] = {
        "api_used": False, "api_pages": 0, "api_error": None,
        "api_http_statuses": [], "api_response_keys": [], "api_total_rows_reported": None,
        "api_page_row_counts": [], "api_rows_seen": 0, "api_expected_pages": None,
        "api_pagination_incomplete": False,
        "scrip_master_used": False, "scrip_master_source": None,
        "scrip_master_rows": 0, "scrip_master_matches": 0,
        "xml_fallback_used": False, "xml_feed_url": None,
    }
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
    issuer_rows = issuer_rows if issuer_rows is not None else []
    bse_lookup_started = time.perf_counter()
    requested_issuer_count = len(issuer_rows)

    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        # --- 1. Get BSE scrip master: BSE API first, FYERS BSE_CM.csv fallback. ---
        master_entries: list[tuple[str, str, str, str]] = []  # (name, code, ISIN, BSE ticker)
        try:
            response = await client.get(
                master_url,
                params={"Group": "", "Scripcode": "", "segment": "Equity", "status": "Active", "scripName": ""},
                headers={"Host": "api.bseindia.com"},
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload if isinstance(payload, list) else (
                payload.get("Table") or payload.get("table") or payload.get("data") or []
                if isinstance(payload, dict) else []
            )
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                lowered = {re.sub(r"[^a-z0-9]", "", str(k).casefold()): v for k, v in row.items()}
                name = next((str(lowered[k]).strip() for k in (
                    "scripname", "scripnm", "companyname", "securityname", "security_name"
                ) if lowered.get(k)), "")
                code = next((str(lowered[k]).strip().split(".")[0] for k in (
                    "scripcd", "scripcode", "securitycode", "security_code"
                ) if lowered.get(k)), "")
                isin = next((str(lowered[k]).strip().upper() for k in (
                    "isin", "isincode", "isinno", "securityisin"
                ) if lowered.get(k)), "")
                ticker = next((str(lowered[k]).strip() for k in (
                    "symbol", "ticker", "scripid", "scripsymbol", "securitysymbol", "shortname"
                ) if lowered.get(k)), "")
                if name and code.isdigit():
                    master_entries.append((name, code, isin, ticker))
            if master_entries:
                diagnostics["scrip_master_source"] = "bse-api"
        except Exception as exc:
            diagnostics["api_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
            logger.warning("NEWSALERT_BSE_SCRIP_MASTER_API_FAILED error=%s", exc)

        # The public BSE API frequently blocks cloud hosts. FYERS exposes the
        # exchange-wide BSE CM symbol master as a plain, automatically refreshed CSV.
        # Even when BSE's own security master returns names, it often omits ISINs.
        # Always supplement by exact scrip code whenever ISIN or ticker is missing;
        # this is what lets company matching prefer ISIN instead of names alone.
        if not master_entries or any(not entry[2] or not entry[3] for entry in master_entries):
            try:
                csv_response = await client.get(
                    "https://public.fyers.in/sym_details/BSE_CM.csv",
                    headers={"Accept": "text/csv, text/plain, */*", "Referer": "https://fyers.in/", "Origin": "https://fyers.in"},
                )
                csv_response.raise_for_status()
                fyers_entries: list[tuple[str, str, str, str]] = []
                for row in csv.reader(io.StringIO(csv_response.text)):
                    if len(row) < 13:
                        continue
                    name = row[1].strip()
                    isin = row[5].strip().upper()
                    # FYERS BSE_CM layout: symbol ticker at 9, exchange at 10,
                    # segment at 11 and numeric BSE scrip code at 12.
                    exchange = row[10].strip()
                    segment = row[11].strip()
                    code = row[12].strip().split(".")[0]
                    # For BSE_CM, column 13 (Underlying symbol) is the plain
                    # base equity ticker used by BSE quote URLs (RAYMOND), while
                    # column 9 is the broker symbol with a series suffix (RAYMOND-A).
                    ticker_value = (row[13].strip() if len(row) > 13 else "") or row[9].strip()
                    ticker = ticker_value.split(":", 1)[-1].strip() if ticker_value else ""
                    if not name or not code.isdigit():
                        continue
                    if exchange and exchange != "12":
                        continue
                    if segment and segment != "10":
                        continue
                    if isin and not re.fullmatch(r"IN[A-Z0-9]{10,12}", isin):
                        isin = ""
                    fyers_entries.append((name, code, isin, ticker))

                if fyers_entries:
                    if master_entries:
                        # Keep BSE's official names/ISINs, while supplementing missing
                        # BSE page tickers from FYERS by the exact numeric scrip code.
                        fyers_by_code = {entry[1]: entry for entry in fyers_entries}
                        supplemented: list[tuple[str, str, str, str]] = []
                        for master_name, code, isin, ticker in master_entries:
                            fyers_match = fyers_by_code.get(code)
                            if fyers_match:
                                isin = isin or fyers_match[2]
                                ticker = ticker or fyers_match[3]
                            supplemented.append((master_name, code, isin, ticker))
                        known_codes = {entry[1] for entry in supplemented}
                        supplemented.extend(entry for entry in fyers_entries if entry[1] not in known_codes)
                        master_entries = supplemented
                        diagnostics["scrip_master_source"] = "bse-api+fyers-bse-cm-csv"
                    else:
                        master_entries = fyers_entries
                        diagnostics["scrip_master_source"] = "fyers-bse-cm-csv"
            except Exception as exc:
                diagnostics["scrip_master_csv_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                logger.warning("NEWSALERT_BSE_SCRIP_MASTER_CSV_FAILED error=%s", exc)

        diagnostics["scrip_master_rows"] = len(master_entries)
        diagnostics["scrip_master_used"] = bool(master_entries)
        logger.info(
            "NEWSALERT_BSE_SCRIP_MASTER_FETCH_DONE requested_issuers=%s master_rows=%s source=%s elapsed_seconds=%.2f",
            requested_issuer_count, len(master_entries), diagnostics.get("scrip_master_source"),
            time.perf_counter() - bse_lookup_started,
        )

        # A second FYERS symbol-master representation is available if the CSV
        # distribution is empty/unavailable. It uses the same BSE CM segment but
        # includes an explicit active/inactive status flag.
        if not master_entries:
            try:
                json_response = await client.get(
                    "https://public.fyers.in/sym_details/BSE_CM_sym_master.json",
                    headers={"Accept": "application/json, text/plain, */*", "Referer": "https://fyers.in/", "Origin": "https://fyers.in"},
                )
                json_response.raise_for_status()
                json_payload = json_response.json()
                json_rows = json_payload.get("symbols", json_payload) if isinstance(json_payload, dict) else {}
                for ticker_key, row in json_rows.items() if isinstance(json_rows, dict) else []:
                    if not isinstance(row, dict):
                        continue
                    exchange = str(row.get("exchange", "12")).strip()
                    segment = str(row.get("segment", "10")).strip()
                    status = str(row.get("tradeStatus", "1")).strip()
                    if exchange != "12" or segment != "10" or status not in {"", "1"}:
                        continue
                    name = str(row.get("symDetails") or row.get("symbolDesc") or row.get("exSymName") or "").strip()
                    code = str(row.get("exToken") or row.get("scripCode") or "").strip().split(".")[0]
                    isin = str(row.get("isin") or "").strip().upper()
                    ticker = str(row.get("symTicker") or ticker_key or "").strip()
                    ticker = ticker.split(":", 1)[-1].strip() if ticker else ""
                    if name and code.isdigit():
                        master_entries.append((name, code, isin, ticker))
                if master_entries:
                    diagnostics["scrip_master_source"] = "fyers-bse-cm-json"
            except Exception as exc:
                diagnostics["scrip_master_json_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                logger.warning("NEWSALERT_BSE_SCRIP_MASTER_JSON_FAILED error=%s", exc)

        # Index only unambiguous issuer keys. ISIN is always preferred over name.
        by_isin: dict[str, str] = {}
        ambiguous_isins: set[str] = set()
        name_codes: dict[str, set[str]] = {}
        code_symbols: dict[str, str] = {}
        codes_by_bse_ticker: dict[str, set[str]] = {}
        for master_name, code, isin, ticker in master_entries:
            if ticker and not ticker.isdigit():
                code_symbols.setdefault(code, ticker)
                base_ticker = ticker.split(":", 1)[-1].strip().upper()
                base_ticker = re.sub(r"-(?:A|B|T|X|M|XT|MT|ST|Z|ZP|P|E|BE)$", "", base_ticker, flags=re.I)
                if base_ticker:
                    codes_by_bse_ticker.setdefault(base_ticker, set()).add(code)
            if isin:
                if isin in ambiguous_isins:
                    pass
                elif isin not in by_isin:
                    by_isin[isin] = code
                elif by_isin[isin] != code:
                    by_isin.pop(isin, None)
                    ambiguous_isins.add(isin)
            for key in _bse_company_name_keys(master_name):
                name_codes.setdefault(key, set()).add(code)

        existing_code_owner: dict[str, int] = {}
        if db is not None:
            try:
                # Only read the ownership index here. collect_nse_items() has
                # already resolved the requested missing-PDF/board-meeting rows
                # against the Company registry. Expanding issuer_rows with every
                # Company record here made an NSE-only run silently backfill the
                # entire BSE master into the database (thousands of per-row writes),
                # which is why the run appeared stuck immediately after the CSV GET.
                owner_rows = (await db.execute(
                    select(Company.id, Company.bse_code).where(
                        Company.bse_code.is_not(None), Company.bse_code != ""
                    )
                )).all()
                existing_code_owner = {
                    str(code).strip(): int(company_id)
                    for company_id, code in owner_rows if str(code or "").strip()
                }
            except Exception as exc:
                logger.warning("NEWSALERT_BSE_CODE_OWNER_INDEX_READ_FAILED error=%s", exc)
        logger.info(
            "NEWSALERT_BSE_CODE_MATCH_START requested_issuers=%s master_rows=%s existing_code_owners=%s",
            requested_issuer_count, len(master_entries), len(existing_code_owner),
        )

        matches = 0
        matches_by_isin = 0
        matches_by_name = 0
        matches_by_nse_symbol = 0
        code_owner_conflicts = 0
        # Reserve newly discovered codes as soon as a Company row claims them.
        # Previously ownership was checked only against the database snapshot and
        # was not updated during matching. Two different Company rows could thus
        # both receive the same newly discovered scrip code in the diagnostics map.
        # A later redundant UPDATE then violated companies.bse_code's UNIQUE key.
        reserved_code_owner = dict(existing_code_owner)
        for issuer in issuer_rows:
            existing_code = str(issuer.get("bse_code") or "").strip()
            if existing_code:
                if not issuer.get("bse_symbol") and code_symbols.get(existing_code):
                    issuer["bse_symbol"] = code_symbols[existing_code]
                continue
            isin = str(issuer.get("isin") or "").strip().upper()
            code = by_isin.get(isin) if isin else None
            match_method = "isin" if code else ""
            if not code:
                possible: set[str] = set()
                for name in (issuer.get("company_name"), *(issuer.get("aliases") or [])):
                    for key in _bse_company_name_keys(name):
                        possible.update(name_codes.get(key, set()))
                if len(possible) == 1:
                    code = next(iter(possible))
                    match_method = "name"
            # Tertiary fallback: an exact NSE ticker match to the BSE cash-market
            # ticker, accepted only when that ticker resolves to one BSE scrip code.
            # This is a lookup key only; it never substitutes the NSE ticker in a URL.
            if not code:
                nse_symbol = str(issuer.get("nse_symbol") or "").strip().upper()
                possible = codes_by_bse_ticker.get(nse_symbol, set()) if nse_symbol else set()
                if len(possible) == 1:
                    code = next(iter(possible))
                    match_method = "nse_symbol"
            if code:
                company_id = issuer.get("company_id")
                owner = reserved_code_owner.get(code)
                # Existing database ownership and claims made earlier in this run
                # are both authoritative. Leave a second company unresolved rather
                # than risk assigning another issuer's BSE scrip code.
                if company_id:
                    company_id = int(company_id)
                    if owner is not None and owner != company_id:
                        code_owner_conflicts += 1
                        continue
                    reserved_code_owner[code] = company_id
                issuer["bse_code"] = code
                if code_symbols.get(code):
                    issuer["bse_symbol"] = code_symbols[code]
                matches += 1
                if match_method == "isin":
                    matches_by_isin += 1
                elif match_method == "name":
                    matches_by_name += 1
                elif match_method == "nse_symbol":
                    matches_by_nse_symbol += 1
        diagnostics["scrip_master_matches"] = matches
        diagnostics["scrip_master_code_owner_conflicts_prevented"] = code_owner_conflicts
        logger.info(
            "NEWSALERT_BSE_CODE_MATCH_DONE requested_issuers=%s matched=%s conflicts=%s elapsed_seconds=%.2f",
            requested_issuer_count, matches, code_owner_conflicts,
            time.perf_counter() - bse_lookup_started,
        )
        diagnostics["scrip_master_matches_by_isin"] = matches_by_isin
        diagnostics["scrip_master_matches_by_name"] = matches_by_name
        diagnostics["scrip_master_matches_by_nse_symbol"] = matches_by_nse_symbol

        # Persist discovered codes automatically, preserving existing/manual codes
        # and avoiding unique-code collisions. The caller owns the transaction.
        persisted_codes = 0
        if db is not None:
            # issuer_rows contains only one representative per issuer that needs
            # fallback routing. Never persist codes for unrelated Company rows.
            seen_company_ids: set[int] = set()
            for issuer in issuer_rows:
                company_id = issuer.get("company_id")
                code = str(issuer.get("bse_code") or "").strip()
                if not company_id or not code.isdigit():
                    continue
                company_id = int(company_id)
                if company_id in seen_company_ids:
                    continue
                seen_company_ids.add(company_id)
                owner = existing_code_owner.get(code)
                if owner is not None and owner != int(company_id):
                    continue
                if owner is None:
                    try:
                        # Keep the unique index as the final guard if another worker
                        # enriches the Company row concurrently. The savepoint lets a
                        # collision skip this one enrichment without aborting the
                        # entire NewsAlert transaction/backfill.
                        async with db.begin_nested():
                            await db.execute(
                                update(Company).where(
                                    Company.id == int(company_id),
                                    or_(Company.bse_code.is_(None), Company.bse_code == ""),
                                ).values(bse_code=code[:40])
                            )
                        existing_code_owner[code] = int(company_id)
                        persisted_codes += 1
                    except IntegrityError:
                        owner_result = await db.execute(
                            select(Company.id).where(Company.bse_code == code).limit(1)
                        )
                        actual_owner = owner_result.scalar_one_or_none()
                        if actual_owner is not None:
                            existing_code_owner[code] = int(actual_owner)
                            if int(actual_owner) != int(company_id):
                                issuer["bse_code"] = None
                                issuer["bse_symbol"] = None
                        logger.warning(
                            "NEWSALERT_BSE_CODE_UNIQUE_CONFLICT_SKIPPED company_id=%s bse_code=%s owner_id=%s",
                            company_id, code, actual_owner,
                        )
        logger.info(
            "NEWSALERT_BSE_CODE_PERSIST_DONE requested_issuers=%s persisted_company_codes=%s elapsed_seconds=%.2f",
            requested_issuer_count, persisted_codes, time.perf_counter() - bse_lookup_started,
        )

        # NSE fallback mode only needs the exchange scrip master. It must never
        # crawl BSE announcement pages while the caller explicitly selected NSE.
        if not collect_announcements:
            diagnostics.update({
                "announcement_collection_skipped": True,
                "announcement_skip_reason": "master_only_for_missing_nse_pdf_or_board_meeting",
                "pages_limit": 0,
                "fetched": 0,
                "lookback_days": days,
                "coverage_company_bse_codes": {
                    str(row["company_id"]): str(row["bse_code"])
                    for row in issuer_rows if row.get("company_id") and row.get("bse_code")
                },
            })
            logger.info(
                "NEWSALERT_BSE_MASTER_ONLY_RESULT candidates=%s matches=%s source=%s",
                len(issuer_rows), diagnostics.get("scrip_master_matches", 0),
                diagnostics.get("scrip_master_source"),
            )
            return CollectorResult([], errors, diagnostics)

        # --- 2. Primary BSE JSON announcements API. ---
        # BSE's response is paginated: Table contains rows and Table1[0].ROWCNT
        # reports the total. A multi-day query can return HTTP 200 with an empty
        # Table on some BSE deployments, so retry the same date window day-by-day.
        api_failed = False
        api_rows_seen = 0
        page_limit = max(1, min(int(max_pages or 500), 500))
        api_window_diagnostics: list[dict[str, Any]] = []

        def unpack_bse_table(payload: Any) -> tuple[list[Any], int | None, list[str]]:
            """Normalize BSE's Table/Table1 JSON shape, including ASP.NET wrappers."""
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    return [], None, []
            if isinstance(payload, list):
                return payload, None, []
            if not isinstance(payload, dict):
                return [], None, []
            keys = sorted(str(key) for key in payload.keys())[:30]
            lowered = {str(key).casefold(): value for key, value in payload.items()}
            table = lowered.get("table") or lowered.get("data") or lowered.get("rows")
            meta = lowered.get("table1") or lowered.get("table_1")
            if table is None and lowered.get("d") is not None:
                nested_rows, nested_total, nested_keys = unpack_bse_table(lowered.get("d"))
                return nested_rows, nested_total, keys + nested_keys[:10]
            if isinstance(table, dict):
                nested = {str(key).casefold(): value for key, value in table.items()}
                table = nested.get("table") or nested.get("rows") or nested.get("data") or []
            if isinstance(table, str):
                try:
                    table = json.loads(table)
                except (TypeError, ValueError):
                    table = []
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (TypeError, ValueError):
                    meta = []
            total: int | None = None
            if isinstance(meta, list) and meta and isinstance(meta[0], dict):
                meta_row = {str(key).casefold(): value for key, value in meta[0].items()}
                try:
                    value = int(str(meta_row.get("rowcnt") or "0").replace(",", "").strip())
                    total = value if value > 0 else None
                except (TypeError, ValueError):
                    total = None
            return (table if isinstance(table, list) else []), total, keys

        def consume_bse_rows(rows: list[Any]) -> None:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                lowered = {re.sub(r"[^a-z0-9]", "", str(k).casefold()): v for k, v in row.items()}

                def val(*keys: str) -> str:
                    for key in keys:
                        value = lowered.get(re.sub(r"[^a-z0-9]", "", key.casefold()))
                        if value is not None and str(value).strip():
                            return " ".join(str(value).split()).strip()
                    return ""

                raw_date = val("NEWS_DT", "DT_TM", "News_submission_dt", "DissemDT")
                published = None
                for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y%m%d", "%d-%m-%Y %H:%M:%S"):
                    try:
                        published = datetime.strptime(raw_date[:19], fmt).replace(tzinfo=ist).astimezone(UTC)
                        break
                    except ValueError:
                        continue
                if published is None:
                    try:
                        parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                        published = (parsed if parsed.tzinfo else parsed.replace(tzinfo=ist)).astimezone(UTC)
                    except ValueError:
                        continue
                if not start <= published.astimezone(ist).date() <= end:
                    continue
                title = val("HEADLINE", "NEWSSUB", "SUBJECT", "ANNOUNCEMENT_TYPE")
                code = val("SCRIP_CD", "SCRIPCODE", "SCRIP_CODE")
                company = val("SLONGNAME", "SCRIP_NAME", "COMPANY_NAME", "SCRIP_NAME_LONG") or code
                if not title or not (code or company):
                    continue
                news_id = val("NEWSID", "NEWS_ID", "ANN_ID", "ID")
                # In BSE's documented response, NSURL is the issuer's BSE quote
                # page, while ATTACHMENTNAME is the actual PDF filename.
                raw_attachment = val("ATTACHMENTNAME", "ATTACHMENT", "PDFURL", "NSURL")
                attachment = None
                if raw_attachment.startswith(("https://", "http://")):
                    attachment = _bse_pdf_url(raw_attachment)
                elif raw_attachment and ".." not in raw_attachment.split("/"):
                    if raw_attachment.startswith("/"):
                        attachment_candidate = urlsplit(announcements_url)._replace(path=raw_attachment).geturl()
                    else:
                        attachment_candidate = attachment_base + quote(raw_attachment.lstrip("/\\"), safe="/%._-()")
                    attachment = _bse_pdf_url(attachment_candidate)
                if not attachment:
                    attachment = _bse_pdf_url(val("NSURL"))
                raw_stock_url = val("NSURL")
                stock_page_url = None
                if raw_stock_url.startswith(("https://", "http://")):
                    parsed_stock = urlsplit(raw_stock_url)
                    stock_host = (parsed_stock.hostname or "").casefold()
                    if (parsed_stock.scheme in {"http", "https"}
                            and (stock_host == "bseindia.com" or stock_host.endswith(".bseindia.com"))
                            and "/stock-share-price/" in parsed_stock.path.casefold()):
                        stock_page_url = raw_stock_url
                identity = news_id or hashlib.sha256(f"{code}|{published.isoformat()}|{title}".encode()).hexdigest()[:32]
                # PDF first; otherwise preserve the precise BSE issuer URL. Generic
                # announcements-list URLs are not a user-facing fallback.
                url = attachment or stock_page_url or announcements_url
                items[identity] = {
                    "title": title[:1000], "url": url, "attachment_url": attachment,
                    "bse_stock_url": stock_page_url,
                    "published_at": published, "source": "BSE Corporate Announcements",
                    "source_key": "bse_india", "source_item_id": f"BSE:{identity}"[:255],
                    "company_name": company[:255], "bse_code": code[:40],
                    "bse_symbol": code_symbols.get(code, ""),
                    "announcement_subject": val("NEWSSUB", "SUBJECT", "HEADLINE")[:1000] or title[:1000],
                    "description": val("MORE", "DETAILS", "DESCRIPTION", "HEADLINE")[:10000],
                    "official_bse_announcement": True, "transport": "official-bse-api",
                }

        query_windows: list[tuple[date, date, str]] = [(start, end, "date-range")]
        window_index = 0
        try:
            while window_index < len(query_windows):
                window_start, window_end, window_kind = query_windows[window_index]
                window_rows_seen = 0
                window_total: int | None = None
                window_had_rows = False
                window_pages = 0
                window_expected_pages: int | None = None
                for page in range(1, page_limit + 1):
                    response = await client.get(
                        api_url,
                        params={
                            **params_base,
                            "strPrevDate": window_start.strftime("%Y%m%d"),
                            "strToDate": window_end.strftime("%Y%m%d"),
                            "pageno": page,
                        },
                        headers={"Host": "api.bseindia.com"},
                    )
                    response.raise_for_status()
                    diagnostics["api_http_statuses"].append(int(response.status_code))
                    rows, reported_total, response_keys = unpack_bse_table(response.json())
                    if response_keys:
                        diagnostics["api_response_keys"] = response_keys
                    if reported_total is not None:
                        window_total = reported_total
                        diagnostics["api_total_rows_reported"] = reported_total
                        if rows and window_expected_pages is None:
                            window_expected_pages = math.ceil(reported_total / len(rows))
                            if diagnostics.get("api_expected_pages") is None:
                                diagnostics["api_expected_pages"] = window_expected_pages
                    diagnostics["api_page_row_counts"].append(len(rows))
                    if not rows:
                        if window_total and window_rows_seen < window_total:
                            logger.warning(
                                "NEWSALERT_BSE_API_EMPTY_PAGE_BEFORE_TOTAL window=%s page=%s rows_seen=%s rowcnt=%s keys=%s",
                                window_kind, page, window_rows_seen, window_total, response_keys,
                            )
                        break
                    diagnostics["api_pages"] += 1
                    diagnostics["api_used"] = True
                    window_had_rows = True
                    window_pages += 1
                    window_rows_seen += len(rows)
                    api_rows_seen += len(rows)
                    diagnostics["api_rows_seen"] = api_rows_seen
                    consume_bse_rows(rows)
                    if window_total is not None:
                        if window_rows_seen >= window_total:
                            break
                    elif len(rows) < 10:
                        break
                    await asyncio.sleep(0.1)

                window_incomplete = window_total is not None and window_rows_seen < window_total
                api_window_diagnostics.append({
                    "window": f"{window_start.isoformat()}..{window_end.isoformat()}",
                    "kind": window_kind, "rows_seen": window_rows_seen,
                    "rowcnt": window_total, "pages": window_pages,
                    "expected_pages": window_expected_pages,
                    "incomplete": bool(window_incomplete),
                })
                if window_incomplete:
                    shortfall_key = "api_range_rows_shortfall" if window_kind == "date-range" else "api_daily_rows_shortfall"
                    diagnostics[shortfall_key] = int(diagnostics.get(shortfall_key) or 0) + (window_total - window_rows_seen)
                # If the 7-day request returns an empty or truncated Table, retry
                # each date independently. This recovers BSE API deployments that
                # answer HTTP 200 but do not honor a multi-day filter consistently.
                if window_index == 0 and window_kind == "date-range" and (not window_had_rows or window_incomplete) and window_start < window_end:
                    diagnostics["api_daily_fallback_used"] = True
                    query_windows.extend(
                        (single_day, single_day, "date-scoped-fallback")
                        for offset in range((window_end - window_start).days + 1)
                        for single_day in [window_start + timedelta(days=offset)]
                    )
                window_index += 1
            diagnostics["api_window_diagnostics"] = api_window_diagnostics
            diagnostics["api_rows_seen"] = api_rows_seen
            if diagnostics.get("api_daily_fallback_used"):
                fallback_windows = [row for row in api_window_diagnostics if row.get("kind") == "date-scoped-fallback"]
                diagnostics["api_pagination_incomplete"] = any(row.get("incomplete") for row in fallback_windows)
            else:
                diagnostics["api_pagination_incomplete"] = any(row.get("incomplete") for row in api_window_diagnostics)
        except Exception as exc:
            api_failed = True
            diagnostics["announcement_api_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
            logger.warning("NEWSALERT_BSE_API_FAILED error=%s", exc)

        # --- 3. RSS/XML fallback when the BSE JSON API is blocked or empty. ---
        if api_failed or not items:
            xml_errors: list[str] = []
            xml_feed_diagnostics: list[dict[str, Any]] = []
            for feed_url in (
                "https://www.bseindia.com/data/xml/announcements.xml",
                "https://www.bseindia.com/data/xml/announcements.aspx",
                "https://beta.bseindia.com/data/xml/announcements.xml",
            ):
                try:
                    response = await client.get(
                        feed_url,
                        headers={"Accept": "application/rss+xml, application/xml, text/xml, */*",
                                 "Referer": "https://www.bseindia.com/"},
                    )
                    response.raise_for_status()
                    root = ET.fromstring(response.text)
                    nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].casefold() in {"item", "entry"}]
                    feed_added = 0
                    date_matched = 0
                    for node in nodes:
                        fields: dict[str, str] = {}
                        for child in node.iter():
                            tag = re.sub(r"[^a-z0-9]", "", child.tag.rsplit("}", 1)[-1].casefold())
                            value = " ".join(str(child.text or "").split()).strip()
                            if tag == "link" and not value:
                                value = str(child.attrib.get("href") or child.attrib.get("url") or "").strip()
                            if tag in {"enclosure", "attachment"}:
                                value = str(child.attrib.get("url") or child.attrib.get("href") or value).strip()
                            if value:
                                fields.setdefault(tag, value)
                        title_raw = fields.get("title") or fields.get("newssub") or fields.get("subject") or ""
                        description = fields.get("description") or fields.get("summary") or fields.get("content") or ""
                        combined = f"{title_raw} {description}"
                        code = fields.get("scripcode") or fields.get("scripcd") or ""
                        if not code:
                            code_match = re.search(r"\((\d{5,7})\)", combined)
                            code = code_match.group(1) if code_match else ""
                        company = fields.get("companyname") or fields.get("scripname") or ""
                        if not company and code and title_raw:
                            company = re.split(r"\s*\(\d{5,7}\)", title_raw, maxsplit=1)[0].strip(" -–—:")
                        title = fields.get("subject") or fields.get("headline") or title_raw
                        if not title:
                            continue
                        raw_date = fields.get("pubdate") or fields.get("published") or fields.get("date") or fields.get("newstime") or ""
                        published = None
                        if raw_date:
                            try:
                                parsed = parsedate_to_datetime(raw_date)
                                published = (parsed if parsed.tzinfo else parsed.replace(tzinfo=ist)).astimezone(UTC)
                            except (TypeError, ValueError, OverflowError):
                                for fmt in ("%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                                    try:
                                        published = datetime.strptime(raw_date[:19], fmt).replace(tzinfo=ist).astimezone(UTC)
                                        break
                                    except ValueError:
                                        continue
                        if published is None or not start <= published.astimezone(ist).date() <= end:
                            continue
                        date_matched += 1
                        detail_link = fields.get("link") or fields.get("guid") or ""
                        if not detail_link.startswith(("http://", "https://")):
                            detail_link = announcements_url
                        else:
                            parsed_link = urlsplit(detail_link)
                            if not (parsed_link.hostname or "").casefold().endswith("bseindia.com"):
                                detail_link = announcements_url
                        raw_attachment = fields.get("attachment") or fields.get("attachmenturl") or fields.get("pdf") or fields.get("enclosure") or ""
                        attachment_candidate = raw_attachment.strip()
                        if attachment_candidate and not attachment_candidate.startswith(("http://", "https://", "//")):
                            if attachment_candidate.startswith("/"):
                                attachment_candidate = urljoin("https://www.bseindia.com", attachment_candidate)
                            elif ".." not in attachment_candidate.split("/"):
                                attachment_candidate = attachment_base + quote(attachment_candidate.lstrip("/\\"), safe="/%._-()")
                        attachment = _bse_pdf_url(attachment_candidate) or _bse_pdf_url(detail_link)
                        detail_parts = urlsplit(detail_link)
                        detail_host = (detail_parts.hostname or "").casefold()
                        stock_page_url = detail_link if (
                            detail_parts.scheme in {"http", "https"}
                            and (detail_host == "bseindia.com" or detail_host.endswith(".bseindia.com"))
                            and "/stock-share-price/" in detail_parts.path.casefold()
                        ) else None
                        identity = fields.get("guid") or fields.get("id") or hashlib.sha256(
                            f"{code}|{published.isoformat()}|{title}".encode("utf-8", errors="replace")
                        ).hexdigest()[:32]
                        if identity in items:
                            continue
                        items[identity] = {
                            "title": title[:1000], "url": attachment or stock_page_url or detail_link,
                            "attachment_url": attachment, "bse_stock_url": stock_page_url,
                            "published_at": published, "source": "BSE Corporate Announcements",
                            "source_key": "bse_india", "source_item_id": f"BSE:{identity}"[:255],
                            "company_name": company[:255], "bse_code": code[:40],
                            "bse_symbol": code_symbols.get(code, ""),
                            "announcement_subject": title[:1000], "description": description[:10000],
                            "official_bse_announcement": True, "transport": "official-bse-xml",
                        }
                        feed_added += 1
                    xml_feed_diagnostics.append({
                        "url": feed_url, "http_status": int(response.status_code),
                        "nodes": len(nodes), "date_matched": date_matched, "added": feed_added,
                    })
                    if feed_added:
                        diagnostics["xml_fallback_used"] = True
                        diagnostics["xml_feed_url"] = feed_url
                        break
                except Exception as exc:
                    xml_errors.append(f"{feed_url}: {type(exc).__name__}: {str(exc)[:140]}")
            diagnostics["xml_feed_diagnostics"] = xml_feed_diagnostics
            if not items and xml_errors:
                errors.append("BSE announcement JSON/XML sources unavailable: " + " | ".join(xml_errors[:2]))

    # Add automatically discovered codes to NSE announcement rows as well, so the
    # canonical pipeline can persist them with the alert metadata and routes can
    # still resolve older items from the Company table.
    issuer_codes_by_name: dict[str, str] = {}
    issuer_codes_by_isin: dict[str, str] = {}
    for issuer in issuer_rows:
        code = str(issuer.get("bse_code") or "").strip()
        if not code:
            continue
        for name in (issuer.get("company_name"), *(issuer.get("aliases") or [])):
            for key in _bse_company_name_keys(name):
                issuer_codes_by_name[key] = code
        if issuer.get("isin"):
            issuer_codes_by_isin[str(issuer["isin"]).strip().upper()] = code
    diagnostics.update({
        "pages_limit": max_pages,
        "fetched": len(items),
        "lookback_days": days,
        "coverage_company_bse_codes": {
            str(row["company_id"]): str(row["bse_code"])
            for row in issuer_rows if row.get("company_id") and row.get("bse_code")
        },
        "coverage_company_bse_symbols": {
            str(row["company_id"]): str(row["bse_symbol"])
            for row in issuer_rows if row.get("company_id") and row.get("bse_symbol")
        },
        "company_code_matches": len(issuer_codes_by_name),
    })
    return CollectorResult(sorted(items.values(), key=lambda row: row["published_at"], reverse=True), errors, diagnostics)

async def collect_nse_items(*, limit: int = 10000, companies: list[CoverageCompany] | None = None, db=None) -> CollectorResult:
    """Collect NSE rows in parallel; resolve BSE only for missing-PDF/board rows.

    The NSE source must not invoke BSE's announcement crawler. BSE is consulted
    only as an issuer-page fallback for NSE corporate announcements without a
    direct PDF and for NSE board-meeting notices (whose attachment is XML/XBRL).
    """
    settings = get_settings()
    if not settings.newsalert_nse_enabled:
        logger.info("NEWSALERT_NSE_DISABLED reason=settings")
        return CollectorResult([], [], {"api_used": False, "enabled": False})

    try:
        collector = FinancialNewsCollector()
        announcement_task = asyncio.create_task(
            collector.collect_nse_announcements(days=7, limit=max(10000, int(limit or 10000)))
        )
        filing_task = asyncio.create_task(
            collect_nse_filing_items(days=7, limit=max(500, min(int(limit or 10000), 5000)))
        )
        announcement_result, filing_result = await asyncio.gather(
            announcement_task, filing_task, return_exceptions=True
        )

        if isinstance(announcement_result, BaseException):
            result = CollectorResult([], [
                f"NSE announcements: {type(announcement_result).__name__}: {str(announcement_result)[:220]}"
            ], {"api_used": False})
        else:
            result = announcement_result
        items = [dict(item) for item in result.items]
        errors = [str(error) for error in result.errors]

        if isinstance(filing_result, BaseException):
            filing_items: list[dict[str, Any]] = []
            filing_errors = [
                f"NSE filings: {type(filing_result).__name__}: {str(filing_result)[:220]}"
            ]
            filing_diagnostics: dict[str, Any] = {"parallel_collection_failed": True}
        else:
            filing_items, filing_errors, filing_diagnostics = filing_result

        all_nse_rows = [*items, *filing_items]
        fallback_candidates = [row for row in all_nse_rows if _needs_bse_issuer_fallback(row)]

        # Build issuer indexes only when an NSE row actually needs a BSE fallback.
        # The DB is read once; records with a valid NSE PDF never cause BSE lookup.
        issuer_registry: list[dict[str, Any]] = []
        if fallback_candidates and db is not None:
            registry_rows = (await db.execute(
                select(Company.id, Company.legal_name, Company.nse_symbol, Company.isin, Company.aliases, Company.bse_code)
                .join(TargetCompany, TargetCompany.company_id == Company.id)
                .where(
                    TargetCompany.active.is_(True),
                    TargetCompany.approval_status == ApprovalStatus.APPROVED,
                )
            )).all()
            for company_id, legal_name, nse_symbol, isin, aliases, bse_code in registry_rows:
                alias_values = aliases if isinstance(aliases, (list, tuple, set)) else []
                issuer_registry.append({
                    "company_name": str(legal_name or ""), "company_id": int(company_id),
                    "nse_symbol": str(nse_symbol or "").strip().upper(),
                    "isin": str(isin or "").strip().upper(),
                    "aliases": [str(value).strip() for value in alias_values if str(value).strip()],
                    "bse_code": str(bse_code or "").strip() or None,
                })
        elif fallback_candidates:
            issuer_registry = [
                {"company_name": company.name, "company_id": company.company_id,
                 "nse_symbol": str(company.nse_symbol or "").strip().upper(),
                 "isin": str(getattr(company, "isin", "") or "").strip().upper(),
                 "aliases": list(company.aliases or ()),
                 "bse_code": str(company.bse_code or "").strip() or None}
                for company in (companies or [])
            ]

        # Conservative DB-company resolution: exact ISIN, then exact NSE ticker,
        # then a unique normalized legal-name/alias. Never fuzzy-match a scrip code.
        by_isin: dict[str, list[dict[str, Any]]] = {}
        by_nse_symbol: dict[str, list[dict[str, Any]]] = {}
        by_name: dict[str, list[dict[str, Any]]] = {}
        for issuer in issuer_registry:
            if issuer.get("isin"):
                by_isin.setdefault(str(issuer["isin"]).upper(), []).append(issuer)
            if issuer.get("nse_symbol"):
                by_nse_symbol.setdefault(str(issuer["nse_symbol"]).casefold(), []).append(issuer)
            for name in (issuer.get("company_name"), *(issuer.get("aliases") or [])):
                for key in _bse_company_name_keys(name):
                    by_name.setdefault(key, []).append(issuer)

        unresolved_groups: dict[str, list[dict[str, Any]]] = {}
        for row in fallback_candidates:
            match: dict[str, Any] | None = None
            isin_key = str(row.get("isin") or "").strip().upper()
            symbol_key = str(row.get("nse_symbol") or row.get("symbol") or "").strip().casefold()
            if isin_key and len(by_isin.get(isin_key, [])) == 1:
                match = by_isin[isin_key][0]
            elif symbol_key and len(by_nse_symbol.get(symbol_key, [])) == 1:
                match = by_nse_symbol[symbol_key][0]
            else:
                name_matches: dict[int, dict[str, Any]] = {}
                for key in _bse_company_name_keys(row.get("company_name")):
                    for candidate in by_name.get(key, []):
                        name_matches[int(candidate["company_id"])] = candidate
                if len(name_matches) == 1:
                    match = next(iter(name_matches.values()))

            if match:
                row["company_id"] = int(match["company_id"])
                row["aliases"] = list(match.get("aliases") or [])
                if not str(row.get("company_name") or "").strip():
                    row["company_name"] = match.get("company_name") or ""
                if not row.get("bse_code") and match.get("bse_code"):
                    row["bse_code"] = str(match["bse_code"]).strip()

            # A validated BSE scrip code is sufficient for a company-page fallback.
            # The coverage workbook is authoritative; do not fetch the exchange-wide
            # master merely to discover a ticker slug. Derive the slug from issuer name.
            if row.get("bse_code"):
                row["bse_stock_url"] = row.get("bse_stock_url") or _build_bse_stock_page_url(
                    row.get("company_name"), row.get("bse_code"), row.get("bse_symbol")
                )
            else:
                if row.get("company_id"):
                    group_key = f"company:{int(row['company_id'])}"
                elif isin_key:
                    group_key = f"isin:{isin_key}"
                elif symbol_key:
                    group_key = f"nse-symbol:{symbol_key}"
                else:
                    name_keys = _bse_company_name_keys(row.get("company_name"))
                    group_key = f"issuer-name:{name_keys[0]}" if name_keys else f"row:{len(unresolved_groups)}"
                unresolved_groups.setdefault(group_key, []).append(row)

        unresolved_for_master = [rows[0] for rows in unresolved_groups.values()]
        logger.info(
            "NEWSALERT_NSE_BSE_FALLBACK_CANDIDATES total_nse_rows=%s fallback_rows=%s unique_issuers=%s",
            len(all_nse_rows), len(fallback_candidates), len(unresolved_for_master),
        )
        bse_result = CollectorResult([], [], {
            "lookup_triggered": False,
            "lookup_reason": "no_missing_pdf_or_board_meeting_issuers",
            "announcement_collection_skipped": True,
            "scrip_master_used": False,
            "scrip_master_matches": 0,
            "fetched": 0,
        })
        # No BSE network lookup is performed for NSE fallback routing. BSE codes
        # come from the BSE Code column in Coverage_List.xlsx and are synchronized
        # into Company.bse_code by the existing coverage-workbook sync. When a
        # company has no BSE code, the NewsAlert response routes it to its NSE
        # quote page using its NSE symbol; never block NSE ingestion on BSE
        # scrip-master downloads/matching.
        missing_coverage_codes = len(unresolved_for_master)

        bse_result = CollectorResult([], [], {
            "lookup_triggered": False,
            "lookup_reason": "coverage_workbook_only_no_bse_scraping",
            "announcement_collection_skipped": True,
            "scrip_master_used": False,
            "scrip_master_matches": 0,
            "fetched": 0,
            "missing_coverage_bse_codes": missing_coverage_codes,
        })

        # Do not append BSE announcements here: --source nse remains NSE-only.
        errors.extend(str(error) for error in bse_result.errors)
        errors.extend(str(error) for error in filing_errors)
        transports = sorted({str(item.get("transport") or "unknown") for item in all_nse_rows})
        attempted = list(getattr(result, "attempted_urls", []) or [])
        methods_attempted = sorted({
            method for method, prefix in (
                ("rss", "rss:"), ("api", "api:"), ("toolkit", "toolkit:"), ("browser", "browser:")
            ) if any(str(url).startswith(prefix) for url in attempted)
        })
        api_used = any(
            "official-nse-api" in transport.casefold()
            or transport.casefold() == "nse-http-api"
            or transport.casefold().startswith("nse-client-api")
            for transport in transports
        )
        toolkit_used = any("toolkit" in transport.casefold() or "jugaad" in transport.casefold() for transport in transports)
        browser_used = any("browser" in transport.casefold() or "playwright" in transport.casefold() for transport in transports)
        browser_api_payloads_used = any("playwright-nse-api-response" in transport.casefold() for transport in transports)
        rss_used = any("rss" in transport.casefold() for transport in transports)

        bse_diagnostics = dict(bse_result.diagnostics or {})
        bse_diagnostics.update({
            "lookup_triggered": False,
            "lookup_reason": "coverage_workbook_only_no_bse_scraping",
            "fallback_candidates": len(fallback_candidates),
            "unique_unresolved_issuers": len(unresolved_groups),
            "missing_coverage_bse_codes": len(unresolved_for_master),
            "unresolved_candidates_sent_to_master": 0,
            "announcement_collection_skipped": True,
        })
        diagnostics = {
            "feed_urls_attempted": attempted,
            "methods_attempted": methods_attempted,
            "transports_used": transports,
            "rss_used": rss_used,
            "api_used": api_used,
            "browser_api_payloads_used": browser_api_payloads_used,
            "toolkit_used": toolkit_used,
            "browser_used": browser_used,
            "lookback_days": 7,
            "limit": limit,
            "parallel_collectors": ["nse_announcements", "nse_filings"],
            "filings": filing_diagnostics,
            "bse": bse_diagnostics,
        }
        logger.info(
            "NEWSALERT_NSE_FALLBACK_PIPELINE_RESULT fetched=%s nse_rows=%s fallback_candidates=%s bse_lookup=%s methods=%s errors=%s diagnostics=%s",
            len(all_nse_rows), len(items), len(fallback_candidates), bool(unresolved_for_master),
            methods_attempted, len(errors), diagnostics,
        )
        return CollectorResult(all_nse_rows, errors, diagnostics)
    except Exception as exc:
        message = f"NSE announcement fallback waterfall: {type(exc).__name__}: {str(exc)[:350]}"
        logger.exception("NEWSALERT_NSE_FALLBACK_PIPELINE_FAILED")
        return CollectorResult([], [message], {"api_used": False, "stage": "nse-fallback-waterfall"})

async def collect_x_items(accounts: list[str], *, days: int = 7) -> CollectorResult:
    """Collect recent public posts from every configured account with diagnostics."""
    settings = get_settings()
    unique_accounts = list(dict.fromkeys(str(value).strip().lstrip("@") for value in accounts if str(value).strip()))
    if not unique_accounts:
        return CollectorResult([], ["NEWSALERT_X_ACCOUNTS/X_ALLOWED_ACCOUNTS is not configured"],
                               {"accounts_checked": 0, "account_results": []})

    template = str(settings.newsalert_x_feed_url_template or os.getenv("NEWSALERT_X_FEED_URL_TEMPLATE", "")).strip()
    configured_feed_urls = _split_csv(
        os.getenv("NEWSALERT_X_FEED_URLS", "")
        or os.getenv("X_POST_FEED_URLS", "")
        or os.getenv("X_FEED_URLS", "")
        or getattr(settings, "x_post_feed_urls", "")
    )[:30]
    collector = XPostsCollector(allowed_accounts=unique_accounts, timeout_seconds=settings.news_source_timeout_seconds)
    if template or configured_feed_urls:
        feed_urls: list[str] = list(configured_feed_urls)
        if template:
            for handle in unique_accounts:
                url = template
                for token in ("{handle}", "{username}", "{account}", "{}"):
                    url = url.replace(token, handle)
                feed_urls.append(url)
        try:
            items, problems = await collector.collect_from_feeds(feed_urls, days=2, max_items=100)
            diagnostics = dict(collector.last_diagnostics or {})
            # Keep the explicit account allowlist, and never relabel a different author.
            valid: list[dict[str, Any]] = []
            for item in items:
                actual = str(item.get("author_handle") or item.get("x_account") or "").lstrip("@").casefold()
                if actual not in {handle.casefold() for handle in unique_accounts}:
                    continue
                item["author_handle"] = actual
                item["x_account"] = actual
                item["source"] = f"X / @{actual}"
                item["content_type"] = "POST"
                item["is_x_post"] = True
                valid.append(item)
            diagnostics.update({"accounts_checked": len(unique_accounts), "posts_found": len(valid), "api_used": False})
            return CollectorResult(valid, list(problems or []), diagnostics)
        except Exception as exc:
            diagnostics = dict(collector.last_diagnostics or {})
            return CollectorResult([], [f"X collection waterfall: {type(exc).__name__}: {str(exc)[:500]}"], diagnostics)

    try:
        rows = await collector.collect_allowed_accounts(days=2, max_items_per_account=250)
        diagnostics = dict(collector.last_diagnostics or {})
        return CollectorResult(rows, list(diagnostics.get("errors") or []), diagnostics)
    except Exception as exc:
        diagnostics = dict(collector.last_diagnostics or {})
        error = f"X collection waterfall: {type(exc).__name__}: {str(exc)[:500]}"
        prior_errors = [str(value) for value in diagnostics.get("errors") or []]
        errors = list(dict.fromkeys([*prior_errors, error]))
        return CollectorResult([], errors, diagnostics)


def _valid_nse_pdf_attachment(value: Any, *, attachment_type: Any = None) -> bool:
    """Validate NSE PDF links, including explicit PDF download handlers without .pdf."""
    candidate = str(value or "").strip()
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not (host == "nseindia.com" or host.endswith(".nseindia.com")):
        return False
    path = parsed.path.rstrip("/").casefold()
    if path.endswith(".pdf"):
        return True
    if str(attachment_type or "").strip().upper() != "PDF":
        return False
    # Extensionless NSE attachment/download handlers are valid only when the
    # source explicitly identified the Attachment column as PDF. Never permit
    # listing pages, XML/XBRL, HTML, CSV or spreadsheet endpoints here.
    return not (
        "corporate-filings-announcements" in path
        or path.endswith((".xml", ".xbrl", ".html", ".htm", ".csv", ".xlsx"))
        or "/xbrl/" in path
    )


def _requires_bse_fallback_metadata(raw: dict[str, Any]) -> bool:
    feed = str(raw.get("nse_filing_feed") or "").strip().casefold()
    if feed == "board_meetings":
        return True
    return not _valid_nse_pdf_attachment(
        raw.get("attachment_url"), attachment_type=raw.get("attachment_type")
    )


async def triage_item(item: NormalizedItem, *, settings=None) -> dict[str, Any]:
    """Assess an item using local deterministic rules only; no external AI calls."""
    del settings  # Kept for backwards compatibility with existing callers.
    return _fallback_triage(item)


async def score_item(item: NormalizedItem, *, settings=None) -> dict[str, Any]:
    """Score an item using local deterministic rules only; no external AI calls."""
    del settings  # Kept for backwards compatibility with existing callers.
    return _fallback_score(item)


def _normalize_raw_item(raw: dict[str, Any]) -> NormalizedItem | None:
    url = canonicalize_url(raw.get("url") or raw.get("link"))
    published = _aware_datetime(raw.get("published_at") or raw.get("published") or raw.get("date"))
    if not url or published is None:
        return None
    raw_type = str(raw.get("content_type") or "NEWS").upper()
    is_x = bool(raw.get("is_x_post")) or raw_type == "POST" or str(raw.get("source") or "").casefold().startswith("x /")
    if is_x:
        content_type = "POST"
        post_text = _x_post_text(raw)
        # A profile name is not a post. Only accept X rows when a body can be
        # extracted; this prevents another bad author-name headline entering DB.
        if not post_text:
            return None
        title = _x_post_headline(post_text)
        raw["title"] = title
        raw["summary"] = post_text[:10000]
        raw["raw_content"] = post_text[:10000]
    else:
        title = " ".join(str(raw.get("title") or raw.get("headline") or "").split()).strip()
        if bool(raw.get("is_youtube")) or raw_type == "INTERVIEW" or "youtube" in str(raw.get("source") or "").casefold():
            content_type = "INTERVIEW"
        else:
            content_type = "NEWS"
    if not title:
        return None
    source = str(raw.get("source") or "News source").strip()[:160]
    description = (
        post_text if is_x else str(raw.get("description") or raw.get("summary") or raw.get("raw_content") or "").strip()
    )
    item_id = (
        raw.get("source_item_id") or raw.get("video_id") or raw.get("announcement_id")
        or raw.get("post_id") or raw.get("tweet_id") or raw.get("status_id")
        or raw.get("guid") or raw.get("id")
    )
    if is_x and not item_id:
        status_match = re.search(r"/(?:status|statuses)/(\d+)(?:/|$)", url, re.I)
        item_id = status_match.group(1) if status_match else None
    author = raw.get("author_handle") or raw.get("author") or raw.get("channel_title")
    return NormalizedItem(title=title[:1000], url=url, published_at=published, source=source,
                          description=description[:10000], content_type=content_type,
                          source_item_id=str(item_id)[:255] if item_id else None,
                          author=str(author)[:255] if author else None, raw=dict(raw))


def _noise_and_age_allowed(item: NormalizedItem, *, now: datetime) -> bool:
    if not item_age_allowed(item, now=now):
        return False
    # Monitored X accounts are an explicit all-post feed. Do not discard their
    # posts because a generic finance/noise keyword filter thinks they are low
    # relevance; the user explicitly wants every collected post stored.
    if item.raw.get("alert_all_posts"):
        return True
    # Other source types retain deterministic noise filtering.
    return not is_noise(item.title, item.description[:1000])


_X_RECOMMENDATION_NOISE = re.compile(
    r"\b(?:multibagger|sure.?shot|stock tip|stock recommendation|buy this stock|"
    r"sell this stock|buy now|sell now|price target|target price|technical levels|"
    r"chart setup|intraday call|options call|call or put|upper circuit stock)\b", re.I,
)
_X_BUSINESS_CONTEXT = re.compile(
    r"\b(?:earnings?|quarter(?:ly)? results?|financial results?|revenue|net profit|"
    r"operating profit|ebitda|margins?|cash flow|balance sheet|debt|fundrais(?:e|ing)|"
    r"capex|capital expenditure|order book|order win|contract|tender|capacity|factory|"
    r"manufactur(?:e|ing)|production|sales|demand|pricing|guidance|outlook|strategy|"
    r"management|promoter|board|governance|dividend|buyback|acquisition|merger|stake|"
    r"investment|expansion|market share|industry|sector|econom(?:y|ic)|gdp|inflation|"
    r"interest rates?|monetary policy|fiscal policy|rbi|sebi|regulation|regulatory|"
    r"export(?:s)?|import(?:s)?|tariff|supply chain|commodity|crude oil|banking|insurance|"
    r"credit growth|npa|asset quality|ipo|qip|listing|default|litigation|court|policy|"
    r"infrastructure|electric vehicle|ev sales|renewable|semiconductor|real estate|"
    r"business update|corporate action|economic data|economic growth)\b", re.I,
)


def _business_relevant_x_item(item: NormalizedItem) -> bool:
    """Keep fundamental business/social context, but reject trading-tip posts.

    A matched issuer is useful context but is not required. Companyless posts need
    an explicit fundamental, industry, regulatory, or macroeconomic signal.
    """
    text = f"{item.title} {item.description[:5000]}"
    if is_noise(item.title, item.description[:1000]) or _X_RECOMMENDATION_NOISE.search(text):
        return False
    return bool(item.company) or bool(_X_BUSINESS_CONTEXT.search(text))


def _item_text_for_matching(item: NormalizedItem) -> str:
    bits = [item.title, item.description]
    symbol = str(item.raw.get("nse_symbol") or "").strip()
    company_name = str(item.raw.get("company_name") or "").strip()
    if symbol:
        bits.append(symbol)
    if company_name:
        bits.append(company_name)
    return "\n".join(bits)


def _row_tokens(item: NormalizedItem) -> set[str]:
    return _tokens(item.title)


def _same_company_story(item: NormalizedItem, existing_rows: Iterable[Any], accepted: Iterable[NormalizedItem], *, window: timedelta = timedelta(hours=48)) -> bool:
    for row in existing_rows:
        if item.company is None or getattr(row, "company_id", None) != item.company.company_id:
            continue
        if str(getattr(row, "content_type", "NEWS") or "NEWS").upper() == "POST":
            continue
        published = _aware_datetime(getattr(row, "published_at", None))
        if published is None or abs((item.published_at - published).total_seconds()) > window.total_seconds():
            continue
        if is_same_story(item.title, getattr(row, "headline", ""), threshold=0.6):
            return True
    for other in accepted:
        if item.company and other.company and item.company.company_id == other.company.company_id:
            if abs((item.published_at - other.published_at).total_seconds()) <= window.total_seconds() and is_same_story(item.title, other.title, threshold=0.6):
                return True
    return False


def _to_db_values(item: NormalizedItem) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "pipeline": "newsalert-v2",
        "collector": str(item.raw.get("_newsalert_collector") or "unknown"),
        "content_type": item.content_type,
        "source_item_id": item.source_item_id,
    }
    if item.company:
        metadata.update({"track_mode": item.company.track_mode, "coverage_target_id": item.company.target_id})
    if item.raw.get("official_nse_announcement"):
        needs_bse_fallback = _requires_bse_fallback_metadata(item.raw)
        metadata.update({
            "official_nse_announcement": True,
            "source_key": str(item.raw.get("source_key") or "nse_india"),
            "nse_symbol": str(item.raw.get("nse_symbol") or item.raw.get("symbol") or "")[:80],
            "issuer_name_from_source": str(item.raw.get("company_name") or item.raw.get("companyName") or "")[:255],
            "announcement_subject": str(item.raw.get("announcement_subject") or item.raw.get("subject") or "")[:1000],
            "transport": str(item.raw.get("transport") or "official-nse")[:100],
            # Exchange PDF/XBRL document behind the row. The list API serves it as the
            # article link; `url` stays the stable identity used for deduplication.
            # Board meetings are always routed to the BSE issuer page. Do not save
            # their XML/XBRL attachment as a clickable document.
            "attachment_url": (None if str(item.raw.get("nse_filing_feed") or "").casefold() == "board_meetings"
                               else str(item.raw.get("attachment_url") or "")[:1500] or None),
            "attachment_type": (None if str(item.raw.get("nse_filing_feed") or "").casefold() == "board_meetings"
                                else str(item.raw.get("attachment_type") or ("PDF" if str(item.raw.get("attachment_url") or "").casefold().split("?", 1)[0].endswith(".pdf") else ""))[:20] or None),
            "source_url": str(item.raw.get("source_url") or "")[:1000] or None,
            "isin": str(item.raw.get("isin") or "")[:20] or None,
            "bse_code": (str(item.raw.get("bse_code") or (item.company.bse_code if item.company else "") or "")[:40] or None) if needs_bse_fallback else None,
            "bse_symbol": (str(item.raw.get("bse_symbol") or "")[:80] or None) if needs_bse_fallback else None,
            "bse_stock_url": (str(item.raw.get("bse_stock_url") or "")[:1500] or None) if needs_bse_fallback else None,
            "nse_filing_feed": str(item.raw.get("nse_filing_feed") or "")[:40] or None,
            "triage_bypassed": True,
            "scoring_bypassed": True,
        })
    if item.raw.get("official_bse_announcement"):
        metadata.update({
            "official_bse_announcement": True,
            "source_key": "bse_india",
            "bse_code": str(item.raw.get("bse_code") or (item.company.bse_code if item.company else "") or "")[:40] or None,
            "bse_symbol": str(item.raw.get("bse_symbol") or "")[:80] or None,
            "issuer_name_from_source": str(item.raw.get("company_name") or "")[:255],
            "announcement_subject": str(item.raw.get("announcement_subject") or item.title)[:1000],
            "attachment_url": str(item.raw.get("attachment_url") or "")[:1000] or None,
            "bse_stock_url": str(item.raw.get("bse_stock_url") or "")[:1500] or None,
            "transport": "official-bse-api",
            "triage_bypassed": True,
            "scoring_bypassed": True,
        })
    if item.is_x:
        post_text = _x_post_text(item.raw) or item.description or item.title
        metadata.update({"x_account": item.source_handle, "author_handle": item.source_handle,
                         "post_text": post_text[:10000], "raw_content": post_text[:10000],
                         "transport": item.raw.get("transport", "public-feed"), "triage_bypassed": True, "scoring_bypassed": True})
    else:
        metadata.update({"triage": item.triage or {}, "score": item.score or {},
                         "triage_model": (item.triage or {}).get("model"), "score_model": (item.score or {}).get("model")})
        if item.raw.get("stockscans_source_url") or str(item.raw.get("source_key") or "").casefold() == "stockscans_interviews":
            metadata.update({
                "source_key": "stockscans_interviews",
                "stockscans_source_url": str(item.raw.get("stockscans_source_url") or "https://www.stockscans.in/interview-scans")[:500],
                "stockscans_company_name": str(item.raw.get("stockscans_company_name") or item.raw.get("company_name") or "")[:255],
                "stockscans_headline": str(item.raw.get("stockscans_headline") or item.title)[:1000],
            })
        if item.content_type == "INTERVIEW":
            metadata.update({
                "channel_title": str(item.raw.get("channel_title") or item.author or "")[:255],
                "interview_companies": list(item.raw.get("interview_companies") or []),
                "company_inference": "coverage_aliases_or_title" if item.raw.get("display_company_name") else "unresolved",
                "transcript_status": str(item.raw.get("transcript_status") or "unknown"),
                "speaker_verification": item.raw.get("speaker_verification") or {},
                "transcript_excerpt": str(item.raw.get("transcript_text") or "")[:4000],
            })
    return {
        "company_id": item.company.company_id if item.company else None,
        "company_name": (
            item.company.name if item.company else str(
                item.raw.get("display_company_name")
                or item.raw.get("company_name")
                or item.raw.get("companyName")
                or item.raw.get("nse_symbol")
                or item.raw.get("symbol")
                or ""
            ).strip()
        )[:255],
        "nse_symbol": (
            item.company.nse_symbol if item.company and item.company.nse_symbol
            else (str(item.raw.get("nse_symbol") or item.raw.get("symbol") or "").strip().upper() or None)
        ),
        "headline": item.title[:1000],
        "url": item.canonical_url[:1500],
        "source": (f"X / @{item.source_handle}" if item.is_x and item.source_handle else item.source)[:160],
        "analyst": item.company.analyst if item.company else None,
        "content_type": item.content_type,
        "published_at": item.published_at,
        "author": item.author or (item.source_handle if item.is_x else None),
        "summary": item.description[:10000] or None,
        "relevance_score": item.score["score"] if item.score else None,
        "materiality": item.score["materiality"] if item.score else None,
        "relevance_rationale": (item.score or {}).get("rationale") or (item.triage or {}).get("rationale"),
        "source_item_id": item.source_item_id,
        "content_hash": _sha256(f"{_normalise(item.title)}|{_normalise(item.description)}|{item.published_at.isoformat()}"),
        "dedupe_key": item.dedupe_key,
        "metadata_json": metadata,
        "processing_status": "VISIBLE",
    }


async def load_approved_companies(db) -> list[CoverageCompany]:
    """Read the currently approved Coverage List as plain immutable records."""
    rows = (await db.execute(
        select(TargetCompany, Company, User)
        .join(Company, Company.id == TargetCompany.company_id)
        .outerjoin(User, User.id == TargetCompany.analyst_id)
        .where(TargetCompany.active.is_(True), TargetCompany.approval_status == ApprovalStatus.APPROVED)
        .order_by(Company.legal_name, TargetCompany.id)
    )).all()
    configured = _configured_terms(get_settings().newsalert_configured_match_terms)
    companies: dict[int, CoverageCompany] = {}
    for target, company, user in rows:
        key = _normalise(company.legal_name)
        if company.id in companies:
            previous = companies[company.id]
            companies[company.id] = CoverageCompany(
                company_id=previous.company_id, target_id=previous.target_id, name=previous.name,
                aliases=tuple(dict.fromkeys((*previous.aliases, *(company.aliases or [])))),
                nse_symbol=previous.nse_symbol, bse_code=previous.bse_code,
                track_mode=previous.track_mode, analyst=previous.analyst,
                extra_terms=tuple(dict.fromkeys((*previous.extra_terms, *configured.get(key, ())))),
            )
            continue
        alias_values = company.aliases if isinstance(company.aliases, (list, tuple, set)) else []
        companies[company.id] = CoverageCompany(
            company_id=int(company.id), target_id=int(target.id), name=str(company.legal_name),
            aliases=tuple(str(v).strip() for v in alias_values if str(v).strip()),
            nse_symbol=str(company.nse_symbol).strip() if company.nse_symbol else None,
            bse_code=str(company.bse_code).strip() if company.bse_code else None,
            track_mode=str(getattr(target.track_mode, "value", target.track_mode) or "OTHERS"),
            analyst=str(user.username) if user and user.is_active else None,
            extra_terms=tuple(configured.get(key, ())),
        )
    return list(companies.values())


async def _load_approved_company_snapshot() -> list[CoverageCompany]:
    """Load Coverage List on a short-lived reader session.

    The caller owns its session and transaction. A second, short-lived reader keeps
    an idle transaction from being held open across slow external collector calls,
    without committing or rolling back the caller's work.
    """
    async with get_sessionmaker()() as coverage_db:
        return await load_approved_companies(coverage_db)


async def _load_recent_rows(db, *, now: datetime) -> list[NewsAlertItem]:
    since = now - timedelta(days=7)
    rows = (await db.execute(select(NewsAlertItem).where(NewsAlertItem.published_at >= since))).scalars().all()
    return list(rows)


async def _prune(db, *, now: datetime, approved_company_ids: set[int]) -> dict[str, int]:
    """Prune expired rows without deleting valid companyless social content.

    Coverage membership is mandatory for ordinary NEWS rows, but it is not a
    visibility requirement for POST or INTERVIEW records. Those two types are
    explicitly supported by the NewsAlert desk even when no unique issuer can
    be associated with the content.
    """
    rows = (await db.execute(select(NewsAlertItem))).scalars().all()
    old_count = social_old_count = coverage_count = irrelevant_interview_count = 0
    unverified_youtube_count = 0
    for row in rows:
        published = _aware_datetime(row.published_at)
        content_type = str(getattr(row, "content_type", "NEWS") or "NEWS").upper()
        source_text = str(getattr(row, "source", "") or "")
        url_text = str(getattr(row, "url", "") or "")
        metadata = getattr(row, "metadata_json", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        source_key = str(metadata.get("source_key") or "").casefold()
        is_stockscan_interview = content_type == "INTERVIEW" and (
            "stockscans" in source_text.casefold()
            or "stockscan" in source_text.casefold()
            or source_key in {"stockscans_interviews", "stockscan"}
            or bool(metadata.get("stockscans_source_url"))
        )
        is_youtube_interview = (not is_stockscan_interview) and content_type == "INTERVIEW" and (
            "youtube" in source_text.casefold() or "youtube.com" in url_text.casefold() or "youtu.be" in url_text.casefold()
        )
        # StockScans is a separate discovery source. Its backfill intentionally
        # covers 10 days and must not inherit the regular YouTube 2-day gate or
        # transcript/speaker verification pruning.
        if is_youtube_interview and published and published >= now - timedelta(days=_youtube_lookback_days()):
            metadata = getattr(row, "metadata_json", None)
            metadata = metadata if isinstance(metadata, dict) else {}
            verification = metadata.get("speaker_verification")
            verified = isinstance(verification, dict) and verification.get("verified") is True
            transcript_status = metadata.get("transcript_status")
            metadata_speaker_verified = (
                isinstance(verification, dict)
                and verification.get("verified") is True
                and str(verification.get("reason") or "").startswith("metadata_explicit_")
            )
            if not verified or (transcript_status != "available" and not metadata_speaker_verified) or not getattr(row, "company_id", None):
                await db.delete(row)
                unverified_youtube_count += 1
                continue
            channel_title = metadata.get("channel_title") if isinstance(metadata, dict) else None
            if not channel_title and "·" in source_text:
                channel_title = source_text.split("·", 1)[1].strip()
            if not YouTubeNewsCollector._is_finance_interview(
                getattr(row, "headline", ""), getattr(row, "summary", ""), channel_title or source_text,
            ):
                await db.delete(row)
                irrelevant_interview_count += 1
                continue
        social_max_age = (
            timedelta(days=30) if is_stockscan_interview else
            timedelta(days=_youtube_lookback_days()) if is_youtube_interview else
            timedelta(hours=48)
        )
        if content_type in {"POST", "INTERVIEW"} and published and published < now - social_max_age:
            await db.delete(row)
            social_old_count += 1
            continue
        if published and published < now - timedelta(days=7) and not is_youtube_interview and not is_stockscan_interview:
            await db.delete(row)
            old_count += 1
            continue
        # Keep official exchange filing rows even if issuer matching is not possible.
        # They are intentionally source-scoped and must not be deleted by the
        # Coverage List cleanup on the same successful run.
        metadata = getattr(row, "metadata_json", None)
        source_is_exchange = any(token in source_text.casefold() for token in ("nse corporate announcements", "bse corporate announcements")) or (
            isinstance(metadata, dict)
            and (metadata.get("official_nse_announcement") is True or metadata.get("official_bse_announcement") is True or metadata.get("collector") == "nse")
        )
        if row.company_id is None and source_is_exchange:
            continue
        # X posts and recent finance interviews are channel/account-scoped feeds.
        # Company matching enriches these records but is not a visibility gate.
        keep_companyless_social = row.company_id is None and content_type in {"POST", "INTERVIEW"}
        if keep_companyless_social:
            continue
        if row.company_id is None or int(row.company_id) not in approved_company_ids:
            await db.delete(row)
            coverage_count += 1
    return {
        "deleted_older_than_7_days": old_count,
        "deleted_social_older_than_48_hours": social_old_count,
        "deleted_not_approved_coverage": coverage_count,
        "deleted_irrelevant_youtube_interviews": irrelevant_interview_count,
        "deleted_unverified_youtube_interviews": unverified_youtube_count,
    }


def _deterministic_assessment(item: NormalizedItem):
    """Local deterministic assessment used while external AI providers are disabled."""
    return assess_fundamental_news(
        item.title,
        item.description,
        item.source,
        content_type=item.content_type,
        portfolio_connection=item.company is not None,
        is_new=True,
    )


def _fallback_triage(item: NormalizedItem) -> dict[str, Any]:
    assessment = _deterministic_assessment(item)
    rationale = assessment.reason or ", ".join(assessment.matched_events) or "Deterministic fundamental-event rules"
    return {
        "relevant": bool(assessment.eligible),
        "rationale": rationale[:1200],
        "event_type": item.content_type,
        "model": "deterministic-fallback-v1",
    }


def _fallback_score(item: NormalizedItem) -> dict[str, Any]:
    assessment = _deterministic_assessment(item)
    return {
        "score": int(max(0, min(100, assessment.score))),
        "materiality": "HIGH" if assessment.is_material else ("MEDIUM" if assessment.eligible else "LOW"),
        "rationale": (assessment.reason or ", ".join(assessment.matched_events) or "Deterministic fundamental-event rules")[:1200],
        "model": "deterministic-fallback-v1",
    }


async def _try_acquire_cycle_lock(db) -> bool:
    """Use a transaction-scoped PostgreSQL advisory lock to prevent overlapping workers."""
    try:
        bind = db.get_bind()
        dialect = str(bind.dialect.name).casefold()
    except Exception:
        dialect = ""
    if dialect not in {"postgresql", "postgres"}:
        # SQLite/dev environments still get the route's in-process asyncio lock.
        return True
    result = await db.execute(text("SELECT pg_try_advisory_xact_lock(:lock_key)"), {"lock_key": 0x4E415632})
    return bool(result.scalar())


async def run_newsalert_pipeline(
    db,
    *,
    feed_urls: list[str] | None = None,
    user_id: int | None = None,
    limit_per_feed: int = 30,
    source_ids: list[str] | None = None,
    include_rss: bool = True,
    include_google_news: bool = True,
    include_nse: bool = True,
    include_youtube: bool = False,
    include_x_posts: bool = True,
    include_stockscan: bool = True,
    include_regulatory: bool = False,
) -> dict[str, Any]:
    """Run the unified, idempotent News Alert cycle; the caller owns commit/rollback."""
    started = time.monotonic()
    now = datetime.now(UTC)
    settings = get_settings()
    if not await _try_acquire_cycle_lock(db):
        logger.info("NEWSALERT_V2_CYCLE_SKIPPED reason=distributed_lock_held")
        return {"status": "skipped_locked", "items_fetched": 0, "created": 0,
                "skipped": {}, "sources": {}, "errors": [], "duration_seconds": 0,
                "reason": "another NewsAlert ingestion cycle is already running"}
    companies = await _load_approved_company_snapshot()
    matcher = build_company_matcher(companies)

    rss_urls = configured_rss_feeds(feed_urls)
    if source_ids and not feed_urls:
        wanted = {str(value).casefold() for value in source_ids}
        profiles = {profile.key.casefold(): profile for profile in NEWS_SOURCES}
        selected_hosts: set[str] = set()
        for key in wanted:
            profile = profiles.get(key)
            if profile:
                selected_hosts.update(domain.casefold() for domain in profile.domains)
        if selected_hosts:
            rss_urls = [url for url in rss_urls if any((urlsplit(url).hostname or "").casefold().endswith(domain) for domain in selected_hosts)]

    youtube_specs = _split_csv(settings.newsalert_youtube_channels) or _split_csv(settings.youtube_channel_ids) or _split_csv(settings.youtube_channel_names)
    # Scrape the union of platform defaults and every user's enabled X handles.
    # User ownership is applied at read time in the NewsAlert API, not here:
    # posts are stored once globally and each user sees only their own selection.
    configured_x_accounts = (
        _split_csv(settings.newsalert_x_accounts)
        + _split_csv(settings.x_allowed_accounts)
    )
    try:
        user_x_accounts = (await db.scalars(
            select(NewsAlertSource.value).where(
                NewsAlertSource.source_type == "x",
                NewsAlertSource.enabled.is_(True),
            )
        )).all()
    except Exception as exc:
        user_x_accounts = []
        logger.warning("NEWSALERT_X_USER_ACCOUNTS_LOAD_FAILED error=%s", str(exc)[:250])

    x_accounts: list[str] = []
    seen_x_accounts: set[str] = set()
    for raw_handle in [*configured_x_accounts, *user_x_accounts]:
        candidate = str(raw_handle or "").strip()
        if not candidate:
            continue
        if candidate.casefold().startswith(("https://x.com/", "https://twitter.com/")):
            candidate = urlsplit(candidate).path.strip("/").split("/", 1)[0]
        candidate = candidate.lstrip("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", candidate):
            continue
        key = candidate.casefold()
        if key not in seen_x_accounts:
            seen_x_accounts.add(key)
            x_accounts.append(candidate)
    logger.info(
        "NEWSALERT_X_ACCOUNT_CONFIGURATION configured=%s user_added=%s total_unique=%s",
        len(configured_x_accounts), len(user_x_accounts), len(x_accounts),
    )
    collector = FinancialNewsCollector()

    async def _collect_google() -> tuple[list[dict[str, Any]], list[str]]:
        return await collect_google_news_items(collector, companies)

    source_jobs = []
    if include_rss:
        source_jobs.append(_safe_collector(
            "rss",
            collect_rss_items(
                rss_urls,
                limit_per_feed=limit_per_feed,
                source_ids=source_ids,
                include_all_sources=not bool(feed_urls),
            ),
        ))
    if include_google_news:
        source_jobs.append(_safe_collector("google_news", _collect_google()))
    if include_nse:
        source_jobs.append(_safe_collector("nse", collect_nse_items(companies=companies, db=db)))
    if include_youtube:
        source_jobs.append(_safe_collector("youtube", collect_youtube_items(youtube_specs, companies)))
    if include_stockscan:
        async def _collect_stockscan_items():
            from ii_platform.web.collectors.stockscan_collector import StockScansCollector
            stockscan = StockScansCollector(max_candidates=200)
            rows = await stockscan.collect(
                lookback_days=max(1, min(int(os.getenv("NEWSALERT_STOCKSCAN_LOOKBACK_DAYS", "30")), 30)),
            )
            return CollectorResult(items=rows, errors=list(stockscan.last_diagnostics.get("errors") or []),
                                   diagnostics=dict(stockscan.last_diagnostics))
        source_jobs.append(_safe_collector("stockscan", _collect_stockscan_items()))
    if include_x_posts:
        source_jobs.append(_safe_collector("x", collect_x_items(x_accounts, days=7)))
    result_pairs = await asyncio.gather(*source_jobs)
    collected_by_source = {name: result for name, result in result_pairs}
    # BSE code discovery/persistence is performed once inside
    # _collect_bse_announcements(), where company ownership is checked against the
    # unique companies.bse_code constraint. Do not replay a diagnostics map here:
    # it is reporting data, not a write plan, and could contain stale/colliding rows.
    source_errors = [{"source": name, "error": error} for name, result in result_pairs for error in result.errors]
    all_raw = []
    for name, result in result_pairs:
        for raw in result.items:
            raw.setdefault("_newsalert_collector", name)
            all_raw.append((name, raw))

    candidates: list[NormalizedItem] = []
    x_candidates: list[NormalizedItem] = []
    skipped = {"invalid": 0, "age_or_noise": 0, "unmatched_or_ambiguous": 0, "irrelevant_social": 0, "duplicate": 0, "triage_rejected": 0, "below_min_score": 0}
    # Prime the BSE unique-code ownership cache once. Looking up the owner for
    # every one of thousands of NSE records would turn enrichment into an N+1
    # query problem.
    bse_code_owner_cache: dict[str, int] = {}
    try:
        bse_owner_rows = (await db.execute(
            select(Company.id, Company.bse_code).where(
                Company.bse_code.is_not(None), Company.bse_code != ""
            )
        )).all()
        bse_code_owner_cache = {
            str(code).strip(): int(company_id)
            for company_id, code in bse_owner_rows if str(code or "").strip()
        }
    except Exception as exc:
        logger.warning("NEWSALERT_BSE_CODE_OWNER_CACHE_FAILED error=%s", exc)
    for collector_name, raw in all_raw:
        if collector_name == "x":
            raw["is_x_post"] = True
            raw["content_type"] = "POST"
            # The database headline must be the post itself, not the account name.
            # Prefer the full body and store a short first-lines excerpt as title.
            raw["alert_all_posts"] = True
            post_text = _x_post_text(raw)
            if post_text:
                raw["title"] = _x_post_headline(post_text)
                raw["summary"] = post_text[:10000]
                raw["raw_content"] = post_text[:100000]
        elif collector_name == "youtube":
            raw["content_type"] = "INTERVIEW"
            raw["is_youtube"] = True
        # Official NSE corporate filings are source-verified records, not scraped
        # publisher articles. Keep their source marker before normalization so the
        # downstream path can apply source-specific acceptance and display rules.
        is_official_bse = bool(raw.get("official_bse_announcement")) or str(raw.get("source_key") or "").casefold() == "bse_india"
        is_official_nse = (
            (collector_name == "nse" and not is_official_bse)
            or bool(raw.get("official_nse_announcement"))
            or str(raw.get("source_key") or "").casefold() == "nse_india"
            or "nse corporate announcements" in str(raw.get("source") or "").casefold()
        )
        if is_official_nse:
            raw["official_nse_announcement"] = True
            raw.setdefault("source_key", "nse_india")
            raw.setdefault("content_type", "NEWS")
        if is_official_bse:
            raw["official_bse_announcement"] = True
            raw.setdefault("source_key", "bse_india")
            raw.setdefault("content_type", "NEWS")
        item = _normalize_raw_item(raw)
        if item is None:
            skipped["invalid"] += 1
            continue
        is_stockscan = collector_name == "stockscan" or bool(raw.get("stockscans_source_url"))
        allowed_by_age = True if is_stockscan else item_age_allowed(item, now=now)
        allowed_by_noise = True if is_stockscan else (is_official_nse or is_official_bse or bool(raw.get("alert_all_posts")) or not is_noise(item.title, item.description[:1000]))
        if not allowed_by_age or not allowed_by_noise:
            skipped["age_or_noise"] += 1
            continue
        # Company matching enriches ordinary news. YouTube is a channel-scoped
        # finance-interview feed: issuer matching is useful metadata, not a gate.
        item.company = _match_raw_company(raw, companies, matcher) or match_company(_item_text_for_matching(item), matcher)
        # Persist any BSE code carried by the exchange row, but never allow a
        # headline/company fuzzy-match to overwrite another company's unique code.
        # The discovery collector normally enriches these values beforehand; this
        # guarded path is only for a direct scrip code present on a BSE announcement.
        discovered_bse_code = str(raw.get("bse_code") or "").strip()
        if item.company is not None and discovered_bse_code.isdigit():
            company_id = int(item.company.company_id)
            code_owner = bse_code_owner_cache.get(discovered_bse_code)
            if code_owner is None:
                try:
                    async with db.begin_nested():
                        await db.execute(
                            update(Company).where(
                                Company.id == company_id,
                                or_(Company.bse_code.is_(None), Company.bse_code == ""),
                            ).values(bse_code=discovered_bse_code[:40])
                        )
                    bse_code_owner_cache[discovered_bse_code] = company_id
                except IntegrityError:
                    # Another worker may have claimed the code after the cache was
                    # loaded. Query once for this rare conflict and remember owner.
                    owner_result = await db.execute(
                        select(Company.id).where(Company.bse_code == discovered_bse_code).limit(1)
                    )
                    actual_owner = owner_result.scalar_one_or_none()
                    if actual_owner is not None:
                        bse_code_owner_cache[discovered_bse_code] = int(actual_owner)
                    logger.warning(
                        "NEWSALERT_BSE_RAW_CODE_UNIQUE_CONFLICT_SKIPPED company_id=%s bse_code=%s owner_id=%s",
                        company_id, discovered_bse_code, actual_owner,
                    )
            elif code_owner != company_id:
                logger.info(
                    "NEWSALERT_BSE_RAW_CODE_OWNER_MISMATCH_SKIPPED company_id=%s bse_code=%s owner_id=%s",
                    company_id, discovered_bse_code, code_owner,
                )
        if is_stockscan and item.company is None:
            skipped["unmatched_or_ambiguous"] += 1
            stockscan_label = str(raw.get("stockscans_company_name") or raw.get("company_name") or raw.get("companyName") or "").strip()
            label_norm = _normalise(stockscan_label)
            near_matches: list[tuple[float, str]] = []
            if label_norm:
                for coverage_company in companies:
                    variants = (coverage_company.name, *coverage_company.aliases)
                    best_ratio = max((difflib.SequenceMatcher(None, label_norm, _normalise(variant)).ratio() for variant in variants if _normalise(variant)), default=0.0)
                    if best_ratio >= 0.60:
                        near_matches.append((best_ratio, coverage_company.name))
            suggestions = ", ".join(name for _, name in sorted(near_matches, reverse=True)[:3]) or "none"
            logger.info(
                "NEWSALERT_STOCKSCANS_REJECT title=%r stockscan_company=%r reason=company_not_in_coverage_list possible_coverage_matches=%r",
                item.title[:220], stockscan_label[:180], suggestions,
            )
            continue
        if item.content_type == "INTERVIEW":
            verified_source_match = bool(raw.get("_youtube_coverage_match"))
            best_company = item.company if (verified_source_match or is_stockscan) else None
            interview_matches = []
            if best_company is None:
                interview_matches = _interview_company_matches(item.title, companies)
                if not interview_matches:
                    interview_matches = _interview_company_matches(item.description[:5000], companies)
                if interview_matches:
                    best_company = interview_matches[0]
            if best_company is not None:
                item.company = best_company
                item.raw["display_company_name"] = best_company.name
                item.raw["interview_companies"] = [best_company.name]
            else:
                # Keep the interview row even when no approved company can be
                # named unambiguously. Never substitute the YouTube channel name.
                item.company = None
                item.raw["display_company_name"] = _guess_interview_company_label(item.title) or "Company not identified"
                item.raw.pop("nse_symbol", None)
                item.raw.pop("company_name", None)
        item.source = item.source or "News source"
        if item.is_x:
            if not item.raw.get("alert_all_posts") and not _business_relevant_x_item(item):
                skipped["irrelevant_social"] = skipped.get("irrelevant_social", 0) + 1
                continue
            # X posts are sourced from the configured allowlist and may be relevant
            # to an industry, macro trend, or market structure without naming a
            # Coverage List company. Preserve those posts as companyless records.
            x_candidates.append(item)
        elif item.content_type == "INTERVIEW":
            # Channel-qualified, recent finance interviews stay visible even without issuer matching.
            candidates.append(item)
        elif item.raw.get("official_nse_announcement") or item.raw.get("official_bse_announcement"):
            # Exchange-originated filings is a primary-source announcement stream. Keep
            # filings even when an issuer's symbol/name is not mapped in Coverage;
            # the UI can display them with their source company/symbol metadata.
            if item.company is None:
                display_name = (
                    item.raw.get("display_company_name")
                    or item.raw.get("company_name")
                    or item.raw.get("companyName")
                    or item.raw.get("nse_symbol")
                    or item.raw.get("symbol")
                    or "NSE announcement"
                )
                item.raw["display_company_name"] = str(display_name).strip()[:255]
            candidates.append(item)
        elif item.company is None:
            skipped["unmatched_or_ambiguous"] += 1
            continue
        else:
            candidates.append(item)

    existing_rows = await _load_recent_rows(db, now=now)
    # Older runs could persist RSS-only NSE rows with no issuer, no symbol and no
    # attachment. They cannot be routed safely and the official API now supplies
    # the canonical issuer/PDF record. Remove only those stale rows whose exact
    # subject + broadcast minute is present in the successful official API batch.
    nse_api_subject_times: set[tuple[str, str]] = set()
    nse_result_for_repair = collected_by_source.get("nse")
    if nse_result_for_repair:
        for raw in nse_result_for_repair.items:
            if "official-nse-api" not in str(raw.get("transport") or "").casefold():
                continue
            published = _aware_datetime(raw.get("published_at") or raw.get("published"))
            subject = _normalise(raw.get("announcement_subject") or raw.get("subject") or raw.get("title"))
            if published is not None and subject:
                minute = published.astimezone(ZoneInfo("Asia/Kolkata")).replace(second=0, microsecond=0).isoformat()
                nse_api_subject_times.add((subject, minute))
    stale_unidentified_nse_rss = 0
    if nse_api_subject_times:
        for existing_row in list(existing_rows):
            source_text = str(existing_row.source or "").casefold()
            if "nse corporate announcements" not in source_text or existing_row.company_id is not None:
                continue
            metadata = existing_row.metadata_json if isinstance(existing_row.metadata_json, dict) else {}
            issuer_fields = (
                metadata.get("nse_symbol") or metadata.get("issuer_name_from_source")
                or metadata.get("isin") or metadata.get("bse_code")
                or metadata.get("attachment_url")
            )
            if issuer_fields:
                continue
            published = _aware_datetime(existing_row.published_at)
            subject = _normalise(metadata.get("announcement_subject") or existing_row.headline)
            if published is None or not subject:
                continue
            minute = published.astimezone(ZoneInfo("Asia/Kolkata")).replace(second=0, microsecond=0).isoformat()
            if (subject, minute) in nse_api_subject_times:
                await db.delete(existing_row)
                existing_rows.remove(existing_row)
                stale_unidentified_nse_rss += 1
    if stale_unidentified_nse_rss:
        logger.info("NEWSALERT_NSE_STALE_RSS_ROWS_REMOVED count=%s", stale_unidentified_nse_rss)

    repaired_social_rows = _repair_existing_social_rows(existing_rows, companies)
    if repaired_social_rows:
        logger.info("NEWSALERT_LEGACY_SOCIAL_REPAIR repaired=%s", repaired_social_rows)
    existing_keys = {str(row.dedupe_key) for row in existing_rows}
    def _is_x_row(row: Any) -> bool:
        return str(getattr(row, "content_type", "") or "").upper() == "POST" and (
            str(getattr(row, "source", "") or "").casefold().startswith("x /")
            or (isinstance(getattr(row, "metadata_json", None), dict) and getattr(row, "metadata_json").get("x_account"))
        )

    # Keep company-feed identity separate from X identity, including URLs and
    # source-specific IDs. An X URL must never suppress a company-matched item.
    existing_company_urls = {
        canonicalize_url(row.url) for row in existing_rows if row.url and not _is_x_row(row)
    }
    existing_x_urls = {
        canonicalize_url(row.url) for row in existing_rows if row.url and _is_x_row(row)
    }
    existing_x_by_url = {
        canonicalize_url(row.url): row for row in existing_rows if row.url and _is_x_row(row)
    }
    existing_x_by_key = {
        str(row.dedupe_key): row for row in existing_rows if _is_x_row(row) and getattr(row, "dedupe_key", None)
    }
    refreshed_x_rows = 0
    existing_company_source_ids = {
        (str(row.source or "").casefold(), str(row.source_item_id))
        for row in existing_rows if row.source_item_id and not _is_x_row(row)
    }
    # Keep direct references to existing regulatory rows. A backfill frequently
    # sees the same announcement again after the scraper learns its real PDF URL;
    # a duplicate must enrich the row rather than being discarded unchanged.
    existing_official_by_source_id: dict[tuple[str, str], Any] = {}
    existing_official_by_url: dict[str, Any] = {}
    existing_official_by_content: dict[tuple[str, str, str, str], Any | None] = {}
    for existing_row in existing_rows:
        if _is_x_row(existing_row):
            continue
        existing_metadata = existing_row.metadata_json if isinstance(existing_row.metadata_json, dict) else {}
        is_regulatory = bool(
            existing_metadata.get("official_nse_announcement")
            or existing_metadata.get("official_bse_announcement")
            or str(existing_metadata.get("source_key") or "").casefold() in {"nse_india", "bse_india"}
        )
        if not is_regulatory:
            continue
        if existing_row.source_item_id:
            existing_official_by_source_id[(str(existing_row.source or "").casefold(), str(existing_row.source_item_id))] = existing_row
        if existing_row.url:
            existing_official_by_url[canonicalize_url(existing_row.url)] = existing_row
        source_key = existing_metadata.get("source_key") or (
            "bse_india" if existing_metadata.get("official_bse_announcement") else "nse_india"
        )
        for content_identity in _official_content_identities(
            source_key=source_key,
            filing_feed=existing_metadata.get("nse_filing_feed"),
            nse_symbol=existing_metadata.get("nse_symbol") or existing_row.nse_symbol,
            company_name=existing_metadata.get("issuer_name_from_source") or existing_row.company_name,
            subject=existing_metadata.get("announcement_subject") or existing_row.headline,
            published_at=existing_row.published_at,
        ):
            if content_identity in existing_official_by_content:
                existing_official_by_content[content_identity] = None
            else:
                existing_official_by_content[content_identity] = existing_row
    existing_x_source_ids = {
        (str(row.source or "").casefold(), str(row.source_item_id))
        for row in existing_rows if row.source_item_id and _is_x_row(row)
    }

    # Canonical URL / source identifier duplicate filter; X has its own key space.
    seen_cycle_keys: set[str] = set()
    seen_cycle_urls: set[tuple[str, str]] = set()
    seen_cycle_company_source_ids: set[tuple[str, str]] = set()
    seen_cycle_x_source_ids: set[tuple[str, str]] = set()
    pre_triage: list[NormalizedItem] = []
    refreshed_official_rows = 0
    for item in [*x_candidates, *candidates]:
        key = item.dedupe_key
        duplicate = key in existing_keys or key in seen_cycle_keys
        url_identity = ("x" if item.is_x else "company", item.canonical_url)
        if not duplicate and url_identity in seen_cycle_urls:
            duplicate = True
        if not duplicate and ((item.canonical_url in existing_x_urls) if item.is_x else (item.canonical_url in existing_company_urls)):
            # Cross-source company stories still reach Jaccard; exact URL repeats don't.
            duplicate = True
        source_identity = (item.source.casefold(), str(item.source_item_id)) if item.source_item_id else None
        official_content_identities: set[tuple[str, str, str, str]] = set()
        if item.raw.get("official_nse_announcement") or item.raw.get("official_bse_announcement"):
            official_content_identities = _official_content_identities(
                source_key=item.raw.get("source_key") or ("bse_india" if item.raw.get("official_bse_announcement") else "nse_india"),
                filing_feed=item.raw.get("nse_filing_feed"),
                nse_symbol=item.raw.get("nse_symbol") or item.raw.get("symbol"),
                company_name=(item.company.name if item.company else item.raw.get("company_name") or item.raw.get("companyName")),
                subject=item.raw.get("announcement_subject") or item.raw.get("subject") or item.title,
                published_at=item.published_at,
            )
            if any(existing_official_by_content.get(identity) is not None for identity in official_content_identities):
                duplicate = True
        if source_identity:
            existing_identity_ids = existing_x_source_ids if item.is_x else existing_company_source_ids
            cycle_identity_ids = seen_cycle_x_source_ids if item.is_x else seen_cycle_company_source_ids
            if not duplicate and (source_identity in existing_identity_ids or source_identity in cycle_identity_ids):
                duplicate = True
        if duplicate:
            if item.is_x:
                existing_x_row = existing_x_by_url.get(item.canonical_url) or existing_x_by_key.get(item.dedupe_key)
                new_post_text = _x_post_text(item.raw)
                if existing_x_row is not None and new_post_text and _existing_x_headline_is_author(existing_x_row):
                    existing_x_row.headline = _x_post_headline(new_post_text)
                    existing_x_row.summary = new_post_text[:10000]
                    old_metadata = existing_x_row.metadata_json if isinstance(existing_x_row.metadata_json, dict) else {}
                    old_metadata = dict(old_metadata)
                    old_metadata.update({"post_text": new_post_text[:10000], "raw_content": new_post_text[:10000],
                                         "x_account": item.source_handle, "author_handle": item.source_handle})
                    existing_x_row.metadata_json = old_metadata
                    refreshed_x_rows += 1
            elif item.raw.get("official_nse_announcement") or item.raw.get("official_bse_announcement"):
                existing_official_row = (
                    (existing_official_by_source_id.get(source_identity) if source_identity else None)
                    or existing_official_by_url.get(item.canonical_url)
                    or next((existing_official_by_content.get(identity) for identity in official_content_identities
                             if existing_official_by_content.get(identity) is not None), None)
                )
                if existing_official_row is not None:
                    old_metadata = existing_official_row.metadata_json if isinstance(existing_official_row.metadata_json, dict) else {}
                    old_metadata = dict(old_metadata)
                    incoming_metadata = {
                        "official_nse_announcement": bool(item.raw.get("official_nse_announcement")),
                        "official_bse_announcement": bool(item.raw.get("official_bse_announcement")),
                        "source_key": str(item.raw.get("source_key") or ("bse_india" if item.raw.get("official_bse_announcement") else "nse_india")),
                        "nse_symbol": str(item.raw.get("nse_symbol") or item.raw.get("symbol") or "").strip().upper(),
                        "issuer_name_from_source": str(item.raw.get("company_name") or "").strip()[:255],
                        "announcement_subject": str(item.raw.get("announcement_subject") or item.raw.get("subject") or item.title).strip()[:1000],
                        "transport": str(item.raw.get("transport") or "official-exchange")[:100],
                        "nse_filing_feed": str(item.raw.get("nse_filing_feed") or "")[:40],
                        "isin": str(item.raw.get("isin") or "").strip().upper()[:20],
                        "bse_code": str(item.raw.get("bse_code") or (item.company.bse_code if item.company else "") or "").strip()[:40],
                        "bse_symbol": str(item.raw.get("bse_symbol") or "").strip()[:80],
                        "bse_stock_url": str(item.raw.get("bse_stock_url") or "").strip()[:1500],
                    }
                    if not _requires_bse_fallback_metadata(item.raw):
                        for fallback_field in ("bse_code", "bse_symbol", "bse_stock_url"):
                            incoming_metadata.pop(fallback_field, None)
                            old_metadata.pop(fallback_field, None)
                    for field_name, value in incoming_metadata.items():
                        if value not in {"", None, False}:
                            old_metadata[field_name] = value
                    incoming_attachment = str(item.raw.get("attachment_url") or "").strip()
                    if item.raw.get("official_nse_announcement") and _valid_nse_pdf_attachment(
                        incoming_attachment, attachment_type=item.raw.get("attachment_type")
                    ):
                        # API-normalized PDF is authoritative; replace stale/bad URLs.
                        old_metadata["attachment_url"] = incoming_attachment[:1500]
                        old_metadata["attachment_type"] = "PDF"
                    elif item.raw.get("official_bse_announcement") and _bse_pdf_url(incoming_attachment):
                        old_metadata["attachment_url"] = incoming_attachment[:1500]
                        old_metadata["attachment_type"] = "PDF"
                    elif item.raw.get("official_nse_announcement") and not _valid_nse_pdf_attachment(
                        old_metadata.get("attachment_url"), attachment_type=old_metadata.get("attachment_type")
                    ):
                        old_metadata.pop("attachment_url", None)
                        old_metadata.pop("attachment_type", None)
                    elif item.raw.get("official_bse_announcement") and not _bse_pdf_url(old_metadata.get("attachment_url")):
                        old_metadata.pop("attachment_url", None)
                        old_metadata.pop("attachment_type", None)
                    if item.raw.get("nse_filing_feed") == "board_meetings":
                        # Board-meeting feed rows intentionally do not link to XML/XBRL.
                        old_metadata.pop("attachment_url", None)
                        old_metadata.pop("attachment_type", None)
                    existing_official_row.metadata_json = old_metadata
                    if not existing_official_row.nse_symbol and incoming_metadata["nse_symbol"]:
                        existing_official_row.nse_symbol = incoming_metadata["nse_symbol"]
                    if item.company is not None:
                        if not existing_official_row.company_id:
                            existing_official_row.company_id = item.company.company_id
                        if not existing_official_row.company_name:
                            existing_official_row.company_name = item.company.name
                    refreshed_official_rows += 1
            skipped["duplicate"] += 1
            continue
        # Do not mark an item memoised here; rejected or failed assessments should not suppress later candidates.
        pre_triage.append(item)
        seen_cycle_keys.add(key)
        seen_cycle_urls.add(url_identity)
        if source_identity:
            (seen_cycle_x_source_ids if item.is_x else seen_cycle_company_source_ids).add(source_identity)

    if refreshed_x_rows:
        logger.info("NEWSALERT_X_DUPLICATE_HEADLINE_REFRESH refreshed=%s", refreshed_x_rows)
    if refreshed_official_rows:
        logger.info("NEWSALERT_EXCHANGE_DUPLICATE_METADATA_REFRESH refreshed=%s", refreshed_official_rows)

    # Non-X candidates get one company-scoped Jaccard check per 48-hour window.
    # Rejected triaged items are memoised only for this cycle.
    semaphore = asyncio.Semaphore(4)
    triage_outcomes: list[tuple[NormalizedItem, dict[str, Any] | None, Exception | None]] = []
    fallback_counts = {"triage": 0, "score": 0}
    async def _triage(item: NormalizedItem):
        async with semaphore:
            try:
                return item, await triage_item(item, settings=settings), None
            except Exception as exc:
                return item, None, exc
    # Recent interviews are already metadata-classified by the collector and
    # have passed upload-time and deterministic noise checks. Do not make their
    # visibility depend on a company-specific AI judgement: companyless videos
    # were previously fetched and then all discarded by structural rules.
    interview_candidates = [item for item in pre_triage if not item.is_x and item.content_type == "INTERVIEW"]
    # Do not route official NSE filings through generic publisher-news triage.
    # The prior run fetched 139 official announcements, then discarded all 139:
    # 135 failed Coverage matching and the remaining 4 failed generic triage.
    # Source authenticity and announcement status are sufficient for display;
    # materiality is informational only and is not a buy/sell recommendation.
    nse_candidates = [
        item for item in pre_triage
        if not item.is_x and (item.raw.get("official_nse_announcement") or item.raw.get("official_bse_announcement"))
    ]
    news_candidates = [
        item for item in pre_triage
        if not item.is_x and item.content_type != "INTERVIEW"
        and not item.raw.get("official_nse_announcement") and not item.raw.get("official_bse_announcement")
    ]
    x_candidates = [item for item in pre_triage if item.is_x]
    for item in nse_candidates:
        item.triage = {
            "relevant": True,
            "rationale": "Official corporate announcement collected from NSE; retained as source content, not an investment recommendation.",
            "event_type": "NSE_ANNOUNCEMENT",
            "model": "official-nse-source-eligibility-v1",
            "triage_bypassed": True,
        }
        item.score = {
            "score": 60,
            "materiality": "MEDIUM",
            "rationale": "Official NSE filing displayed for monitoring. Score is informational only and is not a buy/sell recommendation.",
            "model": "official-nse-display-score-v1",
            "scoring_bypassed": True,
        }
    triage_outcomes = list(await asyncio.gather(*(_triage(item) for item in news_candidates)))
    triaged_relevant: list[NormalizedItem] = []
    accepted_for_similarity: list[NormalizedItem] = []
    for item, triage, error in triage_outcomes:
        if error is not None:
            fallback_counts["triage"] += 1
            source_errors.append({"source": item.source, "url": item.canonical_url, "stage": "triage_fallback", "error": str(error)[:300]})
            triage = _fallback_triage(item)
        item.triage = triage
        if not triage.get("relevant"):
            skipped["triage_rejected"] += 1
            continue
        if _same_company_story(item, existing_rows, accepted_for_similarity):
            skipped["duplicate"] += 1
            continue
        triaged_relevant.append(item)

    async def _score(item: NormalizedItem):
        async with semaphore:
            try:
                return item, await score_item(item, settings=settings), None
            except Exception as exc:
                return item, None, exc
    score_outcomes = await asyncio.gather(*(_score(item) for item in triaged_relevant))
    scored_news: list[NormalizedItem] = []
    for item, score, error in score_outcomes:
        if error is not None:
            fallback_counts["score"] += 1
            source_errors.append({"source": item.source, "url": item.canonical_url, "stage": "score_fallback", "error": str(error)[:300]})
            score = _fallback_score(item)
        item.score = score
        if int(score["score"]) < int(settings.newsalert_min_score):
            skipped["below_min_score"] += 1
            continue
        # Only records that passed triage, scoring, and the configured minimum
        # score suppress similar stories in this cycle. A failed score or a low
        # score must not be memoised as an accepted story.
        if _same_company_story(item, existing_rows, accepted_for_similarity):
            skipped["duplicate"] += 1
            continue
        accepted_for_similarity.append(item)
        scored_news.append(item)

    # Interviews are visible content, not merely candidates for issuer-alert
    # scoring. Give them a neutral, explicitly explained score; this is not a buy/
    # sell recommendation and does not create a company association.
    for item in interview_candidates:
        item.triage = {
            "relevant": True,
            "rationale": "Recent interview passed timestamp, interview-format, and deterministic noise checks.",
            "event_type": "INTERVIEW",
            "model": "deterministic-interview-eligibility-v1",
        }
        item.score = {
            "score": 60,
            "materiality": "MEDIUM",
            "rationale": "Displayed as a recent interview; issuer-specific materiality was not inferred.",
            "model": "deterministic-interview-display-v1",
        }

    # Deterministic cross-source dedup for exact X source identifiers/URLs, with no company lookup.
    x_seen: set[str] = set()
    unique_x: list[NormalizedItem] = []
    for item in x_candidates:
        xkey = item.dedupe_key
        if xkey in x_seen or xkey in existing_keys or item.canonical_url in existing_x_urls:
            skipped["duplicate"] += 1
            continue
        x_seen.add(xkey)
        unique_x.append(item)

    created = 0
    created_by_collector: dict[str, int] = {}
    inserted_rows: list[NewsAlertItem] = []
    for item in [*scored_news, *nse_candidates, *interview_candidates, *unique_x]:
        values = _to_db_values(item)
        row = NewsAlertItem(**values)
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError as exc:
            if _is_dedupe_unique_violation(exc):
                skipped["duplicate"] += 1
                continue
            # A not-null, foreign-key, data-length, or other constraint failure
            # is not a duplicate. Let the cycle fail so the caller can roll back
            # and record the real error rather than silently dropping valid data.
            raise
        created += 1
        collector_key = str(item.raw.get("_newsalert_collector") or "unknown")
        created_by_collector[collector_key] = created_by_collector.get(collector_key, 0) + 1
        inserted_rows.append(row)
        existing_rows.append(row)
        existing_keys.add(item.dedupe_key)
        if item.is_x:
            existing_x_urls.add(item.canonical_url)
            if item.source_item_id:
                existing_x_source_ids.add((item.source.casefold(), str(item.source_item_id)))
        else:
            existing_company_urls.add(item.canonical_url)
            if item.source_item_id:
                existing_company_source_ids.add((item.source.casefold(), str(item.source_item_id)))

    if "nse" in collected_by_source:
        nse_fetched = len(collected_by_source["nse"].items)
        nse_matched = sum(1 for item in nse_candidates if item.company is not None)
        nse_companyless = sum(1 for item in nse_candidates if item.company is None)
        nse_created = int(created_by_collector.get("nse", 0))
        logger.info(
            "NEWSALERT_NSE_ACCEPTANCE_RESULT fetched=%s matched_to_coverage=%s companyless_retained=%s triage_bypassed=%s created=%s",
            nse_fetched, nse_matched, nse_companyless, len(nse_candidates), nse_created,
        )

    # Prune after at least one source succeeded fully OR returned usable items in a
    # partial run. If every source was unavailable and returned nothing, preserve
    # the previous dataset and retry later. This lets partial YouTube success also
    # remove already-stored interviews that fail the stricter finance/interview gate.
    successful_sources = sum(
        1 for _name, result in result_pairs if (not result.errors) or bool(result.items)
    )
    pruned: dict[str, int] = {
        "deleted_older_than_7_days": 0,
        "deleted_social_older_than_48_hours": 0,
        "deleted_not_approved_coverage": 0,
        "deleted_irrelevant_youtube_interviews": 0,
    }
    if successful_sources > 0:
        pruned = await _prune(db, now=now, approved_company_ids={company.company_id for company in companies})

    source_summary: dict[str, Any] = {}
    for name, source_result in collected_by_source.items():
        fetched = len(source_result.items)
        source_status = "failed" if source_result.errors and fetched == 0 else ("partial" if source_result.errors else ("ok" if fetched else "empty"))
        source_summary[name] = {
            "status": source_status,
            "fetched": fetched,
            "created": int(created_by_collector.get(name, 0)),
            "errors": [str(error)[:350] for error in source_result.errors[:10]],
            "diagnostics": source_result.diagnostics,
        }
    result = {
        "status": "partial" if source_errors or any(fallback_counts.values()) else "ok",
        "sources": source_summary,
        "items_fetched": len(all_raw),
        "created": created,
        "created_by_source": created_by_collector,
        "skipped": skipped,
        "failed": len(source_errors),
        "fallback_counts": fallback_counts,
        "errors": source_errors[:100],
        "pruned": pruned,
        "approved_companies": len(companies),
        "duration_seconds": round(time.monotonic() - started, 3),
        "manual_user_id": user_id,
    }
    logger.info("NEWSALERT_V2_CYCLE result=%s", result)
    return result
