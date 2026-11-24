from __future__ import annotations
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote, urlencode, urlparse, urljoin, unquote
import xml.etree.ElementTree as ET
import logging
logger = logging.getLogger(__name__)
from bs4 import BeautifulSoup
# Normal scheduled cycles default to 48 hours. The canonical backfill CLI can
# widen this per invocation with NEWSALERT_YOUTUBE_LOOKBACK_DAYS (up to 10 days).
MAX_LOOKBACK_DAYS = 2
MAX_LOOKBACK_HOURS = MAX_LOOKBACK_DAYS * 24


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


# Per-channel cap per collection cycle so one run can never flood the feed.
MAX_VIDEOS_PER_CHANNEL = _env_int("YOUTUBE_MAX_VIDEOS_PER_CHANNEL", 5, 1, 20)
# These are fallback channel specs only. A deployment can override them with
# YOUTUBE_CHANNEL_IDS as channel IDs, @handles, channel URLs, or channel names.
DEFAULT_CHANNELS = [
    ("UCmRbHAgG2k2vDUvb3xsEunQ", "CNBC-TV18"),
    ("UCI_mwTKUhicNzFrhm33MzBQ", "ET Now"),
    ("UC3uJIdRFTGgLWrUziaHbzrg", "NDTV Profit"),
    ("UChftTVI0QJmyXkajQYt2tiQ", "Moneycontrol"),
    ("UCkXopQ3ubd-rnXnStZqCl2w", "Zee Business"),
]
CHANNEL_NAME_BY_ID = dict(DEFAULT_CHANNELS)
# Financial news publishers: only their videos that pass both content filters are kept.
DEFAULT_FINANCIAL_NEWS_CHANNELS = [
    ("@TheEconomicTimes", "The Economic Times"),
    ("@businessstandard", "Business Standard"),
    ("@livemint", "Mint / Livemint"),
    ("@businesstoday", "Business Today"),
    ("@cnbcawaaz", "CNBC Awaaz"),
    ("@financialexpress", "Financial Express"),
    ("@ndtvprofitindia", "NDTV Profit"),
    ("@ETNOW", "ET NOW"),
    ("@moneycontrol", "Moneycontrol"),
    ("@ZeeBusiness", "Zee Business"),
    ("BQ Prime", "BQ Prime"),
    ("The Hindu BusinessLine", "The Hindu BusinessLine"),
    ("Fortune India", "Fortune India"),
    ("Money9", "Money9"),
    ("CNBC Bajar", "CNBC Bajar"),
    ("Bloomberg Television", "Bloomberg Television"),
    ("CNBC Television", "CNBC Television"),
    ("CNBC International", "CNBC International"),
    ("Yahoo Finance", "Yahoo Finance"),
    ("Reuters", "Reuters"),
    ("Financial Times", "Financial Times"),
    ("The Wall Street Journal", "The Wall Street Journal"),
    ("MarketWatch", "MarketWatch"),
    ("Forbes India", "Forbes India"),
]
# Interview/podcast channels with regular founder, executive, investor, or market-guest conversations.
DEFAULT_INTERVIEW_CHANNELS = [
    ("@thebarbershopwithshantanu6670", "The BarberShop with Shantanu"),
    ("The Neon Show", "The Neon Show"),
    ("YourStory", "YourStory"),
    ("Inc42", "Inc42"),
    ("New To The Street TV", "New To The Street TV"),
    ("Real Vision Finance", "Real Vision Finance"),
    ("The Investors Podcast", "The Investors Podcast"),
    ("Bloomberg Podcasts", "Bloomberg Podcasts"),
    ("The Compound", "The Compound"),
]
# Finance/investing channels: explainers are rejected unless the individual video itself is an interview.
DEFAULT_FINANCE_CONTENT_CHANNELS = [
    ("@soicfinance", "SOIC"),
    ("@vivbajaj", "Vivek Bajaj"),
    ("Elearnmarkets", "Elearnmarkets / Face2Face Podcasts"),
    ("Value Research", "Value Research"),
    ("Finology Ticker", "Finology Ticker"),
    ("Pranjal Kamra", "Pranjal Kamra"),
    ("Groww", "Groww"),
    ("Zerodha", "Zerodha"),
    ("The Plain Bagel", "The Plain Bagel"),
    ("Patrick Boyle", "Patrick Boyle"),
    ("Schwab Network", "Schwab Network"),
    ("The Motley Fool", "The Motley Fool"),
    ("Basant Maheshwari", "Basant Maheshwari"),
    ("Sanjay Bakshi", "Sanjay Bakshi"),
]
DEFAULT_EXTRA_CHANNELS = list(dict.fromkeys([
    *DEFAULT_FINANCIAL_NEWS_CHANNELS,
    *DEFAULT_INTERVIEW_CHANNELS,
    *DEFAULT_FINANCE_CONTENT_CHANNELS,
]))
# Scope: only (a) interviews with company insiders and (b) earnings calls / quarterly
# results. Every rule below reads the VIDEO TITLE. Descriptions are loose text and
# caused unrelated news clips to pass, so they are never used to qualify a video.
INTERVIEW_TITLE_RE = re.compile(
    r"\b(interview|in conversation|conversation with|fireside chat|one[- ]on[- ]one|q\s*&\s*a|"
    r"chat with|talks? to|speaks? (?:to|with|on)|talks? about|podcast with|the big interview|"
    r"exclusive interaction|management interaction|meet the management|dialogue with|"
    r"interacts? with|ask me anything)\b",
    re.I,
)
# Insider roles only. Deliberately excludes generic "president"/"director" so that
# "Trump ... President" or "movie director" cannot qualify a video.
INSIDER_TITLE_RE = re.compile(
    r"\b(promoters?|owners?|founders?|co-?founders?|ceo|chief executive(?: officer)?|md|"
    r"managing directors?|joint managing director|whole[- ]time directors?|executive directors?|"
    r"chairman|chairperson|chairwoman|cfo|coo|cto|cmo|chief financial officer|chief operating officer|"
    r"employees?|staff|workers?|management|leadership|business heads?|general managers?)\b",
    re.I,
)
# Earnings calls and quarterly results coverage.
RESULTS_TITLE_RE = re.compile(
    r"\b(con[- ]?calls?|earnings (?:calls?|conference|updates?|results?|review)|conference calls?|"
    r"analyst (?:calls?|meets?)|investor (?:calls?|meets?)|quarterly results?|"
    r"q[1-4](?:\s*fy\s*\d{2,4})?\s*(?:results?|earnings|profit|loss|pat|revenue|sales|ebitda|margins?|review|update)|"
    r"fy\s*\d{2,4}\s*(?:q[1-4]\s*)?results?|results? (?:review|analysis|highlights|announcement|preview)|"
    r"agm|egm|annual general meetings?|shareholders'? meetings?|postal ballots?)\b",
    re.I,
)
# Market commentary: opening/closing calls, wraps, stock lists, live blogs. Rejected
# even when the title also mentions an interview or results.
MARKET_COMMENTARY_RE = re.compile(
    r"\b(market opening|opening bell|closing bell|market close|market closing|market wrap|"
    r"pre[- ]market|stock market today|stocks? to watch|stocks? in news|stocks? to buy|"
    r"market live|live market|market today|today'?s market|sensex today|nifty today|"
    r"top gainers|top losers|morning brief|market updates?|market highlights|trade setup|"
    r"market (?:open|opens|opening|close|closes|closing)|stock market live|live updates?)\b",
    re.I,
)
OFF_TOPIC_RE = re.compile(
    r"\b(protests?|demonstrations?|politics|political|election campaign|campaign rally|murder|rape|kidnap(?:ping)?|"
    r"celebrity gossip|celebrit(?:y|ies)|actors?|actresses?|films?|movies?|film review|movie review|"
    r"cricket highlights|football highlights|wedding|religious sermon|horoscope|stampede|crime|"
    r"trump|biden|president|election|parliament|lok sabha|cabinet)\b",
    re.I,
)


# Optional: known promoters/CEOs/MDs by name, for interviews whose title does not
# state a role (e.g. "Pradyu Mondal of CSB Bank | Interview"). Comma-separated.
INSIDER_NAMES = [
    name.strip().casefold()
    for name in os.getenv("YOUTUBE_INSIDER_NAMES", "").split(",")
    if name.strip()
]


# Role terms used to search YouTube directly for insider interviews. Each query is
# still validated by exact 48h upload time and the strict insider gate.
INSIDER_SEARCH_ROLES = [
    "CEO", "managing director", "promoter", "founder", "chairman",
    "whole-time director", "CFO", "owner",
]
MAX_INSIDER_RESULTS = _env_int("YOUTUBE_MAX_INSIDER_RESULTS", 60, 1, 300)


