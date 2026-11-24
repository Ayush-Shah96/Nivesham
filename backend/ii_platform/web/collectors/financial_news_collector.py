import json
import hashlib
import re
import asyncio
import httpx
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse
import logging
import os
from datetime import date, time
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

from ii_platform.browser.browser import BrowserClient
from ii_platform.web.collectors.news_source_registry import NewsSourceProfile, enabled_sources

logger = logging.getLogger(__name__)

# Official NSE endpoints: RSS first, followed by bounded public API/toolkit/browser fallbacks.
NSE_ANNOUNCEMENTS_RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_ANNOUNCEMENTS_RSS_ALIASES = (
    NSE_ANNOUNCEMENTS_RSS,
    "https://archives.nseindia.com/content/RSS/Online_announcements.xml",
)
# NSE currently exposes the live/latest list on the application page; keep the
# original announcements route as a fallback because its table/API can vary by session.
NSE_LATEST_ANNOUNCEMENTS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-application?id=allAnnouncements"
NSE_ANNOUNCEMENTS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
NSE_ANNOUNCEMENTS_PAGE_ALIASES = (NSE_LATEST_ANNOUNCEMENTS_PAGE, NSE_ANNOUNCEMENTS_PAGE)
NSE_CORPORATE_ANNOUNCEMENTS_API = "https://www.nseindia.com/api/corporate-announcements"


def _nse_corporate_announcement_page_url(symbol: str = "", source_id: str = "") -> str:
    """Build a human-readable NSE announcement page link, not an XBRL download URL."""
    query: list[tuple[str, str]] = []
    clean_symbol = " ".join(str(symbol or "").split()).strip().upper()
    if clean_symbol:
        query.append(("symbol", clean_symbol))
    # NSE does not expose a stable public permalink for every RSS row. The stable
    # item token avoids collapsing same-company announcements in our own URL key;
    # the actual destination remains NSE's corporate-filings page.
    clean_id = " ".join(str(source_id or "").split()).strip()
    if clean_id:
        query.append(("newsalert_id", clean_id[:96]))
    return f"{NSE_ANNOUNCEMENTS_PAGE}?{urlencode(query, doseq=True)}" if query else NSE_ANNOUNCEMENTS_PAGE


NSE_LOCAL_TZ = ZoneInfo("Asia/Kolkata")
NSE_ACCESS_DENIAL_CODES = {401, 403, 429}
PUBLIC_FEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.5",
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
}

@dataclass(frozen=True)
class CollectionResult:
    source_key: str
    source_name: str
    attempted_urls: tuple[str, ...]
    items: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]


