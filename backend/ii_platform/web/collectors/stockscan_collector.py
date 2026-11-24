from __future__ import annotations

"""Discover recent management-interview videos listed by StockScans.

The page is a discovery source only. A video is accepted only when its source
YouTube URL can be extracted from StockScans and YouTube itself supplies an
exact upload timestamp. No YouTube URL is guessed from a headline.
"""

import asyncio
import json
import html as html_lib
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

STOCKSCANS_INTERVIEW_URL = "https://www.stockscans.in/interview-scans"
STOCKSCANS_INTERVIEW_API_URL = "https://www.stockscans.in/api/scans/interview/run"
YOUTUBE_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?[^\s\"'<>]*|embed/[^\s\"'<>?]+|shorts/[^\s\"'<>?]+)|youtu\.be/[^\s\"'<>?]+)",
    re.I,
)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _parse_stockscans_datetime(value: Any) -> datetime | None:
    """Parse absolute timestamps and StockScans relative-age labels."""
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = _clean(value)
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            parsed = None
            match = re.search(r"(?i)\b(\d+)\s*(minute|min|hour|hr|day|week|month|year)s?\s*ago\b", raw)
            if match:
                amount = int(match.group(1)); unit = match.group(2).casefold()
                if unit.startswith(("minute", "min")): delta = timedelta(minutes=amount)
                elif unit.startswith(("hour", "hr")): delta = timedelta(hours=amount)
                elif unit.startswith("day"): delta = timedelta(days=amount)
                elif unit.startswith("week"): delta = timedelta(weeks=amount)
                elif unit.startswith("month"): delta = timedelta(days=30 * amount)
                else: delta = timedelta(days=365 * amount)
                parsed = datetime.now(timezone.utc) - delta
            elif raw.casefold() in {"just now", "now", "today"}:
                parsed = datetime.now(timezone.utc)
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _youtube_watch_url(raw: str) -> str | None:
    """Normalize an actual source-provided YouTube URL; never synthesize an ID."""
    try:
        parsed = urlparse(raw.strip().rstrip(".,);]"))
        host = (parsed.hostname or "").lower().removeprefix("www.")
        video_id = ""
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
        elif host in {"youtube.com", "m.youtube.com", "youtube-nocookie.com"}:
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith(("/embed/", "/shorts/")):
                video_id = parsed.path.split("/")[2] if len(parsed.path.split("/")) > 2 else ""
        if VIDEO_ID_RE.fullmatch(video_id):
            return f"https://www.youtube.com/watch?v={video_id}"
    except (TypeError, ValueError, IndexError):
        return None
    return None


def _video_urls_in_text(value: str) -> list[str]:
    found: list[str] = []
    for match in YOUTUBE_URL_RE.findall(value or ""):
        url = _youtube_watch_url(match)
        if url and url not in found:
            found.append(url)
    return found


def _looks_like_real_headline(title: str) -> bool:
    """Reject site chrome/footer labels; keep interview headlines permissively."""
    value = _clean(title).casefold()
    if len(value) < 12 or len(value) > 500:
        return False
    if "@" in value or value.startswith(("http://", "https://", "mailto:")):
        return False
    if value in {
        "interview scans", "stockscans", "stock scans", "support@stockscans.in",
        "home", "login", "sign up", "sign in", "privacy policy", "terms of service",
    }:
        return False
    return True