class YouTubeNewsCollector:
    # 404-confirmed IDs are suppressed for the life of this worker process. A
    # restart allows operators to re-check them after repairing configuration.
    INVALID_CHANNEL_IDS: set[str] = set()
    """Collect exact recent YouTube video metadata, then let Coverage matching decide relevance.
    All discovery and metadata reads use public YouTube pages rendered with
    Playwright. The collector does not call the YouTube Data API and deliberately
    does not bypass sign-in, CAPTCHA, or anti-bot controls. Video headline and date
    are read from public page metadata; transcript text never replaces the title.
    """
    DEFAULT_MAX_RESULTS = 50
    VIDEO_PAGE_TIMEOUT = 8.0
    CHANNEL_PAGE_TIMEOUT = 10.0
    def __init__(self, api_key: str | None = None, *, timeout_seconds: float = 20.0) -> None:
        # Retain the legacy argument so existing callers do not break, but never
        # use it. This collector intentionally uses public-page Playwright scraping.
        # Kept only for backward compatibility with older call sites. Never store,
        # read from the environment, or send this value to any service.
        if api_key:
            logger.warning("YouTube API key argument was ignored; public browser scraping is API-free")
        self.timeout_seconds = max(5.0, min(float(timeout_seconds or 20.0), 60.0))
        # Cache exact watch-page metadata per video ID for this collector run.
        # The same video commonly appears in Atom feeds, multiple search queries,
        # and both Google/Bing results; this prevents repeated HTTP requests.
        self._metadata_cache: dict[str, dict[str, Any] | None] = {}
        self._metadata_locks: dict[str, asyncio.Lock] = {}
        self.last_diagnostics: dict[str, Any] = {
            "configured_specs": 0,
            "resolved_channels": 0,
            "feeds_attempted": 0,
            "candidate_videos": 0,
            "search_queries_attempted": 0,
            "errors": [],
            "transport": "httpx+BrowserClient+Playwright+yt-dlp+search-engine",
            "transports_attempted": [],
            "api_used": False,
            "credentials_required": False,
            "max_age_days": MAX_LOOKBACK_DAYS,
            "eligibility": {"accepted": 0, "rejected_not_interview": 0, "rejected_not_finance_related": 0, "rejected_stale_or_unknown_time": 0},
        }
    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()
    def _record_transport(self, name: str) -> None:
        attempted = self.last_diagnostics.setdefault("transports_attempted", [])
        if name not in attempted:
            attempted.append(name)
    @staticmethod
    def _parse_date(value: object) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    @staticmethod
    def _normalise_name(value: object) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    @staticmethod
    def _channel_id_from_html(html: str) -> str | None:
        patterns = (
            r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{20,})"',
            r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{20,})"',
            r'https?://(?:www\.)?youtube\.com/channel/(UC[A-Za-z0-9_-]{20,})',
        )
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        return None
    @staticmethod
    def _channel_title_from_html(html: str) -> str | None:
        soup = BeautifulSoup(html, "lxml")
        for attrs in ({"property": "og:title"}, {"name": "title"}):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                value = " ".join(str(tag.get("content")).split()).strip()
                if value:
                    return value.replace(" - YouTube", "").strip()
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        return title.replace(" - YouTube", "").strip() or None
    @staticmethod
    def _is_finance_related(title: object, description: object = "") -> bool:
        """Require a real finance/business topic; the source channel never implies relevance."""
        title_text = " ".join(str(title or "").split()).casefold()
        description_text = " ".join(str(description or "").split())[:1800].casefold()
        if not title_text:
            return False
        finance_topic = re.compile(
            r"\b(finance|financial|invest(?:ment|ing|or|ors)?|stock(?:s)?|share(?:s)?|equity|securities|"
            r"market(?:s)?|nifty|sensex|nse|bse|ipo|fpo|listing|earnings|revenue|profit(?:s|ability)?|"
            r"loss(?:es)?|quarterly results|q[1-4] results|balance sheet|cash flow|valuation|dividend|buyback|"
            r"mutual funds?|portfolio|asset management|fund manager|hedge fund|private equity|venture capital|"
            r"capital markets?|bond(?:s| yields?)?|interest rates?|inflation|gdp|monetary policy|fiscal policy|"
            r"rupee|currency|forex|rbi|sebi|bank(?:s|ing)?|nbfc|lending|credit|insurance|fintech|econom(?:y|ic|ics)|"
            r"capex|order book|guidance|margins?|exports?|imports?|trade deficit|trade policy|manufactur(?:e|ing|er|ers)|"
            r"industrial|industry|industries|energy sector|power sector|infrastructure|real estate|pharma(?:ceutical)?|"
            r"automotive|auto sector|telecom|technology sector|startups?|merger|acquisition|m&a|funding|fundraising|"
            r"business model|supply chain|commodit(?:y|ies)|crude oil|gold prices?|steel prices?|commodity prices?|"
            r"economic growth|jobs report|market outlook|market strategy|investment thesis|shareholder returns|"
            r"business growth|company results|corporate results|unicorn|valuation|private markets?)\b",
            re.I,
        )
        hard_off_topic = re.compile(
            r"\b(protests?|demonstrations?|dharnas?|political rally|politics|political|party workers?|election campaign|"
            r"campaign rally|murder|rape|kidnap(?:ping)?|crime thriller|celebrity gossip|celebrit(?:y|ies)|actors?|"
            r"actresses?|films?|movies?|film review|movie review|cricket highlights|football highlights|wedding ceremony|"
            r"religious sermon|celebrity interview|movie interview)\b",
            re.I,
        )
        title_has_finance_topic = bool(finance_topic.search(title_text))
        if hard_off_topic.search(title_text) and not title_has_finance_topic:
            return False
        if title_has_finance_topic:
            return True
        # Descriptions may confirm financial substance, but only for a title that is
        # explicitly an interview/conversation or identifies a business/finance guest.
        description_has_finance_topic = bool(finance_topic.search(description_text))
        interview_or_business_context = re.compile(
            r"\b(interview|conversation|dialogue|podcast|fireside chat|discussion with|talks? with|speaks? with|"
            r"ceo|cfo|coo|cto|founder|co-founder|chairman|chairperson|managing director|economist|investor|"
            r"analyst|fund manager|portfolio manager|executive|business leader|entrepreneur|management|"
            r"company|companies|corporate|startup|industry|sector)\b",
            re.I,
        )
        return description_has_finance_topic and bool(interview_or_business_context.search(title_text))
    @staticmethod
    def _is_interview_title(title: object, description: object = "", channel_title: object = "") -> bool:
        """Classify actual interview/conversation formats, not every news clip.
        A finance publisher's name alone never qualifies a video. Explicit format
        language is strongest; otherwise require both a real conversation cue and a
        guest/leadership cue. "Exclusive" alone is not an interview signal.
        """
        title_text = " ".join(str(title or "").split())
        description_text = " ".join(str(description or "").split())[:1800]
        combined = f"{title_text} {description_text}"
        if not title_text:
            return False
        explicit = re.compile(
            r"\b(interview|in conversation|conversation with|fireside chat|one[- ]on[- ]one|"
            r"q\s*&\s*a|exclusive interaction|face to face|chat with|talks? to|speaks? with|"
            r"interacts? with|meet the management|management interaction|special interaction|"
            r"podcast with|the big interview|global dialogues?|dialogue with|interaction with|"
            r"management interview|earnings call with|ask me anything)\b",
            re.I,
        )
        if explicit.search(title_text):
            return True
        guest_or_role = bool(re.search(
            r"\b(ceo|cfo|coo|cto|md|managing director|founder|co-founder|chairman|"
            r"chairperson|promoter|analyst|fund manager|portfolio manager|economist|"
            r"investor|executive|business leader|entrepreneur|chief executive|leadership|"
            r"management|expert|guest|entrepreneur)\b", combined, re.I,
        ))
        conversational = bool(re.search(
            r"\b(discusses?|speaks?|talks?|shares? (?:his|her|their)? ?views?|answers?|"
            r"explains?|on the outlook|on results|on earnings|on the business|"
            r"question(?:s)? and answers?|conversation|dialogue|discussion with|"
            r"interacts?|interviewed|interviewing|responds? to questions)\b", combined, re.I,
        ))
        # Do not count a publisher channel name or "exclusive" as a cue.
        return guest_or_role and conversational
    @classmethod
    def _is_plausible_business_candidate(cls, title: object, description: object = "") -> bool:
        """Permissive discovery only; final pipeline applies the two user filters.

        Do not reject a video based on generic business/interview heuristics here:
        role keywords may occur only in the transcript or description, and company
        relevance is determined against the approved Coverage List downstream.
        """
        return any(" ".join(str(value or "").split()) for value in (title, description))

    @classmethod
    def _is_finance_interview(cls, title: object, description: object = "", channel_title: object = "") -> bool:
        """Strict gate. Keeps only:
          1. Interviews: an interview format in the TITLE plus an insider role in the
             TITLE, or a known insider name (YOUTUBE_INSIDER_NAMES) in title/description.
          2. Earnings calls and quarterly results coverage, judged from the TITLE.
        Everything else is rejected: explainers, market wraps, news clips, politics,
        and entertainment. The description is never used to qualify an interview by
        itself, and channel names never qualify a video.
        """
        title_text = " ".join(str(title or "").split())
        if not title_text or OFF_TOPIC_RE.search(title_text) or MARKET_COMMENTARY_RE.search(title_text):
            return False
        if RESULTS_TITLE_RE.search(title_text):
            return True
        if not INTERVIEW_TITLE_RE.search(title_text):
            return False
        if INSIDER_TITLE_RE.search(title_text):
            return True
        if not INSIDER_NAMES:
            return False
        lowered = f"{title_text} {' '.join(str(description or '').split())[:1800]}".casefold()
        return any(name in lowered for name in INSIDER_NAMES)

    @staticmethod
    def _video_id_from_url(url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.hostname and parsed.hostname.endswith("youtu.be"):
            return parsed.path.strip("/").split("/", 1)[0] or None
        if parsed.path == "/watch":
            from urllib.parse import parse_qs
            return parse_qs(parsed.query).get("v", [None])[0]
        match = re.search(r"(?:v=|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})", url)
        return match.group(1) if match else None
    @classmethod
    def _parse_atom(
        cls, xml: str, channel_name: str, *, diagnostics: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Parse recent finance interviews from Atom, recording exclusion reasons."""
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015",
            "media": "http://search.yahoo.com/mrss/",
        }
        if not channel_name or re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", str(channel_name).strip()):
            author = cls._clean(root.findtext("atom:author/atom:name", default="", namespaces=ns))
            feed_title = cls._clean(root.findtext("atom:title", default="", namespaces=ns))
            channel_name = author or re.sub(r"^Uploads from\s+", "", feed_title, flags=re.I) or channel_name or "YouTube channel"
        rows: list[dict[str, Any]] = []
        for entry in root.findall("atom:entry", ns):
            video = cls._clean(entry.findtext("yt:videoId", default="", namespaces=ns))
            title = cls._clean(entry.findtext("atom:title", default="", namespaces=ns))
            published_raw = cls._clean(entry.findtext("atom:published", default="", namespaces=ns))
            # Atom <updated> can change later and is not a trustworthy substitute.
            # Require an actual clock component so date-only values cannot pass a 72h gate.
            published_at = cls._parse_date(published_raw) if re.search(r"\d{2}:\d{2}", published_raw) else None
            description = entry.findtext("media:group/media:description", default="", namespaces=ns)
            stats = diagnostics if diagnostics is not None else None
            if not video or not title or published_at is None:
                if stats is not None:
                    stats["rejected_missing_exact_timestamp"] = int(stats.get("rejected_missing_exact_timestamp") or 0) + 1
                continue
            published_at = published_at.astimezone(timezone.utc)
            # Discovery is intentionally broad. The approval pipeline owns the
            # permitted date range, Coverage List match, noise and insider checks.
            if not cls._is_plausible_business_candidate(title, description):
                if stats is not None:
                    stats["rejected_not_business_candidate"] = int(stats.get("rejected_not_business_candidate") or 0) + 1
                logger.info(
                    "NEWSALERT_YOUTUBE_CANDIDATE decision=rejected video_id=%s title=%r upload_date=%s channel=%r company_match=%r reason=not_plausible_business_candidate",
                    video[:32], title[:240], published_at.isoformat(), str(channel_name or "YouTube")[:100], "not_checked",
                )
                continue
            if stats is not None:
                stats["discovered_candidates"] = int(stats.get("discovered_candidates") or 0) + 1
            rows.append({
                "title": title,
                "url": f"https://www.youtube.com/watch?v={video}",
                "published_at": published_at,
                "source": f"YouTube · {channel_name}",
                "summary": cls._clean(description)[:10000],
                "channel_title": channel_name,
                "video_id": video,
                "is_youtube": True,
                "is_interview": True,
                "content_type": "INTERVIEW",
                "source_key": "youtube",
                "_timestamp_exact": True,
                "transport": "youtube-atom-http",
            })
        return rows
    @staticmethod
    def _default_specs() -> list[tuple[str, str]]:
        configured = os.getenv("YOUTUBE_CHANNEL_IDS", "").strip()
        if configured:
            configured_specs = [(x.strip(), x.strip()) for x in configured.split(",") if x.strip()]
            # Keep the explicitly configured channels and add known financial
            # publisher defaults so a single custom channel does not accidentally
            # disable broad interview discovery.
            return list(dict.fromkeys([*configured_specs, *DEFAULT_CHANNELS, *DEFAULT_EXTRA_CHANNELS]))
        return [*DEFAULT_CHANNELS, *DEFAULT_EXTRA_CHANNELS]
    @staticmethod
    def _playwright_fetch_sync(url: str, timeout_ms: int) -> str:
        """Fetch a public YouTube page with a fresh, unauthenticated browser.
        This deliberately uses no YouTube Data API, API key, bearer token, or
        persistent browser profile. It reads only the page rendered for a public
        visitor and does not attempt to bypass a login, CAPTCHA, or challenge.
        """
        from playwright.sync_api import sync_playwright
        launch_attempts: list[dict[str, Any]] = []
        executable = os.getenv("PLAYWRIGHT_EXECUTABLE_PATH") or os.getenv("PLAYWRIGHT_BROWSER_EXECUTABLE")
        if executable:
            launch_attempts.append({"executable_path": executable, "headless": True})
        launch_attempts.extend((
            {"channel": "chrome", "headless": True},
            {"channel": "msedge", "headless": True},
            {"headless": True},
        ))
        launch_errors: list[str] = []
        with sync_playwright() as playwright:
            browser = None
            for options in launch_attempts:
                try:
                    browser = playwright.chromium.launch(**options)
                    break
                except Exception as exc:
                    launch_errors.append(f"{options}: {type(exc).__name__}: {str(exc)[:160]}")
            if browser is None:
                raise RuntimeError(
                    "Playwright could not launch Chrome/Edge/Chromium. Run "
                    "'python -m playwright install chromium'. " + " | ".join(launch_errors[-3:])
                )
            context = None
            try:
                context = browser.new_context(
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                    viewport={"width": 1440, "height": 1000},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(700)
                if response is not None and response.status >= 400:
                    raise RuntimeError(f"Public YouTube page returned HTTP {response.status}: {url}")
                html = page.content()
                if not html or len(html.strip()) < 20:
                    raise RuntimeError(f"Playwright returned an empty page for {url}")
                return html
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception as exc:
                        logger.debug("YOUTUBE_CONTEXT_CLOSE_FAILED url=%s error=%s", url, exc)
                try:
                    browser.close()
                except Exception as exc:
                    logger.debug("YOUTUBE_BROWSER_CLOSE_FAILED url=%s error=%s", url, exc)
    async def _fetch_text(self, url: str, *, timeout: float | None = None) -> str:
        """Fetch public YouTube content via HTTP first, then renderer fallbacks.
        Atom feeds never launch a browser unless ordinary HTTP and the lightweight
        BrowserClient fail. For HTML pages, an HTTP 200 consent/challenge shell is
        not considered a successful search result.
        """
        import httpx
        wait = max(4.0, min(float(timeout or self.timeout_seconds), 12.0))
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept": "text/html,application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        failures: list[str] = []
        self._record_transport("httpx-public")
        try:
            is_atom = (urlparse(url).path or "").casefold().endswith("/feeds/videos.xml")
            response = None
            for attempt in range(3 if is_atom else 1):
                async with httpx.AsyncClient(timeout=wait, follow_redirects=True, headers=headers) as client:
                    response = await client.get(url)
                if response.status_code not in (429, 500, 502, 503, 504) or attempt >= (2 if is_atom else 0):
                    break
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = min(4.0, max(0.25, float(retry_after))) if retry_after else min(2.0, 0.5 * (2 ** attempt))
                except (TypeError, ValueError):
                    delay = min(2.0, 0.5 * (2 ** attempt))
                logger.warning("YOUTUBE_HTTP_RETRY status=%s attempt=%s max_attempts=%s endpoint=%s delay_seconds=%.2f", response.status_code, attempt + 1, 3, "atom" if is_atom else "page", delay)
                await asyncio.sleep(delay)
            assert response is not None
            response.raise_for_status()
            body = response.text or ""
            if len(body.strip()) < 20:
                raise RuntimeError("HTTP returned an empty page")
            path = (urlparse(url).path or "").casefold()
            if path.endswith("/feeds/videos.xml"):
                ET.fromstring(body)  # Validate XML, but an empty Atom feed is still valid.
                return body
            lower = body.casefold()
            challenge_markers = ("before you continue to youtube", "consent.youtube.com", "unusual traffic", "confirm you’re not a bot", "confirm you're not a bot")
            if any(marker in lower for marker in challenge_markers):
                raise RuntimeError("HTTP returned a consent/challenge page")
            if "/results" in path:
                if any(marker in body for marker in ('"videoRenderer"', '"ytInitialData"', '"channelRenderer"')):
                    return body
                raise RuntimeError("HTTP search HTML contained no usable YouTube result renderer")
            if "/watch" in path:
                if any(marker in body for marker in ('datePublished', '"videoDetails"', 'property="og:title"', "property='og:title'")):
                    return body
                raise RuntimeError("HTTP watch page lacked usable video metadata")
            if any(marker in body for marker in ('"ytInitialData"', '"channelId"', '"externalId"', 'datePublished')):
                return body
            # Some public channel pages render valid, sparse metadata without those markers.
            if "youtube" in lower and len(body) > 5000:
                return body
            raise RuntimeError("HTTP page did not contain recognizable YouTube metadata")
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                # A missing channel/feed is a configuration problem, not a transient
                # fetch failure. Do not retry it through alternate transports.
                raise
            failures.append(f"httpx: {type(exc).__name__}: {str(exc)[:180]}")
        # Lightweight project fetcher is the second technique. It is useful when
        # direct httpx is denied but does not require a persistent browser profile.
        self._record_transport("BrowserClient-public")
        try:
            from ii_platform.browser.browser import BrowserClient
            body = await BrowserClient(timeout=min(wait, 8.0), retries=1).fetch(url)
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            body = str(body or "")
            if len(body.strip()) > 20:
                path = (urlparse(url).path or "").casefold()
                if path.endswith("/feeds/videos.xml"):
                    ET.fromstring(body)
                lower = body.casefold()
                if not any(marker in lower for marker in ("unusual traffic", "confirm you're not a bot", "before you continue to youtube")):
                    if path.endswith("/feeds/videos.xml") or any(x in body for x in ('"videoRenderer"', '"ytInitialData"', 'datePublished', '"channelId"', '"externalId"')) or len(body) > 5000:
                        return body
            failures.append("BrowserClient: response did not contain usable public metadata")
        except Exception as exc:
            failures.append(f"BrowserClient: {type(exc).__name__}: {str(exc)[:180]}")
        # Last fallback: a fresh unauthenticated Playwright browser, bounded per URL.
        self._record_transport("Playwright-public")
        try:
            timeout_ms = max(4000, min(int(wait * 1000), 12000))
            body = await asyncio.wait_for(
                asyncio.to_thread(self._playwright_fetch_sync, url, timeout_ms), timeout=wait + 2.0,
            )
            if not body or len(body.strip()) < 20:
                raise RuntimeError("Playwright returned an empty page")
            lower = body.casefold()
            if any(marker in lower for marker in ("unusual traffic", "confirm you're not a bot", "before you continue to youtube")):
                raise RuntimeError("Playwright returned a consent/challenge page")
            return body
        except Exception as exc:
            failures.append(f"Playwright: {type(exc).__name__}: {str(exc)[:180]}")
        raise RuntimeError("All public YouTube fetch techniques failed: " + " | ".join(failures))
    @classmethod
    def _channel_from_search_html(
        cls,
        html: str,
        requested_name: str,
    ) -> tuple[str | None, str | None]:
        """Resolve a channel by parsing channelRenderer objects from public HTML."""
        target = cls._normalise_name(str(requested_name or "").lstrip("@"))
        if not target:
            return None, None
        marker = '"channelRenderer":'
        candidates: list[tuple[int, str, str]] = []
        start = 0
        while True:
            idx = html.find(marker, start)
            if idx < 0:
                break
            obj_start = idx + len(marker)
            while obj_start < len(html) and html[obj_start].isspace():
                obj_start += 1
            if obj_start >= len(html) or html[obj_start] != "{":
                start = idx + len(marker)
                continue
            depth = 0
            in_string = False
            escaped = False
            end = None
            for pos in range(obj_start, len(html)):
                ch = html[pos]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = pos + 1
                        break
            if end is None:
                break
            try:
                renderer = json.loads(html[obj_start:end])
            except (json.JSONDecodeError, TypeError):
                renderer = None
            start = end
            if not isinstance(renderer, dict):
                continue
            channel_id = cls._clean(renderer.get("channelId"))
            if not channel_id.startswith("UC"):
                continue
            title_obj = renderer.get("title") or {}
            if isinstance(title_obj, dict):
                title = cls._clean(title_obj.get("simpleText"))
                if not title:
                    title = cls._clean(" ".join(
                        str(run.get("text") or "")
                        for run in (title_obj.get("runs") or [])
                        if isinstance(run, dict)
                    ))
            else:
                title = cls._clean(title_obj)
            if not title:
                continue
            normalized_title = cls._normalise_name(title)
            score = (
                100 if normalized_title == target
                else 90 if target in normalized_title or normalized_title in target
                else 0
            )
            if score:
                candidates.append((score, channel_id, title))
        if not candidates:
            return None, None
        candidates.sort(key=lambda row: (-row[0], len(row[2]), row[2].casefold()))
        _, channel_id, title = candidates[0]
        return channel_id, title
    async def _resolve_channel_spec(self, spec: str) -> tuple[str | None, str | None]:
        cleaned = self._clean(spec)
        if not cleaned:
            return None, None
        if cleaned.startswith("UC") and len(cleaned) >= 24:
            return cleaned, CHANNEL_NAME_BY_ID.get(cleaned)
        # Known built-in channels are resolved without another HTTP request.
        normalized_spec = self._normalise_name(cleaned.lstrip("@"))
        for channel_id, channel_name in DEFAULT_CHANNELS:
            if normalized_spec == self._normalise_name(channel_name):
                return channel_id, channel_name
        parsed = urlparse(cleaned)
        direct = re.search(r"(?:^|/)(UC[A-Za-z0-9_-]{20,})(?:/|$)", cleaned)
        if direct:
            cid = direct.group(1)
            return cid, CHANNEL_NAME_BY_ID.get(cid)
        channel_url = cleaned
        if not parsed.scheme:
            if cleaned.startswith("@"):
                channel_url = f"https://www.youtube.com/{cleaned}"
            else:
                channel_url = f"https://www.youtube.com/@{cleaned.replace(' ', '')}"
        # Resolve channel identity from public page metadata via Playwright.
        try:
            html = await self._fetch_text(channel_url, timeout=self.CHANNEL_PAGE_TIMEOUT)
            channel_id = self._channel_id_from_html(html)
            if channel_id:
                return channel_id, self._channel_title_from_html(html) or CHANNEL_NAME_BY_ID.get(channel_id)
        except Exception:
            pass
        # Final no-key fallback: use YouTube's public channel search page. This
        # handles names such as "CNBC Awaaz" and "Zee News" without inventing
        # @handles from spaces or relying on a video search result.
        try:
            search_url = (
                "https://www.youtube.com/results?search_query="
                + quote(f"channel {cleaned.lstrip('@')}")
            )
            html = await self._fetch_text(
                search_url,
                timeout=self.CHANNEL_PAGE_TIMEOUT,
            )
            channel_id, channel_title = (
                self._channel_from_search_html(
                    html,
                    cleaned,
                )
            )
            if channel_id:
                return channel_id, channel_title
        except Exception:
            pass
        return None, None
    async def resolve_channel_ids(self, channel_specs: Iterable[str] | None) -> list[tuple[str, str | None]]:
        configured_specs = [str(x).strip() for x in (channel_specs or []) if str(x).strip()]
        # A deployment's custom list must not suppress the maintained publisher
        # defaults; combine and de-duplicate while preserving operator priority.
        default_specs = [sid for sid, _ in self._default_specs()]
        specs = list(dict.fromkeys([*configured_specs, *default_specs]))
        resolved: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        bounded_specs = list(dict.fromkeys(specs))[:60]
        semaphore = asyncio.Semaphore(3)
        async def resolve(spec: str):
            async with semaphore:
                try:
                    channel_id, channel_name = await self._resolve_channel_spec(spec)
                    return spec, channel_id, channel_name, None
                except Exception as exc:
                    return spec, None, None, f"{type(exc).__name__}: {str(exc)[:180]}"
        results = await asyncio.gather(*(resolve(spec) for spec in bounded_specs))
        disabled_ids = {value.strip() for value in os.getenv("NEWSALERT_YOUTUBE_DISABLED_CHANNEL_IDS", "").split(",") if value.strip()}
        for spec, channel_id, channel_name, error in results:
            if channel_id in disabled_ids:
                self.last_diagnostics.setdefault("errors", []).append(
                    f"Skipping operator-disabled YouTube channel mapping: {channel_id} ({spec})"
                )
                continue
            if channel_id in self.INVALID_CHANNEL_IDS:
                self.last_diagnostics.setdefault("errors", []).append(
                    f"Skipping previously 404-confirmed YouTube channel mapping: {channel_id} ({spec})"
                )
                continue
            if channel_id and channel_id not in seen:
                seen.add(channel_id)
                resolved.append((channel_id, channel_name or CHANNEL_NAME_BY_ID.get(channel_id) or spec))
            elif not channel_id:
                message = f"Channel spec could not be resolved from public pages: {spec}"
                if error:
                    message += f" ({error})"
                self.last_diagnostics.setdefault("errors", []).append(message)
        return resolved
    async def _fetch_rss(self, channel_id: str, channel_name: str, *, days: int = MAX_LOOKBACK_DAYS) -> list[dict[str, Any]]:
        """Fetch the public channel Atom feed with HTTP-first fallbacks and a 72h gate."""
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={quote(channel_id)}"
        self._record_transport("YouTube-Atom-feed")
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", str(channel_id or "")):
            raise ValueError("Invalid YouTube channel ID format; expected UC-prefixed channel ID")
        try:
            content = await self._fetch_text(url, timeout=min(self.timeout_seconds, 8.0))
        except Exception as exc:
            message = str(exc)
            if "404" in message or "Not Found" in message:
                invalid = self.last_diagnostics.setdefault("invalid_channel_ids", [])
                if channel_id not in invalid:
                    invalid.append(channel_id)
                self.INVALID_CHANNEL_IDS.add(channel_id)
                logger.warning("YOUTUBE_CHANNEL_MAPPING_INVALID channel=%s channel_id=%s reason=atom_404 action=suppress_until_worker_restart", channel_name[:100], channel_id)
            raise
        atom_stats = self.last_diagnostics.setdefault("atom_eligibility", {
            "accepted": 0,
            "rejected_not_interview": 0,
            "rejected_not_finance_related": 0,
            "rejected_stale_or_future": 0,
            "rejected_missing_exact_timestamp": 0,
        })
        rows = self._parse_atom(content, channel_name, diagnostics=atom_stats)
        # Preserve exact-timestamp candidates outside the active window too; the
        # approval pipeline rejects them with a per-video reason instead of silently
        # dropping them here. The Atom feed itself is naturally bounded to recent uploads.
        rows.sort(key=lambda row: row["published_at"], reverse=True)
        return rows[:MAX_VIDEOS_PER_CHANNEL]
    async def _fetch_channel_videos_page(self, channel_id: str, channel_name: str) -> list[dict[str, Any]]:
        """Scrape a channel's public /videos page using Playwright.
        YouTube embeds recent upload metadata in ytInitialData. We only reject an
        explicit channel mismatch; a missing owner browseId is allowed because
        YouTube omits it on some renderer variants.
        """
        url = f"https://www.youtube.com/channel/{quote(channel_id, safe='')}/videos"
        self._record_transport("YouTube-channel-videos-page")
        try:
            html = await self._fetch_text(url, timeout=self.CHANNEL_PAGE_TIMEOUT)
        except Exception as exc:
            self.last_diagnostics.setdefault("errors", []).append(
                f"channel videos page {channel_name}: {type(exc).__name__}: {str(exc)[:160]}"
            )
            return []
        rows: list[dict[str, Any]] = []
        page_stats = self.last_diagnostics.setdefault("channel_page_eligibility", {
            "accepted": 0,
            "rejected_not_interview": 0,
            "rejected_not_finance_related": 0,
        })
        for renderer in self._extract_video_renderers(html):
            item = self._video_from_renderer(renderer, {channel_id}, diagnostics=page_stats)
            if item is None:
                continue
            # /videos is channel-scoped, so the channel name is more reliable than
            # renderer byline text when YouTube changes the latter's shape.
            item["channel_title"] = channel_name
            item["source"] = f"YouTube · {channel_name}"
            rows.append(item)
            if len(rows) >= 30:
                break
        logger.info(
            "YouTube channel-page fallback channel=%s id=%s rows=%s",
            channel_name,
            channel_id,
            len(rows),
        )
        return rows
    async def _fetch_exact_video_title(self, item: dict[str, Any]) -> dict[str, Any]:
        """Validate the stored title against public watch-page metadata when possible."""
        video_id = str(item.get("video_id") or "").strip()
        if not video_id:
            return item
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            html = await self._fetch_text(url, timeout=self.VIDEO_PAGE_TIMEOUT)
            soup = BeautifulSoup(html, "lxml")
            title = ""
            for attrs in ({"property": "og:title"}, {"name": "twitter:title"}):
                tag = soup.find("meta", attrs=attrs)
                if tag and tag.get("content"):
                    title = self._clean(tag.get("content"))
                    if title:
                        break
            if not title:
                for node in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
                    raw = node.string or node.get_text()
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    objs = payload if isinstance(payload, list) else [payload]
                    for obj in objs:
                        if isinstance(obj, dict):
                            title = self._clean(obj.get("headline") or obj.get("name"))
                            if title:
                                break
                    if title:
                        break
            if title:
                item["title"] = title[:1000]
            # Keep channel/description metadata from the Atom feed, which is stable.
        except Exception:
            pass
        return item
    @staticmethod
    def _parse_published_time(value: object) -> datetime | None:
        """Parse common YouTube relative/absolute published-time strings."""
        now = datetime.now(timezone.utc)
        text = " ".join(str(value or "").split()).strip().casefold()
        if not text:
            return None
        if text in {"just now", "now"}:
            return now
        match = re.search(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", text)
        if match:
            amount = int(match.group(1))
            unit = match.group(2)
            if unit == "second":
                return now - timedelta(seconds=amount)
            if unit == "minute":
                return now - timedelta(minutes=amount)
            if unit == "hour":
                return now - timedelta(hours=amount)
            if unit == "day":
                return now - timedelta(days=amount)
            if unit == "week":
                return now - timedelta(weeks=amount)
            if unit == "month":
                return now - timedelta(days=30 * amount)
            if unit == "year":
                return now - timedelta(days=365 * amount)
        for fmt in ("%b %d, %Y", "%b %d, %Y · %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text.title(), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None
    @classmethod
    def _extract_video_renderers(cls, html: str) -> list[dict[str, Any]]:
        """Extract videoRenderer JSON objects from ytInitialData without brittle regex."""
        marker = '"videoRenderer":{'
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            idx = html.find(marker, start)
            if idx < 0:
                break
            obj_start = idx + len('"videoRenderer":')
            depth = 0
            in_string = False
            escape = False
            end = None
            for pos in range(obj_start, len(html)):
                ch = html[pos]
                if in_string:
                    if escape:
                        escape = False
                    elif ch == "\\\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = pos + 1
                        break
            if end is None:
                break
            raw = html[obj_start:end]
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    rows.append(payload)
            except Exception:
                pass
            start = end
        return rows
    @classmethod
    def _video_from_renderer(cls, renderer: dict[str, Any], allowed_channel_ids: set[str], *, diagnostics: dict[str, Any] | None = None) -> dict[str, Any] | None:
        video_id = cls._clean(renderer.get("videoId"))
        if not video_id:
            return None
        def text_value(value: object) -> str:
            if isinstance(value, dict):
                if value.get("simpleText"):
                    return cls._clean(value.get("simpleText"))
                runs = value.get("runs") or []
                return cls._clean(" ".join(str(x.get("text") or "") for x in runs if isinstance(x, dict)))
            return cls._clean(value)
        title = text_value(renderer.get("title"))
        if not title:
            return None
        owner = text_value(renderer.get("ownerText"))
        owner_endpoint = (((renderer.get("ownerText") or {}).get("runs") or [{}])[0] or {}).get("navigationEndpoint") or {}
        browse_id = str(((owner_endpoint.get("browseEndpoint") or {}).get("browseId") or "")).strip()
        # YouTube's public search renderer does not always include owner browseId.
        # A missing browseId is not evidence that the video belongs to another
        # channel, especially when the query itself is presenter-targeted. Reject
        # only an explicit channel mismatch.
        if allowed_channel_ids and browse_id and browse_id not in allowed_channel_ids:
            return None
        snippets = renderer.get("detailedMetadataSnippets") or []
        description = ""
        if isinstance(snippets, list):
            chunks = []
            for snippet in snippets:
                if not isinstance(snippet, dict):
                    continue
                chunks.append(text_value(snippet.get("snippetText")))
            description = cls._clean(" ".join(chunks))
        published = text_value(renderer.get("publishedTimeText"))
        channel_title = text_value(renderer.get("longBylineText")) or owner
        if not cls._is_plausible_business_candidate(title, description):
            if diagnostics is not None:
                diagnostics["rejected_not_insider_interview"] = int(diagnostics.get("rejected_not_insider_interview") or 0) + 1
            return None
        published_at = cls._parse_published_time(published)
        # Relative text outside the configured lookback is dropped here.
        # here, before any watch-page metadata request is spent on it.
        if published_at is not None and published_at < datetime.now(timezone.utc) - timedelta(days=MAX_LOOKBACK_DAYS):
            return None
        return {
            "title": title,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "published_at": published_at,
            "source": f"YouTube · {channel_title or 'YouTube'}",
            "summary": description[:10000],
            "channel_title": channel_title or None,
            "channel_id": browse_id or None,
            "video_id": video_id,
            "is_youtube": True,
            "is_interview": True,
            "content_type": "INTERVIEW",
            "source_key": "youtube",
            "_timestamp_exact": False,
            "transport": "youtube-search-html",
        }
    @staticmethod
    def _exact_recent(row: dict[str, Any], *, now: datetime | None = None) -> bool:
        published = row.get("published_at")
        if not isinstance(published, datetime) or not row.get("_timestamp_exact"):
            return False
        now = now or datetime.now(timezone.utc)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        published = published.astimezone(timezone.utc)
        return now - timedelta(days=MAX_LOOKBACK_DAYS) <= published <= now
    async def _yt_dlp_search(
        self, query: str, *, max_results: int = 8, allowed_channel_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Use optional yt-dlp search as a bounded public discovery fallback."""
        if getattr(self, "_yt_dlp_unavailable", False):
            return []
        self._record_transport("yt-dlp-search")
        limit = max(1, min(int(max_results or 8), 12))
        args = [
            sys.executable, "-m", "yt_dlp", "--dump-single-json", "--flat-playlist",
            "--skip-download", "--no-warnings", "--playlist-end", str(limit),
            f"ytsearch{limit}:{query}",
        ]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=16.0)
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.communicate()
            self.last_diagnostics.setdefault("errors", []).append(f"yt-dlp search timed out: {query[:100]}")
            return []
        except Exception as exc:
            message = f"yt-dlp unavailable: {type(exc).__name__}: {str(exc)[:160]}"
            if "No module named" in str(exc) or "No module named" in message:
                self._yt_dlp_unavailable = True
            self.last_diagnostics.setdefault("errors", []).append(message)
            return []
        if proc.returncode != 0 and not stdout:
            message = (stderr.decode("utf-8", "replace") if stderr else "yt-dlp returned no output")[:200]
            if "No module named yt_dlp" in message or "No module named 'yt_dlp'" in message:
                self._yt_dlp_unavailable = True
            self.last_diagnostics.setdefault("errors", []).append(f"yt-dlp search failed: {message}")
            return []
        raw = stdout.decode("utf-8", errors="replace").strip()
        payload = None
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:
                for line in reversed(raw.splitlines()):
                    try:
                        payload = json.loads(line)
                        break
                    except Exception:
                        continue
        if not isinstance(payload, dict):
            return []
        entries = payload.get("entries") if isinstance(payload.get("entries"), list) else [payload]
        rows: list[dict[str, Any]] = []
        allowed = allowed_channel_ids or set()
        for entry in entries[:limit]:
            if not isinstance(entry, dict):
                continue
            video_id = self._clean(entry.get("id"))
            webpage_url = self._clean(entry.get("webpage_url") or entry.get("original_url"))
            if not webpage_url and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"
            video_id = video_id or self._video_id_from_url(webpage_url)
            if not video_id or not webpage_url:
                continue
            channel_id = self._clean(entry.get("channel_id"))
            if allowed and channel_id and channel_id not in allowed:
                continue
            timestamp = entry.get("release_timestamp") or entry.get("timestamp")
            try:
                published = datetime.fromtimestamp(float(timestamp), tz=timezone.utc) if timestamp is not None else None
            except (TypeError, ValueError, OverflowError, OSError):
                published = None
            title = self._clean(entry.get("title"))
            description = self._clean(entry.get("description"))
            channel = self._clean(entry.get("channel") or entry.get("uploader"))
            candidate = {
                "title": title, "url": f"https://www.youtube.com/watch?v={video_id}",
                "published_at": published, "summary": description[:10000],
                "channel_title": channel, "channel_id": channel_id or None,
                "video_id": video_id, "is_youtube": True, "is_interview": True,
                "content_type": "INTERVIEW", "source_key": "youtube",
                "source": f"YouTube · {channel or 'YouTube'}",
                "_timestamp_exact": published is not None, "transport": "yt-dlp-search",
            }
            if title and self._is_plausible_business_candidate(title, description):
                rows.append(candidate)
        return rows
    async def _search_engine_video_urls(self, query: str, *, max_results: int = 8) -> list[str]:
        """Last discovery fallback: find indexed public YouTube URLs via Google/Bing."""
        import httpx
        self._record_transport("Google/Bing-index-search")
        search_queries = [
            ("https://www.google.com/search?" + urlencode({"q": f"site:youtube.com/watch {query}", "num": "20"})),
            ("https://www.bing.com/search?" + urlencode({"q": f"site:youtube.com/watch {query}", "count": "20"})),
        ]
        found: list[str] = []
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36", "Accept-Language": "en-IN,en;q=0.9"}
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True, headers=headers) as client:
            for search_url in search_queries:
                try:
                    response = await client.get(search_url)
                    response.raise_for_status()
                    soup = BeautifulSoup(response.text, "html.parser")
                    for anchor in soup.select("a[href]"):
                        href = unquote(str(anchor.get("href") or ""))
                        candidates = [href]
                        parsed = urlparse(href)
                        if parsed.query:
                            from urllib.parse import parse_qs
                            values = parse_qs(parsed.query)
                            candidates.extend(values.get("q", [])); candidates.extend(values.get("url", []))
                        for value in candidates:
                            value = unquote(value)
                            if not ("youtube.com/watch" in value or "youtu.be/" in value or "youtube.com/shorts/" in value):
                                continue
                            video_id = self._video_id_from_url(value)
                            if not video_id:
                                continue
                            canonical = f"https://www.youtube.com/watch?v={video_id}"
                            if canonical not in found:
                                found.append(canonical)
                            if len(found) >= max(1, int(max_results)):
                                return found
                except Exception as exc:
                    self.last_diagnostics.setdefault("errors", []).append(f"search-engine video discovery: {type(exc).__name__}: {str(exc)[:130]}")
        return found
    async def _discover_recent_interviews(
        self, query: str, *, allowed_channel_ids: set[str] | None = None, max_results: int = 8,
    ) -> list[dict[str, Any]]:
        """True waterfall: YouTube HTML -> yt-dlp -> Google/Bing, validating after each.
        A source returning candidate URLs is not counted as successful until each
        accepted result has exact, recent upload metadata.
        """
        allowed = allowed_channel_ids or set()
        desired = max(1, min(int(max_results or 8), 10))
        errors = self.last_diagnostics.setdefault("errors", [])
        output: dict[str, dict[str, Any]] = {}
        discovery_stats = {
            "query": str(query)[:240], "youtube_html_candidates": 0,
            "yt_dlp_candidates": 0, "search_engine_urls": 0,
            "accepted_recent_interviews": 0, "rejected_unknown_or_stale_time": 0,
        }
        self.last_diagnostics.setdefault("search_discoveries", []).append(discovery_stats)
        now = datetime.now(timezone.utc)
        semaphore = asyncio.Semaphore(4)
        async def validate(candidates: Iterable[dict[str, Any]], transport: str) -> None:
            candidate_rows = list(candidates)[:max(1, desired)]
            async def one(candidate: dict[str, Any]) -> None:
                url = self._clean(candidate.get("url"))
                video_id = self._video_id_from_url(url)
                if not video_id:
                    return
                url = f"https://www.youtube.com/watch?v={video_id}"
                row = candidate
                if not self._exact_recent(row, now=now):
                    async with semaphore:
                        try:
                            exact = await self.fetch_video_metadata(url)
                        except Exception as exc:
                            errors.append(f"video metadata {video_id}: {type(exc).__name__}: {str(exc)[:100]}")
                            exact = None
                    if exact is None:
                        return
                    if not exact.get("channel_id"):
                        exact["channel_id"] = candidate.get("channel_id")
                    # Keep source/search context when watch metadata is intentionally sparse.
                    if not exact.get("channel_title"):
                        exact["channel_title"] = candidate.get("channel_title")
                    if not exact.get("summary"):
                        exact["summary"] = candidate.get("summary") or candidate.get("description") or ""
                    exact["transport"] = f"{transport}+watch-metadata"
                    row = exact
                channel_id = self._clean(row.get("channel_id"))
                if allowed and channel_id and channel_id not in allowed:
                    return
                if not self._exact_recent(row, now=now):
                    discovery_stats["rejected_unknown_or_stale_time"] += 1
                    eligibility = self.last_diagnostics.setdefault("eligibility", {})
                    eligibility["rejected_stale_or_unknown_time"] = int(eligibility.get("rejected_stale_or_unknown_time") or 0) + 1
                    return
                title = row.get("title")
                summary = row.get("summary")
                channel_title = row.get("channel_title")
                if not self._is_plausible_business_candidate(title, summary):
                    eligibility = self.last_diagnostics.setdefault("eligibility", {})
                    eligibility["rejected_not_insider_interview"] = int(eligibility.get("rejected_not_insider_interview") or 0) + 1
                    return
                eligibility = self.last_diagnostics.setdefault("eligibility", {})
                eligibility["accepted"] = int(eligibility.get("accepted") or 0) + 1
                row["url"] = url
                row.setdefault("video_id", video_id)
                row.setdefault("source", f"YouTube · {row.get('channel_title') or 'YouTube'}")
                row.setdefault("is_youtube", True)
                row.setdefault("is_interview", True)
                row.setdefault("content_type", "INTERVIEW")
                row.setdefault("source_key", "youtube")
                row.setdefault("transport", transport)
                output[url] = row
                discovery_stats["accepted_recent_interviews"] += 1
            await asyncio.gather(*(one(candidate) for candidate in candidate_rows), return_exceptions=True)
        # Technique 1: YouTube public search. _fetch_text itself tries HTTP, the
        # project BrowserClient, then an unauthenticated Playwright renderer.
        candidates: list[dict[str, Any]] = []
        try:
            html = await self._fetch_search_page(query)
            page_stats = self.last_diagnostics.setdefault("search_page_eligibility", {
                "accepted": 0,
                "rejected_not_interview": 0,
                "rejected_not_finance_related": 0,
            })
            for renderer in self._extract_video_renderers(html):
                row = self._video_from_renderer(renderer, allowed, diagnostics=page_stats)
                if row and row.get("url"):
                    candidates.append(row)
            discovery_stats["youtube_html_candidates"] += len(candidates)
        except Exception as exc:
            errors.append(f"YouTube HTML search: {type(exc).__name__}: {str(exc)[:160]}")
        await validate(candidates, "youtube-html-search")
        # Technique 2: yt-dlp search if the first technique did not fill the result budget.
        if len(output) < desired:
            try:
                candidates = await self._yt_dlp_search(query, max_results=desired, allowed_channel_ids=allowed)
                discovery_stats["yt_dlp_candidates"] += len(candidates)
                await validate(candidates, "yt-dlp-search")
            except Exception as exc:
                errors.append(f"yt-dlp fallback: {type(exc).__name__}: {str(exc)[:160]}")
        # Technique 3: Google/Bing index search, only as needed after validated output.
        if len(output) < desired:
            try:
                urls = await self._search_engine_video_urls(query, max_results=min(desired, 5))
                discovery_stats["search_engine_urls"] += len(urls)
                candidates = [{"url": url, "video_id": self._video_id_from_url(url), "transport": "search-engine-index"} for url in urls]
                # Avoid spending a second metadata fetch on candidates already rejected.
                candidates = [row for row in candidates if str(row.get("url")) not in output]
                await validate(candidates, "search-engine-index")
            except Exception as exc:
                errors.append(f"search-engine fallback: {type(exc).__name__}: {str(exc)[:160]}")
        rows = sorted(output.values(), key=lambda row: row["published_at"], reverse=True)
        return rows[:desired]
    async def _fetch_search_page(self, query: str) -> str:
        """Fetch a public YouTube search page through the bounded fetch waterfall."""
        url = "https://www.youtube.com/results?search_query=" + quote(query)
        return await self._fetch_text(url, timeout=self.CHANNEL_PAGE_TIMEOUT)
    async def search_company_presenter_videos(
        self,
        company_names: Iterable[str] | None,
        presenter_names: Iterable[str] | None,
        *,
        channel_ids: Iterable[str] | None = None,
        max_results_per_query: int = 10,
        max_queries: int = 250,
        concurrency: int = 6,
        days: int = MAX_LOOKBACK_DAYS,
    ) -> list[dict[str, Any]]:
        """Search company/presenter combinations using the complete fallback chain."""
        companies = [self._clean(v) for v in (company_names or []) if self._clean(v)]
        presenters = [self._clean(v) for v in (presenter_names or []) if self._clean(v)]
        if not companies or not presenters:
            return []
        allowed_ids = {str(x).strip() for x in (channel_ids or []) if str(x).strip()}
        queries: list[tuple[str, str]] = []
        seen: set[str] = set()
        budget = max(1, min(int(max_queries or 20), 30))
        for company in companies:
            for presenter in presenters:
                query = f"{company} {presenter} interview"
                key = self._normalise_name(query)
                if key and key not in seen:
                    seen.add(key); queries.append((query, company))
                if len(queries) >= budget: break
            if len(queries) >= budget: break
        semaphore = asyncio.Semaphore(max(1, min(int(concurrency or 4), 5)))
        rows: dict[str, dict[str, Any]] = {}
        async def run(query: str, company: str) -> None:
            async with semaphore:
                self.last_diagnostics["search_queries_attempted"] = int(self.last_diagnostics.get("search_queries_attempted") or 0) + 1
                candidates = await self._discover_recent_interviews(query, allowed_channel_ids=allowed_ids, max_results=max_results_per_query)
                company_norm = self._normalise_name(company)
                tokens = [t for t in company_norm.split() if len(t) >= 3]
                for row in candidates:
                    hay = self._normalise_name(f"{row.get('title','')} {row.get('summary','')} {row.get('channel_title','')}")
                    if company_norm and company_norm not in hay and tokens and not any(token in hay for token in tokens):
                        continue
                    row["_youtube_company_search"] = True
                    rows.setdefault(str(row.get("url")), row)
        await asyncio.gather(*(run(query, company) for query, company in queries), return_exceptions=True)
        output = sorted(rows.values(), key=lambda row: row["published_at"], reverse=True)
        self.last_diagnostics["candidate_videos"] = max(int(self.last_diagnostics.get("candidate_videos") or 0), len(output))
        logger.info("YOUTUBE_COMPANY_PRESENTER_WATERFALL queries=%s rows=%s", len(queries), len(output))
        return output
    async def search_presenter_videos(
        self,
        presenter_names: Iterable[str] | None,
        *,
        channel_ids: Iterable[str] | None = None,
        max_results: int = 50,
        days: int = MAX_LOOKBACK_DAYS,
    ) -> list[dict[str, Any]]:
        """Discover interviews by presenter using HTML, yt-dlp, and indexed search."""
        names = [self._clean(v) for v in (presenter_names or []) if self._clean(v)]
        if not names:
            return []
        allowed = {str(x).strip() for x in (channel_ids or []) if str(x).strip()}
        results: dict[str, dict[str, Any]] = {}
        semaphore = asyncio.Semaphore(3)
        async def run(name: str) -> None:
            async with semaphore:
                self.last_diagnostics["search_queries_attempted"] = int(self.last_diagnostics.get("search_queries_attempted") or 0) + 1
                rows = await self._discover_recent_interviews(f"{name} interview", allowed_channel_ids=allowed, max_results=min(15, max_results))
                tokens = [t for t in self._normalise_name(name).split() if len(t) >= 4]
                for row in rows:
                    hay = self._normalise_name(f"{row.get('title','')} {row.get('summary','')} {row.get('channel_title','')}")
                    if tokens and not any(token in hay for token in tokens):
                        continue
                    results.setdefault(str(row.get("url")), row)
        await asyncio.gather(*(run(name) for name in names), return_exceptions=True)
        rows = sorted(results.values(), key=lambda row: row["published_at"], reverse=True)
        self.last_diagnostics["candidate_videos"] = max(int(self.last_diagnostics.get("candidate_videos") or 0), len(rows))
        return rows[:max(1, int(max_results))]
    async def fetch_video_metadata(
        self, url: str, *, allow_stale: bool = False, allow_any_title: bool = False
    ) -> dict[str, Any] | None:
        """Fetch exact video metadata; optional flags support curated external feeds."""
        video_id = self._video_id_from_url(str(url or "").strip())
        if not video_id:
            return None
        lock = self._metadata_locks.setdefault(video_id, asyncio.Lock())
        async with lock:
            if video_id in self._metadata_cache:
                cached = self._metadata_cache[video_id]
                self.last_diagnostics["metadata_cache_hits"] = int(self.last_diagnostics.get("metadata_cache_hits") or 0) + 1
                return dict(cached) if cached else None
            self.last_diagnostics["metadata_requests"] = int(self.last_diagnostics.get("metadata_requests") or 0) + 1
            result = await self._fetch_video_metadata_uncached(
                url, allow_stale=allow_stale, allow_any_title=allow_any_title
            )
            self._metadata_cache[video_id] = dict(result) if result else None
            return dict(result) if result else None
    async def _fetch_video_metadata_uncached(
        self, url: str, *, allow_stale: bool = False, allow_any_title: bool = False
    ) -> dict[str, Any] | None:
        """Fetch exact metadata for one known YouTube video URL.
        Used for YouTube links embedded in an allowlisted person's X post. This is
        intentionally limited to the small number of linked videos rather than
        opening every upload on a channel.
        """
        video_id = self._video_id_from_url(str(url or "").strip())
        if not video_id:
            return None
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            html = await self._fetch_text(watch_url, timeout=min(self.timeout_seconds, 10.0))
        except Exception as exc:
            logger.debug("YOUTUBE_WATCH_METADATA_FAILED video=%s error=%s", video_id, exc)
            return None
        soup = BeautifulSoup(html, "lxml")
        channel_id_match = re.search(r'"(?:channelId|externalId)"\s*:\s*"(UC[A-Za-z0-9_-]{20,})"', html)
        channel_id = channel_id_match.group(1) if channel_id_match else None
        title = ""
        channel_title = ""
        description = ""
        published_at = None
        for attrs in (
            {"property": "og:title"},
            {"name": "twitter:title"},
        ):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                title = self._clean(tag.get("content"))
                if title:
                    break
        for node in soup.find_all(
            "script",
            attrs={"type": re.compile(r"application/ld\+json", re.I)},
        ):
            raw = node.string or node.get_text()
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            objs = payload if isinstance(payload, list) else [payload]
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                if not title:
                    title = self._clean(
                        obj.get("headline")
                        or obj.get("name")
                    )
                if not published_at:
                    value = obj.get("datePublished")
                    if value and re.search(r"\d{2}:\d{2}", str(value)):
                        published_at = self._parse_date(value)
                if not description:
                    description = self._clean(obj.get("description") or obj.get("abstract"))
                author = obj.get("author")
                if isinstance(author, dict):
                    channel_title = self._clean(
                        author.get("name")
                    )
                if title and published_at:
                    break
        now = datetime.now(timezone.utc)
        if published_at is not None:
            published_at = published_at.astimezone(timezone.utc)
        outside_lookback = not (now - timedelta(days=MAX_LOOKBACK_DAYS) <= published_at <= now) if published_at else True
        implausible_title = not self._is_plausible_business_candidate(title, description)
        if (not title or published_at is None or (outside_lookback and not allow_stale)
                or (implausible_title and not allow_any_title)):
            # Standard YouTube discovery rejects stale/implausible videos. Curated
            # external sources can opt out without changing the normal YouTube path.
            return None
        return {
            "title": title[:1000],
            "url": watch_url,
            "published_at": published_at,
            "source": f"YouTube · {channel_title or 'YouTube'}",
            "summary": description[:10000],
            "channel_title": channel_title or None,
            "channel_id": channel_id,
            "video_id": video_id,
            "is_youtube": True,
            "is_interview": True,
            "content_type": "INTERVIEW",
            "source_key": "youtube",
            "_timestamp_exact": True,
            "transport": "youtube-watch-metadata",
        }
    async def search_channels(
        self,
        channel_specs: Iterable[str] | None,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        days: int = MAX_LOOKBACK_DAYS,
    ) -> list[dict[str, Any]]:
        """Collect recent interview-like videos using Atom -> channel HTML -> search.
        The Atom feed existed but was not called by the old main collection path;
        this made every run depend on Playwright rendering channel pages successfully.
        """
        days = max(1, min(int(days or MAX_LOOKBACK_DAYS), 10))
        specs = [str(value).strip() for value in (channel_specs or []) if str(value).strip()]
        self.last_diagnostics = {
            "configured_specs": len(specs) or len(self._default_specs()),
            "resolved_channels": 0,
            "feeds_attempted": 0,
            "channel_pages_attempted": 0,
            "candidate_videos": 0,
            "search_queries_attempted": 0,
            "errors": [],
            "transport": "public-atom+playwright-html",
            "api_used": False,
            "credentials_required": False,
            "max_age_days": MAX_LOOKBACK_DAYS,
            "eligibility": {"accepted": 0, "rejected_not_interview": 0, "rejected_not_finance_related": 0, "rejected_stale_or_unknown_time": 0},
            "atom_eligibility": {"accepted": 0, "rejected_not_interview": 0, "rejected_not_finance_related": 0, "rejected_stale_or_future": 0, "rejected_missing_exact_timestamp": 0},
            "channel_page_eligibility": {"accepted": 0, "rejected_not_interview": 0, "rejected_not_finance_related": 0},
            "search_page_eligibility": {"accepted": 0, "rejected_not_interview": 0, "rejected_not_finance_related": 0},
        }
        resolved = await self.resolve_channel_ids(channel_specs)
        self.last_diagnostics["resolved_channels"] = len(resolved)
        if not resolved:
            self.last_diagnostics["errors"].append("No configured YouTube channels could be resolved from public pages")
            logger.error("YOUTUBE_CHANNEL_RESOLUTION_EMPTY specs=%s", self.last_diagnostics["configured_specs"])
            return []
        max_results = max(1, min(int(max_results or self.DEFAULT_MAX_RESULTS), 100))
        per_channel = max(1, min(max_results, 30, MAX_VIDEOS_PER_CHANNEL))
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        now_limit = now
        async def fetch_channel(channel_id: str, channel_name: str) -> list[dict[str, Any]]:
            candidates: dict[str, dict[str, Any]] = {}
            # First choice: public Atom feed. Keep valid RSS results even if later HTML
            # rendering is blocked or a browser cleanup raises.
            try:
                self.last_diagnostics["feeds_attempted"] += 1
                for row in await self._fetch_rss(channel_id, channel_name, days=days):
                    published = row.get("published_at")
                    url = str(row.get("url") or "")
                    if url and isinstance(published, datetime) and cutoff <= published.astimezone(timezone.utc) <= now_limit:
                        candidates[url] = row
            except Exception as exc:
                self.last_diagnostics["errors"].append(
                    f"YouTube Atom {channel_name}: {type(exc).__name__}: {str(exc)[:200]}"
                )
            useful_threshold = min(per_channel, 8)
            if len(candidates) < useful_threshold:
                try:
                    self.last_diagnostics["channel_pages_attempted"] += 1
                    page_rows = await self._fetch_channel_videos_page(channel_id, channel_name)
                    async def enrich(row: dict[str, Any]):
                        url = str(row.get("url") or "")
                        exact = await self.fetch_video_metadata(url) if url else None
                        if not exact or not self._exact_recent(exact, now=now):
                            return None
                        if exact.get("channel_id") and exact.get("channel_id") != channel_id:
                            return None
                        exact["channel_title"] = exact.get("channel_title") or channel_name
                        exact["source"] = f"YouTube · {exact['channel_title']}"
                        exact["transport"] = "channel-videos-page+watch-metadata"
                        return url, exact
                    exact_candidates = await asyncio.gather(*(enrich(row) for row in page_rows[:3]), return_exceptions=True)
                    for exact_candidate in exact_candidates:
                        if isinstance(exact_candidate, tuple):
                            url, exact = exact_candidate
                            candidates[url] = exact
                except Exception as exc:
                    self.last_diagnostics["errors"].append(
                        f"YouTube channel page {channel_name}: {type(exc).__name__}: {str(exc)[:200]}"
                    )
            if len(candidates) < useful_threshold:
                try:
                    query = f'"{channel_name}" interview business management earnings in conversation podcast'
                    self.last_diagnostics["search_queries_attempted"] += 1
                    fallback_rows = await self._discover_recent_interviews(
                        query, allowed_channel_ids={channel_id}, max_results=per_channel,
                    )
                    for row in fallback_rows:
                        row["channel_title"] = row.get("channel_title") or channel_name
                        row["source"] = f"YouTube · {row['channel_title']}"
                        row["source_key"] = "youtube"
                        candidates[str(row["url"])] = row
                except Exception as exc:
                    self.last_diagnostics["errors"].append(
                        f"YouTube search waterfall {channel_name}: {type(exc).__name__}: {str(exc)[:200]}"
                    )
            eligible: list[dict[str, Any]] = []
            counts = self.last_diagnostics.setdefault("eligibility", {})
            for row in candidates.values():
                published = row.get("published_at")
                if isinstance(published, datetime):
                    published_utc = (published.replace(tzinfo=timezone.utc) if published.tzinfo is None
                                     else published.astimezone(timezone.utc))
                else:
                    published_utc = None
                if (not self._exact_recent(row, now=now)
                        or published_utc is None
                        or not cutoff <= published_utc <= now_limit):
                    counts["rejected_stale_or_unknown_time"] = int(counts.get("rejected_stale_or_unknown_time") or 0) + 1
                    continue
                if not self._is_plausible_business_candidate(row.get("title"), row.get("summary")):
                    if not self._is_interview_title(row.get("title"), row.get("summary"), row.get("channel_title")):
                        counts["rejected_not_interview"] = int(counts.get("rejected_not_interview") or 0) + 1
                    else:
                        counts["rejected_not_finance_related"] = int(counts.get("rejected_not_finance_related") or 0) + 1
                    continue
                counts["accepted"] = int(counts.get("accepted") or 0) + 1
                eligible.append(row)
            rows = sorted(eligible, key=lambda row: row["published_at"], reverse=True)
            return rows[:per_channel]
        # Keep collection concurrency bounded as the channel pool grows, to reduce
        # duplicate requests and avoid overwhelming public YouTube endpoints.
        channel_semaphore = asyncio.Semaphore(6)
        async def fetch_channel_bounded(channel_id: str, channel_name: str):
            async with channel_semaphore:
                return await fetch_channel(channel_id, channel_name)
        batches = await asyncio.gather(
            *(fetch_channel_bounded(channel_id, channel_name) for channel_id, channel_name in resolved),
            return_exceptions=True,
        )
        rows_by_url: dict[str, dict[str, Any]] = {}
        for (channel_id, channel_name), batch in zip(resolved, batches):
            if isinstance(batch, BaseException):
                self.last_diagnostics["errors"].append(
                    f"YouTube channel {channel_name}: {type(batch).__name__}: {str(batch)[:220]}"
                )
                continue
            for row in batch:
                url = str(row.get("url") or "").strip()
                if url and row.get("published_at"):
                    rows_by_url[url] = row
        results = sorted(rows_by_url.values(), key=lambda row: row["published_at"], reverse=True)
        self.last_diagnostics["candidate_videos"] = len(results)
        self.last_diagnostics["errors"] = self.last_diagnostics["errors"][:50]
        logger.info(
            "YOUTUBE_COLLECTION_DONE specs=%s resolved=%s atom=%s pages=%s search=%s candidates=%s errors=%s lookback_days=%s",
            self.last_diagnostics["configured_specs"], self.last_diagnostics["resolved_channels"],
            self.last_diagnostics["feeds_attempted"], self.last_diagnostics["channel_pages_attempted"],
            self.last_diagnostics["search_queries_attempted"], len(results),
            len(self.last_diagnostics["errors"]), days,
        )
        return results[:max_results]
    async def discover_insider_interviews(
        self,
        *,
        extra_names: Iterable[str] | None = None,
        max_queries: int = 40,
    ) -> list[dict[str, Any]]:
        """Find recent interviews with company insiders by searching YouTube.

        Complements the channel scan, which only sees the configured channels.
        Queries combine insider roles (CEO, MD, promoter, owner, etc.) and known
        insider names. Each candidate goes through _discover_recent_interviews,
        which enforces the configured exact upload-time window and the strict insider gate.
        """
        names = [*INSIDER_NAMES, *[str(n).strip() for n in (extra_names or []) if str(n).strip()]]
        queries: list[str] = [f"{role} interview India listed company" for role in INSIDER_SEARCH_ROLES]
        queries.extend(f"promoter interview {segment} India" for segment in ("banking", "auto", "pharma", "FMCG", "capital goods"))
        queries.extend(f"{quarter} results earnings call management India" for quarter in ("Q1 FY27", "Q2 FY27", "Q3 FY26", "Q4 FY26"))
        queries.extend([
            "concall highlights management commentary India",
            "quarterly results management interview India",
            "earnings call transcript highlights India company",
            "annual general meeting chairman speech India",
            "AGM 2026 shareholders chairman address India",
        ])
        queries.extend(f"{name} interview" for name in names)
        unique: list[str] = []
        seen: set[str] = set()
        for query in queries:
            key = query.casefold()
            if key not in seen:
                seen.add(key)
                unique.append(query)
        unique = unique[:max(1, min(int(max_queries or 40), 80))]

        semaphore = asyncio.Semaphore(3)
        found: dict[str, dict[str, Any]] = {}

        async def run(query: str) -> None:
            async with semaphore:
                self.last_diagnostics["search_queries_attempted"] = int(
                    self.last_diagnostics.get("search_queries_attempted") or 0
                ) + 1
                try:
                    rows = await self._discover_recent_interviews(query, max_results=8)
                except Exception as exc:
                    self.last_diagnostics.setdefault("errors", []).append(
                        f"insider search {query[:60]}: {type(exc).__name__}: {str(exc)[:120]}"
                    )
                    return
            for row in rows:
                url = str(row.get("url") or "")
                if url and isinstance(row.get("published_at"), datetime):
                    row.setdefault("_insider_query", query)
                    found.setdefault(url, row)

        await asyncio.gather(*(run(query) for query in unique), return_exceptions=True)
        results = sorted(found.values(), key=lambda row: row["published_at"], reverse=True)
        self.last_diagnostics["insider_search_candidates"] = len(results)
        categories = {"agm": 0, "results_or_concall": 0, "interview": 0}
        for row in results:
            title_text = str(row.get("title") or "")
            if re.search(r"\b(agm|egm|annual general meeting|shareholders?'? meeting)\b", title_text, re.I):
                categories["agm"] += 1
            elif RESULTS_TITLE_RE.search(title_text):
                categories["results_or_concall"] += 1
            else:
                categories["interview"] += 1
        self.last_diagnostics["insider_search_categories"] = categories
        logger.info("YOUTUBE_INSIDER_SEARCH queries=%s results=%s categories=%s", len(unique), len(results), categories)
        return results[:MAX_INSIDER_RESULTS]

    async def search_company(
        self,
        company: str,
        *,
        max_results: int = 10,
        channel_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not company:
            return []
        terms = [x for x in re.split(r"[^A-Za-z0-9]+", company) if len(x) >= 4]
        rows = await self.search_channels(channel_ids, max_results=max(max_results, 20))
        matched: list[dict[str, Any]] = []
        needle = self._normalise_name(company)
        for row in rows:
            hay = self._normalise_name(f"{row.get('title', '')} {row.get('summary', '')}")
            if needle and needle in hay:
                matched.append(row)
                continue
            if len(terms) >= 2 and all(self._normalise_name(t) in hay for t in terms[:3]):
                matched.append(row)
        return matched[:max_results]