class FinancialNewsCollector:
    """Multi-source publisher/regulatory/exchange news collector.

    Strategy per source:
      1. configured/explicit RSS or Atom feed URLs
      2. RSS/Atom link discovery from official source pages
      3. article JSON-LD extraction from public section pages
      4. conservative article-card fallback

    The collector never requires one hard-coded RSS URL to remain valid.
    """

    def __init__(self, browser: BrowserClient | None = None) -> None:
        self.browser = browser or BrowserClient()

    async def extract_article_headline(self, url: str) -> str | None:
        """Extract the publisher's real article headline from a public article page.

        This is deliberately a fallback for malformed/issuer-only feed titles. It
        checks structured NewsArticle JSON-LD and standard OpenGraph/Twitter/H1
        fields without treating the article body as a headline.
        """
        url = str(url or '').strip()
        if not url:
            return None
        try:
            content = await self.browser.fetch(url)
        except Exception as exc:
            logger.debug("Headline repair fetch failed url=%s error=%s", url, exc)
            return None

        soup = BeautifulSoup(content, "lxml")

        # Prefer structured NewsArticle metadata.
        for node in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
            raw = node.string or node.get_text()
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            for obj in self._flatten_jsonld(payload):
                if not isinstance(obj, dict):
                    continue
                types = obj.get("@type")
                types = types if isinstance(types, list) else [types]
                if any(str(t) in {"NewsArticle", "Article", "ReportageNewsArticle"} for t in types):
                    headline = str(obj.get("headline") or obj.get("name") or "").strip()
                    if headline:
                        return self._clean(headline)[:1000]

        for attrs in (
            {"property": "og:title"},
            {"name": "twitter:title"},
            {"name": "headline"},
        ):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                headline = self._clean(tag.get("content", "")).strip()
                if headline:
                    return headline[:1000]

        heading = soup.find("h1")
        if heading:
            headline = self._clean(heading.get_text(" ", strip=True))
            if headline:
                return headline[:1000]
        return None

    @staticmethod
    def _google_news_fallback_url(source: NewsSourceProfile) -> str | None:
        """Public RSS fallback that keeps the requested publisher domain in results.

        Several publishers return 403 to server-side HTTP clients. Google News' public
        RSS search endpoint can still expose the publisher's article URLs without
        requiring login or bypassing a publisher challenge.
        """
        queries = {
            "economic_times": "site:economictimes.indiatimes.com (markets OR companies OR stocks OR economy OR industry)",
            "business_standard": "site:business-standard.com (markets OR companies OR stocks OR economy OR industry)",
            "livemint": "site:livemint.com (markets OR companies OR stocks OR economy OR industry)",
            "hindu_businessline": "site:thehindubusinessline.com (markets OR companies OR stocks OR economy OR industry)",
                "business_today": "site:businesstoday.in (markets OR business OR companies OR economy OR industry)",
            "moneycontrol": "site:moneycontrol.com (markets OR business OR india OR companies)",
            "ndtv_profit": "site:ndtvprofit.com (markets OR business OR earnings OR economy)",
            "bloomberg_india": "site:bloomberg.com (india OR asia OR markets OR economy)",
            "cnbc_tv18": "site:cnbctv18.com (markets OR business OR companies OR economy)",
        }
        query = queries.get(source.key)
        if not query:
            return None
        return (
            "https://news.google.com/rss/search?q="
            + quote(query)
            + "&hl=en-IN&gl=IN&ceid=IN:en"
        )

    @staticmethod
    def _playwright_fetch_sync(url: str, timeout_ms: int = 0) -> str:
        """Fetch a public article/section page in a real browser context.

        This is a compatibility fallback for WAF/CDN responses such as 403. It does
        not solve or bypass a CAPTCHA/login; it simply uses the same public URL with
        a normal Chromium/Chrome/Edge browser context.
        """
        from os import environ
        from playwright.sync_api import sync_playwright

        errors: list[str] = []
        launch_attempts: list[dict[str, Any]] = []
        executable = environ.get("PLAYWRIGHT_EXECUTABLE_PATH") or environ.get("PLAYWRIGHT_BROWSER_EXECUTABLE")
        if executable:
            launch_attempts.append({"executable_path": executable, "headless": True})
        launch_attempts.extend((
            {"channel": "chrome", "headless": True},
            {"channel": "msedge", "headless": True},
            {"headless": True},
        ))
        with sync_playwright() as playwright:
            browser = None
            for kwargs in launch_attempts:
                try:
                    browser = playwright.chromium.launch(**kwargs)
                    break
                except Exception as exc:
                    errors.append(f"{kwargs}: {type(exc).__name__}: {exc}")
            if browser is None:
                raise RuntimeError("Playwright launch failed: " + " | ".join(errors[-3:]))
            try:
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    locale="en-IN",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=max(5000, int(timeout_ms or 20000)))
                page.wait_for_timeout(1200)
                return page.content()
            finally:
                browser.close()

    async def _browser_fetch(self, url: str) -> str:
        return await asyncio.to_thread(self._playwright_fetch_sync, url)

    @staticmethod
    def _is_access_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(token in text for token in ("403", "429", "forbidden", "failed to fetch", "timed out", "timeout"))

    async def collect_source(self, source: NewsSourceProfile, *, limit: int = 50) -> CollectionResult:
        # Keep NSE collection isolated from generic publisher parsing. The dedicated
        # NSE method owns RSS + official API/toolkit/browser fallbacks and normalization.
        if source.key == "nse_india":
            return await self.collect_nse_announcements(days=7, limit=limit)
        errors: list[str] = []
        attempted: list[str] = []
        items: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        candidates = list(source.feed_urls)
        discovery_urls = list(source.discovery_urls)
        page_urls = list(source.article_urls)
        fallback_feed = self._google_news_fallback_url(source)
        browser_fallback_sources = {"moneycontrol", "ndtv_profit", "bloomberg_india"}

        async def add_rows(feed_url: str, content: str) -> int:
            # BeautifulSoup/XML parsing is synchronous CPU work. Keep it off the
            # FastAPI event loop so background NewsAlert ingestion cannot delay
            # unrelated login/page requests.
            rows = await asyncio.to_thread(
                self.parse_content, feed_url, content, source=source
            )
            added = 0
            for row in rows:
                link = row.get("url")
                if not link or link in seen_urls:
                    continue
                seen_urls.add(link)
                items.append(row)
                added += 1
                if len(items) >= limit:
                    break
            return added

        # Publisher pages in this environment frequently sit behind bot/WAF
        # controls. For the slow set, use a public Google News RSS search first.
        # The individual article link is preserved, and headline-only Coverage List
        # matching still happens later in the ingestion pipeline.
        # Direct publisher RSS is preferred whenever the registry supplies it.
        # Generic Google News is only a fallback for sources without a working direct feed.
        fallback_first_sources = {
            key for key in {
                "economic_times", "business_standard", "livemint",
                "hindu_businessline", "business_today",
                "moneycontrol", "ndtv_profit", "bloomberg_india", "cnbc_tv18",
            }
            if not source.feed_urls
        }
        if fallback_feed and source.key in fallback_first_sources:
            try:
                attempted.append(f"fallback-first:{fallback_feed}")
                content = await self.browser.fetch(fallback_feed)
                await add_rows(fallback_feed, content)
            except Exception as exc:
                try:
                    attempted.append(f"browser-fallback-first:{fallback_feed}")
                    content = await self._browser_fetch(fallback_feed)
                    await add_rows(fallback_feed, content)
                except Exception as browser_exc:
                    logger.debug(
                        "News fallback-first failed for %s: %s | %s",
                        source.key, exc, browser_exc,
                    )

            # These publishers frequently time out behind WAF/CDN controls.
            # Once their public Google News RSS fallback produced usable items,
            # stop here rather than spending another 45–75 seconds on a direct
            # publisher connection. The returned Google News entry remains
            # article-specific and is preserved for the Open button.
            if items:
                return CollectionResult(
                    source.key,
                    source.name,
                    tuple(attempted),
                    tuple(items[:limit]),
                    tuple(),
                )

        # Discovery first. Some sources are slow to answer their home page, so a
        # source-specific budget keeps the whole job from being consumed by one URL.
        for url in discovery_urls:
            try:
                if not self._allowed(url, source):
                    continue
                attempted.append(url)
                content = await self.browser.fetch(url)
                discovered = self._discover_feed_urls(url, content, source)
                for feed in discovered:
                    if feed not in candidates:
                        candidates.append(feed)
            except Exception as exc:
                if self._is_access_error(exc):
                    try:
                        browser_content = await self._browser_fetch(url)
                        discovered = self._discover_feed_urls(url, browser_content, source)
                        for feed in discovered:
                            if feed not in candidates:
                                candidates.append(feed)
                        continue
                    except Exception:
                        pass
                if not (source.key in browser_fallback_sources and self._is_access_error(exc)):
                    errors.append(f"discovery {url}: {exc}")

        # Feed-first. Direct publisher RSS is preferred; one failed feed never stops
        # the rest of the source.
        for feed_url in candidates:
            if len(items) >= limit:
                break
            if not self._allowed(feed_url, source):
                continue
            try:
                attempted.append(feed_url)
                content = await self.browser.fetch(feed_url)
                await add_rows(feed_url, content)
            except Exception as exc:
                if self._is_access_error(exc):
                    try:
                        browser_content = await self._browser_fetch(feed_url)
                        await add_rows(feed_url, browser_content)
                        continue
                    except Exception:
                        pass
                if not (source.key in browser_fallback_sources and self._is_access_error(exc)):
                    errors.append(f"feed {feed_url}: {exc}")

        # Browser-rendered publisher pages. This is the main fix for WAF/CDN 403s.
        if len(items) < limit and page_urls:
            for page_url in page_urls[: max(2, int(getattr(source, "max_pages", 2) or 2))]:
                if len(items) >= limit:
                    break
                if not self._allowed(page_url, source):
                    continue
                try:
                    attempted.append(f"browser:{page_url}")
                    if source.key == "zerodha_pulse":
                        try:
                            content = await self.browser.fetch(page_url)
                        except Exception:
                            content = await self._browser_fetch(page_url)
                    else:
                        content = await self._browser_fetch(page_url)
                    await add_rows(page_url, content)
                except Exception as exc:
                    # Keep a compact diagnostic but don't fail a source if fallback
                    # discovery still has another route.
                    errors.append(f"browser {page_url}: {exc}")

        # Public Google News RSS fallback. Results still point to the requested
        # publisher domain; this is used only when direct/browsed publisher access
        # produced too few items.
        if len(items) < limit and fallback_feed:
            try:
                attempted.append(f"fallback:{fallback_feed}")
                content = await self.browser.fetch(fallback_feed)
                await add_rows(fallback_feed, content)
            except Exception as exc:
                errors.append(f"fallback {fallback_feed}: {exc}")

        # If the fallback browser is available but Google News failed, try the feed
        # once through a real browser as a final compatibility step.
        if len(items) < limit and fallback_feed:
            try:
                attempted.append(f"browser-fallback:{fallback_feed}")
                content = await self._browser_fetch(fallback_feed)
                await add_rows(fallback_feed, content)
            except Exception as exc:
                errors.append(f"browser-fallback {fallback_feed}: {exc}")

        # Once items were recovered, access errors on one path are not reported as a
        # source failure. Keep non-access parsing errors because they are actionable.
        if items:
            hard_errors = [
                e for e in errors
                if not self._is_access_error(RuntimeError(e))
            ]
            errors = hard_errors[:8]
        return CollectionResult(source.key, source.name, tuple(attempted), tuple(items[:limit]), tuple(errors))

    async def collect_company_google_news(
        self,
        company_name: str,
        *,
        nse_symbol: str | None = None,
        max_results: int = 12,
    ) -> list[dict[str, Any]]:
        """Collect recent company-specific stories through Google's public RSS index.

        This is intentionally company-scoped (rather than a generic source page),
        following the colleague implementation. It improves relevance because the
        query itself names the Coverage List company.
        """
        query_parts = [f'"{company_name}"']
        if nse_symbol:
            query_parts.append(f'"{nse_symbol}"')
        query = " OR ".join(query_parts) + " when:7d"
        url = (
            "https://news.google.com/rss/search?q="
            + quote(query)
            + "&hl=en-IN&gl=IN&ceid=IN:en"
        )
        content = await self.browser.fetch(url)
        rows = await asyncio.to_thread(self.parse_content, url, content, source=None)
        return rows[: max(1, min(int(max_results), 30))]

    async def collect_all(self, *, limit_per_source: int = 50) -> list[CollectionResult]:
        sources = enabled_sources()
        semaphore = asyncio.Semaphore(4)

        async def run(source: NewsSourceProfile) -> CollectionResult:
            async with semaphore:
                return await self.collect_source(source, limit=limit_per_source)

        return list(await asyncio.gather(*(run(source) for source in sources)))

    async def _fetch_http_text(self, url: str, *, timeout_seconds: float = 18.0) -> str:
        """Fetch a public page/feed with bounded timeouts and ordinary HTTP only."""
        timeout_seconds = max(4.0, min(float(timeout_seconds or 18.0), 25.0))
        timeout = httpx.Timeout(timeout_seconds, connect=min(7.0, timeout_seconds))
        async with httpx.AsyncClient(
            headers=PUBLIC_FEED_HEADERS,
            follow_redirects=True,
            timeout=timeout,
            trust_env=True,
        ) as client:
            response = await client.get(url)
            # Do not try to circumvent an explicit public-source access denial.
            if response.status_code in {401, 403, 429}:
                raise RuntimeError(f"public source returned HTTP {response.status_code}")
            response.raise_for_status()
            content = response.text
            if not content or len(content.strip()) < 40:
                raise RuntimeError("public source returned an empty/truncated response")
            return content

    async def _fetch_public_feed_text(self, url: str, *, timeout_seconds: float = 18.0) -> tuple[str, str]:
        """Try the configured HTTP transport first, then the existing normal browser transport."""
        errors: list[str] = []
        try:
            return await self._fetch_http_text(url, timeout_seconds=timeout_seconds), "httpx-public"
        except Exception as exc:
            errors.append(f"httpx: {type(exc).__name__}: {str(exc)[:180]}")
        # The browser fallback requests the exact public URL; it does not log in or
        # call a hidden JSON endpoint. A blocked URL is still reported as blocked.
        try:
            content = await self.browser.fetch(url)
            if content and len(str(content).strip()) >= 40:
                return str(content), "browser-public"
            raise RuntimeError("browser returned empty content")
        except Exception as exc:
            errors.append(f"BrowserClient: {type(exc).__name__}: {str(exc)[:180]}")
        raise RuntimeError("; ".join(errors))

    @staticmethod
    def _nse_record_value(record: Any, *names: str) -> str:
        """Return the first meaningful NSE API/RSS field across casing variants."""
        if not isinstance(record, dict):
            return ""
        normalized = {
            re.sub(r"[^a-z0-9]", "", str(key).casefold()): value
            for key, value in record.items()
        }
        for name in names:
            key = re.sub(r"[^a-z0-9]", "", str(name).casefold())
            value = normalized.get(key)
            if isinstance(value, dict):
                value = value.get("href") or value.get("url") or value.get("value") or value.get("name")
            if isinstance(value, (list, tuple)):
                value = next((v for v in value if str(v or "").strip()), "")
            cleaned = " ".join(str(value or "").split()).strip()
            if cleaned and cleaned.casefold() not in {"none", "null", "n/a", "-"}:
                return cleaned
        return ""

    @classmethod
    def _nse_records_from_payload(cls, payload: Any, *, depth: int = 0) -> list[dict[str, Any]]:
        """Unwrap list/dict response shapes seen across NSE endpoints/toolkits."""
        if depth > 6 or payload is None:
            return []
        if isinstance(payload, str):
            raw = payload.strip()
            if raw.startswith(("[", "{")) and len(raw) <= 20_000_000:
                try:
                    return cls._nse_records_from_payload(json.loads(raw), depth=depth + 1)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return []
            return []
        if isinstance(payload, list):
            output: list[dict[str, Any]] = []
            for value in payload:
                output.extend(cls._nse_records_from_payload(value, depth=depth + 1))
            return output
        if not isinstance(payload, dict):
            return []

        record_markers = {
            "symbol", "smname", "company", "companyname", "subject", "title", "headline",
            "attchmntfile", "attachmenturl", "url", "link", "seqid", "broadcastdate",
            "broadcastdatetime", "announcementdate", "publisheddate", "pubdate", "an_dt", "dt",
            "sortdate", "exchdisstime", "dissemdt",
        }
        normalized_keys = {re.sub(r"[^a-z0-9]", "", str(k).casefold()) for k in payload}
        if normalized_keys & record_markers:
            return [payload]

        rows: list[dict[str, Any]] = []
        for key, value in payload.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized_key in {
                "data", "records", "results", "rows", "announcements", "corporateannouncements",
                "corporateannouncement", "announcementdata", "announcedata", "equity", "items",
                "result", "table", "tabledata", "table_data", "response",
            }:
                rows.extend(cls._nse_records_from_payload(value, depth=depth + 1))
        return rows

    @classmethod
    def _parse_nse_datetime(cls, value: Any) -> datetime | None:
        """Parse NSE timestamps; naive NSE timestamps are India local time, not UTC."""
        if not value:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            raw = " ".join(str(value).strip().split())
            if not raw:
                return None
            parsed = None
            # Some feeds expose Unix epoch seconds/milliseconds instead of a date string.
            if re.fullmatch(r"\d{10}(?:\d{3})?", raw):
                try:
                    numeric = int(raw)
                    if len(raw) == 13:
                        numeric /= 1000
                    parsed = datetime.fromtimestamp(numeric, tz=timezone.utc)
                except (ValueError, OverflowError, OSError):
                    parsed = None
            iso_value = raw.replace("Z", "+00:00")
            if parsed is None:
                try:
                    parsed = datetime.fromisoformat(iso_value)
                except ValueError:
                    pass
            if parsed is None:
                for fmt in (
                    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y",
                    "%d-%b-%Y %I:%M:%S %p", "%d-%b-%Y %I:%M %p",
                    "%d-%b-%y %H:%M:%S", "%d-%b-%y %H:%M",
                    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
                    "%d-%m-%Y %I:%M:%S %p", "%d-%m-%Y %I:%M %p",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %I:%M %p",
                    "%Y%m%d %H%M%S", "%Y%m%d%H%M%S", "%d%m%Y%H%M%S", "%Y%m%d",
                    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
                    "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M %p",
                    "%d-%B-%Y %H:%M:%S", "%d-%B-%Y %H:%M", "%d-%B-%Y",
                ):
                    try:
                        parsed = datetime.strptime(raw, fmt)
                        break
                    except ValueError:
                        continue
            if parsed is None:
                # Try the generic parser for RFC/email timestamps.
                parsed = cls._parse_date(raw)
                if parsed is None:
                    return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=NSE_LOCAL_TZ)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _normalise_nse_record(cls, record: dict[str, Any], *, transport: str) -> dict[str, Any] | None:
        """Normalize one RSS/API/toolkit/browser record to the existing pipeline schema."""
        symbol = cls._nse_record_value(record, "symbol", "nse_symbol", "security_symbol", "ticker").upper()
        company = cls._nse_record_value(
            record, "sm_name", "company_name", "companyName", "company", "issuer", "security_name", "name"
        )
        subject = cls._nse_record_value(
            record, "subject", "announcement_subject", "announcementTitle", "newsSub", "title", "headline"
        )
        description = cls._nse_record_value(
            record, "desc", "details", "detail", "announcementText", "attchmntText", "description", "summary"
        )
        date_aliases = (
            "broadcastDateTime", "broadcast_date_time", "broadcastDate", "broadcast_date",
            "announcementDateTime", "announcement_datetime", "announcementDate", "announcement_date",
            "publishedDateTime", "published_datetime", "publishedDate", "published_date", "pubDate",
            "exchdisstime", "dissemDT", "disseminationTime", "dateAndTime", "date_time",
            "dt", "an_dt", "sort_date", "date", "published_at", "published", "timestamp",
            "createdAt", "created_at", "updatedAt", "lastModified", "last_modified",
        )
        date_raw = ""
        published = None
        for date_alias in date_aliases:
            candidate_date = cls._nse_record_value(record, date_alias)
            if not candidate_date:
                continue
            candidate_parsed = cls._parse_nse_datetime(candidate_date)
            if candidate_parsed is not None:
                date_raw = candidate_date
                published = candidate_parsed
                break
        if published is None:
            return None

        base_title = company or symbol or subject or description
        title = cls._nse_headline(base_title, subject or description, record)
        title = " ".join(str(title or "").split()).strip(" -–—|•")
        if not title or len(title) < 4:
            return None
        if title.casefold() in {"nse india", "corporate announcements", "national stock exchange"}:
            return None

        source_id = cls._nse_record_value(
            record, "seq_id", "seqId", "announcement_id", "announcementId", "id", "guid", "newsid", "source_item_id"
        )
        if not source_id:
            source_id = hashlib.sha256(
                f"{symbol}|{company}|{subject}|{published.isoformat()}|{description[:500]}".encode(
                    "utf-8", errors="replace"
                )
            ).hexdigest()[:32]

        # NSE's RSS <link> and attchmntFile fields frequently point straight to an
        # XBRL/XML/PDF attachment. That is useful evidence, but it is not the
        # human-readable corporate-announcement page users expect to open.
        # Preserve the attachment separately and make the clicked URL the actual
        # NSE announcements page filtered to the issuer. Keep a stable per-item
        # query token so different announcements for the same issuer do not share
        # the same canonical/deduplication URL.
        # Only fields which explicitly represent an attachment may populate the
        # document URL. Generic `url`/`link`/`href` values are often the issuer
        # quote-page link, not the PDF shown in NSE's Attachment column.
        attachment_candidate = cls._nse_record_value(
            record,
            "attchmntFile", "attachmentFile", "attachment_file", "attachment",
            "attachment_url", "attachmentUrl", "fileUrl", "file_url",
            "pdfUrl", "pdf_url", "downloadUrl", "download_url", "attachmentLink",
            "documentUrl", "document_url", "announcementFile", "announcement_file",
            "annexureFile", "annexure_file", "filePath", "file_path",
        )
        # NSE has used slightly different attachment property names across API
        # deployments. Search only attachment/document/file URL-like fields as a
        # fallback; exclude XBRL/XML and presentation-only metadata explicitly.
        if not attachment_candidate:
            for raw_key in record:
                key = re.sub(r"[^a-z0-9]", "", str(raw_key).casefold())
                if any(token in key for token in ("xbrl", "xml", "filesize", "filetype", "mimetype", "text", "desc", "name")):
                    continue
                looks_like_attachment = (
                    "pdf" in key
                    or (any(token in key for token in ("attach", "attch", "annexure", "document"))
                        and any(token in key for token in ("file", "url", "link", "path", "doc", "pdf")))
                    or key in {"fileurl", "filepath", "downloadurl", "documenturl"}
                )
                if looks_like_attachment:
                    attachment_candidate = cls._nse_record_value(record, str(raw_key))
                    if attachment_candidate:
                        break
        if not attachment_candidate:
            generic_link = cls._nse_record_value(record, "url", "link", "href")
            generic_path = urlparse(generic_link).path.casefold() if generic_link else ""
            if generic_path.rstrip("/").endswith(".pdf"):
                attachment_candidate = generic_link

        attachment_url = ""
        if attachment_candidate:
            attachment_candidate = attachment_candidate.replace("&amp;", "&").strip()
            # Some exchange payloads wrap a URL in a short anchor fragment.
            if "<a" in attachment_candidate.casefold():
                href_match = re.search(r"(?:href|src)\s*=\s*['\"]([^'\"]+)['\"]", attachment_candidate, flags=re.I)
                if href_match:
                    attachment_candidate = href_match.group(1).strip()
            parsed_input = urlparse(attachment_candidate)
            if parsed_input.scheme in {"http", "https"} or attachment_candidate.startswith("//"):
                candidate = urljoin("https://nsearchives.nseindia.com/corporate/", attachment_candidate)
            elif attachment_candidate.lstrip("/").casefold().startswith(("corporate/", "content/", "archives/")):
                candidate = urljoin("https://nsearchives.nseindia.com/", attachment_candidate)
            else:
                candidate = urljoin("https://nsearchives.nseindia.com/corporate/", attachment_candidate)
            parsed_candidate = urlparse(candidate)
            candidate_host = (parsed_candidate.hostname or "").casefold()
            candidate_is_nse = parsed_candidate.scheme in {"http", "https"} and (
                candidate_host == "nseindia.com" or candidate_host.endswith(".nseindia.com")
            )
            candidate_path = parsed_candidate.path.rstrip("/").casefold()
            candidate_is_markup = candidate_path.endswith((".xml", ".xbrl", ".html", ".htm", ".csv", ".xlsx")) or "/xbrl/" in candidate_path
            candidate_is_listing_page = "corporate-filings-announcements" in candidate_path
            # The corporate-announcements API's explicit Attachment field is the
            # same document shown in NSE's Attachment column. Some valid download
            # handlers are extensionless or query-based, so do not reject them just
            # because the path does not end in .pdf. XBRL/XML is a separate field.
            if candidate_is_nse and not candidate_is_markup and not candidate_is_listing_page:
                attachment_url = candidate

        isin = cls._nse_record_value(record, "sm_isin", "isin", "isin_code", "security_isin").upper()
        link = _nse_corporate_announcement_page_url(symbol, str(source_id))

        summary = description or (subject if subject != title else "")
        return {
            "title": title[:1000],
            "url": link,
            "attachment_url": attachment_url or None,
            "published_at": published,
            "source": "NSE Corporate Announcements",
            "source_key": "nse_india",
            "source_url": NSE_ANNOUNCEMENTS_PAGE,
            "source_item_id": str(source_id)[:255],
            "nse_symbol": symbol,
            "isin": isin,
            "company_name": company[:255],
            "announcement_subject": subject[:1000],
            "attachment_type": "PDF" if attachment_url else None,
            "description": summary[:10000],
            "summary": summary[:10000],
            "content_type": "NEWS",
            "transport": transport,
        }

    @classmethod
    def _normalise_nse_payload(cls, payload: Any, *, transport: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for record in cls._nse_records_from_payload(payload):
            row = cls._normalise_nse_record(record, transport=transport)
            if row is not None:
                rows.append(row)
        return rows

    def _parse_nse_rss_content(self, feed_url: str, content: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Dedicated, namespace-tolerant parser for NSE's XML announcements feed.

        The generic feedparser path previously returned zero rows without exposing
        whether entries were present but had unsupported links/date fields. Parse
        item/entry nodes directly as a second independent route and report each
        rejection category instead of silently returning an empty list.
        """
        import xml.etree.ElementTree as ET
        diagnostics: dict[str, Any] = {
            "characters": len(content or ""), "root": None, "entries": 0,
            "normalized": 0, "missing_date_or_fields": 0, "sample_fields": [],
        }
        try:
            root = ET.fromstring(content or "")
        except ET.ParseError as exc:
            diagnostics["xml_error"] = str(exc)[:220]
            return [], diagnostics
        diagnostics["root"] = root.tag.rsplit("}", 1)[-1]
        rows: list[dict[str, Any]] = []
        entry_nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].casefold() in {"item", "entry"}]
        diagnostics["entries"] = len(entry_nodes)
        for entry in entry_nodes:
            record: dict[str, Any] = {}
            # Capture standard text values and link attributes regardless of XML namespace.
            for node in entry.iter():
                local = re.sub(r"[^a-z0-9]", "", node.tag.rsplit("}", 1)[-1].casefold())
                value = " ".join(str(node.text or "").split()).strip()
                if local == "link":
                    value = str(node.attrib.get("href") or node.attrib.get("resource") or value).strip()
                elif local in {"guid", "id"} and not value:
                    value = str(node.attrib.get("href") or node.attrib.get("resource") or "").strip()
                elif local in {"enclosure", "attachment"} and not value:
                    value = str(node.attrib.get("url") or node.attrib.get("href") or "").strip()
                if value:
                    previous = str(record.get(local) or "")
                    if not previous or len(value) > len(previous):
                        record[local] = value
            # Align custom namespaced fields with normalizer aliases.
            alias_map = {
                "companyname": "company_name", "smname": "sm_name", "nse_symbol": "symbol",
                "securitysymbol": "symbol", "announcementtitle": "subject",
                "announcementtext": "desc", "details": "desc", "attachmenturl": "attchmntFile",
                "attachmentfile": "attchmntFile", "fileurl": "attchmntFile",
                "broadcastdatetime": "broadcastDateTime", "broadcastdate": "broadcastDate",
                "announcementdatetime": "announcementDateTime", "announcementdate": "announcementDate",
                "publishedatetime": "publishedDateTime", "publisheddate": "publishedDate",
                "sortdate": "sort_date", "exchdisstime": "exchdisstime", "disseminationtime": "disseminationTime",
                "sequenceid": "seq_id", "seqid": "seq_id", "pubdate": "pubDate",
            }
            for old_key, new_key in alias_map.items():
                if record.get(old_key) and not record.get(new_key):
                    record[new_key] = record[old_key]
            # Atom's link field often has href but no text; custom identity remains stable.
            if not record.get("link"):
                for child in entry:
                    if child.tag.rsplit("}", 1)[-1].casefold() == "link":
                        href = child.attrib.get("href") or child.attrib.get("resource")
                        if href:
                            record["link"] = str(href).strip()
                            break
            if not record.get("seq_id"):
                record["seq_id"] = record.get("guid") or record.get("id") or ""
            if not any(record.get(k) for k in ("title", "symbol", "sm_name", "company_name", "subject", "desc", "description")):
                diagnostics["missing_date_or_fields"] += 1
                continue
            row = self._normalise_nse_record(record, transport="official-nse-rss")
            if row is None:
                diagnostics["missing_date_or_fields"] += 1
                continue
            # Prefer a record-level attachment or canonical link; the normalizer
            # creates a stable NSE announcement URL if neither is supplied.
            rows.append(row)
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("source_item_id") or row.get("url") or "")
            if key:
                unique.setdefault(key, row)
        diagnostics["normalized"] = len(unique)
        if entry_nodes:
            diagnostics["sample_fields"] = sorted({key for node in entry_nodes[:1] for key in (
                re.sub(r"[^a-z0-9]", "", child.tag.rsplit("}", 1)[-1].casefold()) for child in node.iter()
            )})[:40]
        return list(unique.values()), diagnostics

    async def _fetch_nse_api_payload(self, start_day: date, end_day: date) -> Any:
        """Use the canonical session-aware NSEApiClient for corporate-announcement JSON."""
        from ii_platform.nse.client import NSEApiClient

        timeout = httpx.Timeout(connect=6.0, read=18.0, write=6.0, pool=6.0)
        async with NSEApiClient(retries=2, timeout=timeout, min_request_interval_seconds=0.3) as client:
            payload = await client.get_corporate_announcements(
                start_day, end_day, segment="equities", query_each_day=True
            )
            api_diag = dict(getattr(client, "last_corporate_announcement_diagnostics", {}) or {})
            self._last_nse_api_diagnostics = api_diag
            logger.info(
                "FINANCIAL_NEWS_NSE_CLIENT_API_DIAGNOSTICS raw=%s requests=%s errors=%s latest=%s",
                len(payload or []), len(api_diag.get("requests") or []), len(api_diag.get("errors") or []),
                api_diag.get("latest_an_dt"),
            )
            if not payload:
                raise RuntimeError(
                    "NSEApiClient returned zero announcement rows; "
                    + json.dumps(api_diag, ensure_ascii=False, default=str)[:1200]
                )
            return payload

    @staticmethod
    def _jugaad_nse_fetch_sync(start_day: date, end_day: date) -> Any:
        """Use the independent jugaad-data NSELive toolkit with request timeouts."""
        import requests.sessions
        # The package exports NSELive from jugaad_data.nse. Use that stable public
        # import path (not an implementation-module path that has changed between releases).
        from jugaad_data.nse import NSELive

        original_session_request = requests.sessions.Session.request

        def bounded_session_request(session, method, url, **kwargs):
            kwargs.setdefault("timeout", (5, 14))
            response = original_session_request(session, method, url, **kwargs)
            if getattr(response, "status_code", None) in NSE_ACCESS_DENIAL_CODES:
                raise RuntimeError(
                    f"NSE toolkit request explicitly denied with HTTP {response.status_code}"
                )
            return response

        # NSELive warms up a public page in its constructor, so bound that request too.
        requests.sessions.Session.request = bounded_session_request
        try:
            client = NSELive()
            # The current package documents date objects for from_date/to_date.
            ranged = client.corporate_announcements(
                segment="equities", from_date=start_day, to_date=end_day
            )
            if ranged:
                return ranged
            # The toolkit endpoint's unbounded recent-list request is a distinct query
            # variant. Use it only when the bounded request returned no rows.
            return client.corporate_announcements(segment="equities")
        finally:
            requests.sessions.Session.request = original_session_request

    @staticmethod
    def _playwright_scrape_nse_sync(timeout_ms: int = 22000) -> tuple[list[Any], str, dict[str, Any]]:
        """Load the public NSE announcements page and observe its normal public XHR response.

        This is a standard browser render/scrape only. It does not solve challenges,
        sign in, rotate identities, or bypass an explicit 401/403/429 response.
        """
        from os import environ
        from playwright.sync_api import sync_playwright

        errors: list[str] = []
        launch_attempts: list[dict[str, Any]] = []
        executable = environ.get("PLAYWRIGHT_EXECUTABLE_PATH") or environ.get("PLAYWRIGHT_BROWSER_EXECUTABLE")
        if executable:
            launch_attempts.append({"executable_path": executable, "headless": True})
        launch_attempts.extend((
            {"channel": "chrome", "headless": True},
            {"channel": "msedge", "headless": True},
            {"headless": True},
        ))
        payloads: list[Any] = []
        api_statuses: list[int] = []
        page_status: int | None = None
        with sync_playwright() as playwright:
            browser = None
            for kwargs in launch_attempts:
                try:
                    browser = playwright.chromium.launch(**kwargs)
                    break
                except Exception as exc:
                    errors.append(f"{kwargs}: {type(exc).__name__}: {str(exc)[:120]}")
            if browser is None:
                raise RuntimeError("Playwright launch failed: " + " | ".join(errors[-3:]))
            try:
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    locale="en-IN",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()

                def capture_response(response) -> None:
                    try:
                        parsed = urlparse(response.url)
                        if (parsed.hostname or "").casefold() not in {"www.nseindia.com", "nseindia.com"}:
                            return
                        if "corporate-announcement" not in parsed.path.casefold():
                            return
                        api_statuses.append(int(response.status))
                        if response.status in NSE_ACCESS_DENIAL_CODES:
                            return
                        if 200 <= response.status < 300:
                            payload = response.json()
                            if payload is not None:
                                payloads.append(payload)
                    except Exception:
                        return

                page.on("response", capture_response)
                captured_pages: list[dict[str, Any]] = []
                page_statuses: list[dict[str, Any]] = []
                final_content = ""
                retry_count = 0
                did_reload = False

                def table_row_count() -> int:
                    try:
                        return int(page.locator("table tbody tr, table tr").count())
                    except Exception:
                        return 0

                def wait_for_data_window() -> None:
                    # NSE builds the table client-side. A short fixed sleep can return
                    # HTTP 200 before its public XHR has completed.
                    for _ in range(6):
                        if payloads or table_row_count() > 1:
                            return
                        page.wait_for_timeout(250)

                for page_url in NSE_ANNOUNCEMENTS_PAGE_ALIASES:
                    before_payloads = len(payloads)
                    try:
                        response = page.goto(
                            page_url, wait_until="domcontentloaded",
                            timeout=max(6000, int(timeout_ms or 10000)),
                        )
                    except Exception as exc:
                        # A navigation timeout on one alias must not prevent trying
                        # the alternate official NSE page or parsing partial HTML.
                        errors.append(f"navigation {page_url}: {type(exc).__name__}: {str(exc)[:100]}")
                        response = None
                    status = int(response.status) if response is not None else None
                    page_status = status
                    page_statuses.append({"url": page_url, "status": status})
                    if status in NSE_ACCESS_DENIAL_CODES:
                        raise RuntimeError(f"NSE public announcements page explicitly denied request with HTTP {status}")
                    wait_for_data_window()
                    final_content = page.content()
                    # Resolve relative attachment links to absolute official NSE links
                    # while the page URL is still known.
                    try:
                        page.eval_on_selector_all(
                            "a[href]", "els => els.forEach(a => a.setAttribute('href', a.href))"
                        )
                        final_content = page.content()
                    except Exception:
                        pass
                    captured_pages.append({
                        "url": page_url,
                        "html": final_content,
                        "table_rows": table_row_count(),
                        "api_payloads_delta": len(payloads) - before_payloads,
                    })
                    page_has_data = len(payloads) > before_payloads or table_row_count() > 1
                    if page_has_data:
                        continue
                    # One ordinary reload of a page that responded successfully but
                    # exposed no data; then try the other official public page alias.
                    if status and 200 <= status < 300 and not did_reload:
                        retry_count += 1
                        did_reload = True
                        try:
                            response = page.reload(
                                wait_until="domcontentloaded",
                                timeout=max(6000, int(timeout_ms or 10000)),
                            )
                        except Exception as exc:
                            errors.append(f"reload {page_url}: {type(exc).__name__}: {str(exc)[:100]}")
                            response = None
                        status = int(response.status) if response is not None else status
                        page_status = status
                        page_statuses.append({"url": page_url + " [reload]", "status": status})
                        if status in NSE_ACCESS_DENIAL_CODES:
                            raise RuntimeError(f"NSE public announcements page explicitly denied request with HTTP {status}")
                        wait_for_data_window()
                        final_content = page.content()
                        try:
                            page.eval_on_selector_all(
                                "a[href]", "els => els.forEach(a => a.setAttribute('href', a.href))"
                            )
                            final_content = page.content()
                        except Exception:
                            pass
                        captured_pages.append({
                            "url": page_url + " [reload]",
                            "html": final_content,
                            "table_rows": table_row_count(),
                            "api_payloads_delta": len(payloads) - before_payloads,
                        })
                        if len(payloads) > before_payloads or table_row_count() > 1:
                            continue

                if any(status in NSE_ACCESS_DENIAL_CODES for status in api_statuses):
                    raise RuntimeError(
                        "NSE browser XHR explicitly denied request with HTTP "
                        + str(next(status for status in api_statuses if status in NSE_ACCESS_DENIAL_CODES))
                    )
                # Keep all public pages visited in this run so their rendered tables can
                # be parsed together; links were absolutized above.
                content = "\n".join(str(page_info["html"]) for page_info in captured_pages if page_info.get("html"))
                return payloads, content, {
                    "page_status": page_status,
                    "page_statuses": page_statuses,
                    "api_statuses": api_statuses,
                    "api_payloads": len(payloads),
                    "html_chars": len(content),
                    "table_rows": sum(int(page_info.get("table_rows") or 0) for page_info in captured_pages),
                    "retry_count": retry_count,
                    "pages_attempted": [page_info["url"] for page_info in captured_pages],
                    "navigation_errors": errors[:8],
                }
            finally:
                browser.close()

    def _parse_nse_announcement_html(self, content: str, *, transport: str) -> list[dict[str, Any]]:
        """Parse visible NSE announcement tables and embedded public JSON when present."""
        soup = BeautifulSoup(content or "", "lxml")
        rows: list[dict[str, Any]] = []

        # Some NSE builds expose the records directly in a JSON script even when table
        # rendering has not completed. This is parsing delivered page content, not an API call.
        for node in soup.find_all("script"):
            raw = node.string or node.get_text()
            if not raw or len(raw) > 8_000_000:
                continue
            stripped = raw.strip()
            if not stripped or stripped[0] not in "[{":
                continue
            try:
                payload = json.loads(stripped)
            except Exception:
                continue
            rows.extend(self._normalise_nse_payload(payload, transport=transport))

        for table in soup.find_all("table"):
            table_rows = table.find_all("tr")
            if len(table_rows) < 2:
                continue
            header_cells = table_rows[0].find_all(["th", "td"])
            headers = [re.sub(r"[^a-z0-9]", "", cell.get_text(" ", strip=True).casefold()) for cell in header_cells]
            if not any(term in headers for term in ("symbol", "companyname", "subject", "broadcastdatetime", "broadcastdate", "dt")):
                continue
            index = {name: pos for pos, name in enumerate(headers)}

            def find_index(*aliases: str, default: int | None = None) -> int | None:
                for alias in aliases:
                    normalized = re.sub(r"[^a-z0-9]", "", alias.casefold())
                    if normalized in index:
                        return index[normalized]
                return default

            symbol_i = find_index("symbol", default=0)
            company_i = find_index("company name", "company", default=1)
            subject_i = find_index("subject", "announcement subject", default=2)
            detail_i = find_index("details", "detail", "description", default=3)
            attachment_i = find_index("attachment", "attachments", "attchmntfile", "pdf", default=4)
            date_i = find_index("broadcast date/time", "broadcast datetime", "broadcast date", "date", "dt", default=6)
            for tr in table_rows[1:]:
                cells = tr.find_all(["td", "th"])
                if not cells:
                    continue

                def cell_value(cell_index: int | None) -> str:
                    if cell_index is None or cell_index >= len(cells):
                        return ""
                    return " ".join(cells[cell_index].get_text(" ", strip=True).split())

                # The symbol cell also contains an NSE link. Never take the first
                # link in the row: inspect only the Attachment column and accept
                # an actual PDF href. The XBRL column is a separate XML link.
                attachment_href = ""
                if attachment_i is not None and attachment_i < len(cells):
                    if attachment_i is not None and attachment_i < len(cells):
                        attachment_cell = cells[attachment_i]
                        candidate_hrefs: list[str] = []
                        for anchor in attachment_cell.find_all(["a", "area"]):
                            candidate_hrefs.append(str(anchor.get("href") or ""))
                            candidate_hrefs.extend(str(anchor.get(attr) or "") for attr in ("data-href", "data-url", "data-download-url"))
                            onclick = str(anchor.get("onclick") or "")
                            candidate_hrefs.extend(re.findall(r"['\"]([^'\"]+)['\"]", onclick, flags=re.I))
                        # Some page builds put the download path on the icon or
                        # its parent instead of an anchor href.
                        for node in attachment_cell.find_all(attrs={"data-href": True}):
                            candidate_hrefs.append(str(node.get("data-href") or ""))
                        for node in attachment_cell.find_all(attrs={"data-url": True}):
                            candidate_hrefs.append(str(node.get("data-url") or ""))
                        for node in attachment_cell.find_all(attrs={"onclick": True}):
                            onclick = str(node.get("onclick") or "")
                            candidate_hrefs.extend(re.findall(r"['\"]([^'\"]+)['\"]", onclick, flags=re.I))
                        for raw_href in candidate_hrefs:
                            raw_href = raw_href.strip().replace("&amp;", "&")
                            if not raw_href or raw_href.casefold().startswith(("javascript:", "#")):
                                continue
                            parsed_href = urlparse(raw_href)
                            if parsed_href.scheme in {"http", "https"} or raw_href.startswith("//"):
                                candidate = urljoin("https://nsearchives.nseindia.com/corporate/", raw_href)
                            elif raw_href.lstrip("/").casefold().startswith(("corporate/", "content/", "archives/")):
                                candidate = urljoin("https://nsearchives.nseindia.com/", raw_href)
                            else:
                                candidate = urljoin("https://nsearchives.nseindia.com/corporate/", raw_href)
                            parsed_candidate = urlparse(candidate)
                            host = (parsed_candidate.hostname or "").casefold()
                            candidate_path = parsed_candidate.path.rstrip("/").casefold()
                            is_markup = candidate_path.endswith((".xml", ".xbrl", ".html", ".htm", ".csv", ".xlsx")) or "/xbrl/" in candidate_path
                            is_listing = "corporate-filings-announcements" in candidate_path
                            # This parser inspects only the NSE Attachment column;
                            # the XBRL/XML link is in a different table column.
                            if (
                                parsed_candidate.scheme in {"http", "https"}
                                and (host == "nseindia.com" or host.endswith(".nseindia.com"))
                                and not is_markup and not is_listing
                            ):
                                attachment_href = candidate
                                break
                item = {
                    "symbol": cell_value(symbol_i),
                    "company_name": cell_value(company_i),
                    "subject": cell_value(subject_i),
                    "desc": cell_value(detail_i),
                    "attchmntFile": attachment_href,
                    "dt": cell_value(date_i),
                }
                if any(item.get(key) for key in ("symbol", "company_name", "subject", "desc")):
                    rows.extend(self._normalise_nse_payload([item], transport=transport))

        # Stable deduplication is shared with the cross-transport merge below.
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            result.setdefault(str(row.get("source_item_id") or row.get("url")), row)
        return list(result.values())

    async def collect_nse_announcements(self, *, days: int = 7, limit: int = 500) -> CollectionResult:
        """Collect NSE announcements through a bounded RSS → public API → toolkit → browser waterfall.

        RSS remains the first transport. If it contains too few usable recent rows, this
        method supplements it through NSE's public corporate-announcement JSON endpoint,
        the independent ``jugaad-data`` NSELive toolkit, then a normal browser render of
        the public announcements page. Every transport is normalized here and returned to
        the existing canonical pipeline, which owns matching, validation, deduplication,
        and SQL persistence into ``news_alert_items``.
        """
        source = next((s for s in enabled_sources() if s.key == "nse_india"), None)
        if source is None:
            source = NewsSourceProfile(
                key="nse_india", name="National Stock Exchange (NSE India)",
                domains=("nseindia.com",), homepage_url="https://www.nseindia.com/",
                source_type="exchange-rss", tags=("exchange", "filings", "corporate-announcements"),
            )

        lookback_days = max(1, min(int(days or 7), 30))
        # The official NSE JSON endpoint returns several thousand records per
        # seven-day window. A 500/1500 cap silently discarded valid announcements
        # after fetching them, so retain the full merged window (bounded defensively).
        limit = max(1, min(int(limit or 10000), 10000))
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now(NSE_LOCAL_TZ)
        cutoff = now_utc - timedelta(days=lookback_days)
        start_day = now_local.date() - timedelta(days=lookback_days - 1)
        end_day = now_local.date()

        attempted: list[str] = []
        errors: list[str] = []
        items_by_identity: dict[str, dict[str, Any]] = {}
        identity_by_url: dict[str, str] = {}
        identity_by_content: dict[str, str] = {}
        # Less strict date-level keys are used only when they identify exactly one
        # record. This repairs API/page timestamp rounding differences without
        # merging repeated same-subject filings from the same issuer/day.
        identity_by_content_day: dict[str, set[str]] = {}
        hard_denial = False
        try:
            fallback_min_items = max(1, min(int(os.getenv("NEWSALERT_NSE_FALLBACK_MIN_ITEMS", "30")), 100))
        except (TypeError, ValueError):
            fallback_min_items = 30

        merge_diagnostics = {"rows_seen": 0, "bad_date": 0, "outside_window": 0, "bad_url": 0, "accepted_new": 0, "duplicate_or_merged": 0}

        def merge_rows(rows: list[dict[str, Any]], transport: str) -> int:
            added = 0
            for row in rows:
                merge_diagnostics["rows_seen"] += 1
                published = self._parse_nse_datetime(row.get("published_at") or row.get("published"))
                if published is None:
                    merge_diagnostics["bad_date"] += 1
                    continue
                if published < cutoff or published > now_utc + timedelta(minutes=5):
                    merge_diagnostics["outside_window"] += 1
                    continue
                url = str(row.get("url") or "").strip()
                if not url:
                    merge_diagnostics["bad_url"] += 1
                    continue
                parsed = urlparse(url)
                host = (parsed.hostname or "").casefold()
                if parsed.scheme not in {"http", "https"} or not (host == "nseindia.com" or host.endswith(".nseindia.com")):
                    merge_diagnostics["bad_url"] += 1
                    continue
                row["published_at"] = published
                row["source"] = "NSE Corporate Announcements"
                row["source_key"] = "nse_india"
                row["content_type"] = "NEWS"
                # Keep the specific transport attached by the parser. Overwriting
                # it with the wrapper label hid whether a record came from direct API,
                # toolkit, browser-captured API response, or browser HTML.
                row["transport"] = str(row.get("transport") or transport or "official-nse")
                row["nse_symbol"] = str(row.get("nse_symbol") or "").strip().upper()
                row["source_item_id"] = str(row.get("source_item_id") or "").strip()[:255]
                stable_url = url.split("#", 1)[0]
                source_identity = row["source_item_id"]
                # RSS GUIDs and the NSE JSON seq_id are not always the same.
                # Build a second, conservative cross-transport identity so the API
                # attachment/company fields enrich the matching RSS item rather
                # than leaving a parallel attachment-less duplicate.
                issuer_keys = list(dict.fromkeys(
                    re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
                    for value in (row.get("nse_symbol"), row.get("company_name"))
                    if str(value or "").strip()
                ))
                subject_key = re.sub(
                    r"[^a-z0-9]+", " ",
                    str(row.get("announcement_subject") or row.get("title") or "").casefold(),
                ).strip()
                content_keys: list[str] = []
                content_day_keys: list[str] = []
                if issuer_keys and subject_key:
                    local_published = published.astimezone(NSE_LOCAL_TZ)
                    minute = local_published.replace(second=0, microsecond=0).isoformat()
                    day = local_published.date().isoformat()
                    content_keys = [f"content:{issuer_key}|{subject_key}|{minute}" for issuer_key in issuer_keys]
                    content_day_keys = [f"content-day:{issuer_key}|{subject_key}|{day}" for issuer_key in issuer_keys]
                matched_identity = (
                    identity_by_url.get(stable_url)
                    or next((identity_by_content[key] for key in content_keys if key in identity_by_content), None)
                )
                if matched_identity is None:
                    incoming_transport = str(row.get("transport") or transport or "").casefold()
                    possible_day_matches: set[str] = set()
                    for key in content_day_keys:
                        for candidate_identity in identity_by_content_day.get(key, set()):
                            candidate_row = items_by_identity.get(candidate_identity) or {}
                            candidate_transport = str(candidate_row.get("transport") or "").casefold()
                            # Date-only matching is solely for cross-transport
                            # enrichment (RSS/API/browser clock rounding). Do not
                            # merge two distinct records from the same transport.
                            if candidate_transport and candidate_transport != incoming_transport:
                                possible_day_matches.add(candidate_identity)
                    if len(possible_day_matches) == 1:
                        matched_identity = next(iter(possible_day_matches))
                identity = matched_identity or (f"id:{source_identity}" if source_identity else f"url:{stable_url}")
                prior = items_by_identity.get(identity)
                if prior is None:
                    items_by_identity[identity] = row
                    identity_by_url[stable_url] = identity
                    for content_key in content_keys:
                        identity_by_content[content_key] = identity
                    for content_key in content_day_keys:
                        identity_by_content_day.setdefault(content_key, set()).add(identity)
                    added += 1
                    merge_diagnostics["accepted_new"] += 1
                else:
                    merge_diagnostics["duplicate_or_merged"] += 1
                    identity_by_url[stable_url] = identity
                    for content_key in content_keys:
                        identity_by_content[content_key] = identity
                    for content_key in content_day_keys:
                        identity_by_content_day.setdefault(content_key, set()).add(identity)
                    # Prefer an actual attachment/source URL and/or the richer description.
                    if len(str(row.get("summary") or "")) > len(str(prior.get("summary") or "")):
                        prior["summary"] = row.get("summary")
                        prior["description"] = row.get("description") or row.get("summary")
                    # The official API is richer than RSS: merge issuer identity,
                    # attachment and details even when RSS discovered the same item first.
                    for field in ("attachment_url", "attachment_type", "detail_url", "xbrl_url", "nse_symbol", "company_name", "announcement_subject", "isin"):
                        if row.get(field) and (not prior.get(field) or "official-nse-api" in str(row.get("transport") or "")):
                            prior[field] = row[field]
                    if source_identity and not prior.get("source_item_id"):
                        prior["source_item_id"] = source_identity
                    if "official-nse-api" in str(row.get("transport") or ""):
                        prior["transport"] = "official-nse-api"
                # Do not stop at the RSS cap: the API must be allowed to supplement it.

            return added

        def newest_collected_day() -> date | None:
            observed: list[date] = []
            for item in items_by_identity.values():
                published = self._parse_nse_datetime(item.get("published_at") or item.get("published"))
                if published is not None:
                    observed.append(published.astimezone(NSE_LOCAL_TZ).date())
            return max(observed) if observed else None

        def needs_supplement() -> bool:
            # Row count/newest date alone are not enough: RSS can return hundreds of
            # items all from today while omitting the earlier part of the backfill.
            # Require broad date coverage and run independent methods when coverage is thin.
            newest = newest_collected_day()
            observed_days = {
                self._parse_nse_datetime(item.get("published_at") or item.get("published")).astimezone(NSE_LOCAL_TZ).date()
                for item in items_by_identity.values()
                if self._parse_nse_datetime(item.get("published_at") or item.get("published")) is not None
            }
            minimum_distinct_days = min(5, lookback_days)
            return (
                len(items_by_identity) < fallback_min_items
                or newest is None
                or newest < end_day
                or len(observed_days) < minimum_distinct_days
            )

        # 1. Official RSS feed and archive-host alias.
        for feed_url in NSE_ANNOUNCEMENTS_RSS_ALIASES:
            if len(items_by_identity) >= limit:
                break
            attempted.append(f"rss:{feed_url}")
            try:
                content = await self._fetch_http_text(feed_url, timeout_seconds=16.0)
                rows, rss_diag = await asyncio.to_thread(self._parse_nse_rss_content, feed_url, content)
                if not rows:
                    # Keep the existing parser as a secondary syntax fallback, but do not
                    # silently confuse its generic zero-entry result with a transport error.
                    generic_rows = await asyncio.to_thread(self.parse_content, feed_url, content, source=source)
                    if generic_rows:
                        rows = generic_rows
                accepted = merge_rows(rows, "official-nse-rss")
                logger.info(
                    "FINANCIAL_NEWS_NSE_METHOD_RESULT method=rss url=%s content_chars=%s root=%s xml_entries=%s normalized=%s skipped=%s fallback_parsed=%s accepted=%s total=%s diagnostics=%s",
                    feed_url, len(content), rss_diag.get("root"), rss_diag.get("entries"),
                    rss_diag.get("normalized"), rss_diag.get("missing_date_or_fields"), len(rows),
                    accepted, len(items_by_identity), json.dumps(rss_diag, ensure_ascii=False, default=str)[:700],
                )
                # Merge every configured official RSS/archive alias. A successful
                # first feed can still be incomplete or lag another official alias.
            except Exception as exc:
                message = f"RSS {feed_url}: {type(exc).__name__}: {str(exc)[:200]}"
                errors.append(message)
                # Do not reopen the same URL with a different transport after an explicit denial.
                if any(token in str(exc).casefold() for token in ("http 401", "http 403", "http 429", "explicitly denied")):
                    logger.warning("FINANCIAL_NEWS_NSE_RSS_ACCESS_DENIED url=%s error=%s", feed_url, str(exc)[:180])

        # 2. Always query NSE's official JSON endpoint, even when RSS has filled
        # the display cap. RSS is only a supplemental discovery source: it omits
        # issuer metadata and attachments and is capped by NSE at 500 entries.
        attempted.append(f"api:{NSE_CORPORATE_ANNOUNCEMENTS_API}")
        self._last_nse_api_diagnostics = {}
        try:
            payload = await self._fetch_nse_api_payload(start_day, end_day)
            raw_api_records = self._nse_records_from_payload(payload)
            api_rows = await asyncio.to_thread(
                self._normalise_nse_payload, payload, transport="official-nse-api"
            )
            accepted = merge_rows(api_rows, "official-nse-api")
            logger.info(
                "FINANCIAL_NEWS_NSE_METHOD_RESULT method=api raw_records=%s normalized=%s accepted=%s total=%s diagnostics=%s",
                len(raw_api_records), len(api_rows), accepted, len(items_by_identity),
                json.dumps(getattr(self, "_last_nse_api_diagnostics", {}) or {}, ensure_ascii=False, default=str)[:900],
            )
            api_diag = getattr(self, "_last_nse_api_diagnostics", {}) or {}
            if api_diag.get("hard_denial"):
                hard_denial = True
                errors.append(f"NSE API partial results; further requests denied: {api_diag.get('hard_denial')}")
            # The RSS feed does not identify the issuer or attachment for many rows.
            # When the richer official API has returned normalized rows, do not let
            # those unidentified RSS-only records create companyless listing-page
            # duplicates. Keep the RSS fallback rows if the API produced no usable
            # records, or if the RSS item itself has a direct PDF attachment.
            if api_rows:
                unidentified_rss = [
                    identity for identity, row in items_by_identity.items()
                    if "rss" in str(row.get("transport") or "").casefold()
                    and not str(row.get("nse_symbol") or "").strip()
                    and not str(row.get("company_name") or "").strip()
                    and not str(row.get("attachment_url") or "").strip()
                ]
                for identity in unidentified_rss:
                    items_by_identity.pop(identity, None)
                if unidentified_rss:
                    merge_diagnostics["unidentified_rss_removed"] = len(unidentified_rss)
                    merge_diagnostics["accepted_new"] = max(
                        0, merge_diagnostics["accepted_new"] - len(unidentified_rss)
                    )
        except Exception as exc:
            errors.append(f"NSE API: {type(exc).__name__}: {str(exc)[:220]}")
            hard_denial = any(
                token in str(exc).casefold()
                for token in ("http 401", "http 403", "http 429", "explicitly denied")
            )
            logger.warning("FINANCIAL_NEWS_NSE_API_RESULT status=%s error=%s", "denied" if hard_denial else "failed", str(exc)[:220])

        # 3. Toolkit fallback. Merge it whenever earlier public sources leave room,
        # not only when a day-count heuristic says the feed is thin. This catches
        # same-day omissions that broad date coverage cannot reveal. Stop after an
        # explicit denial; do not bypass access controls.
        if not hard_denial and needs_supplement():
            attempted.append("toolkit:jugaad-data.NSELive.corporate_announcements")
            try:
                payload = await asyncio.wait_for(
                    asyncio.to_thread(self._jugaad_nse_fetch_sync, start_day, end_day), timeout=22.0
                )
                toolkit_raw = self._nse_records_from_payload(payload)
                toolkit_rows = await asyncio.to_thread(
                    self._normalise_nse_payload, payload, transport="jugaad-data-nse-toolkit"
                )
                accepted = merge_rows(toolkit_rows, "jugaad-data-nse-toolkit")
                logger.info(
                    "FINANCIAL_NEWS_NSE_METHOD_RESULT method=toolkit raw_records=%s normalized=%s accepted=%s total=%s",
                    len(toolkit_raw), len(toolkit_rows), accepted, len(items_by_identity),
                )
            except Exception as exc:
                errors.append(f"NSE toolkit: {type(exc).__name__}: {str(exc)[:220]}")
                hard_denial = any(
                    token in str(exc).casefold()
                    for token in ("http 401", "http 403", "http 429", "explicitly denied")
                )
                logger.warning("FINANCIAL_NEWS_NSE_TOOLKIT_RESULT status=%s error=%s", "denied" if hard_denial else "failed", str(exc)[:220])

        # 4. Ordinary browser rendering of the official announcement page. It is the
        # final public fallback and runs while below the cap, so it can supplement
        # missing same-day rows even if the other methods covered every date.
        # No challenge solving or login.
        # Render the page only when the actual NSE announcement feed is incomplete.
        # Missing attachment metadata is routed through the BSE issuer fallback by
        # the canonical pipeline; it must not trigger a slow browser scrape.
        if not hard_denial and needs_supplement():
            missing_pdf_before_browser = sum(
                1 for item in items_by_identity.values()
                if not str(item.get("attachment_url") or "").strip()
                and str(item.get("source") or "").casefold() == "nse corporate announcements"
            )
            if missing_pdf_before_browser:
                logger.info(
                    "FINANCIAL_NEWS_NSE_PDF_ENRICHMENT_REQUESTED missing_before=%s api_rows=%s",
                    missing_pdf_before_browser,
                    sum(1 for item in items_by_identity.values() if "official-nse-api" in str(item.get("transport") or "")),
                )
            attempted.append(f"browser:{NSE_ANNOUNCEMENTS_PAGE}")
            try:
                payloads, content, browser_diag = await asyncio.wait_for(
                    # Two public NSE aliases plus a single reload can take longer
                    # than one navigation timeout. Bound each hop and the whole
                    # scrape independently so one stalled page cannot block backfill.
                    asyncio.to_thread(self._playwright_scrape_nse_sync, 8000), timeout=50.0
                )
                browser_rows: list[dict[str, Any]] = []
                for payload in payloads:
                    browser_rows.extend(self._normalise_nse_payload(payload, transport="playwright-nse-api-response"))
                html_rows = await asyncio.to_thread(
                    self._parse_nse_announcement_html, content, transport="playwright-nse-html"
                )
                browser_rows.extend(html_rows)
                accepted = merge_rows(browser_rows, "playwright-nse-public-page")
                missing_pdf_after_browser = sum(
                    1 for item in items_by_identity.values()
                    if not str(item.get("attachment_url") or "").strip()
                    and str(item.get("source") or "").casefold() == "nse corporate announcements"
                )
                if missing_pdf_before_browser:
                    logger.info(
                        "FINANCIAL_NEWS_NSE_PDF_ENRICHMENT_RESULT missing_before=%s missing_after=%s browser_rows=%s",
                        missing_pdf_before_browser, missing_pdf_after_browser, len(browser_rows),
                    )
                logger.info(
                    "FINANCIAL_NEWS_NSE_METHOD_RESULT method=browser api_payloads=%s page_status=%s pages=%s html_chars=%s table_rows=%s parsed=%s accepted=%s total=%s",
                    browser_diag.get("api_payloads"), browser_diag.get("page_status"), browser_diag.get("pages_attempted"),
                    browser_diag.get("html_chars"), browser_diag.get("table_rows"), len(browser_rows), accepted,
                    len(items_by_identity),
                )
            except Exception as exc:
                error_text = str(exc).strip() or repr(exc)
                errors.append(f"NSE browser scrape: {type(exc).__name__}: {error_text[:220]}")
                hard_denial = any(
                    token in error_text.casefold()
                    for token in ("http 401", "http 403", "http 429", "explicitly denied")
                )
                logger.warning(
                    "FINANCIAL_NEWS_NSE_BROWSER_RESULT status=%s error_type=%s error=%s",
                    "denied" if hard_denial else "failed", type(exc).__name__, error_text[:220],
                )


        # Attachment gaps are deliberately not sent through repeated static HTML
        # fallbacks. The canonical pipeline resolves BSE code/stock URL for those
        # specific rows, avoiding redundant page requests after a complete API run.

        items = sorted(items_by_identity.values(), key=lambda row: row["published_at"], reverse=True)[:limit]
        # Include local-day distribution in diagnostics: a high row count is not enough
        # if the result stopped before the current exchange date.
        date_counts: dict[str, int] = {}
        for item in items:
            published = self._parse_nse_datetime(item.get("published_at") or item.get("published"))
            if published is not None:
                day_key = published.astimezone(NSE_LOCAL_TZ).date().isoformat()
                date_counts[day_key] = date_counts.get(day_key, 0) + 1
        latest_item_day = max(date_counts) if date_counts else None
        # Mark whether the waterfall was complete or ended on an explicit public-source denial.
        used_transports = sorted({str(item.get("transport") or "unknown") for item in items})
        if not items and not errors:
            errors.append("No valid in-window NSE announcements were returned by RSS, API, toolkit, or browser page")
        # attachment_url is populated only from NSE's explicit Attachment field (or
        # a PDF link in that table column), never from the separate XBRL/XML column.
        # Some valid download handlers are extensionless, so URL suffix alone is not
        # a reliable PDF-presence test.
        pdf_count = sum(1 for item in items if str(item.get("attachment_url") or "").strip())
        merge_diagnostics["pdf_attachments"] = pdf_count
        merge_diagnostics["missing_pdf_attachments"] = max(0, len(items) - pdf_count)
        merge_diagnostics["missing_pdf_sample"] = [
            {
                "symbol": str(item.get("nse_symbol") or "")[:40],
                "subject": str(item.get("announcement_subject") or item.get("title") or "")[:120],
                "source_item_id": str(item.get("source_item_id") or "")[:80],
                "transport": str(item.get("transport") or "")[:50],
            }
            for item in items
            if not str(item.get("attachment_url") or "").strip()
        ][:8]
        logger.info(
            "FINANCIAL_NEWS_NSE_WATERFALL_RESULT fetched=%s latest_item_day=%s requested_end_day=%s date_counts=%s rss=%s api_or_toolkit_or_browser=%s attempted=%s hard_denial=%s transports=%s errors=%s merge_diagnostics=%s",
            len(items), latest_item_day, end_day.isoformat(), date_counts,
            sum(1 for item in items if "rss" in str(item.get("transport") or "")),
            sum(1 for item in items if "rss" not in str(item.get("transport") or "")),
            len(attempted), hard_denial, used_transports, len(errors), merge_diagnostics,
        )
        return CollectionResult(
            "nse_india", "NSE Corporate Announcements", tuple(attempted), tuple(items), tuple(errors[:20])
        )

    # Backwards-compatible single URL interface used by the RSS pipeline. Explicit
    # NEWSALERT_RSS_FEEDS URLs are valid feed sources even when their host is not in
    # the built-in registry; they are parsed as public RSS/Atom, not article APIs.
    async def collect(self, feed_url: str) -> list[dict[str, Any]]:
        feed_url = str(feed_url or "").strip()
        parsed = urlparse(feed_url)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            raise ValueError("Configured news feed must be a valid public HTTP(S) URL")
        source = next(
            (s for s in enabled_sources() if any(host == d or host.endswith("." + d) for d in s.domains)),
            None,
        )
        attempts: list[str] = []
        contents: list[tuple[str, str]] = []
        try:
            contents.append((await self._fetch_http_text(feed_url, timeout_seconds=16.0), "httpx-public"))
        except Exception as exc:
            attempts.append(f"httpx: {type(exc).__name__}: {str(exc)[:180]}")
        if not contents:
            try:
                contents.append((await self.browser.fetch(feed_url), "BrowserClient"))
            except Exception as exc:
                attempts.append(f"BrowserClient: {type(exc).__name__}: {str(exc)[:180]}")
        rows: list[dict[str, Any]] = []
        for content, transport in contents:
            rows = await asyncio.to_thread(self.parse_content, feed_url, content, source=source)
            if rows:
                for row in rows:
                    row.setdefault("transport", transport)
                break
        if not rows:
            # A page may be served successfully but return a challenge/empty shell.
            # Try a normal browser-rendered fetch once for the exact configured URL.
            try:
                browser_content = await asyncio.to_thread(self._playwright_fetch_sync, feed_url, 15000)
                rows = await asyncio.to_thread(self.parse_content, feed_url, browser_content, source=source)
                for row in rows:
                    row.setdefault("transport", "public-browser-fallback")
            except Exception as exc:
                attempts.append(f"Playwright public page: {type(exc).__name__}: {str(exc)[:180]}")
        if not rows and attempts:
            raise RuntimeError("; ".join(attempts[:3]))
        return rows

    def parse_content(self, feed_url: str, content: str, *, source: NewsSourceProfile | None = None) -> list[dict[str, Any]]:
        stripped = content.lstrip().lower()
        if stripped.startswith("<?xml") or stripped.startswith("<rss") or stripped.startswith("<feed") or "<rss" in stripped[:1000]:
            rows = self._parse_xml(feed_url, content, source=source)
            if rows:
                return rows
        return self._parse_html(feed_url, content, source)

    @staticmethod
    def _nse_headline(title: str, summary: str, entry: Any = None) -> str:
        """Promote NSE's announcement subject/details into the stored headline.

        NSE RSS entries can expose only the issuer symbol/name as ``title`` while
        the actual filing subject is carried in summary/description/custom fields.
        Keep the issuer in the headline for deterministic Coverage matching, but
        always surface the filing subject when it is available.
        """
        base = " ".join(str(title or "").split()).strip()
        candidates = []
        if entry is not None:
            # Explicit announcement subjects are more useful than long feed summaries.
            for key in (
                "subject", "announcement_subject", "announcementtitle", "attchmnttext",
                "announcementtext", "desc", "headline", "announcement", "details",
            ):
                value = entry.get(key) if hasattr(entry, "get") else None
                if value:
                    candidates.append(str(value))
            tags = entry.get("tags") if hasattr(entry, "get") else None
            if isinstance(tags, list):
                candidates.extend(
                    str(tag.get("term") or "")
                    for tag in tags
                    if isinstance(tag, dict)
                )
        candidates.append(summary)
        for candidate in candidates:
            detail = " ".join(str(candidate or "").split()).strip(" -–—|•")
            if not detail or len(detail) < 4:
                continue
            if detail.casefold() == base.casefold():
                continue
            # Avoid treating a bare publisher/channel label as an announcement subject.
            if detail.casefold() in {"corporate announcements", "nse india", "national stock exchange"}:
                continue
            return f"{base} — {detail}"[:1000] if base else detail[:1000]
        return base[:1000]

    def _parse_xml(self, feed_url: str, content: str, *, source: NewsSourceProfile | None) -> list[dict[str, Any]]:
        try:
            import feedparser
            parsed = feedparser.parse(content)
            feed_title = (parsed.feed.get("title") or (source.name if source else self._source_name(feed_url))).strip()
            rows: list[dict[str, Any]] = []
            for entry in parsed.entries:
                link = self._best_feed_link(entry, feed_url)
                title = (entry.get("title") or "").strip()
                if not link or not title:
                    continue
                raw_title = title
                summary = self._clean(entry.get("summary") or entry.get("description") or entry.get("content") or "")
                nse_symbol = ""
                nse_company_name = ""
                nse_subject = ""
                nse_source_id = ""
                if source and source.key == "nse_india":
                    nse_symbol = self._feed_field(entry, (
                        "symbol", "nse_symbol", "security_symbol", "securitysymbol", "ticker",
                    )).upper()
                    nse_company_name = self._feed_field(entry, (
                        "company_name", "companyname", "sm_name", "company", "issuer", "security_name",
                    ))
                    nse_subject = self._feed_field(entry, (
                        "subject", "announcement_subject", "announcementtitle", "attchmnttext", "announcementtext", "desc",
                    ))
                    nse_source_id = self._feed_field(entry, (
                        "seq_id", "seqid", "announcement_id", "announcementid", "guid", "id",
                    ))
                    title = self._nse_headline(raw_title, summary, entry)
                if source and source.key == "nse_india":
                    nse_date_raw = self._feed_field(entry, (
                        "dt", "an_dt", "sort_date", "broadcastDateTime", "broadcastDate", "broadcast_date",
                        "announcementDate", "announcement_date", "publishedDate", "published_date", "pubDate",
                        "published", "updated", "date", "exchdisstime", "dissemDT",
                    ))
                    published = self._parse_nse_datetime(nse_date_raw) if nse_date_raw else None
                    if published is None:
                        published = self._entry_datetime(entry)
                else:
                    published = self._entry_datetime(entry)
                if published is None:
                    continue
                absolute_link = urljoin(feed_url, link)
                nse_attachment_url = ""
                if source and source.key == "nse_india":
                    attachment = self._feed_field(entry, (
                        "attchmntfile", "attachment", "attachment_file", "attachmentfile", "attachment_url", "fileurl",
                        "xbrl", "xbrlfile", "pdfurl", "downloadurl",
                    ))
                    if attachment:
                        attachment_candidate = urljoin(feed_url, attachment.replace("&amp;", "&"))
                        attachment_parts = urlparse(attachment_candidate)
                        attachment_host = (attachment_parts.hostname or "").casefold()
                        if attachment_parts.scheme in {"http", "https"} and (attachment_host == "nseindia.com" or attachment_host.endswith(".nseindia.com")) and not _is_markup_document(attachment_candidate):
                            # Store a document/XBRL link as evidence, never as the
                            # end-user headline link.
                            nse_attachment_url = attachment_candidate

                # Google News RSS uses news.google.com entry links; keep those
                # because they redirect to the publisher article. Do not replace
                # them with the feed's <source href>, which would discard the
                # article-specific destination.
                google_news = (urlparse(absolute_link).hostname or "").lower() in {"news.google.com", "www.news.google.com"}
                if source and not self._allowed(absolute_link, source) and not google_news:
                    source_obj = entry.get("source") or {}
                    source_href = source_obj.get("href") if hasattr(source_obj, "get") else None
                    if source_href and self._allowed(str(source_href), source):
                        # Only use source_href when the entry itself is unusable.
                        # The canonicalizer later converts exchange XML entries to
                        # a human-readable publisher page.
                        absolute_link = str(source_href)
                    else:
                        continue
                entry_source = entry.get("source") or {}
                entry_source_name = ""
                if hasattr(entry_source, "get"):
                    entry_source_name = str(entry_source.get("title") or "").strip()
                row_source = "NSE Corporate Announcements" if source and source.key == "nse_india" else (entry_source_name or feed_title)
                row = self._row(feed_url, row_source, title, absolute_link, published, summary, source)
                if source and source.key == "nse_india":
                    item_id = nse_source_id or str(entry.get("id") or entry.get("guid") or "").strip()
                    if not item_id:
                        item_id = hashlib.sha256(f"{nse_symbol}|{nse_company_name}|{nse_subject}|{published.isoformat()}|{summary[:400]}".encode("utf-8", errors="replace")).hexdigest()[:32]
                    row.update({
                        "url": _nse_corporate_announcement_page_url(nse_symbol, item_id),
                        "attachment_url": nse_attachment_url or None,
                        "source": "NSE Corporate Announcements",
                        "source_key": "nse_india",
                        "nse_symbol": nse_symbol,
                        "company_name": nse_company_name or raw_title,
                        "announcement_subject": nse_subject,
                        "source_item_id": item_id,
                        "content_type": "NEWS",
                        "transport": "official-nse-rss",
                    })
                rows.append(row)
            # feedparser can return an empty entry list without raising for some
            # exchange RSS/XML variants. The previous code returned [] immediately,
            # so the ElementTree parser was never given a chance to read those rows.
            return rows if rows else self._parse_xml_etree(feed_url, content, source=source)
        except Exception:
            return self._parse_xml_etree(feed_url, content, source=source)

    def _parse_xml_etree(self, feed_url: str, content: str, *, source: NewsSourceProfile | None) -> list[dict[str, Any]]:
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return []

        feed_title = source.name if source else self._source_name(feed_url)
        channel_title = root.find(".//channel/title")
        if channel_title is not None and channel_title.text:
            feed_title = channel_title.text.strip()

        rows: list[dict[str, Any]] = []
        # Some feeds use namespaces (or change them without notice); match entries
        # by their local element name instead of relying only on unqualified XPath.
        entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].casefold() in {"item", "entry"}]
        for item in entries:
            def text(*paths: str) -> str:
                wanted = {re.sub(r"[^a-z0-9]", "", path.rsplit("}", 1)[-1].casefold()) for path in paths}
                for node in item.iter():
                    local = re.sub(r"[^a-z0-9]", "", node.tag.rsplit("}", 1)[-1].casefold())
                    if local in wanted and node.text and node.text.strip():
                        return " ".join(node.text.split()).strip()
                return ""

            def field(names: Iterable[str]) -> str:
                wanted = {re.sub(r"[^a-z0-9]", "", str(name).casefold()) for name in names}
                for node in item.iter():
                    local = re.sub(r"[^a-z0-9]", "", node.tag.rsplit("}", 1)[-1].casefold())
                    if local in wanted and node.text and node.text.strip():
                        return " ".join(node.text.split()).strip()
                return ""

            raw_title = text("title", "{http://www.w3.org/2005/Atom}title")
            title = raw_title
            link = ""
            atom_links = item.findall("{http://www.w3.org/2005/Atom}link")
            for atom_link in atom_links:
                href = atom_link.attrib.get("href", "").strip()
                rel = atom_link.attrib.get("rel", "alternate").lower()
                typ = atom_link.attrib.get("type", "").lower()
                if href and (rel in {"alternate", "canonical"} or "html" in typ):
                    link = href
                    break
            if not link:
                link = text("link", "{http://www.w3.org/2005/Atom}link")
            published = text(
                "pubDate", "published", "broadcastDateTime", "broadcastDate", "broadcast_date", "an_dt", "sort_date",
                "announcementDate", "announcement_date", "publishedDate", "published_date", "dateAndTime",
                "date_time", "exchdisstime", "dissemDT", "date", "updated",
            )
            description = text("description", "summary", "{http://www.w3.org/2005/Atom}summary", "details", "detail")
            nse_symbol = field(("symbol", "nse_symbol", "security_symbol", "ticker")).upper() if source and source.key == "nse_india" else ""
            nse_company = field(("company_name", "companyName", "sm_name", "company", "issuer", "security_name")) if source and source.key == "nse_india" else ""
            nse_subject = field(("subject", "announcement_subject", "announcementTitle", "attchmntText", "announcementText", "desc")) if source and source.key == "nse_india" else ""
            nse_source_id = field(("seq_id", "seqId", "announcement_id", "announcementId", "guid", "id")) if source and source.key == "nse_india" else ""
            nse_attachment_url = ""
            if source and source.key == "nse_india":
                # The subject is the most useful short headline; summary carries
                # disclosure detail and remains available separately in the record.
                title = self._nse_headline(raw_title, nse_subject or description)
                attachment = field(("attchmntFile", "attachment", "attachmentFile", "attachment_url", "fileUrl", "xbrl", "xbrlFile", "pdfUrl", "downloadUrl"))
                if attachment:
                    candidate = urljoin(feed_url, attachment.replace("&amp;", "&"))
                    candidate_parts = urlparse(candidate)
                    candidate_host = (candidate_parts.hostname or "").casefold()
                    if candidate_parts.scheme in {"http", "https"} and (candidate_host == "nseindia.com" or candidate_host.endswith(".nseindia.com")) and not _is_markup_document(candidate):
                        nse_attachment_url = candidate
            # Google News RSS puts the true publisher in each entry's <source>.
            publisher_node = item.find("source")
            if publisher_node is None:
                publisher_node = item.find("{http://www.w3.org/2005/Atom}source")
            publisher_name = " ".join("".join(publisher_node.itertext()).split()) if publisher_node is not None else ""
            row_source = "NSE Corporate Announcements" if source and source.key == "nse_india" else (publisher_name or feed_title)
            if title and link:
                published_at = (
                    self._parse_nse_datetime(published)
                    if source and source.key == "nse_india"
                    else self._parse_date(published)
                )
                if published_at is None:
                    continue
                absolute_link = urljoin(feed_url, link)
                row = self._row(feed_url, row_source, title, absolute_link, published_at, self._clean(description), source)
                if source and source.key == "nse_india":
                    item_id = nse_source_id or hashlib.sha256(f"{nse_symbol}|{nse_company}|{nse_subject}|{published_at.isoformat()}|{description[:400]}".encode("utf-8", errors="replace")).hexdigest()[:32]
                    row.update({
                        "url": _nse_corporate_announcement_page_url(nse_symbol, item_id),
                        "attachment_url": nse_attachment_url or None,
                        "source": "NSE Corporate Announcements", "source_key": "nse_india",
                        "nse_symbol": nse_symbol, "company_name": nse_company or raw_title,
                        "announcement_subject": nse_subject,
                        "source_item_id": item_id,
                        "content_type": "NEWS", "transport": "official-nse-rss",
                    })
                rows.append(row)
        return rows

    def _parse_html(self, page_url: str, content: str, source: NewsSourceProfile | None) -> list[dict[str, Any]]:
        if source is not None and source.key == "zerodha_pulse":
            return self._parse_zerodha_pulse(page_url, content, source)
        soup = BeautifulSoup(content, "xml")
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        source_name = source.name if source else self._source_name(page_url)

        for node in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
            raw = node.string or node.get_text()
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            for obj in self._flatten_jsonld(payload):
                if not isinstance(obj, dict):
                    continue
                types = obj.get("@type")
                types = types if isinstance(types, list) else [types]
                if not any(str(t) in {"NewsArticle", "Article", "ReportageNewsArticle"} for t in types):
                    continue
                title = (obj.get("headline") or obj.get("name") or "").strip()
                link = obj.get("url") or page_url
                if not title or link in seen:
                    continue
                published = self._parse_date(obj.get("datePublished") or obj.get("dateModified"))
                if published is None:
                    continue
                seen.add(link)
                rows.append(self._row(
                    page_url,
                    source_name,
                    title,
                    urljoin(page_url, link),
                    published,
                    self._clean(obj.get("description") or obj.get("articleBody") or ""),
                    source,
                ))

        if rows:
            return rows

        # Conservative card fallback: require an actual href and a title-sized
        # heading to avoid polluting the DB with navigation links.
        for article in soup.find_all("article"):
            heading = article.find(["h1", "h2", "h3", "h4"])
            anchor = article.find("a", href=True)
            if not heading or not anchor:
                continue
            title = heading.get_text(" ", strip=True)
            link = urljoin(page_url, anchor.get("href", ""))
            if len(title) < 12 or link in seen or (source is not None and not self._allowed(link, source)):
                continue
            time_node = article.find("time")
            published_value = ""
            if time_node is not None:
                published_value = str(time_node.get("datetime") or time_node.get("content") or time_node.get_text(" ", strip=True))
            if not published_value:
                date_meta = article.find(attrs={"itemprop": re.compile(r"^(datePublished|dateCreated|dateModified)$", re.I)})
                if date_meta is not None:
                    published_value = str(date_meta.get("content") or date_meta.get("datetime") or date_meta.get_text(" ", strip=True))
            published = self._parse_date(published_value)
            if published is None:
                continue
            seen.add(link)
            rows.append(self._row(page_url, source_name, title, link, published, "", source))

        return rows

    def _parse_zerodha_pulse(
        self, page_url: str, content: str, source: NewsSourceProfile
    ) -> list[dict[str, Any]]:
        """Parse Pulse cards but keep each original publisher URL, not Pulse.

        Pulse is an aggregator, so outgoing links are expected to be on the
        publisher's domain. We require a visible publication-age marker near the
        headline to avoid capturing navigation/footer links as articles.
        """
        soup = BeautifulSoup(content, "lxml")
        now = datetime.now(timezone.utc)
        relative_re = re.compile(
            r"\b(\d+)\s*(minutes?|mins?|hours?|days?|weeks?)\s+ago\b", re.I
        )
        publisher_map = {
            "thehindu.com": "The Hindu",
            "thehindubusinessline.com": "The Hindu BusinessLine",
            "business-standard.com": "Business Standard",
            "economictimes.indiatimes.com": "The Economic Times",
            "indiatimes.com": "The Economic Times",
            "livemint.com": "LiveMint",
            "ndtvprofit.com": "NDTV Profit",
            "ndtv.com": "NDTV Business",
            "moneycontrol.com": "Moneycontrol",
            "cnbctv18.com": "CNBC-TV18",
            "reuters.com": "Reuters",
            "bloomberg.com": "Bloomberg",
            "businesstoday.in": "Business Today",
        }

        def parse_relative(text: str, card) -> datetime | None:
            time_node = card.find("time") if card is not None else None
            if time_node is not None:
                absolute = self._parse_date(
                    time_node.get("datetime") or time_node.get("content") or time_node.get_text(" ", strip=True)
                )
                if absolute is not None:
                    return absolute.astimezone(timezone.utc)
            match = relative_re.search(text or "")
            if match:
                amount = int(match.group(1))
                unit = match.group(2).lower()
                if unit.startswith(("minute", "min")):
                    delta = timedelta(minutes=amount)
                elif unit.startswith("hour"):
                    delta = timedelta(hours=amount)
                elif unit.startswith("day"):
                    delta = timedelta(days=amount)
                else:
                    delta = timedelta(weeks=amount)
                return now - delta
            lower = (text or "").lower()
            if "just now" in lower or "moments ago" in lower:
                return now
            if "yesterday" in lower:
                return now - timedelta(days=1)
            return None

        def origin_label(host: str, card_text: str, time_match) -> str:
            if time_match:
                tail = card_text[time_match.end():].strip(" \t\r\n—–-•|:")
                tail = re.split(r"\s{2,}|\s+\|\s+", tail, maxsplit=1)[0].strip(" \t\r\n—–-•|:")
                if 2 <= len(tail) <= 70 and not re.search(r"\b(?:ago|read more|share)\b", tail, re.I):
                    # Keep common display names as Pulse shows them; they identify
                    # the originating publisher, unlike the aggregator brand.
                    if normalize_label := tail:
                        if normalize_label.casefold() not in {"zerodha pulse", "pulse", "read more"}:
                            return normalize_label
            for domain, label in publisher_map.items():
                if host == domain or host.endswith("." + domain):
                    return label
            return host.removeprefix("www.") or source.name

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        pulse_host = "pulse.zerodha.com"
        for anchor in soup.find_all("a", href=True):
            href = urljoin(page_url, str(anchor.get("href") or "").strip())
            parsed = urlparse(href)
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in {"http", "https"} or not host:
                continue
            if host == pulse_host or host.endswith("." + pulse_host):
                continue
            # This user explicitly removed Financial Express from the source set;
            # do not re-introduce it indirectly via the aggregator.
            if host == "financialexpress.com" or host.endswith(".financialexpress.com"):
                continue
            if href in seen or parsed.path.lower().endswith((".pdf", ".xml", ".jpg", ".png", ".mp4")):
                continue

            card = anchor
            card_text = anchor.get_text(" ", strip=True)
            for parent in list(anchor.parents)[:7]:
                text = parent.get_text(" ", strip=True)
                if len(text) > 15 and len(text) <= 1800 and (relative_re.search(text) or "yesterday" in text.lower() or parent.find("time")):
                    card = parent
                    card_text = text
                    break
            published = parse_relative(card_text, card)
            if published is None:
                continue

            title = anchor.get_text(" ", strip=True)
            if len(title) < 18 or len(title) > 1000:
                heading = card.find(["h1", "h2", "h3", "h4"]) if hasattr(card, "find") else None
                title = heading.get_text(" ", strip=True) if heading else str(anchor.get("title") or "").strip()
                # If this anchor was a short publisher/category link within the card,
                # use the headline's own outbound link instead of linking to the source
                # homepage or a category page.
                heading_anchor = heading.find("a", href=True) if heading is not None else None
                if heading_anchor is not None:
                    headline_link = urljoin(page_url, str(heading_anchor.get("href") or "").strip())
                    headline_host = (urlparse(headline_link).hostname or "").lower()
                    if headline_host and headline_host != pulse_host and not headline_host.endswith("." + pulse_host):
                        if headline_host == "financialexpress.com" or headline_host.endswith(".financialexpress.com"):
                            continue
                        href = headline_link
                        host = headline_host
                    else:
                        continue
            title = self._clean(title)[:1000]
            if len(title) < 18:
                continue
            title_fold = title.casefold()
            pulse_noise_terms = (
                "candlestick", "candle stick", "technical analysis", "chart pattern",
                "top gainers", "top losers", "stocks to buy", "stocks to sell",
                "stock recommendation", "share recommendation", "price target",
                "market wrap", "market live", "stock price", "share price",
                "sensex today", "nifty today", "intraday picks", "trading strategy",
            )
            pulse_movement = re.search(
                r"\b(?:stocks?|shares?)\b.{0,70}\b(?:rally|rallies|surge|soar|soared|jump|jumps|rose|rise|rises|gain|gains|gained|fall|falls|fell|drop|drops|slip|slips|decline|declines|plunge|plunges|tumble|tumbles|zoom|zooms|climb|climbs|sink|sinks|hit (?:a )?(?:high|low)|touch(?:ed)? (?:a )?(?:high|low))\b",
                title_fold,
            ) or re.search(
                r"\b(?:rally|rallies|surge|soar|jump|rose|rise|gain|fall|fell|drop|slip|decline|plunge|tumble|zoom|climb|sink|dips?|slides?)\b.{0,70}\b(?:stocks?|shares?)\b",
                title_fold,
            )
            sensex_move = re.search(
                r"\b(?:sensex|nifty)\b.{0,80}\b(?:rally|rallies|surge|surges|jump|jumps|rose|rise|rises|fell|fall|falls|drop|drops|slip|slips|dips?|slides?|declines?|plunges?)\b",
                title_fold,
            )
            if any(term in title_fold for term in pulse_noise_terms) or pulse_movement or sensex_move:
                continue
            # Exclude non-article share/account links even if they are inside a card.
            if any(marker in title.casefold() for marker in ("sign in", "subscribe", "watch live", "home page")):
                continue

            time_match = relative_re.search(card_text)
            publisher = origin_label(host, card_text, time_match)
            summary = card_text.replace(title, "", 1).strip()[:10000]
            row = self._row(page_url, publisher, title, href, published, summary, source)
            row["url"] = href
            row["source_url"] = href
            row["source_key"] = "zerodha_pulse"
            rows.append(row)
            seen.add(href)
        # Sort newest first; this page mixes cards and other linked content.
        rows.sort(key=lambda item: item.get("published_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows

    @staticmethod
    def _flatten_jsonld(payload: Any) -> Iterable[dict[str, Any]]:
        if isinstance(payload, dict):
            graph = payload.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    if isinstance(item, dict):
                        yield item
            yield payload
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    yield item

    @staticmethod
    def _allowed(url: str, source: NewsSourceProfile) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == domain or host.endswith("." + domain) for domain in source.domains)

    @classmethod
    def _discover_feed_urls(cls, page_url: str, content: str, source: NewsSourceProfile) -> list[str]:
        soup = BeautifulSoup(content, "lxml")
        candidates: list[str] = []
        for link in soup.find_all("link", href=True):
            rel = " ".join(link.get("rel") or []).lower()
            typ = (link.get("type") or "").lower()
            href = urljoin(page_url, link.get("href", ""))
            if "alternate" in rel and ("rss" in typ or "atom" in typ or href.lower().endswith((".xml", "/rss", "rss.xml"))):
                if cls._allowed(href, source):
                    candidates.append(href)
        for anchor in soup.find_all("a", href=True):
            text = anchor.get_text(" ", strip=True).lower()
            href = urljoin(page_url, anchor.get("href", ""))
            marker = f"{text} {href.lower()}"
            if any(token in marker for token in ("rss", "atom", "feed", "xml")) and cls._allowed(href, source):
                candidates.append(href)
        # Stable deterministic order, no duplicates.
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _row(
        feed_url: str,
        source_name: str,
        title: str,
        link: str,
        published: datetime,
        summary: str,
        source: NewsSourceProfile | None,
    ) -> dict[str, Any]:
        return {
            "title": title.strip(),
            "url": link,
            "published_at": published.astimezone(timezone.utc),
            "source": source_name or (
                source.name if source else FinancialNewsCollector._source_name(feed_url)
            ),
            "source_key": source.key if source else None,
            "source_url": feed_url,
            "summary": summary[:10000],
    }


    @staticmethod
    def _source_name(url: str) -> str:
        return (urlparse(url).hostname or "unknown").lower().removeprefix("www.")

    @staticmethod
    def _clean(value: str) -> str:
        return BeautifulSoup(value or "", "lxml").get_text(" ", strip=True)

    @staticmethod
    def _parse_date(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                dt = parsedate_to_datetime(text)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                return None

    @staticmethod
    def _feed_field(entry: Any, names: Iterable[str]) -> str:
        """Read optional RSS custom fields across feedparser's key normalizations."""
        if not hasattr(entry, "get"):
            return ""
        for name in names:
            value = entry.get(name)
            if value is None:
                # Feedparser commonly converts CamelCase/custom XML tags to lower-case.
                value = entry.get(str(name).replace("_", "").casefold())
            if isinstance(value, dict):
                value = value.get("href") or value.get("url") or value.get("value")
            if isinstance(value, list):
                value = next((v for v in value if str(v or "").strip()), "")
            text = " ".join(str(value or "").split()).strip()
            if text:
                return text
        return ""

    @classmethod
    def _entry_datetime(cls, entry: Any) -> datetime | None:
        for key in (
            "published", "pubdate", "broadcastdate", "broadcast_date", "an_dt",
            "sort_date", "date", "updated", "created",
        ):
            value = entry.get(key) if hasattr(entry, "get") else None
            if value:
                parsed = cls._parse_date(value)
                if parsed is not None:
                    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        value = getattr(entry, "published_parsed", None)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
        value = getattr(entry, "updated_parsed", None)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
        return None

    @staticmethod
    def _best_feed_link(entry: Any, feed_url: str) -> str | None:
        """Choose a real article/web link from an RSS/Atom entry.

        Prefer alternate/canonical HTML links over the first link returned by a
        parser. Exchange feeds sometimes expose a raw XML/XBRL file as the entry
        URL; that is retained as source metadata but should not become the user
        facing Open link.
        """
        links = entry.get("links") or []
        candidates: list[tuple[int, str]] = []
        for link_obj in links:
            href = str(link_obj.get("href") or "").strip() if hasattr(link_obj, "get") else ""
            if not href:
                continue
            rel = str(link_obj.get("rel") or "").lower() if hasattr(link_obj, "get") else ""
            typ = str(link_obj.get("type") or "").lower() if hasattr(link_obj, "get") else ""
            score = 0
            if rel in {"alternate", "canonical"}:
                score += 100
            if "html" in typ:
                score += 20
            if href.lower().split("?")[0].endswith(".xml"):
                score -= 80
            candidates.append((score, href))
        if candidates:
            candidates.sort(key=lambda item: (-item[0], item[1]))
            return candidates[0][1]
        return str(entry.get("link") or "").strip() or None

# ---------------------------------------------------------------------------
# NSE Board Meetings and Corporate Filings Actions (NewsAlert NSE source).
# Collected by collect_nse_filing_items(), which the NSE pipeline step calls
# alongside FinancialNewsCollector.collect_nse_announcements(). Never raises.
# ---------------------------------------------------------------------------

NSE_ORIGIN = "https://nsearchives.nseindia.com/"
# NSE caps large equity windows, so requests are split into the same two-day
# windows the corporate announcements ingest uses.
WINDOW_DAYS = 2
MAX_LOOKBACK_DAYS = 7


@dataclass(frozen=True)
class NseFilingFeed:
    key: str
    source: str
    page_url: str
    api_path: str
    subject_fields: tuple[str, ...]
    detail_fields: tuple[str, ...]
    date_fields: tuple[str, ...]
    fallback_title: str
    # When True, a row's event date is its broadcast timestamp only. Meeting dates
    # are never used for filtering, and rows without a broadcast time are dropped.
    broadcast_only: bool = False
    # Extra days to query beyond the lookback, because NSE's date filter does not
    # necessarily follow broadcast time; upcoming meetings are fetched and then
    # filtered locally by broadcast date.
    query_forward_days: int = 0
    query_lead_days: int = 0


NSE_FILING_FEEDS: tuple[NseFilingFeed, ...] = (
    NseFilingFeed(
        key="board_meetings",
        source="NSE Board Meetings",
        page_url="https://www.nseindia.com/companies-listing/corporate-filings-board-meetings",
        api_path="/api/corporate-board-meetings",
        subject_fields=("bm_purpose", "purpose", "bm_desc", "desc", "subject"),
        detail_fields=("bm_desc", "description", "details", "remarks"),
        # NSE board-meeting API rows use bm_timestamp/sysTime for the
        # dissemination timestamp. bm_date is the future meeting date, not the
        # announcement publication time, so do not use it for recency filtering.
        date_fields=("bm_timestamp", "sysTime", "sys_time", "exchdisstime", "broadcastDate", "broadcast_date", "disseminationTime", "an_dt"),
        fallback_title="Board meeting",
        broadcast_only=True,
        query_forward_days=30,
        query_lead_days=3,
    ),
    NseFilingFeed(
        key="corporate_actions",
        source="NSE Corporate Filings Actions",
        page_url="https://www.nseindia.com/companies-listing/corporate-filings-actions",
        api_path="/api/corporates-corporateActions",
        subject_fields=("subject", "purpose", "ca_purpose", "desc"),
        detail_fields=("remarks", "details", "description", "faceVal", "series"),
        date_fields=("timestamp", "broadcastDate", "an_dt", "exDate", "recDate", "date"),
        fallback_title="Corporate action",
    ),
)

_ATTACHMENT_FIELDS = (
    "attchmntFile", "attachment", "attachmentFile", "attachment_file", "attachment_url",
    "attachmentUrl", "fileUrl", "file_url", "pdfUrl", "pdf_url", "downloadUrl", "download_url",
)
_SYMBOL_FIELDS = ("symbol", "nse_symbol", "securitySymbol", "bm_symbol", "sm_symbol")
_COMPANY_FIELDS = ("companyName", "company", "comp", "sm_name", "company_name", "name", "bm_company", "bm_company_name", "bm_name", "bm_scrip_name", "securityName", "security_name")
_ID_FIELDS = ("bm_id", "bmId", "seq_id", "seqId", "announcement_id", "announcementId", "id", "newsId")


def _filing_clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def _value(record: dict[str, Any], *names: str) -> str:
    return _filing_clean(FinancialNewsCollector._nse_record_value(record, *names))


def _is_markup_document(url: str) -> bool:
    """True for XBRL/XML/HTML documents. These are machine filings, not readable links."""
    path = urlparse(str(url or "")).path.casefold()
    return path.endswith((".xml", ".xbrl", ".html", ".htm")) or "/xbrl/" in path


def _attachment_url(value: str) -> str | None:
    """Return a human-openable NSE document (PDF/XBRL) URL, never a listing page."""
    if not value:
        return None
    candidate = urljoin(NSE_ORIGIN, value.replace("&amp;", "&").strip())
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"}:
        return None
    if not (host == "nseindia.com" or host.endswith(".nseindia.com")):
        return None
    if "companies-listing" in parsed.path.casefold():
        return None
    if _is_markup_document(candidate):
        return None
    return candidate


def _page_url(feed: NseFilingFeed, symbol: str, source_item_id: str) -> str:
    """Stable per-item fallback link on the official NSE filings page."""
    query: list[tuple[str, str]] = []
    if symbol:
        query.append(("symbol", symbol))
    # Stable per-item token keeps distinct filings for one issuer from collapsing
    # into one canonical URL key.
    if source_item_id:
        query.append(("newsalert_id", source_item_id[:96]))
    return f"{feed.page_url}?{urlencode(query)}" if query else feed.page_url


def _normalise_record(
    feed: NseFilingFeed,
    record: dict[str, Any],
    *,
    start_day,
    end_day,
    stats: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    def _drop(reason: str) -> None:
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1
        return None

    symbol = _value(record, *_SYMBOL_FIELDS).upper()
    company = _value(record, *_COMPANY_FIELDS)
    if not symbol and not company:
        return None

    subject = _value(record, *feed.subject_fields) or feed.fallback_title
    details = _value(record, *feed.detail_fields)
    if details.casefold() == subject.casefold():
        details = ""

    raw_date = _value(record, *feed.date_fields)
    published = FinancialNewsCollector._parse_nse_datetime(raw_date) if raw_date else None
    if published is None and feed.broadcast_only:
        return _drop("no_broadcast_time")
    if published is None:
        published = datetime.combine(end_day, time.min, tzinfo=NSE_LOCAL_TZ)
    elif published.tzinfo is None:
        published = published.replace(tzinfo=NSE_LOCAL_TZ)
    local_day = published.astimezone(NSE_LOCAL_TZ).date()
    if local_day < start_day or local_day > end_day:
        return _drop("broadcast_outside_window")

    item_id = _value(record, *_ID_FIELDS)
    if item_id:
        source_item_id = f"{feed.key}:{item_id}"
    else:
        digest_basis = "|".join((feed.key, symbol, company, subject, published.isoformat(), details[:500]))
        source_item_id = f"{feed.key}:{hashlib.sha256(digest_basis.encode('utf-8', errors='replace')).hexdigest()[:32]}"

    title = subject
    if company and company.casefold() not in subject.casefold():
        title = f"{company}: {subject}"

    attachment = _attachment_url(_value(record, *_ATTACHMENT_FIELDS))

    return {
        "title": title[:1000],
        # Clicked URL: the NSE PDF/XBRL document when NSE supplies one, otherwise the
        # official filings page. Consumers pick the attachment for display; the page
        # URL remains the dedupe identity.
        "url": _page_url(feed, symbol, source_item_id),
        "attachment_url": attachment,
        "published_at": published,
        "source": feed.source,
        "source_key": "nse_india",
        "source_url": feed.page_url,
        "source_item_id": source_item_id[:255],
        "nse_symbol": symbol,
        "isin": _value(record, "sm_isin", "isin", "isin_code", "security_isin", "bm_isin").upper(),
        "company_name": company[:255],
        "announcement_subject": subject[:1000],
        "description": details[:10000],
        "summary": details[:10000] or None,
        "content_type": "NEWS",
        "transport": "official-nse-api",
        "official_nse_announcement": True,
        "nse_filing_feed": feed.key,
    }


async def collect_nse_filing_items(*, days: int = 7, limit: int = 500) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Collect NSE board meetings and corporate filing actions. Never raises.

    Returns (items, errors, diagnostics) in the same shape the NSE announcement
    collector hands to the NewsAlert pipeline.
    """
    lookback = max(1, min(int(days), MAX_LOOKBACK_DAYS))
    end_day = datetime.now(NSE_LOCAL_TZ).date()
    start_day = end_day - timedelta(days=lookback - 1)

    def _windows_for(feed: NseFilingFeed) -> list[tuple[Any, Any]]:
        query_start = start_day - timedelta(days=feed.query_lead_days)
        query_end = end_day + timedelta(days=feed.query_forward_days)
        out: list[tuple[Any, Any]] = []
        cursor = query_start
        while cursor <= query_end:
            window_end = min(query_end, cursor + timedelta(days=WINDOW_DAYS - 1))
            out.append((cursor, window_end))
            cursor = window_end + timedelta(days=1)
        return out

    items: list[dict[str, Any]] = []
    errors: list[str] = []
    diagnostics: dict[str, Any] = {}
    seen: set[str] = set()

    from ii_platform.nse import NSEApiClient

    try:
        async with NSEApiClient() as client:
            # Fetch independent feed/date windows concurrently. Bound concurrency
            # to four so NSE is not flooded, while still avoiding the previous
            # 20-window serial wait for board-meeting lookback coverage.
            jobs: list[tuple[NseFilingFeed, Any, Any]] = [
                (feed, window_start, window_end)
                for feed in NSE_FILING_FEEDS
                for window_start, window_end in _windows_for(feed)
            ]
            semaphore = asyncio.Semaphore(4)

            async def _fetch_window(feed: NseFilingFeed, window_start, window_end):
                async with semaphore:
                    try:
                        payload = await client.get_json(
                            feed.api_path,
                            params={
                                "index": "equities",
                                "from_date": window_start.strftime("%d-%m-%Y"),
                                "to_date": window_end.strftime("%d-%m-%Y"),
                            },
                            referer=feed.page_url,
                        )
                        return payload, None
                    except Exception as exc:
                        return None, exc

            results = await asyncio.gather(
                *(_fetch_window(feed, window_start, window_end) for feed, window_start, window_end in jobs),
                return_exceptions=True,
            )
            feed_stats_by_key: dict[str, dict[str, int]] = {
                feed.key: {"fetched": 0} for feed in NSE_FILING_FEEDS
            }
            feed_rows_by_key: dict[str, int] = {feed.key: 0 for feed in NSE_FILING_FEEDS}
            feed_errors_by_key: dict[str, int] = {feed.key: 0 for feed in NSE_FILING_FEEDS}
            feed_sample_logged: set[str] = set()
            feed_window_counts: dict[str, int] = {feed.key: 0 for feed in NSE_FILING_FEEDS}
            for feed, _, _ in jobs:
                feed_window_counts[feed.key] += 1

            for (feed, window_start, window_end), result in zip(jobs, results):
                if isinstance(result, BaseException):
                    payload, exc = None, result
                else:
                    payload, exc = result
                if exc is not None:
                    feed_errors_by_key[feed.key] += 1
                    message = (
                        f"{feed.source} {window_start.isoformat()}..{window_end.isoformat()}: "
                        f"{type(exc).__name__}: {str(exc)[:200]}"
                    )
                    errors.append(message)
                    logger.warning("NSE_FILINGS_WINDOW_FAILED feed=%s error=%s", feed.key, message)
                    continue

                feed_stats = feed_stats_by_key[feed.key]
                for record in FinancialNewsCollector._nse_records_from_payload(payload):
                    feed_stats["fetched"] = feed_stats.get("fetched", 0) + 1
                    if feed.key not in feed_sample_logged and isinstance(record, dict):
                        feed_sample_logged.add(feed.key)
                        date_like = {
                            k: v for k, v in record.items()
                            if any(t in str(k).casefold() for t in ("date", "time", "stamp", "dt", "timestamp"))
                        }
                        logger.info("NSE_FILINGS_SAMPLE feed=%s date_fields=%s", feed.key, date_like)
                    row = _normalise_record(
                        feed, record, start_day=start_day, end_day=end_day, stats=feed_stats
                    )
                    if row is None or row["source_item_id"] in seen:
                        continue
                    seen.add(row["source_item_id"])
                    items.append(row)
                    feed_rows_by_key[feed.key] += 1

            for feed in NSE_FILING_FEEDS:
                diagnostics[feed.key] = {
                    "source": feed.source,
                    "rows": feed_rows_by_key[feed.key],
                    "window_errors": feed_errors_by_key[feed.key],
                    "windows": feed_window_counts[feed.key],
                    **feed_stats_by_key[feed.key],
                }
    except Exception as exc:
        message = f"NSE filings client: {type(exc).__name__}: {str(exc)[:220]}"
        errors.append(message)
        logger.exception("NSE_FILINGS_CLIENT_FAILED")

    items.sort(key=lambda row: row["published_at"], reverse=True)
    items = items[: max(1, int(limit))]
    diagnostics["from_date"] = start_day.isoformat()
    diagnostics["to_date"] = end_day.isoformat()
    diagnostics["collected"] = len(items)
    diagnostics["parallel_window_fetch"] = True
    diagnostics["max_parallel_windows"] = 4
    logger.info(
        "NSE_FILINGS_COLLECT_DONE collected=%s errors=%s diagnostics=%s",
        len(items), len(errors), diagnostics,
    )
    return items, errors, diagnostics