def _stockscans_positional_row(value: list[Any]) -> dict[str, Any] | None:
    """Decode StockScans positional rows without assuming field positions never change."""
    strings = [_clean(x) for x in value if isinstance(x, str) and _clean(x)]
    video_id = next((x for x in strings if VIDEO_ID_RE.fullmatch(x)), "")
    # Prefer an actual title-like string rather than an arbitrary ID/date/status.
    title_candidates = [x for x in strings if _looks_like_real_headline(x)
                        and not VIDEO_ID_RE.fullmatch(x)
                        and not re.fullmatch(r"(?i)(?:live|scheduled|published|private|public)", x)
                        and not _parse_stockscans_datetime(x)]
    if not title_candidates:
        return None
    title = max(title_candidates, key=lambda x: (len(x), " " in x))
    def nested_company_entries(items: Any) -> list[list[Any]]:
        found: list[list[Any]] = []
        if isinstance(items, list):
            looks_like_company = (len(items) >= 2 and isinstance(items[0], str)
                                  and isinstance(items[1], str)
                                  and (items[0].startswith(("NSE:", "BSE:")) or (len(items) >= 3 and isinstance(items[2], bool))))
            if looks_like_company:
                found.append(items)
            else:
                for child in items:
                    found.extend(nested_company_entries(child))
        return found
    company_entries = nested_company_entries(value)
    selected = next((entry for entry in company_entries if len(entry) >= 3 and entry[2] is True), None)
    if selected is None and company_entries:
        selected = company_entries[0]
    symbol = _clean(selected[0]) if selected and isinstance(selected[0], str) else ""
    company = _clean(selected[1]) if selected and isinstance(selected[1], str) else ""
    # Find a real timestamp or relative-age label anywhere in the row.
    published_raw = next((x for x in strings if _parse_stockscans_datetime(x) is not None), "")
    channel = next((x for x in strings if x not in {title, video_id, company, symbol, published_raw}
                    and len(x) < 100 and not re.fullmatch(r"(?i)(?:live|scheduled|published|private|public)", x)), "")
    card_text = " | ".join(x for x in (company, symbol, channel, published_raw) if x)
    return {
        "title": title[:1000],
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
        "company_name": company[:255], "company_symbol": symbol[:100],
        "channel_title": channel[:255], "stockscans_published_at": published_raw,
        "stockscans_card_text": card_text[:3000],
        "stockscans_source_url": STOCKSCANS_INTERVIEW_URL,
    }


