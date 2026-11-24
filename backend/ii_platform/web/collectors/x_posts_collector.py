from __future__ import annotations

"""Drop-in NewsAlert collector for recent public posts from approved X accounts.

It uses public RSS/Atom feeds when explicitly configured, public profile HTML on
TwitterViewer, and Google/Bing public search-result HTML for individual status URLs.
Where available, a public X status page provides a metadata fallback. No X/Twitter
API, bearer-token request, login, cookie reuse, CAPTCHA solving, or challenge bypass
is used. A blocked source is not retried through that same source.

Integration contract:
- collect_allowed_accounts(...) -> list[post_row]
- collect_from_feeds(...) -> (list[post_row], list[str])

Rows use the same fields as the existing NewsAlert X collector so the caller can
pass them through the same persistence/deduplication/alert pipeline.
"""

import asyncio
import email.utils
import logging
import os
import re
from bs4 import BeautifulSoup

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote, urlparse, urlencode, parse_qs, unquote, unquote_plus

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_ACCOUNTS = (
    "HemindraHazari",
    "Nigel__DSouza",
    "Joshieien",
    "Unseenvalue",
    "ResearchSOIC",
)

_STATUS_RE = re.compile(
    r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,50})/status/(\d+)",
    re.IGNORECASE,
)
_RELATIVE_STATUS_RE = re.compile(
    r"^/?(?:([A-Za-z0-9_]{1,50})/)?(?:status|statuses|post|posts)/(\d+)(?:[/?#]|$)",
    re.IGNORECASE,
)
_YOUTUBE_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=[\w-]+|shorts/[\w-]+)|youtu\.be/[\w-]+)\S*",
    re.IGNORECASE,
)

# Use browser-rendered DOM only. The selectors are intentionally broad because
# TwitterViewer can change its CSS class names; extraction also walks up from
# links that contain a verifiable public post ID.
_EXTRACT_POSTS_JS = r"""(expectedHandle) => {
  const expected = String(expectedHandle || '').replace(/^@/, '').toLowerCase();
  const clean = value => String(value || '')
    .replace(/\u00a0/g, ' ')
    .replace(/\r/g, '\n')
    .replace(/[\t ]+/g, ' ')
    .replace(/\n[ \t]+/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  const flat = value => clean(value).replace(/\s+/g, ' ').trim();
  const norm = value => flat(value).toLowerCase().replace(/[^a-z0-9@_]+/g, ' ').trim();
  const statusFromUrl = (url, depth = 0) => {
    if (depth > 2) return null;
    try {
      const u = new URL(url, location.href);
      const host = (u.hostname || '').toLowerCase();
      const path = decodeURIComponent(u.pathname || '');
      let m;
      if (/(^|\.)x\.com$/.test(host) || /(^|\.)twitter\.com$/.test(host)) {
        m = path.match(/^\/([A-Za-z0-9_]{1,50})\/status(?:es)?\/(\d+)/i);
        if (m) return {handle: m[1].toLowerCase(), id: m[2], href: u.href};
      }
      if (host === 'twitterviewer.app' || host.endsWith('.twitterviewer.app')) {
        m = path.match(/^\/([A-Za-z0-9_]{1,50})\/(?:status|statuses|post|posts)\/(\d+)/i);
        if (m) return {handle: m[1].toLowerCase(), id: m[2], href: u.href};
        m = path.match(/^\/(?:status|statuses|post|posts)\/(\d+)/i);
        if (m) return {handle: expected, id: m[1], href: u.href};
      }
      // Some mirror/viewer links wrap the original post URL in a query parameter.
      for (const key of ['url', 'post', 'post_url', 'tweet', 'status', 'target', 'q']) {
        const wrapped = u.searchParams.get(key);
        if (wrapped && wrapped !== url) {
          const nested = statusFromUrl(wrapped, depth + 1);
          if (nested) return nested;
        }
      }
    } catch (_) {}
    return null;
  };

  const displayLabel = flat(document.querySelector('h1')?.innerText || '');
  const profileIdentity = new Set([
    norm(expected), norm('@' + expected), norm(displayLabel),
    norm(displayLabel + ' @' + expected), norm(displayLabel + '@' + expected),
    norm('@' + expected + ' ' + displayLabel), norm('@' + expected + displayLabel),
  ].filter(Boolean));
  const isChromeLine = line => {
    const value = flat(line);
    if (!value) return true;
    if (/^(?:reply|replies|repost|reposts|like|likes|share|bookmark|copy link|view post|open on x|show more|show this thread|load more posts|translate post|post analytics)$/i.test(value)) return true;
    if (/^(?:\d+[,.]?\d*\s*)?(?:replies|reposts|likes|views|bookmarks)$/i.test(value)) return true;
    if (/^(?:\d+(?:\.\d+)?[kmb]?)$/i.test(value)) return true;
    if (/^(?:\d+[smhdw]|\d+\s*(?:seconds?|minutes?|hours?|days?|weeks?)\s+ago)$/i.test(value)) return true;
    if (/^(?:posts?|replies|media|likes|followers|following|loading\.\.\.)$/i.test(value)) return true;
    if (/^https?:\/\/(?:x\.com|twitter\.com)\//i.test(value)) return true;
    return false;
  };
  const isIdentityOnly = value => {
    const valueNorm = norm(value);
    if (!valueNorm) return true;
    if (profileIdentity.has(valueNorm)) return true;
    const noTimeSuffix = flat(value).replace(/\s*(?:\d+[smhdw]|\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?)\s+ago)$/i, '').trim();
    if (profileIdentity.has(norm(noTimeSuffix))) return true;
    if (new RegExp('^@?' + expected + '(?:\\s+[^ ]+)?$','i').test(flat(value))) return true;
    if (new RegExp('^(?:[^@]{1,70}\\s+)?@' + expected + '(?:\\s*[·|•—–:-].*)?$','i').test(flat(value))) return true;
    return false;
  };
  const validPostText = value => {
    const text = clean(value);
    if (!text || isIdentityOnly(text)) return false;
    const visibleLines = text.split(/\n+/).map(flat).filter(line => line && !isChromeLine(line));
    if (!visibleLines.length) return false;
    const onlyIdentity = visibleLines.every(line => isIdentityOnly(line));
    return !onlyIdentity;
  };

  const bodySelectors = [
    '[data-testid="tweetText"]', '[data-testid="postText"]',
    '[data-testid="post-text"]', '[data-testid*="post-text"]',
    '[data-role="post-text"]', '[class*="post-text"]', '[class*="postText"]',
    '[class*="tweet-text"]', '[class*="tweetText"]',
    '[class*="post-body"]', '[class*="postBody"]',
    '[class*="post-content"]', '[class*="postContent"]',
    '[class*="tweet-content"]', '[class*="tweetContent"]'
  ];
  const extractFromCard = card => {
    if (!card) return {text: '', rank: 0};
    // TwitterViewer can change CSS classes, so use semantic post-body selectors
    // first, then post paragraphs, and only then filtered card text.
    for (let i = 0; i < bodySelectors.length; i++) {
      let candidates = [];
      try { candidates = [...card.querySelectorAll(bodySelectors[i])]; } catch (_) {}
      const good = candidates.map(el => clean(el.innerText || el.textContent))
        .filter(value => validPostText(value) && value.length <= 25000)
        .sort((a, b) => b.length - a.length);
      if (good.length) return {text: good[0], rank: 100 - i};
    }
    const paragraphs = [...card.querySelectorAll('p, [role="paragraph"]')]
      .map(el => clean(el.innerText || el.textContent))
      .filter(value => validPostText(value) && value.length <= 25000);
    if (paragraphs.length) {
      const unique = [...new Set(paragraphs)];
      // A post can contain several paragraphs; keep their order rather than only
      // returning the username or whichever paragraph happens to be longest.
      const joined = unique.join('\n\n');
      if (validPostText(joined)) return {text: joined, rank: 70};
      const longest = unique.sort((a, b) => b.length - a.length)[0];
      return {text: longest || '', rank: 60};
    }
    const rawLines = clean(card.innerText || card.textContent || '').split(/\n+/).map(flat).filter(Boolean);
    let lines = rawLines.filter(line => !isChromeLine(line));
    // Strip only leading profile metadata, never a matching word later in the post.
    while (lines.length && (isIdentityOnly(lines[0]) || /^@?[A-Za-z0-9_]{1,50}\s*[·|•—–:-]\s*(?:\d+[smhdw]|\d{1,2}[:/]\d{1,2})$/i.test(lines[0]))) {
      lines.shift();
    }
    const text = clean(lines.join('\n'));
    return validPostText(text) ? {text, rank: 20} : {text: '', rank: 0};
  };

  const parseStatusAnchor = anchor => statusFromUrl(anchor.href || anchor.getAttribute('href') || '');
  const links = [...document.querySelectorAll('a[href]')];
  const byId = new Map();
  const postRoots = [
    'article', '[role="article"]', '[data-testid="tweet"]',
    '[data-post-id]', '[data-tweet-id]', '[class*="post-card"]',
    '[class*="tweet-card"]', '[class*="status-card"]', '[class*="timeline-item"]'
  ].join(',');

  for (const anchor of links) {
    const status = parseStatusAnchor(anchor);
    if (!status || status.handle !== expected) continue;
    const ancestors = [];
    let node = anchor;
    for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
      if (!ancestors.includes(node)) ancestors.push(node);
    }
    // Prefer a semantic post card, but still walk ancestors if the site changes
    // from <article> markup to an ordinary div-based timeline.
    let semanticRoot = null;
    try { semanticRoot = anchor.closest(postRoots); } catch (_) {}
    if (semanticRoot && !ancestors.includes(semanticRoot)) ancestors.push(semanticRoot);
    let best = {text: '', rank: 0, size: Infinity};
    for (const candidate of ancestors) {
      if (!candidate || !candidate.querySelectorAll) continue;
      const statuses = new Set();
      for (const link of candidate.querySelectorAll('a[href]')) {
        const parsed = parseStatusAnchor(link);
        if (parsed && parsed.handle === expected) statuses.add(parsed.id);
      }
      const isSemanticCard = Boolean(candidate.matches && candidate.matches(postRoots));
      if (!statuses.has(status.id) || statuses.size > 4) continue;
      // A page/feed wrapper with two neighboring post IDs must not donate the
      // first card's text to the second post. Multiple same-handle IDs are only
      // accepted inside a semantic post card (for a legitimate quoted/threaded post).
      if (statuses.size > 1 && !isSemanticCard) continue;
      const extracted = extractFromCard(candidate);
      if (!extracted.text) continue;
      const textLength = flat(candidate.innerText || candidate.textContent || '').length;
      const rank = extracted.rank + (isSemanticCard ? 25 : 0);
      // Prefer an actual body selector, then semantic card, then the smallest
      // containing element. This avoids using the profile name/timestamp as title.
      if (rank > best.rank || (rank === best.rank && textLength < best.size)) {
        best = {text: extracted.text, rank, size: textLength};
      }
      if (best.rank >= 120) break;
    }
    if (!best.text || !validPostText(best.text)) continue;
    const row = {
      handle: status.handle,
      post_id: status.id,
      url: `https://x.com/${status.handle}/status/${status.id}`,
      source_url: `https://twitterviewer.app/${status.handle}`,
      text: clean(best.text),
      published_at: ''
    };
    // The timestamp may be located in a sibling anchor outside the text node.
    const relatedTime = ancestors.map(root => {
      try { return root.querySelector('time[datetime], time[dateTime]'); } catch (_) { return null; }
    }).find(el => el);
    if (relatedTime) row.published_at = relatedTime.getAttribute('datetime') || relatedTime.getAttribute('dateTime') || '';
    const old = byId.get(status.id);
    if (!old || row.text.length > old.text.length || (!old.published_at && row.published_at)) byId.set(status.id, row);
  }
  return [...byId.values()];
}"""


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def _normalise_handle(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    if raw.startswith(("https://", "http://")):
        try:
            parsed = urlparse(raw)
        except ValueError:
            return ""
        host = (parsed.hostname or "").casefold()
        if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "twitterviewer.app", "www.twitterviewer.app"}:
            return ""
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return ""
        raw = parts[0]
    raw = raw.lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", raw):
        return ""
    return raw.casefold()