def _json_candidate_rows(value: Any, rows: list[dict[str, Any]]) -> None:
    """Walk embedded application JSON, preserving interview records with or without direct URLs."""
    if isinstance(value, dict):
        # The interview API uses a `rows` array of positional records rather than
        # objects with named title/company fields. Decode those rows explicitly.
        api_rows = value.get("rows")
        if isinstance(api_rows, list):
            for api_row in api_rows:
                if isinstance(api_row, list):
                    decoded = _stockscans_positional_row(api_row)
                    if decoded:
                        rows.append(decoded)
        # Field names vary by frontend version; accept common names without
        # assuming that any random URL is a video source.
        # StockScans has changed its frontend/API field names over time. Match
        # camelCase, snake_case, and common database/API variants.
        title = _clean(
            value.get("title") or value.get("headline") or value.get("videoTitle")
            or value.get("video_title") or value.get("interviewTitle")
            or value.get("interview_title") or value.get("interviewHeadline")
            or value.get("videoHeadline") or value.get("scanTitle") or value.get("scan_title")
            or value.get("headlineText") or value.get("headline_text") or value.get("name") or value.get("label")
        )
        company = _clean(
            value.get("companyName") or value.get("company_name")
            or value.get("company") or value.get("issuerName") or value.get("issuer_name")
            or value.get("companyTitle") or value.get("company_title")
            or value.get("issuer") or value.get("stockName") or value.get("stock_name")
            or value.get("companyDisplayName") or value.get("company_display_name")
        )
        channel = _clean(
            value.get("channelName") or value.get("channel_name") or value.get("channel_title")
            or value.get("publisher") or value.get("sourceName") or value.get("source_name")
        )
        candidate_urls: list[str] = []
        # Some APIs return the 11-character YouTube ID rather than a URL.
        for key, item in value.items():
            key_norm = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if key_norm in {"videoid", "youtubeid", "youtubevideoid", "youtubeid", "youtubevideoid", "youtubevideo", "videoid"} and isinstance(item, str):
                if VIDEO_ID_RE.fullmatch(item.strip()):
                    candidate_urls.append(f"https://www.youtube.com/watch?v={item.strip()}")
        for key, item in value.items():
            if isinstance(item, str) and any(token in str(key).casefold() for token in (
                "youtube", "video", "sourceurl", "watchurl", "embed", "url", "link",
            )):
                candidate_urls.extend(_video_urls_in_text(item))
        # Some implementations put the YouTube URL in an arbitrary string field.
        if not candidate_urls:
            for item in value.values():
                if isinstance(item, str):
                    candidate_urls.extend(_video_urls_in_text(item))
        # Treat objects with interview/video metadata as candidates even if the
        # YouTube URL is stored elsewhere or must be resolved from the headline.
        looks_like_record = bool(title and (
            candidate_urls
            or any(token in " ".join(map(str, value.keys())).casefold()
                   for token in ("company", "channel", "published", "video", "interview", "youtube"))
        ))
        if looks_like_record and _looks_like_real_headline(title):
            urls = list(dict.fromkeys(candidate_urls)) or [""]
            for url in urls:
                rows.append({
                    "title": title[:1000], "url": url,
                    "company_name": company[:255], "channel_title": channel[:255],
                    "stockscans_card_text": _clean(" ".join(str(v) for v in value.values() if isinstance(v, (str, int, float))))[:3000],
                    "stockscans_source_url": STOCKSCANS_INTERVIEW_URL,
                })
        for key, child in value.items():
            if key == "rows" and isinstance(child, list):
                continue  # already decoded above; avoid duplicate positional candidates
            if isinstance(child, str):
                # Some API wrappers serialize the actual records as a JSON string.
                candidate_text = child.strip()
                if candidate_text[:1] in {"{", "["} and candidate_text[-1:] in {"}", "]"}:
                    try:
                        _json_candidate_rows(json.loads(candidate_text), rows)
                    except (ValueError, TypeError):
                        pass
            else:
                _json_candidate_rows(child, rows)
    elif isinstance(value, list):
        # Also handle a top-level positional row list if the API schema changes.
        decoded = _stockscans_positional_row(value)
        if decoded:
            rows.append(decoded)
            return
        for child in value:
            _json_candidate_rows(child, rows)


class StockScansCollector:
    """Scrape the public Interview Scans page and resolve exact YouTube metadata."""

    def __init__(self, *, timeout_seconds: float = 15.0, max_candidates: int = 1000) -> None:
        self.timeout_seconds = max(5.0, min(float(timeout_seconds), 30.0))
        self.max_candidates = max(1, min(int(max_candidates), 2000))
        self.last_diagnostics: dict[str, Any] = {
            "source_url": STOCKSCANS_INTERVIEW_URL,
            "page_fetched": False,
            "page_candidates": 0,
            "source_youtube_urls": 0,
            "metadata_verified": 0,
            "errors": [],
        }

    async def _fetch_page(self, url: str) -> str:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True, headers=headers) as client:
            for attempt in range(3):
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    return response.text
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    status = exc.response.status_code
                    if status not in {429, 500, 502, 503, 504} or attempt == 2:
                        raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = exc
                    if attempt == 2:
                        raise
                await asyncio.sleep(0.4 * (2 ** attempt))
        if last_error:
            raise last_error
        return ""

    @staticmethod
    def _extract_page_candidates(html: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        rows: list[dict[str, Any]] = []

        # Read card text even when the video URL is not embedded. StockScans may
        # expose the headline/company but only resolve the playable URL through search.
        card_seen: set[tuple[str, str]] = set()
        for node in soup.find_all(["article", "a", "div", "li"]):
            text_blob = _clean(node.get_text(" ", strip=True))
            if not text_blob or len(text_blob) < 15 or len(text_blob) > 3500:
                continue
            headings = [_clean(h.get_text(" ", strip=True)) for h in node.find_all(["h1", "h2", "h3", "h4"], limit=4)]
            title = next((h for h in headings if len(h) >= 8), "")
            if not title:
                # Many cards use a clickable title without heading semantics.
                for link in node.find_all("a", limit=8):
                    label = _clean(link.get("title") or link.get("aria-label") or link.get_text(" ", strip=True))
                    if len(label) >= 12 and len(label) < 350 and not label.casefold().startswith(("http", "watch")):
                        title = label
                        break
            if not title or not _looks_like_real_headline(title):
                continue
            company = ""
            channel = ""
            for possible in node.find_all(True, limit=40):
                label = _clean(possible.get_text(" ", strip=True))
                classes = " ".join(possible.get("class") or []).casefold()
                if label and len(label) <= 180 and label != title:
                    if not company and any(word in classes for word in ("company", "issuer", "stock", "symbol")):
                        company = label
                    if not channel and any(word in classes for word in ("channel", "publisher", "source")):
                        channel = label
            key = (title.casefold(), company.casefold())
            if key not in card_seen:
                card_seen.add(key)
                rows.append({"title": title[:1000], "url": "", "company_name": company[:255],
                             "channel_title": channel[:255], "stockscans_card_text": text_blob[:3000],
                             "stockscans_source_url": STOCKSCANS_INTERVIEW_URL})

        # Direct links, iframe URLs, and data attributes from rendered cards.
        for node in soup.find_all(True):
            for attr in ("href", "src", "data-href", "data-url", "data-video-url", "data-youtube-url", "content"):
                raw = node.get(attr)
                if not isinstance(raw, str):
                    continue
                for url in _video_urls_in_text(raw):
                    container = node
                    # A few levels up usually captures one card rather than the whole page.
                    for _ in range(5):
                        if container.parent and getattr(container.parent, "name", None) not in {"body", "html"}:
                            container = container.parent
                    title = ""
                    company = ""
                    channel = ""
                    for heading in container.find_all(["h1", "h2", "h3", "h4"], limit=5):
                        text = _clean(heading.get_text(" ", strip=True))
                        if text:
                            title = text
                            break
                    if not title:
                        title = _clean(node.get("title") or node.get("aria-label"))
                    if not title:
                        # The link's own visible text is a last resort, not an entire page dump.
                        title = _clean(node.get_text(" ", strip=True))[:1000]
                    text_blob = _clean(container.get_text(" ", strip=True))[:3000]
                    # StockScans cards show the company as a separate label; retain
                    # it as metadata so the pipeline can match Coverage List aliases.
                    for possible in container.find_all(True, limit=30):
                        label = _clean(possible.get_text(" ", strip=True))
                        if label and len(label) <= 180 and label != title and not company:
                            classes = " ".join(possible.get("class") or []).casefold()
                            if any(word in classes for word in ("company", "issuer", "stock", "symbol")):
                                company = label
                        if not channel:
                            classes = " ".join(possible.get("class") or []).casefold()
                            if any(word in classes for word in ("channel", "publisher", "source")):
                                channel = label[:255]
                    rows.append({
                        "title": title[:1000], "url": url, "company_name": company[:255],
                        "channel_title": channel[:255], "stockscans_card_text": text_blob,
                        "stockscans_source_url": STOCKSCANS_INTERVIEW_URL,
                    })

        # Some React/Next versions embed the data rather than rendering the source
        # link as an anchor. Parse JSON-LD and known application state scripts.
        for script in soup.find_all("script"):
            raw = script.string or script.get_text()
            if not raw:
                continue
            # Next.js and other React apps often serialize data into JS strings
            # that are not valid JSON; recover title/company pairs conservatively.
            if any(key in raw.casefold() for key in ("interview", "headline", "videotitle", "companyname")):
                title_matches = re.findall(r'"(?:title|headline|videoTitle|interviewTitle)"\s*:\s*"([^"\\]{12,500})"', raw, re.I)
                company_matches = re.findall(r'"(?:companyName|company_name|issuerName)"\s*:\s*"([^"\\]{2,200})"', raw, re.I)
                for index, headline in enumerate(title_matches):
                    if _looks_like_real_headline(headline):
                        rows.append({
                            "title": headline,
                            "url": "",
                            "company_name": company_matches[min(index, len(company_matches)-1)] if company_matches else "",
                            "stockscans_card_text": raw[:3000],
                            "stockscans_source_url": STOCKSCANS_INTERVIEW_URL,
                        })
            if not ("youtube" in raw.casefold() or "video" in raw.casefold() or "interview" in raw.casefold()):
                continue
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                # URLs can be stringified inside a JS state assignment.
                title_match = re.search(r'"(?:title|headline|videoTitle|interviewTitle)"\s*:\s*"([^"\\]{12,500})"', raw, re.I)
                company_match = re.search(r'"(?:companyName|company_name|issuerName|company)"\s*:\s*"([^"\\]{2,200})"', raw, re.I)
                channel_match = re.search(r'"(?:channelName|channel_title|publisher|sourceName)"\s*:\s*"([^"\\]{2,200})"', raw, re.I)
                urls = _video_urls_in_text(raw) or [""]
                if title_match and _looks_like_real_headline(title_match.group(1)):
                    for url in urls:
                        rows.append({
                            "title": title_match.group(1), "url": url,
                            "company_name": company_match.group(1) if company_match else "",
                            "channel_title": channel_match.group(1) if channel_match else "",
                            "stockscans_card_text": raw[:3000],
                            "stockscans_source_url": STOCKSCANS_INTERVIEW_URL,
                        })
                continue
            _json_candidate_rows(payload, rows)

        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            url = _youtube_watch_url(str(row.get("url") or ""))
            title = _clean(row.get("title"))
            if not title or len(title) < 4:
                continue
            row["url"] = url or ""
            key = f"headline:{title.casefold()}|{_clean(row.get('company_name')).casefold()}"
            old = unique.get(key)
            if old is None or (not old.get("url") and url) or (not old.get("company_name") and row.get("company_name")):
                unique[key] = row
        return list(unique.values())

    async def _fetch_api_response(self, url: str, method: str = "GET", payload: dict[str, Any] | None = None) -> str:
        """Fetch a public same-origin JSON API, supporting POST-based Next routes."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": STOCKSCANS_INTERVIEW_URL,
            "Origin": "https://www.stockscans.in",
            "X-Requested-With": "XMLHttpRequest",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True, headers=headers) as client:
            response = await client.request(method, url, json=(payload if payload is not None else {}) if method == "POST" else None)
            logger.info(
                "NEWSALERT_STOCKSCANS_API_RESPONSE method=%s path=%s status=%s content_type=%s bytes=%s",
                method, urlparse(url).path, response.status_code,
                response.headers.get("content-type", ""), len(response.content),
            )
            response.raise_for_status()
            return response.text

    @staticmethod
    def _parse_api_candidate_payload(payload_text: str) -> list[dict[str, Any]]:
        """Parse JSON or serialized JSON response from the interview API."""
        rows: list[dict[str, Any]] = []
        try:
            payload = json.loads(payload_text)
        except (ValueError, TypeError):
            payload = None
        if payload is not None:
            _json_candidate_rows(payload, rows)
            if not rows:
                if isinstance(payload, dict):
                    root_summary = {str(key): type(val).__name__ for key, val in list(payload.items())[:30]}
                elif isinstance(payload, list):
                    root_summary = {"root_type": "list", "root_length": len(payload), "first_item_type": type(payload[0]).__name__ if payload else "empty"}
                else:
                    root_summary = {"root_type": type(payload).__name__}
                logger.warning(
                    "NEWSALERT_STOCKSCANS_API_SCHEMA_MISS bytes=%s root=%s preview=%r",
                    len(payload_text), json.dumps(root_summary, ensure_ascii=False)[:1600], payload_text[:2200],
                )
        else:
            # Handle JSON embedded in an envelope, SSE, or serialized response.
            for match in re.finditer(r"\{.{0,20000}?(?:title|headline|videoTitle|companyName|youtubeVideoId).{0,20000}?\}", payload_text, re.I | re.S):
                try:
                    fragment = json.loads(match.group(0))
                except (ValueError, TypeError):
                    continue
                _json_candidate_rows(fragment, rows)
            if not rows:
                titles = re.findall(r"[\"'](?:title|headline|videoTitle|video_title|interviewTitle|interview_title|interviewHeadline|scanTitle|headlineText)[\"']\s*:\s*[\"']([^\"']{12,500})[\"']", payload_text, re.I)
                companies = re.findall(r"[\"'](?:companyName|company_name|issuerName|issuer_name|companyTitle|company_title|company|stockName)[\"']\s*:\s*[\"']([^\"']{2,200})[\"']", payload_text, re.I)
                ids = re.findall(r"[\"'](?:youtubeVideoId|youtube_video_id|videoId|video_id)[\"']\s*:\s*[\"']([A-Za-z0-9_-]{11})[\"']", payload_text, re.I)
                urls = _video_urls_in_text(payload_text) or [f"https://www.youtube.com/watch?v={video_id}" for video_id in dict.fromkeys(ids)]
                for i, title in enumerate(titles):
                    if _looks_like_real_headline(title):
                        rows.append({"title": title, "url": urls[i] if len(urls) == len(titles) else (urls[0] if len(urls) == 1 else ""), "company_name": companies[min(i, len(companies)-1)] if companies else "", "stockscans_source_url": STOCKSCANS_INTERVIEW_URL})
        return rows

    async def _fetch_dynamic_candidates(self, page_html: str, *, lookback_days: int = 30) -> list[dict[str, Any]]:
        """Fetch the public Interview Scans API using its returned cursor until the lookback is covered."""
        all_rows: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, min(int(lookback_days), 30)))
        # Bound requests even if the endpoint repeats a cursor or ignores pagination.
        for page_number in range(1, 101):
            try:
                payload_text = await self._fetch_api_response(
                    STOCKSCANS_INTERVIEW_API_URL, "POST",
                    ({"cursor": cursor} if cursor else {}),
                )
                page_rows = self._parse_api_candidate_payload(payload_text)
                try:
                    payload = json.loads(payload_text)
                except (ValueError, TypeError):
                    payload = {}
                next_cursor = _clean(payload.get("next")) if isinstance(payload, dict) else ""
                all_rows.extend(page_rows)
                logger.info(
                    "NEWSALERT_STOCKSCANS_DYNAMIC_RESULT endpoint=%s page=%s rows=%s total_rows=%s has_next=%s",
                    urlparse(STOCKSCANS_INTERVIEW_API_URL).path, page_number, len(page_rows),
                    len(all_rows), bool(next_cursor),
                )
                if not next_cursor or next_cursor in seen_cursors or not page_rows:
                    break
                seen_cursors.add(next_cursor)
                # The API cursor starts with the timestamp. Once the page has
                # reached older records than our cutoff, no further page is needed.
                cursor_date = _parse_stockscans_datetime(next_cursor.split("|", 1)[0])
                if cursor_date is not None and cursor_date < cutoff:
                    break
                cursor = next_cursor
            except Exception as exc:
                message = f"StockScans interview API page {page_number}: {type(exc).__name__}: {str(exc)[:180]}"
                self.last_diagnostics["errors"].append(message)
                logger.warning("NEWSALERT_STOCKSCANS_API_FAILED error=%s", message)
                break
        # Keep the newest version of each actual video/headline.
        unique: dict[str, dict[str, Any]] = {}
        for row in all_rows:
            identity = _clean(row.get("video_id") or row.get("url")) or (
                _clean(row.get("title")).casefold() + "|" + _clean(row.get("company_name")).casefold()
            )
            unique.setdefault(identity, row)
        return list(unique.values())

    async def _google_video_url(self, title: str, company: str = "") -> str | None:
        """Resolve a real headline via Google first, then public search fallbacks.

        Google may return a consent/CAPTCHA page (302 followed by 429). Avoid
        retrying that same blocked endpoint; try Bing and DuckDuckGo instead.
        """
        if not _looks_like_real_headline(title):
            return None
        query = " ".join(part for part in (f'"{title}"', company, "site:youtube.com/watch") if part)
        providers = [
            ("google", "https://www.google.com/search?" + urlencode({"q": query, "num": "8"})),
            ("bing", "https://www.bing.com/search?" + urlencode({"q": query, "count": "8"})),
            ("duckduckgo", "https://html.duckduckgo.com/html/?" + urlencode({"q": query})),
        ]
        for provider, url in providers:
            try:
                html = await self._fetch_page(url)
            except Exception as exc:
                logger.info("NEWSALERT_STOCKSCANS_SEARCH_FAILED provider=%s title=%r error=%s", provider, title[:180], type(exc).__name__)
                continue
            soup = BeautifulSoup(html, "html.parser")
            candidates: list[str] = []
            for anchor in soup.find_all("a", href=True):
                href = html_lib.unescape(str(anchor.get("href") or ""))
                parsed_href = urlparse(href)
                if parsed_href.path == "/url":
                    href = parse_qs(parsed_href.query).get("q", [""])[0] or parse_qs(parsed_href.query).get("uddg", [""])[0]
                if "duckduckgo.com/l/?" in href:
                    href = parse_qs(urlparse(href).query).get("uddg", [""])[0]
                video_url = _youtube_watch_url(urljoin(url, href))
                if video_url and video_url not in candidates:
                    candidates.append(video_url)
            if candidates:
                logger.info("NEWSALERT_STOCKSCANS_SEARCH_RESOLVED provider=%s title=%r", provider, title[:180])
                return candidates[0]
        return None

    async def collect(self, *, lookback_days: int = 30) -> list[dict[str, Any]]:
        """Collect recent StockScans records from its API; never search engines for video URLs."""
        try:
            html = await self._fetch_page(STOCKSCANS_INTERVIEW_URL)
            self.last_diagnostics["page_fetched"] = True
        except Exception as exc:
            message = f"StockScans page: {type(exc).__name__}: {str(exc)[:220]}"
            self.last_diagnostics["errors"].append(message)
            logger.warning("NEWSALERT_STOCKSCANS_FETCH_FAILED error=%s", message)
            return []

        # The API is primary and supplies the actual video ID, headline, company,
        # and publication time. HTML cards are supplemental only.
        api_candidates = await self._fetch_dynamic_candidates(html, lookback_days=lookback_days)
        page_candidates = self._extract_page_candidates(html)
        self.last_diagnostics["api_candidates"] = len(api_candidates)
        self.last_diagnostics["html_candidates"] = len(page_candidates)
        candidates = api_candidates + page_candidates
        unique_candidates: dict[str, dict[str, Any]] = {}
        for row in candidates:
            title = _clean(row.get("title"))
            # Reject YouTube channel IDs and other IDs mistakenly exposed as card titles.
            if (not _looks_like_real_headline(title)
                    or re.fullmatch(r"UC[A-Za-z0-9_-]{22}", title)
                    or VIDEO_ID_RE.fullmatch(title)):
                continue
            key = _clean(row.get("video_id") or row.get("url")) or f"{title.casefold()}|{_clean(row.get('company_name')).casefold()}"
            previous = unique_candidates.get(key)
            if previous is None or (not previous.get("url") and row.get("url")):
                unique_candidates[key] = row
        candidates = list(unique_candidates.values())[: self.max_candidates]
        self.last_diagnostics["page_candidates"] = len(candidates)
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, min(int(lookback_days), 30)))

        async def enrich(candidate: dict[str, Any]) -> dict[str, Any] | None:
            title = _clean(candidate.get("title"))
            company = _clean(candidate.get("company_name"))
            if not _looks_like_real_headline(title) or not company:
                self.last_diagnostics["invalid_headline_or_company"] = int(self.last_diagnostics.get("invalid_headline_or_company", 0)) + 1
                return None
            if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", title) or VIDEO_ID_RE.fullmatch(title):
                return None

            published = _parse_stockscans_datetime(candidate.get("stockscans_published_at"))
            if published is None:
                published = _parse_stockscans_datetime(candidate.get("stockscans_card_text"))
            if published is None:
                self.last_diagnostics["missing_stockscans_timestamp"] = int(self.last_diagnostics.get("missing_stockscans_timestamp", 0)) + 1
                return None
            if published < cutoff or published > datetime.now(timezone.utc) + timedelta(minutes=5):
                self.last_diagnostics["outside_lookback"] = int(self.last_diagnostics.get("outside_lookback", 0)) + 1
                return None

            # API row's video ID is source data; its direct URL is already built
            # by the positional decoder. Do not Google/Bing/DDG-search channel IDs.
            video_url = _youtube_watch_url(str(candidate.get("url") or ""))
            if not video_url:
                self.last_diagnostics["missing_source_video_url"] = int(self.last_diagnostics.get("missing_source_video_url", 0)) + 1
                return None
            video_id = parse_qs(urlparse(video_url).query).get("v", [""])[0]
            stockscans_symbol = _clean(candidate.get("company_symbol") or candidate.get("symbol"))
            if stockscans_symbol.upper().startswith(("NSE:", "BSE:")):
                stockscans_symbol = stockscans_symbol.split(":", 1)[1].strip()
            source_label = _clean(candidate.get("channel_title")) or "StockScans Interview Scans"
            card_text = _clean(candidate.get("stockscans_card_text"))
            evidence = " | ".join(part for part in (company, stockscans_symbol, source_label, card_text) if part)
            exact: dict[str, Any] = {
                "title": title[:1000], "url": video_url,
                "summary": evidence[:10000], "description": evidence[:10000],
                "channel_title": source_label, "video_id": video_id,
                "company_name": company, "stockscans_company_name": company,
                "company_symbol": stockscans_symbol or None, "nse_symbol": stockscans_symbol or None,
                "stockscans_source_url": STOCKSCANS_INTERVIEW_URL,
                "stockscans_headline": title[:1000],
                "source": f"StockScans Interview Scans · {source_label}",
                "source_key": "stockscans_interviews", "published_at": published,
                "_timestamp_exact": True, "content_type": "INTERVIEW",
                "is_stockscan_interview": True, "is_interview": True,
                "source_item_id": video_id,
            }
            self.last_diagnostics["source_only_metadata_fallback"] = int(self.last_diagnostics.get("source_only_metadata_fallback", 0)) + 1
            return exact

        enriched = await asyncio.gather(*(enrich(row) for row in candidates), return_exceptions=True)
        items = [row for row in enriched if isinstance(row, dict)]
        self.last_diagnostics["metadata_verified"] = len(items)
        items.sort(key=lambda item: item["published_at"], reverse=True)
        logger.info(
            "NEWSALERT_STOCKSCANS_RESULT candidates=%s api_candidates=%s source_youtube_urls=%s metadata_verified=%s outside_lookback=%s missing_source_video_url=%s sample_titles=%r errors=%s",
            len(candidates), len(api_candidates), sum(1 for row in candidates if _youtube_watch_url(str(row.get("url") or ""))), len(items),
            self.last_diagnostics.get("outside_lookback", 0), self.last_diagnostics.get("missing_source_video_url", 0),
            [str(row.get("title", ""))[:100] for row in candidates[:8]], len(self.last_diagnostics["errors"]),
        )
        return items