_DISPLAY_NAMES = {
    "hemindrahazari": "Hemindra Hazari",
    "nigel__dsouza": "Nigel D'Souza",
    "joshieien": "Ajay Joshi Chemicals",
    "unseenvalue": "Unseenvalue",
    "researchsoic": "ResearchSOIC",
}


def _is_identity_or_chrome_text(value: Any, handle: str) -> bool:
    """Reject profile labels and timeline controls masquerading as post text."""
    text = _clean(value)
    if not text:
        return True
    normalized = re.sub(r"[^a-z0-9@_]+", " ", text.casefold()).strip()
    labels = {
        handle.casefold(),
        f"@{handle}".casefold(),
        re.sub(r"[^a-z0-9@_]+", " ", _DISPLAY_NAMES.get(handle.casefold(), "").casefold()).strip(),
    }
    labels.discard("")
    display = _DISPLAY_NAMES.get(handle.casefold(), "")
    labels.update({
        re.sub(r"[^a-z0-9@_]+", " ", f"{display} @{handle}".casefold()).strip(),
        re.sub(r"[^a-z0-9@_]+", " ", f"{display}@{handle}".casefold()).strip(),
        re.sub(r"[^a-z0-9@_]+", " ", f"@{handle} {display}".casefold()).strip(),
    })
    labels.discard("")
    without_time = re.sub(r"\s*(?:\d+[smhdw]|\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?)\s+ago)$", "", text, flags=re.I).strip()
    without_time_normalized = re.sub(r"[^a-z0-9@_]+", " ", without_time.casefold()).strip()
    if normalized in labels or without_time_normalized in labels:
        return True
    if re.fullmatch(rf"(?:[^@|·•—–:]+\s*)?@?{re.escape(handle)}(?:\s*[|·•—–:].*)?", text, flags=re.I):
        return True
    if re.fullmatch(
        r"(?:reply|replies|repost|reposts|like|likes|share|bookmark|view post|open on x|show more|show this thread|load more posts|posts|media|followers|following|loading\.\.\.)",
        text, flags=re.I,
    ):
        return True
    # A profile-only card often contains the handle plus a relative timestamp.
    if re.fullmatch(rf"@?{re.escape(handle)}\s*[·|•—–:-]\s*(?:\d+[smhdw]|\d+\s+(?:seconds?|minutes?|hours?|days?)\s+ago)", text, flags=re.I):
        return True
    return False


def _published_datetime(value: Any) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _snowflake_datetime(post_id_or_url: str) -> datetime | None:
    """Derive UTC publication time from the public X status snowflake ID."""
    raw = str(post_id_or_url or "")
    match = _STATUS_RE.search(raw)
    if match:
        post_id = match.group(2)
    elif raw.isdigit():
        post_id = raw
    else:
        match = _RELATIVE_STATUS_RE.match(raw)
        if not match:
            return None
        post_id = match.group(2)
    try:
        epoch_ms = (int(post_id) >> 22) + 1288834974657
        return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _in_window(value: datetime | None, *, hours: int = 48, now: datetime | None = None) -> bool:
    if not isinstance(value, datetime):
        return False
    now = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return now - timedelta(hours=hours) <= value <= now + timedelta(minutes=1)


def _youtube_urls(text: str) -> list[str]:
    found: list[str] = []
    for match in _YOUTUBE_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,);]}")
        if url not in found:
            found.append(url)
    return found


class XPostsCollector:
    """Collect recent public posts from a small configured account allowlist.

    A rolling 48-hour window matches the existing XPostsCollector. The caller's
    normal database URL deduplication should prevent an old post from alerting on
    every polling cycle.
    """

    def __init__(
        self,
        *,
        allowed_accounts: Iterable[str] | None = None,
        timeout_seconds: float = 30.0,
        bearer_token: str | None = None,
        **_ignored: Any,
    ) -> None:
        # Honor the caller's approved allowlist. The prior version silently
        # discarded the five accounts passed by NewsAlert and hardcoded four, so
        # it skipped @unseenvalue and could report misleading account diagnostics.
        requested_accounts = DEFAULT_ALLOWED_ACCOUNTS if allowed_accounts is None else ([allowed_accounts] if isinstance(allowed_accounts, str) else list(allowed_accounts))
        self.allowed_accounts = {
            _normalise_handle(value)
            for value in requested_accounts
            if _normalise_handle(value)
        }
        self.timeout_seconds = max(10.0, min(float(timeout_seconds or 30.0), 90.0))
        if bearer_token:
            logger.warning("TwitterViewer collector ignores bearer_token; no API token is used")
        self.last_diagnostics: dict[str, Any] = {
            "transport": "twitterviewer-public-profile-html",
            "api_used": False,
            "credentials_required": False,
            "accounts_checked": 0,
            "accounts_succeeded": 0,
            "posts_found": 0,
            "errors": [],
        }

    @staticmethod
    def _make_post_row(text: str, handle: str, url: str, published: datetime) -> dict[str, Any]:
        clean_text = _clean(text)
        hashtags = re.findall(r"(?<!\w)#([A-Za-z0-9_]+)", clean_text)
        cashtags = re.findall(r"(?<!\w)\$([A-Za-z][A-Za-z0-9._-]{0,14})", clean_text)
        canonical_url = f"https://x.com/{handle}/status/{url.rsplit('/', 1)[-1]}"
        return {
            # X posts are headline-equivalent content: preserve the complete body.
            "title": clean_text[:10000],
            "summary": clean_text[:10000],
            "raw_content": clean_text[:100000],
            "url": canonical_url,
            "published_at": published.astimezone(timezone.utc),
            "source": f"X / @{handle}",
            "source_url": f"https://twitterviewer.app/{handle}",
            "twitterviewer_profile_url": f"https://twitterviewer.app/{handle}",
            "author_handle": handle,
            "x_account": handle,
            "x_cashtags": list(dict.fromkeys(tag.upper() for tag in cashtags)),
            "x_hashtags": list(dict.fromkeys(hashtags)),
            "youtube_urls": _youtube_urls(clean_text),
            "content_type": "POST",
            "source_key": "x_posts",
            "is_x_post": True,
            "transport": "twitterviewer-public-profile-html",
            # The user explicitly asked for alerts on every new post from these
            # allowlisted handles, rather than only keyword/business-matched posts.
            "twitterviewer_source": True,
            "alert_all_posts": True,
            "_timestamp_exact": True,
        }

    async def _collect_one_account(
        self,
        page: Any,
        handle: str,
        *,
        cutoff: datetime,
        max_items: int,
        max_load_more: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str], bool]:
        """Return rows, account diagnostics, errors, and whether the run must stop."""
        detail: dict[str, Any] = {
            "account": handle,
            "profile_url": f"https://twitterviewer.app/{handle}",
            "attempted": True,
            "http_status": None,
            "posts_seen": 0,
            "fresh_posts": 0,
            "load_more_attempts": 0,
            "status": "empty_recent_window",
        }
        errors: list[str] = []
        posts: dict[str, dict[str, Any]] = {}
        stop_run = False
        response_statuses: list[tuple[str, int]] = []

        def remember_response(response: Any) -> None:
            try:
                response_statuses.append((str(response.url), int(response.status)))
            except Exception:
                return

        page.on("response", remember_response)
        try:
            response = await page.goto(
                detail["profile_url"],
                wait_until="domcontentloaded",
                timeout=int(self.timeout_seconds * 1000),
            )
            if response is not None:
                detail["http_status"] = int(response.status)
                if int(response.status) in {401, 403, 429}:
                    detail["status"] = "http_blocked" if int(response.status) != 429 else "rate_limited"
                    errors.append(
                        f"@{handle}: TwitterViewer returned HTTP {response.status}; stopped without retrying around the block"
                    )
                    return [], detail, errors, True
            # TwitterViewer's public HTML shell can load before its posts timeline.
            # The profile route is primary. If it yields no usable posts (and is not
            # blocked/rate-limited), try the normal homepage input as a second entry
            # route: https://twitterviewer.app -> type @handle -> View. This uses the
            # site's public UI only; it does not call private endpoints or APIs.
            seen_status_ids: set[str] = set()

            def process_payloads(payloads: Any) -> bool:
                """Merge visible posts and report whether the oldest timeline item is stale."""
                tail_time: datetime | None = None
                tail_id = ""
                for payload in payloads or []:
                    if not isinstance(payload, dict):
                        continue
                    post_handle = _normalise_handle(payload.get("handle"))
                    post_id = _clean(payload.get("post_id"))
                    text = str(payload.get("text") or "").strip()
                    if post_handle != handle or not post_id.isdigit():
                        continue
                    seen_status_ids.add(post_id)
                    published = _snowflake_datetime(post_id) or _published_datetime(payload.get("published_at"))
                    # The status ID is the strongest original-time signal. Keep the
                    # last status in DOM order as a marker for when we've paged past
                    # the rolling cutoff.
                    if published is not None:
                        tail_id, tail_time = post_id, published
                    if not text or _is_identity_or_chrome_text(text, handle):
                        continue
                    if published is None or published < cutoff or not _in_window(published, now=datetime.now(timezone.utc)):
                        continue
                    url = f"https://x.com/{handle}/status/{post_id}"
                    row = self._make_post_row(text, handle, url, published)
                    old = posts.get(url)
                    old_text = str((old or {}).get("raw_content") or "")
                    # If hydration improves text on a later DOM snapshot, keep the
                    # complete post body instead of a shorter author/preview string.
                    if old is None or len(row["raw_content"]) > len(old_text):
                        posts[url] = row
                detail["posts_seen"] = max(int(detail.get("posts_seen") or 0), len(seen_status_ids))
                return bool(tail_id and tail_time and tail_time < cutoff)

            async def wait_for_timeline() -> None:
                try:
                    await page.wait_for_function(
                        r"""() => {
                          const statusPath = /^\/(?:[A-Za-z0-9_]{1,50}\/)?(?:status|statuses|post|posts)\/\d+(?:[/?#]|$)/i;
                          const xPath = /^\/[A-Za-z0-9_]{1,50}\/status(?:es)?\/\d+(?:[/?#]|$)/i;
                          for (const a of document.querySelectorAll('a[href]')) {
                            try {
                              const u = new URL(a.href || a.getAttribute('href') || '', location.href);
                              const host = (u.hostname || '').toLowerCase();
                              if ((host === 'x.com' || host.endsWith('.x.com') || host === 'twitter.com' || host.endsWith('.twitter.com')) && xPath.test(u.pathname)) return true;
                              if ((host === 'twitterviewer.app' || host.endsWith('.twitterviewer.app')) && statusPath.test(u.pathname)) return true;
                            } catch (_) {}
                          }
                          return !!document.querySelector('[data-testid="tweetText"], [data-testid="postText"], [class*="post-text"], [class*="tweet-text"]');
                        }""",
                        timeout=min(6000, int(self.timeout_seconds * 1000)),
                        polling=200,
                    )
                except Exception:
                    # A short settle is enough before the next independent route.
                    await page.wait_for_timeout(650)

            async def parse_current_timeline(*, allow_load_more: bool = True) -> None:
                await wait_for_timeline()
                payloads = await page.evaluate(_EXTRACT_POSTS_JS, handle)
                # One late-hydration retry, not a repeated long wait on every page.
                if not payloads:
                    await page.wait_for_timeout(700)
                    payloads = await page.evaluate(_EXTRACT_POSTS_JS, handle)

                loop_limit = max_load_more if allow_load_more else 0
                for pass_index in range(loop_limit + 1):
                    reached_cutoff = process_payloads(payloads)
                    if len(posts) >= max_items or reached_cutoff or pass_index >= loop_limit:
                        break
                    # Use only the visible public "Load more posts" UI control.
                    clicked = await page.evaluate(
                        """() => {
                          const elements = [...document.querySelectorAll('button, a, [role="button"]')];
                          const el = elements.find(item => {
                            const text = (item.innerText || item.textContent || '').replace(/\\s+/g, ' ').trim();
                            const style = window.getComputedStyle(item);
                            const visible = !!(item.getClientRects().length && style.visibility !== 'hidden' && style.display !== 'none');
                            return visible && /load more posts/i.test(text);
                          });
                          if (!el) return false;
                          el.click();
                          return true;
                        }"""
                    )
                    if not clicked:
                        break
                    detail["load_more_attempts"] += 1
                    before_ids = set(seen_status_ids)
                    await page.wait_for_timeout(850)
                    payloads = await page.evaluate(_EXTRACT_POSTS_JS, handle)
                    after_ids = {
                        str(row.get("post_id")) for row in (payloads or [])
                        if isinstance(row, dict) and row.get("post_id")
                    }
                    if after_ids and after_ids.issubset(before_ids):
                        break

            await parse_current_timeline()

            # Decide whether the homepage UI fallback is safe. Never use a different
            # route to retry around explicit 401/403/429 responses or challenge pages.
            twitterviewer_blocks = [
                (url, code) for url, code in response_statuses
                if "twitterviewer.app" in url.casefold() and code in {401, 403, 429}
            ]
            try:
                initial_body = _clean((await page.locator("body").inner_text())[:6000]).casefold()
            except Exception:
                initial_body = ""
            initial_challenge_markers = (
                "verify you are human", "checking your browser", "too many requests",
                "rate limit exceeded", "unusual traffic", "access denied", "captcha", "just a moment",
            )
            is_challenge = any(marker in initial_body for marker in initial_challenge_markers)

            if not posts and not twitterviewer_blocks and not is_challenge:
                try:
                    homepage = "https://twitterviewer.app/"
                    home_response = await page.goto(
                        homepage,
                        wait_until="domcontentloaded",
                        timeout=int(self.timeout_seconds * 1000),
                    )
                    detail["homepage_fallback_attempted"] = True
                    detail["homepage_url"] = homepage
                    if home_response is not None:
                        home_status = int(home_response.status)
                        detail["homepage_http_status"] = home_status
                        if home_status in {401, 403, 429}:
                            detail["status"] = "rate_limited" if home_status == 429 else "http_blocked"
                            errors.append(
                                f"@{handle}: TwitterViewer homepage returned HTTP {home_status}; stopped without retrying around the block"
                            )
                            return [], detail, errors, True

                    # The homepage and profile view have both used standard text
                    # inputs over time. Prefer an input that advertises username or
                    # URL, then fall back to any visible non-hidden text input.
                    field = None
                    for selector in (
                        'input[placeholder*="username" i]',
                        'input[placeholder*="@username" i]',
                        'input[placeholder*="profile" i]',
                        'input[placeholder*="post URL" i]',
                        'input[aria-label*="username" i]',
                        'input[aria-label*="profile" i]',
                        'input:not([type="hidden"]):not([type="password"]):not([type="submit"])',
                        'textarea',
                    ):
                        candidate = page.locator(selector).first
                        try:
                            if await candidate.count() and await candidate.is_visible() and await candidate.is_enabled():
                                field = candidate
                                break
                        except Exception:
                            continue

                    if field is None:
                        errors.append(f"@{handle}: TwitterViewer homepage had no visible username/profile input")
                    else:
                        await field.fill(f"@{handle}")
                        view_button = page.get_by_role("button", name=re.compile(r"^\s*view\s*$", re.I)).first
                        clicked_view = False
                        try:
                            if await view_button.count() and await view_button.is_visible() and await view_button.is_enabled():
                                await view_button.click(timeout=2500)
                                clicked_view = True
                        except Exception:
                            clicked_view = False
                        if not clicked_view:
                            try:
                                await field.press("Enter", timeout=2500)
                            except Exception:
                                errors.append(f"@{handle}: could not submit the TwitterViewer homepage username form")

                        # Most versions navigate to /{handle}; allow a client-side
                        # render at / as well, but wait briefly for actual post markup.
                        try:
                            await page.wait_for_function(
                                "(h) => { const p = decodeURIComponent(location.pathname || '/').replace(/^\\/+|\\/+$/g, '').toLowerCase(); return p === String(h || '').toLowerCase() || !!document.querySelector('[data-testid=\"tweetText\"], [data-testid=\"postText\"], [class*=\"post-text\"], [class*=\"tweet-text\"]'); }",
                                arg=handle,
                                timeout=min(7000, int(self.timeout_seconds * 1000)),
                                polling=200,
                            )
                        except Exception:
                            await page.wait_for_timeout(700)

                        # Start a clean ID set for the second entry route, and retry
                        # the same full-body parser against its rendered timeline.
                        seen_status_ids.clear()
                        detail["entry_route"] = "homepage-form"
                        await parse_current_timeline(allow_load_more=True)
                except Exception as exc:
                    errors.append(f"@{handle}: TwitterViewer homepage fallback: {type(exc).__name__}: {str(exc)[:160]}")
            else:
                detail["homepage_fallback_attempted"] = False

            # Stop on rate-limit/human-verification indicators; do not try another
            # URL or alter headers/cookies to get around them.
            host_blocks = [
                (url, code) for url, code in response_statuses
                if "twitterviewer.app" in url.casefold() and code in {401, 403, 429}
            ]
            if host_blocks:
                _, code = host_blocks[-1]
                detail["status"] = "rate_limited" if code == 429 else "http_blocked"
                errors.append(f"@{handle}: TwitterViewer page resource returned HTTP {code}; stopping the run")
                return [], detail, errors, True

            try:
                body_text = _clean((await page.locator("body").inner_text())[:6000]).casefold()
                detail["page_title"] = _clean(await page.title())[:160]
            except Exception:
                body_text = ""
            challenge_markers = (
                "verify you are human",
                "checking your browser",
                "too many requests",
                "rate limit exceeded",
                "unusual traffic",
                "access denied",
                "captcha",
                "just a moment",
            )
            if any(marker in body_text for marker in challenge_markers):
                detail["status"] = "challenge_or_rate_limited"
                errors.append(f"@{handle}: TwitterViewer showed a challenge/rate-limit page; no bypass attempted")
                return [], detail, errors, True

            detail["fresh_posts"] = len(posts)
            if posts:
                detail["status"] = "ok"
            elif detail.get("http_status") is None:
                detail["status"] = "navigation_no_response"
                errors.append(f"@{handle}: no main-page HTTP response was observed")
        except Exception as exc:
            detail["status"] = "failed"
            detail["exception"] = f"{type(exc).__name__}: {str(exc)[:180]}"
            errors.append(f"@{handle}: {type(exc).__name__}: {str(exc)[:180]}")
        finally:
            try:
                page.remove_listener("response", remember_response)
            except Exception:
                pass

        output = sorted(posts.values(), key=lambda row: row["published_at"], reverse=True)[:max_items]
        detail["fresh_posts"] = len(output)
        return output, detail, errors, stop_run

    async def collect_allowed_accounts(
        self,
        *,
        max_items_per_account: int = 100,
        days: int | None = 2,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Collect allowlisted posts from the rolling 48-hour window.

        ``days`` is retained for compatibility with the existing collector API;
        the alert collector deliberately uses a strict 48-hour rolling window.
        """
        rows, _errors = await self.collect_from_feeds([], days=2, max_items=max_items_per_account)
        return rows

    async def _collect_public_feed_rows(
        self, feed_urls: Iterable[str], *, max_items: int = 100,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Parse explicitly configured public RSS/Atom feeds (never a JSON API)."""
        supplied = list(dict.fromkeys(_clean(value) for value in feed_urls if _clean(value)))
        # Do not mistakenly request configured profile-page templates as XML feeds.
        # Only URLs that look like an RSS/Atom endpoint enter this strategy; the
        # independent profile scraper still handles ordinary public profile URLs.
        urls = []
        for value in supplied:
            path = (urlparse(value).path or "").casefold()
            query = (urlparse(value).query or "").casefold()
            if any(marker in path for marker in ("/rss", "/feed", ".rss", ".xml", "/atom")) or any(marker in query for marker in ("format=rss", "format=atom", "feed=rss")):
                urls.append(value)
        urls = urls[:30]
        if not urls:
            return [], []
        try:
            import httpx
        except Exception as exc:
            return [], [f"public RSS support unavailable: {type(exc).__name__}: {str(exc)[:140]}"]
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept": "application/atom+xml,application/rss+xml,application/xml,text/xml,text/html;q=0.8,*/*;q=0.5",
        }
        allowed = set(self.allowed_accounts)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        rows: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        async with httpx.AsyncClient(timeout=min(self.timeout_seconds, 12.0), follow_redirects=True, headers=headers) as client:
            for url in urls:
                try:
                    response = await client.get(url)
                    if response.status_code in {401, 403, 429}:
                        errors.append(f"public feed {url}: HTTP {response.status_code}")
                        continue
                    response.raise_for_status()
                    content = response.text or ""
                    root = ET.fromstring(content)
                except Exception as exc:
                    errors.append(f"public feed {url}: {type(exc).__name__}: {str(exc)[:180]}")
                    continue
                entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
                for entry in entries:
                    def child_text(names: tuple[str, ...]) -> str:
                        wanted = {name.rsplit("}", 1)[-1].split(":")[-1].casefold() for name in names}
                        for node in entry.iter():
                            local_name = str(node.tag).rsplit("}", 1)[-1].split(":")[-1].casefold()
                            if local_name not in wanted:
                                continue
                            value = _clean(" ".join(node.itertext()))
                            if value:
                                return value
                        return ""
                    title = child_text(("title", "{http://www.w3.org/2005/Atom}title"))
                    body = child_text(("description", "encoded", "content:encoded", "{http://www.w3.org/2005/Atom}content", "{http://www.w3.org/2005/Atom}summary"))
                    link = child_text(("link", "{http://www.w3.org/2005/Atom}link"))
                    if not link:
                        node = entry.find("{http://www.w3.org/2005/Atom}link")
                        if node is not None:
                            link = _clean(node.get("href"))
                    author = child_text(("author", "creator", "dc:creator", "{http://purl.org/dc/elements/1.1/}creator", "{http://www.w3.org/2005/Atom}author"))
                    # Twitter syndication feeds often put the handle in URL/text
                    # rather than <author>; extract it only when exact and allowlisted.
                    match = _STATUS_RE.search(link) or _STATUS_RE.search(body) or _STATUS_RE.search(title)
                    if not match:
                        continue
                    handle, post_id = match.group(1).casefold(), match.group(2)
                    if handle not in allowed:
                        continue
                    published_raw = child_text(("pubDate", "published", "updated", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated"))
                    published = _published_datetime(published_raw)
                    if published is None and post_id:
                        published = _snowflake_datetime(post_id)
                    if published is None or not _in_window(published, now=datetime.now(timezone.utc)) or published < cutoff:
                        continue
                    content_text = body or title
                    content_text = re.sub(r"https?://\S+", " ", content_text)
                    content_text = _clean(content_text)
                    # Avoid accidentally persisting feed chrome instead of the post.
                    content_text = re.sub(rf"^@?{re.escape(handle)}\s*(?:on X|on Twitter)?\s*[:|\-–—]*\s*", "", content_text, flags=re.I)
                    if len(content_text) < 10:
                        continue
                    canonical_url = f"https://x.com/{handle}/status/{post_id}"
                    rows.setdefault(canonical_url, self._make_post_row(content_text, handle, canonical_url, published))
                    if len(rows) >= max(1, min(int(max_items), 250)) * len(allowed):
                        break
        return list(rows.values()), errors[:30]

    @staticmethod
    def _status_from_href(raw: str) -> tuple[str, str] | None:
        match = _STATUS_RE.search(raw or "")
        if match:
            return match.group(1).casefold(), match.group(2)
        try:
            parsed = urlparse(raw)
            # Public profile HTML frequently uses relative hrefs such as
            # /handle/status/123. Accept those only as path-style status links;
            # absolute URLs remain restricted by the X/Twitter regex above.
            if not parsed.scheme and not parsed.netloc:
                relative = re.match(r"^/?([A-Za-z0-9_]{1,50})/(?:status|statuses|post|posts)/(\d+)(?:[/?#]|$)", parsed.path, re.I)
                if relative:
                    return relative.group(1).casefold(), relative.group(2)
            query = dict()
            if parsed.query:
                from urllib.parse import parse_qs
                query = parse_qs(parsed.query)
            redirect_values = []
            for key in ("url", "q", "uddg", "target", "r"):
                redirect_values.extend(query.get(key, []))
            # Bing sometimes wraps a destination as u=a1<base64url>; decode that
            # public redirect format without issuing an extra request.
            for encoded in query.get("u", []):
                try:
                    import base64
                    value = encoded[2:] if encoded.startswith("a1") else encoded
                    value += "=" * (-len(value) % 4)
                    decoded = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8", errors="ignore")
                    redirect_values.append(decoded)
                except Exception:
                    pass
            for value in redirect_values:
                candidate = _STATUS_RE.search(unquote(value))
                if candidate:
                    return candidate.group(1).casefold(), candidate.group(2)
        except ValueError:
            pass
        return None

    @staticmethod
    def _search_result_text(anchor: Any, handle: str) -> str:
        """Extract a likely post snippet from a search result card, not its chrome."""
        candidates: list[str] = []
        node = anchor
        for _ in range(5):
            if node is None:
                break
            value = _clean(node.get_text(" ", strip=True))
            if 15 <= len(value) <= 3000:
                candidates.append(value)
            node = node.parent
        # Shortest containing result text tends to avoid unrelated page content.
        candidates.sort(key=lambda value: (len(value), -sum(ch.isalpha() for ch in value)))
        for value in candidates:
            value = re.sub(r"^.*?\b(?:on X|on Twitter)\s*[:|\-–—]\s*", "", value, flags=re.I)
            value = re.sub(rf"^@?{re.escape(handle)}\s*[:|\-–—]\s*", "", value, flags=re.I)
            value = re.sub(r"\b(?:X|Twitter)\s*·\s*\d+\s*(?:minutes?|hours?|days?) ago\b", " ", value, flags=re.I)
            value = _clean(value)
            profile_only = bool(re.fullmatch(
                rf"(?:[^:()]+\s*)?\(@?{re.escape(handle)}\)\s*(?:[/|·-]\s*)?(?:X|Twitter)?",
                value, flags=re.I,
            ))
            generic_only = bool(re.fullmatch(r"(?:posts?|replies|media|likes|followers|following|x|twitter)\W*", value, flags=re.I))
            if len(value) >= 15 and not profile_only and not generic_only:
                return value[:10000]
        return ""

    async def _fetch_public_status_text(self, url: str, handle: str) -> str:
        """Read public metadata from an individual status page, with no API calls."""
        import httpx
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        try:
            async with httpx.AsyncClient(timeout=min(self.timeout_seconds, 10.0), follow_redirects=True, headers=headers) as client:
                response = await client.get(url)
                if response.status_code in {401, 403, 429} or response.status_code >= 400:
                    return ""
                soup = BeautifulSoup(response.text or "", "html.parser")
                for attrs in (
                    {"property": "og:description"}, {"name": "description"},
                    {"name": "twitter:description"},
                ):
                    node = soup.find("meta", attrs=attrs)
                    value = _clean(node.get("content")) if node else ""
                    if value and len(value) >= 12:
                        value = re.sub(rf"^@?{re.escape(handle)}\s*(?:on X|on Twitter)?\s*[:|\-–—]*\s*", "", value, flags=re.I)
                        if re.match(r"^(?:the )?(?:latest )?posts? (?:by|from) @?", value, flags=re.I):
                            continue
                        if re.search(r"\b(?:followers|following|profile|posts by)\b", value, flags=re.I) and not re.search(r"[.!?]", value):
                            continue
                        if len(value) >= 12:
                            return value[:10000]
                article = soup.select_one("article")
                if article:
                    value = _clean(article.get_text(" ", strip=True))
                    if len(value) >= 15:
                        return value[:10000]
        except Exception:
            return ""
        return ""

    async def _collect_search_engine_posts(
        self, handles: Iterable[str], *, max_items_per_account: int = 100,
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
        """Discover individual public status URLs through search-result HTML.

        Google/Bing are only fallback discovery pages, not search APIs. Each row
        must resolve to an allowlisted status URL and have verifiable post time from
        its Snowflake ID (or page timestamp). No profile URL is ever saved as a post.
        """
        import httpx
        handle_list = list(dict.fromkeys(_normalise_handle(value) for value in handles if _normalise_handle(value)))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        }
        rows: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        diagnostics: dict[str, Any] = {"accounts_searched": 0, "status_urls_seen": 0, "fresh_posts": 0, "snippets": 0}
        from_date = cutoff.date().isoformat()
        async with httpx.AsyncClient(timeout=min(self.timeout_seconds, 10.0), follow_redirects=True, headers=headers) as client:
            for handle in handle_list:
                diagnostics["accounts_searched"] += 1
                account_candidates: dict[str, str] = {}
                queries = (
                    f'site:x.com/{handle}/status after:{from_date} {handle}',
                    f'site:twitter.com/{handle}/status after:{from_date} {handle}',
                )
                for query in queries:
                    search_urls = (
                        "https://www.google.com/search?" + urlencode({"q": query, "num": "20"}),
                        "https://www.bing.com/search?" + urlencode({"q": query, "count": "20"}),
                    )
                    for search_url in search_urls:
                        try:
                            response = await client.get(search_url)
                            if response.status_code in {401, 403, 429}:
                                errors.append(f"Search fallback {urlparse(search_url).hostname}: HTTP {response.status_code}")
                                continue
                            response.raise_for_status()
                            soup = BeautifulSoup(response.text or "", "html.parser")
                            for anchor in soup.select("a[href]"):
                                parsed_status = self._status_from_href(str(anchor.get("href") or ""))
                                if not parsed_status:
                                    continue
                                post_handle, post_id = parsed_status
                                if post_handle != handle:
                                    continue
                                status_url = f"https://x.com/{handle}/status/{post_id}"
                                snippet = self._search_result_text(anchor, handle)
                                previous = account_candidates.get(status_url, "")
                                if len(snippet) > len(previous):
                                    account_candidates[status_url] = snippet
                        except Exception as exc:
                            errors.append(f"Search fallback {urlparse(search_url).hostname}: {type(exc).__name__}: {str(exc)[:120]}")
                # If both Google and Bing returned no individual status links,
                # make one final independent HTML-search attempt through DuckDuckGo.
                if not account_candidates:
                    ddg_queries = (
                        f'site:x.com/{handle}/status {handle} after:{from_date}',
                        f'"{handle}" "x.com/{handle}/status/"',
                    )
                    for query in ddg_queries:
                        search_url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
                        try:
                            response = await client.get(search_url)
                            if response.status_code in {401, 403, 429}:
                                errors.append(f"Search fallback duckduckgo: HTTP {response.status_code}")
                                continue
                            response.raise_for_status()
                            soup = BeautifulSoup(response.text or "", "html.parser")
                            for anchor in soup.select("a[href]"):
                                parsed_status = self._status_from_href(str(anchor.get("href") or ""))
                                if not parsed_status:
                                    continue
                                post_handle, post_id = parsed_status
                                if post_handle != handle:
                                    continue
                                status_url = f"https://x.com/{handle}/status/{post_id}"
                                snippet = self._search_result_text(anchor, handle)
                                if len(snippet) > len(account_candidates.get(status_url, "")):
                                    account_candidates[status_url] = snippet
                        except Exception as exc:
                            errors.append(f"Search fallback duckduckgo: {type(exc).__name__}: {str(exc)[:120]}")
                diagnostics["status_urls_seen"] += len(account_candidates)
                # Search result cards can omit the snippet. Fetch a bounded number
                # of public status pages as the next HTML-only fallback.
                for status_url, snippet in list(account_candidates.items())[:max(1, min(int(max_items_per_account), 25))]:
                    post_id = status_url.rsplit("/", 1)[-1]
                    published = _snowflake_datetime(post_id)
                    if not published or published < cutoff or not _in_window(published, now=datetime.now(timezone.utc)):
                        continue
                    diagnostics["fresh_posts"] += 1
                    if len(snippet) < 12:
                        snippet = await self._fetch_public_status_text(status_url, handle)
                    snippet = _clean(snippet)
                    if len(snippet) < 12:
                        continue
                    # Remove common search engine title suffixes while retaining post content.
                    snippet = re.sub(r"\s*[-|–—]\s*(?:X|Twitter)\s*$", "", snippet, flags=re.I).strip()
                    if len(snippet) < 12:
                        continue
                    diagnostics["snippets"] += 1
                    rows.setdefault(status_url, self._make_post_row(snippet, handle, status_url, published))
        return list(rows.values()), errors[:40], diagnostics

    async def collect_from_feeds(
        self,
        feed_urls: Iterable[str] = (),
        *,
        days: int = 2,
        max_items: int = 100,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Run public-page transports, prioritizing TwitterViewer profile timelines.

        The profile route ``https://twitterviewer.app/{handle}`` is primary; the
        homepage ``https://twitterviewer.app`` is a UI-based fallback that submits
        the approved handle when a profile route returns no usable posts. Optional
        public RSS/Atom and direct HTML/search pages are later fallbacks only. No
        official X API, bearer token or login is used.
        """
        del days  # NewsAlert contract is a rolling 48-hour window.
        if not self.allowed_accounts:
            error = "No allowed X accounts configured."
            self.last_diagnostics = {"transport": "none", "api_used": False, "accounts_checked": 0, "posts_found": 0, "errors": [error]}
            return [], [error]

        combined: dict[str, dict[str, Any]] = {}

        def merge_row(row: dict[str, Any]) -> None:
            key = _clean(row.get("url"))
            if not key:
                return
            previous = combined.get(key)
            if previous is None:
                combined[key] = row
                return
            previous_text = _clean(previous.get("raw_content") or previous.get("summary") or previous.get("title"))
            new_text = _clean(row.get("raw_content") or row.get("summary") or row.get("title"))
            previous_published = previous.get("published_at")
            new_published = row.get("published_at")
            # Prefer the most complete post body, not a username/title snippet.
            if (len(new_text) > len(previous_text)) or (len(new_text) == len(previous_text) and previous_published is None and new_published is not None):
                combined[key] = row

        errors: list[str] = []
        transports: list[str] = []
        profile_diag: dict[str, Any] = {}
        direct_profile_diag: dict[str, Any] = {}
        search_diag: dict[str, Any] = {}
        feed_urls = list(feed_urls or [])

        # PRIMARY: rendered TwitterViewer profile pages, one URL per allowed handle.
        try:
            transports.append("twitterviewer-public-profile-html")
            profile_rows, profile_errors = await self._collect_public_profiles(max_items=max_items)
            errors.extend(profile_errors)
            profile_diag = dict(self.last_diagnostics or {})
            for row in profile_rows:
                handle = str(row.get("author_handle") or "").casefold()
                if handle in self.allowed_accounts and not _is_identity_or_chrome_text(
                    row.get("raw_content") or row.get("summary") or row.get("title"), handle
                ):
                    merge_row(row)
        except Exception as exc:
            errors.append(f"TwitterViewer primary profile collector: {type(exc).__name__}: {str(exc)[:180]}")

        # FALLBACK 1: explicitly configured public RSS/Atom feeds (if any).
        if feed_urls:
            try:
                transports.append("public-rss-atom-html")
                rows, problems = await self._collect_public_feed_rows(feed_urls, max_items=max_items)
                errors.extend(problems)
                for row in rows:
                    handle = str(row.get("author_handle") or "").casefold()
                    if handle in self.allowed_accounts and not _is_identity_or_chrome_text(
                        row.get("raw_content") or row.get("summary") or row.get("title"), handle
                    ):
                        merge_row(row)
            except Exception as exc:
                errors.append(f"Public feed fallback: {type(exc).__name__}: {str(exc)[:180]}")

        # FALLBACK 2: ordinary public profile HTML on X; only HTML parsing, no APIs.
        try:
            transports.append("x-public-profile-html-fallback")
            direct_rows, direct_errors, direct_profile_diag = await self._collect_direct_x_profile_html(max_items=max_items)
            errors.extend(direct_errors)
            for row in direct_rows:
                handle = str(row.get("author_handle") or "").casefold()
                if handle in self.allowed_accounts and not _is_identity_or_chrome_text(
                    row.get("raw_content") or row.get("summary") or row.get("title"), handle
                ):
                    merge_row(row)
        except Exception as exc:
            errors.append(f"Direct public X HTML fallback: {type(exc).__name__}: {str(exc)[:180]}")

        per_handle_before_search: dict[str, int] = {}
        for row in combined.values():
            handle = str(row.get("author_handle") or "").casefold()
            per_handle_before_search[handle] = per_handle_before_search.get(handle, 0) + 1
        search_threshold = min(3, max(1, int(max_items or 100)))
        missing = [handle for handle in sorted(self.allowed_accounts) if per_handle_before_search.get(handle, 0) < search_threshold]
        # FALLBACK 3: public search-result HTML + individual public status metadata.
        if missing:
            try:
                transports.append("Google-Bing-DuckDuckGo-public-search-html")
                rows, problems, search_diag = await self._collect_search_engine_posts(missing, max_items_per_account=max_items)
                errors.extend(problems)
                for row in rows:
                    handle = str(row.get("author_handle") or "").casefold()
                    if handle in self.allowed_accounts and not _is_identity_or_chrome_text(
                        row.get("raw_content") or row.get("summary") or row.get("title"), handle
                    ):
                        merge_row(row)
            except Exception as exc:
                errors.append(f"Search-engine fallback: {type(exc).__name__}: {str(exc)[:180]}")

        now = datetime.now(timezone.utc)
        output = [row for row in combined.values() if _in_window(row.get("published_at"), now=now)]
        output.sort(key=lambda row: row.get("published_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        per_account: dict[str, int] = {}
        bounded: list[dict[str, Any]] = []
        cap = max(1, min(int(max_items or 100), 250))
        for row in output:
            handle = str(row.get("author_handle") or "").casefold()
            if handle not in self.allowed_accounts or per_account.get(handle, 0) >= cap:
                continue
            text_body = _clean(row.get("raw_content") or row.get("summary") or row.get("title"))
            if _is_identity_or_chrome_text(text_body, handle):
                continue
            # Keep the profile source URL distinct from this post's unique URL.
            row["source_url"] = f"https://twitterviewer.app/{handle}"
            row["twitterviewer_profile_url"] = row["source_url"]
            per_account[handle] = per_account.get(handle, 0) + 1
            bounded.append(row)

        all_errors = list(dict.fromkeys(errors))
        self.last_diagnostics = {
            "transport": "+".join(transports),
            "transports_attempted": transports,
            "primary_source": "https://twitterviewer.app/{handle}",
            "homepage_fallback_source": "https://twitterviewer.app",
            "api_used": False,
            "credentials_required": False,
            "accounts_checked": len(self.allowed_accounts),
            "accounts_succeeded": sum(1 for handle in self.allowed_accounts if per_account.get(handle, 0)),
            "accounts_missing_posts": [handle for handle in sorted(self.allowed_accounts) if per_account.get(handle, 0) == 0],
            "account_results": profile_diag.get("account_results", []),
            "profile_diagnostics": profile_diag,
            "direct_profile_diagnostics": direct_profile_diag,
            "search_diagnostics": search_diag,
            "posts_found": len(bounded),
            "lookback_hours": 48,
            "max_items_per_account": cap,
            "errors": all_errors[:40],
        }
        logger.info(
            "NEWSALERT_X_PUBLIC_COLLECTOR_RESULT api_used=false primary=twitterviewer accounts=%s posts=%s transports=%s errors=%s",
            len(self.allowed_accounts), len(bounded), transports, len(all_errors),
        )
        return bounded, all_errors[:40]

    async def _collect_direct_x_profile_html(
        self,
        *,
        max_items: int = 100,
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
        """Read public X profile HTML directly; no API routes or authenticated context.

        This is an independent fallback to the TwitterViewer mirror. It accepts only
        explicit status URLs belonging to an allowlisted handle and only timestamps
        verifiable from the public status ID or visible <time> metadata.
        """
        import httpx

        handles = sorted(self.allowed_accounts)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        rows: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        account_results: list[dict[str, Any]] = []
        diagnostics: dict[str, Any] = {
            "transport": "x-public-profile-html",
            "accounts_attempted": 0,
            "status_urls_seen": 0,
            "fresh_status_urls": 0,
            "posts_found": 0,
            "api_used": False,
        }

        async def scrape_one(client: Any, handle: str) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
            detail: dict[str, Any] = {"account": handle, "profile_url": f"https://x.com/{handle}", "status": "empty", "http_status": None, "status_urls_seen": 0, "posts_found": 0}
            local_errors: list[str] = []
            try:
                response = await client.get(detail["profile_url"])
                detail["http_status"] = int(response.status_code)
                if response.status_code in {401, 403, 429}:
                    detail["status"] = "blocked_or_rate_limited"
                    local_errors.append(f"@{handle}: public X profile returned HTTP {response.status_code}; no access-control bypass attempted")
                    return [], detail, local_errors
                response.raise_for_status()
                body = response.text or ""
                if len(body.strip()) < 100:
                    detail["status"] = "empty_html"
                    return [], detail, [f"@{handle}: public X profile returned a short HTML document"]
                soup = BeautifulSoup(body, "html.parser")
                candidates: dict[str, tuple[str, str | None]] = {}
                # Hydrated pages may expose article cards; keep text and time near
                # the matching status URL to avoid collecting profile-page chrome.
                for article in soup.select("article, [data-testid='tweet'], [data-testid*='post']"):
                    links = []
                    for anchor in article.select("a[href]"):
                        parsed_status = self._status_from_href(str(anchor.get("href") or ""))
                        if parsed_status and parsed_status[0] == handle:
                            href_handle, post_id = parsed_status
                            links.append(post_id)
                    if not links:
                        continue
                    text_candidates = []
                    for selector in ("[data-testid='tweetText']", "[data-testid*='post-text']", "[lang]", "div[dir='auto']", "p"):
                        for node in article.select(selector):
                            value = _clean(node.get_text(" ", strip=True))
                            if len(value) >= 12 and not re.fullmatch(r"(?:Reply|Repost|Like|Share|Bookmark|Views?)", value, re.I):
                                text_candidates.append(value)
                    text_value = max(text_candidates, key=len, default="")[:10000]
                    time_node = article.select_one("time[datetime], time[dateTime]")
                    time_value = (time_node.get("datetime") or time_node.get("dateTime")) if time_node else None
                    for post_id in links:
                        old = candidates.get(post_id, ("", None))
                        if len(text_value) > len(old[0]) or (not old[1] and time_value):
                            candidates[post_id] = (text_value, time_value)
                # Even non-hydrated public pages sometimes include status links in
                # their initial HTML. Add those IDs and attempt public status metadata.
                for raw in re.findall(r"https?://(?:www\.)?(?:x\.com|twitter\.com)/[A-Za-z0-9_]{1,50}/status/\d+", body, re.I):
                    parsed_status = self._status_from_href(raw)
                    if parsed_status and parsed_status[0] == handle:
                        candidates.setdefault(parsed_status[1], ("", None))
                # Also extract relative status paths from normal rendered anchors.
                for anchor in soup.select("a[href]"):
                    parsed_status = self._status_from_href(str(anchor.get("href") or ""))
                    if parsed_status and parsed_status[0] == handle:
                        candidates.setdefault(parsed_status[1], ("", None))
                detail["status_urls_seen"] = len(candidates)
                found: list[dict[str, Any]] = []
                # Direct status-page fetches can be slow when X returns a public
                # shell without tweet text. Bound that metadata fallback; the mirror
                # and independent search engines still cover other recent posts.
                cap = max(1, min(int(max_items or 100), 8))
                for post_id, (text_value, visible_time) in list(candidates.items())[:cap]:
                    published = _snowflake_datetime(post_id) or _published_datetime(visible_time)
                    if not published or published < cutoff or not _in_window(published, now=datetime.now(timezone.utc)):
                        continue
                    if len(text_value) < 12:
                        text_value = await self._fetch_public_status_text(f"https://x.com/{handle}/status/{post_id}", handle)
                    text_value = _clean(text_value)
                    if len(text_value) < 12:
                        continue
                    post_url = f"https://x.com/{handle}/status/{post_id}"
                    found.append(self._make_post_row(text_value, handle, post_url, published))
                detail["posts_found"] = len(found)
                detail["status"] = "ok" if found else "empty_recent_window"
                return found, detail, local_errors
            except Exception as exc:
                detail["status"] = "error"
                local_errors.append(f"@{handle} public X HTML: {type(exc).__name__}: {str(exc)[:140]}")
                return [], detail, local_errors

        timeout = httpx.Timeout(min(self.timeout_seconds, 8.0))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            semaphore = asyncio.Semaphore(3)
            async def bounded(handle: str):
                async with semaphore:
                    return await scrape_one(client, handle)
            results = await asyncio.gather(*(bounded(handle) for handle in handles), return_exceptions=True)
        for handle, result in zip(handles, results):
            diagnostics["accounts_attempted"] += 1
            if isinstance(result, BaseException):
                errors.append(f"@{handle} public X HTML: {type(result).__name__}: {str(result)[:140]}")
                account_results.append({"account": handle, "status": "error"})
                continue
            account_rows, detail, local_errors = result
            account_results.append(detail)
            errors.extend(local_errors)
            diagnostics["status_urls_seen"] += int(detail.get("status_urls_seen") or 0)
            for row in account_rows:
                rows.setdefault(str(row["url"]), row)
        output = sorted(rows.values(), key=lambda item: item.get("published_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        diagnostics["fresh_status_urls"] = len(output)
        diagnostics["posts_found"] = len(output)
        diagnostics["account_results"] = account_results
        return output, list(dict.fromkeys(errors))[:40], diagnostics

    async def _collect_public_profiles(
        self,
        *,
        days: int = 2,
        max_items: int = 100,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Compatibility entrypoint matching the existing X collector shape.

        ``feed_urls`` is intentionally ignored: TwitterViewer is read via its
        public profile pages, not treated as an RSS feed or API endpoint.
        """
        if not self.allowed_accounts:
            self.last_diagnostics = {
                "transport": "twitterviewer-public-profile-html",
                "api_used": False,
                "credentials_required": False,
                "accounts_checked": 0,
                "accounts_succeeded": 0,
                "posts_found": 0,
                "errors": ["No allowed TwitterViewer accounts configured."],
            }
            return [], self.last_diagnostics["errors"]

        max_items = max(1, min(int(max_items or 100), 250))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        delay_raw = os.getenv("NEWSALERT_TWITTERVIEWER_ACCOUNT_DELAY_SECONDS", "2.0")
        try:
            account_delay = max(1.0, min(float(delay_raw), 10.0))
        except (TypeError, ValueError):
            account_delay = 2.0
        try:
            max_load_more = int(os.getenv("NEWSALERT_TWITTERVIEWER_MAX_LOAD_MORE", "4"))
        except (TypeError, ValueError):
            max_load_more = 1
        max_load_more = max(0, min(max_load_more, 8))

        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            error = f"Playwright unavailable: {type(exc).__name__}: {str(exc)[:180]}"
            self.last_diagnostics = {
                "transport": "twitterviewer-public-profile-html",
                "api_used": False,
                "credentials_required": False,
                "accounts_checked": 0,
                "accounts_succeeded": 0,
                "posts_found": 0,
                "errors": [error],
            }
            return [], [error]

        handles = sorted(self.allowed_accounts)
        all_rows: dict[str, dict[str, Any]] = {}
        all_errors: list[str] = []
        account_details: list[dict[str, Any]] = []
        transports_attempted = ["twitterviewer-public-profile-html"]
        stop_reason = ""

        async with async_playwright() as playwright:
            browser = None
            launch_options: list[dict[str, Any]] = []
            executable = os.getenv("PLAYWRIGHT_EXECUTABLE_PATH") or os.getenv("PLAYWRIGHT_BROWSER_EXECUTABLE")
            if executable:
                launch_options.append({"executable_path": executable, "headless": True})
            launch_options.extend([
                {"channel": "chrome", "headless": True},
                {"channel": "msedge", "headless": True},
                {"headless": True},
            ])
            launch_errors: list[str] = []
            for options in launch_options:
                try:
                    browser = await playwright.chromium.launch(**options)
                    break
                except Exception as exc:
                    launch_errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
            if browser is None:
                error = "No public browser available: " + " | ".join(launch_errors[-3:])
                self.last_diagnostics = {
                    "transport": "+".join(transports_attempted),
                    "api_used": False,
                    "credentials_required": False,
                    "accounts_checked": 0,
                    "accounts_succeeded": 0,
                    "posts_found": 0,
                    "errors": [error],
                }
                return [], [error]

            try:
                context = await browser.new_context(
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                    viewport={"width": 1365, "height": 900},
                    java_script_enabled=True,
                )
                page = await context.new_page()
                try:
                    for index, handle in enumerate(handles):
                        if index:
                            await asyncio.sleep(account_delay)
                        found, detail, errors, stop_run = await self._collect_one_account(
                            page,
                            handle,
                            cutoff=cutoff,
                            max_items=max_items,
                            max_load_more=max_load_more,
                        )
                        account_details.append(detail)
                        all_errors.extend(errors)
                        for row in found:
                            all_rows.setdefault(row["url"], row)
                        if stop_run:
                            stop_reason = f"stopped_after_{handle}_{detail.get('status', 'blocked')}"
                            # Mark remaining accounts as intentionally skipped.
                            for remaining in handles[index + 1:]:
                                account_details.append({
                                    "account": remaining,
                                    "attempted": False,
                                    "status": "skipped_after_source_block",
                                    "fresh_posts": 0,
                                })
                            break
                finally:
                    await context.close()
            finally:
                await browser.close()

        final_now = datetime.now(timezone.utc)
        output = [
            row for row in all_rows.values()
            if _in_window(row.get("published_at"), now=final_now)
        ]
        output.sort(key=lambda row: row["published_at"], reverse=True)
        per_account_counts: dict[str, int] = {}
        bounded: list[dict[str, Any]] = []
        for row in output:
            handle = str(row.get("author_handle") or "").casefold()
            if per_account_counts.get(handle, 0) >= max_items:
                continue
            per_account_counts[handle] = per_account_counts.get(handle, 0) + 1
            bounded.append(row)

        missing = [handle for handle in handles if per_account_counts.get(handle, 0) == 0]
        unique_errors = list(dict.fromkeys(all_errors))
        self.last_diagnostics = {
            "transport": "+".join(transports_attempted),
            "transports_attempted": transports_attempted,
            "api_used": False,
            "credentials_required": False,
            "accounts_checked": len(handles),
            "accounts_attempted": sum(1 for item in account_details if item.get("attempted")),
            "accounts_succeeded": sum(1 for item in account_details if item.get("status") in {"ok", "empty_recent_window"}),
            "accounts_missing_posts": missing,
            "account_results": account_details,
            "posts_found": len(bounded),
            "lookback_hours": 48,
            "max_items_per_account": max_items,
            "account_delay_seconds": account_delay,
            "stopped_reason": stop_reason or None,
            "errors": unique_errors[:40],
        }
        logger.info(
            "NEWSALERT_TWITTERVIEWER_DIAGNOSTIC accounts=%s attempted=%s posts=%s stopped=%s",
            len(handles), self.last_diagnostics["accounts_attempted"], len(bounded), stop_reason or "none",
        )
        return bounded, unique_errors[:40]


# Compatibility alias for the earlier standalone draft; the backend uses XPostsCollector.
TwitterViewerPostsCollector = XPostsCollector