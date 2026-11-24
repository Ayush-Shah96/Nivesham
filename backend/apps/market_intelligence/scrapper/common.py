from __future__ import annotations

"""Shared transport and document-extraction utilities for Market Intelligence.

This module deliberately contains no sector dispatch or source-specific business
logic. The six sector modules own their URLs, browser actions, detail-page rules,
and any source-specific row normalization.
"""

import asyncio
import csv
import io
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable
from urllib.parse import parse_qsl, urljoin, urlparse, unquote_plus
from urllib.request import Request, urlopen

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 "
    "InvestmentIntelligenceMarketScraper/2.0"
)
MAX_HTML_CHARS = 16_000_000
MAX_TEXT_CHARS = 1_000_000
MAX_NETWORK_ITEMS = 600
MAX_NETWORK_BODY_CHARS = 8_000_000
MAX_NETWORK_TOTAL_CHARS = 64_000_000
MAX_SCRIPT_CHARS = 8_000_000
MAX_TABLES_PER_DOCUMENT = 200
MAX_ROWS_PER_TABLE = 10_000
MAX_FILTERS_PER_DOCUMENT = 500
MAX_KPIS_PER_DOCUMENT = 500
MAX_LINKED_RESOURCES = 250
MAX_LINK_BYTES = 20_000_000


class SourceUnavailable(RuntimeError):
    """The public source could not provide a usable response."""


class SourceRequiresConfiguration(RuntimeError):
    """The source needs a local dependency/configuration to run its scraper."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def daily_stamp() -> datetime:
    now = utc_now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value).replace(",", "")
    if not text or text.lower() in {"-", "—", "–", "na", "n/a", "null", "none", "not available"}:
        return None
    # A percent is stored as the displayed number (e.g. 12.5), not a ratio.
    match = re.search(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", text)
    try:
        return float(match.group(0)) if match else None
    except ValueError:
        return None


def parse_date(value: Any, *, reference: datetime | None = None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        if 500_000_000 <= number <= 4_000_000_000:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        return None
    text = clean_text(value)
    if not text:
        return None
    text_iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text_iso)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y",
        "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %Y", "%B %Y",
        "%Y-%m", "%Y%m%d", "%d-%b-%Y", "%b/%d/%Y", "%b-%d-%Y",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    abbreviated = re.fullmatch(r"([A-Za-z]{3})[\s/-]([0-9]{1,2})", text)
    if abbreviated:
        ref = reference or utc_now()
        try:
            month_num = datetime.strptime(abbreviated.group(1), "%b").month
            day_num = int(abbreviated.group(2))
            year = ref.year
            parsed = datetime(year, month_num, day_num, tzinfo=timezone.utc)
            if parsed > ref.replace(hour=23, minute=59, second=59, microsecond=0):
                parsed = parsed.replace(year=year - 1)
            return parsed
        except ValueError:
            return None
    quarter = re.search(r"\bQ([1-4])\s*[-/]?\s*(20\d{2})\b", text, re.I)
    if quarter:
        q, year = int(quarter.group(1)), int(quarter.group(2))
        month = q * 3
        return datetime(year, month, 1, tzinfo=timezone.utc)
    return None


def _headers(referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,application/pdf,text/csv,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if referer:
        headers["Referer"] = referer
    return headers


async def fetch_http(url: str, *, timeout: float = 45.0) -> dict[str, Any]:
    async with httpx.AsyncClient(headers=_headers(), follow_redirects=True, timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        try:
            text = response.text
        except Exception:
            text = response.content.decode("utf-8", errors="replace")
        return {
            "url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            # Capture JavaScript and XML responses too: public dashboard chart
            # configurations are frequently returned as application/javascript,
            # not HTML or JSON. Dropping them made the browser fallback the only
            # way to obtain chart data and hid a large part of source payloads.
            "html": text if any(token in content_type.lower() for token in (
                "text", "json", "javascript", "xml", "csv", "x-www-form-urlencoded"
            )) or not content_type else "",
            "body_bytes": len(response.content),
            "headers": {k: v for k, v in response.headers.items() if k.lower() in {"content-type", "last-modified", "etag", "cache-control"}},
        }


def _safe_text_response(response: Any, content_type: str, url_text: str) -> bool:
    interesting = re.compile(r"(?:api|ajax|fetch|json|chart|history|series|graph|graphql|data|table|export|report|vahan|analytics)", re.I)
    request_type = ""
    try:
        request_type = response.request.resource_type
    except Exception:
        pass
    if request_type in {"xhr", "fetch", "document"}:
        return True
    return bool(interesting.search(url_text) or any(m in content_type.lower() for m in ("json", "text", "javascript", "csv", "xml")))


def _safe_source_request_fields(request: Any) -> dict[str, str]:
    """Return non-secret query/form fields needed to interpret source XHRs.

    NISE sends the selected Year1 value and some filter values in its AJAX
    requests. Capturing those fields beside each response is more reliable than
    trying to infer the year later from whichever DOM snapshot happened to be
    taken last. Anti-forgery, authentication, cookie and other credential-like
    fields are deliberately discarded.
    """
    output: dict[str, str] = {}
    try:
        request_url = str(request.url or "")
    except Exception:
        request_url = ""
    try:
        pairs = parse_qsl(urlparse(request_url).query, keep_blank_values=True)
    except Exception:
        pairs = []

    try:
        post_data = str(request.post_data or "")
    except Exception:
        post_data = ""
    if post_data:
        parsed_body: Any = None
        try:
            parsed_body = json.loads(post_data)
        except Exception:
            parsed_body = None
        if isinstance(parsed_body, dict):
            pairs.extend((str(key), value) for key, value in parsed_body.items())
        elif isinstance(parsed_body, list):
            pairs.extend((str(index), value) for index, value in enumerate(parsed_body[:80]))
        else:
            try:
                pairs.extend(parse_qsl(post_data, keep_blank_values=True))
            except Exception:
                pass

    sensitive = re.compile(
        r"token|csrf|xsrf|auth|password|passwd|cookie|session|secret|credential|"
        r"verification|captcha|email|phone|mobile|otp|signature",
        re.I,
    )
    for raw_key, raw_value in pairs[:150]:
        key = str(raw_key or "").strip()[:120]
        if not key or sensitive.search(key):
            continue
        if isinstance(raw_value, (dict, list)):
            try:
                value = json.dumps(raw_value, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                value = str(raw_value)
        else:
            value = str(raw_value if raw_value is not None else "")
        value = unquote_plus(value).strip()[:300]
        if value:
            output[key] = value
    return output


def _matched_source_network_action(request: Any, allowed_actions: Iterable[str]) -> str | None:
    """Match one explicitly allowed MVC action in an XHR URL or request body."""
    try:
        request_text = f"{request.url or ''} {request.post_data or ''}"
    except Exception:
        request_text = ""
    decoded = unquote_plus(request_text).casefold()
    compact = re.sub(r"[^a-z0-9]+", "", decoded)
    tokens = set(re.findall(r"[a-z0-9]+", decoded))
    for action in allowed_actions:
        clean_action = str(action or "").strip()
        token = re.sub(r"[^a-z0-9]+", "", clean_action.casefold())
        if token and (token in tokens or token in compact):
            return clean_action
    return None


def _infer_summary_controller_action(request: Any) -> str | None:
    """Retain named NISE /Summary XHRs even when their action is new to us.

    This is intentionally limited to Fetch/XHR requests whose URL is under the
    NISE Summary controller. It lets diagnostics and the source adapter inspect
    Module's real response names instead of silently dropping unknown actions.
    """
    try:
        request_url = str(request.url or "")
        post_data = str(request.post_data or "")
        path = urlparse(request_url).path
    except Exception:
        return None
    if "/summary/" not in path.casefold():
        return None
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    if leaf and leaf.casefold() not in {"index", "summary", "overview", "getdata", "data"}:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,100}", leaf):
            return leaf
    decoded = unquote_plus(post_data)
    match = re.search(
        r"(?:action|handler|method|function|requesttype|chartaction)\s*[=:]\s*['\"]?([A-Za-z][A-Za-z0-9_]{2,100})",
        decoded,
        re.I,
    )
    if match:
        return match.group(1)
    # Last resort: action identifiers embedded in form data or MVC route tokens.
    match = re.search(r"\b((?:Cell|Module|Pnl|Panel)[A-Za-z0-9_]*(?:Chart|Summary))\b", decoded, re.I)
    return match.group(1) if match else None


def _decode_json_response_body(body: str) -> Any:
    """Decode normal, anti-XSSI-prefixed and JSON-string-wrapped responses."""
    candidate = str(body or "").lstrip("\ufeff\u200b \t\r\n")
    if candidate.startswith(")]}'"):
        newline = candidate.find("\n")
        candidate = candidate[newline + 1:] if newline >= 0 else candidate[4:]
    parsed: Any = None
    for _ in range(3):
        try:
            parsed = json.loads(candidate)
        except Exception:
            return None
        if not isinstance(parsed, str):
            return parsed
        nested = parsed.lstrip("\ufeff\u200b \t\r\n")
        if not nested or nested == candidate or nested[0] not in "{[\"":
            return parsed
        candidate = nested
    return parsed


def _embedded_html_fragments(value: Any, *, depth: int = 0, limit: int = 30) -> list[str]:
    """Find HTML partials nested inside AJAX JSON envelopes such as {d: html}."""
    if depth > 7 or limit <= 0:
        return []
    output: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= 8 and re.search(
            r"<(?:!doctype\s+html|html|head|body|div|section|article|table|script|svg|canvas)\b",
            text[:20000], re.I,
        ):
            return [text[:MAX_HTML_CHARS]]
        if text and text[:1] in "{[\"":
            nested = _decode_json_response_body(text)
            if nested is not None and nested != value:
                return _embedded_html_fragments(nested, depth=depth + 1, limit=limit)
        return []
    if isinstance(value, dict):
        preferred = ("html", "htmlcontent", "content", "partial", "partialview", "view", "result", "data", "d", "response", "body")
        keys = sorted(value.keys(), key=lambda key: (str(key).casefold() not in preferred, str(key)))
        for key in keys:
            output.extend(_embedded_html_fragments(value.get(key), depth=depth + 1, limit=limit - len(output)))
            if len(output) >= limit:
                break
    elif isinstance(value, list):
        for child in value[:100]:
            output.extend(_embedded_html_fragments(child, depth=depth + 1, limit=limit - len(output)))
            if len(output) >= limit:
                break
    deduped: list[str] = []
    seen: set[str] = set()
    for item in output:
        signature = item[:2000]
        if signature not in seen:
            seen.add(signature)
            deduped.append(item)
    return deduped[:limit]


def _network_fragment_artifact(
    html: str,
    *,
    url: str,
    state: str,
    method: str,
    status_code: int | None,
    content_type: str,
    resource_type: str,
    request_headers: dict[str, str] | None = None,
    response_headers: dict[str, str] | None = None,
    request_payload_fields: dict[str, str] | None = None,
    endpoint_action: str | None = None,
    request_id: str | None = None,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Retain one AJAX partial and its executable chart metadata separately.

    The raw fragment remains available for exact replay/inspection, while inline
    scripts, script target IDs and DOM chart targets are indexed independently
    so runtime widgets can be joined back to the endpoint that initialized them.
    """
    soup = BeautifulSoup(str(html or ""), "html.parser")
    scripts: list[dict[str, Any]] = []
    script_target_ids: set[str] = set()
    for index, node in enumerate(soup.find_all("script"), start=1):
        text = node.string or node.get_text("\n", strip=False) or ""
        src = str(node.get("src") or "")
        scripts.append({
            "index": index,
            "id": str(node.get("id") or ""),
            "type": str(node.get("type") or ""),
            "src": src,
            "text": text,
            "chars": len(text),
        })
        for match in re.finditer(
            r"(?:\$|jQuery)\s*\(\s*['\"]#([A-Za-z0-9_:.\-]+)['\"]\s*\)\s*\.dx(?:Pie)?Chart|(?:getElementById|querySelector)\s*\(\s*['\"]#?([A-Za-z0-9_:.\-]+)['\"]",
            text, re.I,
        ):
            target = match.group(1) or match.group(2)
            if target:
                script_target_ids.add(target)
    dom_ids = [str(node.get("id")) for node in soup.find_all(id=True) if str(node.get("id") or "").strip()]
    chart_target_ids = sorted({
        item for item in dom_ids
        if re.search(r"chart|graph|plot|series|manufact|pnl|cell|module", item, re.I)
    } | script_target_ids)
    headings = [
        clean_text(node.get_text(" ", strip=True))[:180]
        for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])[:30]
    ]
    return {
        "url": url,
        "path": urlparse(url).path,
        "state": state,
        "method": method,
        "status_code": status_code,
        "content_type": content_type,
        "resource_type": resource_type,
        "request_headers": request_headers or {},
        "response_headers": response_headers or {},
        "request_payload_fields": request_payload_fields or {},
        "endpoint_action": endpoint_action,
        "request_id": request_id,
        "received_at": received_at,
        "body_chars": len(html),
        "html": str(html),
        "inline_scripts": scripts,
        "dom_ids": dom_ids[:500],
        "chart_target_ids": chart_target_ids[:250],
        "headings": [value for value in headings if value],
        "truncated": False,
    }


def _render_page_sync(
    url: str,
    *,
    timeout_ms: int = 120_000,
    clicks: tuple[str, ...] = (),
    sweep_selects: tuple[str, ...] = (),
    capture_network: bool = True,
    auto_sweep_tabs: bool = True,
    settle_ms: int = 900,
    capture_runtime_charts: bool = False,
    reload_between_clicks: bool = False,
    sweep_selects_after_clicks: bool = False,
    sweep_selects_before_clicks: bool = True,
    apply_source_filters: tuple[dict[str, Any], ...] = (),
    discover_source_filters_after_clicks: bool = False,
    network_action_allowlist: tuple[str, ...] = (),
    network_action_required: tuple[str, ...] = (),
    network_action_timeout_ms: int = 18_000,
    network_idle_timeout_ms: int | None = None,
    capture_runtime_grid_full_load: bool = True,
) -> dict[str, Any]:
    """Render with Playwright Sync API in a worker thread.

    No asyncio.wait_for wraps the worker thread: that pattern cancels the await
    but cannot stop the underlying browser thread, which was the Solar DCR bug.
    Navigation and click operations each have their own explicit timeout.
    """
    # Backward-compatible alias used by older scraper callers. The current
    # collector uses this budget for waiting on targeted network actions.
    if network_idle_timeout_ms is not None:
        network_action_timeout_ms = max(1_000, int(network_idle_timeout_ms))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise SourceRequiresConfiguration(
            "Playwright is required for JavaScript-rendered Market Intelligence pages. "
            "Install the package and browser binary specified by requirements.txt."
        ) from exc

    responses: list[dict[str, Any]] = []
    network_failures: list[dict[str, Any]] = []
    response_chars = 0
    network_action_allowlist = tuple(
        str(item).strip() for item in network_action_allowlist if str(item).strip()
    )
    network_action_required = tuple(
        str(item).strip() for item in network_action_required if str(item).strip()
    ) or network_action_allowlist
    # The allowlist controls which requests are retained. The required set is
    # intentionally narrower so optional Summary and sold-chart actions do
    # not hold a manufacturing capture open or create false timeouts.
    # Attach every response to the view that initiated its request. Using the
    # mutable active_capture_state inside on_response is racy: a delayed Cell
    # XHR can finish after the collector reloads Summary for the Module click.
    request_capture_states: dict[Any, str] = {}

    def request_state_key(request: Any) -> Any:
        # Use Playwright's underlying request identity where available. It is
        # stable across the request/response wrappers; id(wrapper) can differ
        # if Playwright materializes a fresh Python facade for response.request.
        implementation = getattr(request, "_impl_obj", None)
        stable_guid = getattr(implementation, "_guid", None)
        if stable_guid:
            return str(stable_guid)
        return id(implementation if implementation is not None else request)

    browser_errors: list[str] = []
    click_audit: list[dict[str, Any]] = []
    interactive_filters_by_state: dict[str, list[dict[str, Any]]] = {}
    source_filter_audit: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    # Attach XHR/fetch payloads to the active public dashboard filter. Without
    # this, responses collected after a user clicks "Capacity" or "Subsidy"
    # are indistinguishable and can be mislabeled as the default measure.
    active_capture_state = "initial"
    settle_ms = min(max(int(settle_ms), 250), 5000)
    render_started = time.monotonic()
    # A source with many dropdowns can otherwise spend several minutes in
    # sequential filter sweeps. Keep a hard, visible budget for diagnostics.
    sweep_budget_seconds = min(150.0, max(30.0, timeout_ms / 1000.0 - 30.0))
    sweep_deadline = render_started + sweep_budget_seconds
    max_total_sweep_options = 85
    total_sweep_options = 0
    logger.info("SOURCE_BROWSER_START url=%s timeout_ms=%d sweep_budget_seconds=%.1f sweep_selects=%s", url, timeout_ms, sweep_budget_seconds, list(sweep_selects))
    try:
        import os
        executable = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH") or os.environ.get("PLAYWRIGHT_BROWSER_EXECUTABLE")
    except Exception:
        executable = None

    with sync_playwright() as playwright:
        browser = None
        launch_attempts: list[dict[str, Any]] = []
        if executable:
            launch_attempts.append({"executable_path": executable, "headless": True})
        launch_attempts.extend(({"channel": "chrome", "headless": True}, {"channel": "msedge", "headless": True}, {"headless": True}))
        for kwargs in launch_attempts:
            try:
                browser = playwright.chromium.launch(**kwargs)
                break
            except Exception as exc:
                browser_errors.append(f"{kwargs}: {type(exc).__name__}: {exc}")
        if browser is None:
            raise SourceUnavailable("Unable to start Chrome, Edge or Playwright Chromium: " + " | ".join(browser_errors[-3:]))
        logger.info("SOURCE_BROWSER_LAUNCHED url=%s elapsed_seconds=%.1f", url, time.monotonic() - render_started)

        try:
            context = browser.new_context(
                viewport={"width": 1600, "height": 1400},
                user_agent=USER_AGENT,
                accept_downloads=True,
            )
            page = context.new_page()
            page.set_default_timeout(min(max(timeout_ms, 10_000), 180_000))
            page.set_default_navigation_timeout(min(max(timeout_ms, 10_000), 180_000))

            def on_request(request: Any) -> None:
                try:
                    request_capture_states[request_state_key(request)] = active_capture_state
                    if len(request_capture_states) > MAX_NETWORK_ITEMS * 4:
                        for request_id in list(request_capture_states)[:MAX_NETWORK_ITEMS * 2]:
                            request_capture_states.pop(request_id, None)
                except Exception:
                    return

            def on_request_failed(request: Any) -> None:
                if network_action_allowlist:
                    try:
                        action = _matched_source_network_action(request, network_action_allowlist)
                        if not action and "__NISE_SUMMARY_ACTIONS__" in network_action_allowlist:
                            action = _infer_summary_controller_action(request)
                        if action:
                            path = urlparse(str(request.url or "")).path[:240]
                            failure = str(getattr(request, "failure", None) or "request failed")[:300]
                            network_failures.append({
                                "endpoint_action": action,
                                "path": path,
                                "method": str(request.method or "GET"),
                                "failure": failure,
                                "state": active_capture_state,
                            })
                            logger.warning(
                                "SOURCE_XHR_FAILED action=%s path=%s state=%s failure=%s",
                                action, path, active_capture_state, failure,
                            )
                    except Exception:
                        pass
                request_capture_states.pop(request_state_key(request), None)

            def on_response(response: Any) -> None:
                nonlocal response_chars
                if not capture_network or len(responses) >= MAX_NETWORK_ITEMS:
                    return
                try:
                    request = response.request
                    response_url = str(response.url)
                    try:
                        resource_type = request.resource_type
                    except Exception:
                        resource_type = "unknown"
                    endpoint_action = None
                    if network_action_allowlist:
                        # Solar's NISE connector only retains the named
                        # manufacturing XHRs. This excludes inventory actions,
                        # document assets and unrelated page traffic.
                        if resource_type not in {"xhr", "fetch"}:
                            return
                        endpoint_action = _matched_source_network_action(request, network_action_allowlist)
                        if not endpoint_action and "__NISE_SUMMARY_ACTIONS__" in network_action_allowlist:
                            endpoint_action = _infer_summary_controller_action(request)
                        if not endpoint_action:
                            return
                    content_type = str(response.headers.get("content-type") or "")
                    if not _safe_text_response(response, content_type, response_url):
                        return
                    body = response.text()
                    if not body:
                        return
                    remaining = MAX_NETWORK_TOTAL_CHARS - response_chars
                    limit = min(MAX_NETWORK_BODY_CHARS, max(0, remaining))
                    if limit <= 0:
                        return
                    truncated = len(body) > limit
                    stored_body = body[:limit]
                    try:
                        request_method = request.method
                    except Exception:
                        request_method = "GET"
                    try:
                        status = response.status
                    except Exception:
                        status = None
                    # Keep only non-sensitive transport metadata. Never persist
                    # Cookie, Set-Cookie, Authorization or proxy credentials.
                    safe_response_headers: dict[str, str] = {}
                    safe_request_headers: dict[str, str] = {}
                    try:
                        response_headers = response.all_headers()
                    except Exception:
                        response_headers = {}
                    try:
                        request_headers = request.all_headers()
                    except Exception:
                        request_headers = {}
                    request_payload_fields = _safe_source_request_fields(request)
                    for header_name in ("content-type", "content-length", "cache-control", "etag", "last-modified"):
                        if response_headers.get(header_name) is not None:
                            safe_response_headers[header_name] = str(response_headers[header_name])[:500]
                    for header_name in ("accept", "content-type", "referer", "origin", "x-requested-with"):
                        if request_headers.get(header_name) is not None:
                            safe_request_headers[header_name] = str(request_headers[header_name])[:500]
                    response_state = request_capture_states.pop(request_state_key(request), active_capture_state)
                    responses.append({
                        "url": response_url,
                        "state": response_state,
                        "method": request_method,
                        "content_type": content_type,
                        "resource_type": resource_type,
                        "status_code": status,
                        "response_headers": safe_response_headers,
                        "request_headers": safe_request_headers,
                        "request_payload_fields": request_payload_fields,
                        "endpoint_action": endpoint_action,
                        "request_id": request_state_key(request),
                        "body": stored_body,
                        "body_chars": len(body),
                        "truncated": truncated,
                        "received_at": utc_now().isoformat(),
                    })
                    response_chars += len(stored_body)
                    if endpoint_action:
                        logger.info(
                            "SOURCE_XHR_CAPTURED action=%s state=%s status=%s method=%s path=%s body_chars=%s payload_fields=%s",
                            endpoint_action, response_state, status, request_method,
                            urlparse(response_url).path[:200], len(body),
                            sorted(request_payload_fields.keys())[:30],
                        )
                except Exception:
                    return

            def source_action_matches_expected(actual: str, expected_name: str) -> bool:
                """Match known NISE chart roles when MVC action names vary by deployment."""
                actual_compact = re.sub(r"[^a-z0-9]+", "", str(actual or "").casefold())
                expected_compact = re.sub(r"[^a-z0-9]+", "", str(expected_name or "").casefold())
                if actual_compact == expected_compact:
                    return True
                has_chart = "chart" in actual_compact
                has_module = any(token in actual_compact for token in ("pnl", "module", "panel"))
                has_cell = "cell" in actual_compact
                has_sold = any(token in actual_compact for token in ("inv", "sold", "sales", "dispatch"))
                has_manufacturer = any(token in actual_compact for token in ("mfg", "manufactur", "company", "maker", "producer"))
                if expected_compact == "pnlmfgchart":
                    return has_chart and has_module and has_manufacturer and not has_sold
                if expected_compact == "pnlchart":
                    return has_chart and has_module and not has_manufacturer and not has_sold
                if expected_compact == "cellchart":
                    return has_chart and has_cell and not any(token in actual_compact for token in ("wafer", "inv", "sold", "sales"))
                if expected_compact == "wafertocell":
                    return "wafer" in actual_compact and has_cell or actual_compact.endswith("wafertocell") or actual_compact == "wafertocell"
                return False

            def wait_for_source_actions(
                response_start_index: int,
                *,
                expected: tuple[str, ...] = (),
                timeout_ms: int = 18_000,
                require_all: bool = True,
            ) -> dict[str, Any]:
                """Wait for targeted NISE XHR roles, not every request on the page."""
                expected_names = tuple(expected or network_action_allowlist)
                deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
                observed: set[str] = set()
                matched_by_expected: dict[str, str] = {}
                while time.monotonic() < deadline:
                    observed = {
                        str(item.get("endpoint_action") or "")
                        for item in responses[response_start_index:]
                        if item.get("endpoint_action")
                    }
                    matched_by_expected = {
                        name: next((actual for actual in observed if source_action_matches_expected(actual, name)), "")
                        for name in expected_names
                    }
                    matched = {name for name, actual in matched_by_expected.items() if actual}
                    if (expected_names and (len(matched) == len(expected_names) if require_all else bool(matched))) or (not expected_names and observed):
                        break
                    page.wait_for_timeout(100)
                missing = sorted(name for name, actual in matched_by_expected.items() if not actual)
                result = {
                    "expected": list(expected_names),
                    "observed": sorted(observed),
                    "matched": {name: actual for name, actual in matched_by_expected.items() if actual},
                    "missing": missing,
                    "complete": not missing if expected_names else bool(observed),
                }
                logger.info(
                    "SOURCE_XHR_WAIT_COMPLETE expected=%s observed=%s missing=%s complete=%s elapsed_seconds=%.1f",
                    list(expected_names), result["observed"], missing, result["complete"],
                    time.monotonic() - render_started,
                )
                return result

            page.on("request", on_request)
            page.on("requestfailed", on_request_failed)
            page.on("response", on_response)
            # Do not discard a useful page just because a tracking pixel, bundle,
            # or long-polling request prevents DOMContentLoaded. Waiting for the
            # response commit lets us start extracting the actual content and
            # then wait for the DOM in a separately bounded step.
            logger.info("SOURCE_BROWSER_NAVIGATION_START url=%s", url)
            try:
                response = page.goto(url, wait_until="commit", timeout=min(max(timeout_ms, 10_000), 180_000))
                logger.info("SOURCE_BROWSER_NAVIGATION_COMMIT url=%s status=%s elapsed_seconds=%.1f", url, getattr(response, "status", None), time.monotonic() - render_started)
            except Exception as exc:
                browser_errors.append(f"navigation-commit:{type(exc).__name__}:{exc}")
                logger.warning("SOURCE_BROWSER_NAVIGATION_ERROR url=%s error=%s: %s", url, type(exc).__name__, exc)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=min(20_000, max(4_000, timeout_ms // 4)))
                logger.info("SOURCE_BROWSER_DOM_READY url=%s elapsed_seconds=%.1f", url, time.monotonic() - render_started)
            except Exception as exc:
                browser_errors.append(f"domcontentloaded:{type(exc).__name__}:{exc}")
                logger.warning("SOURCE_BROWSER_DOM_WAIT_TIMEOUT url=%s elapsed_seconds=%.1f error=%s", url, time.monotonic() - render_started, exc)
            try:
                page.wait_for_load_state("networkidle", timeout=min(8_000, max(2_000, timeout_ms // 8)))
                logger.info("SOURCE_BROWSER_NETWORK_IDLE url=%s elapsed_seconds=%.1f", url, time.monotonic() - render_started)
            except Exception as exc:
                logger.info("SOURCE_BROWSER_NETWORK_IDLE_SKIPPED url=%s elapsed_seconds=%.1f reason=%s", url, time.monotonic() - render_started, str(exc)[:180])
            page.wait_for_timeout(settle_ms)
            logger.info("SOURCE_BROWSER_INITIAL_SETTLED url=%s elapsed_seconds=%.1f", url, time.monotonic() - render_started)

            def snapshot(label: str) -> None:
                try:
                    html = page.content()
                    truncated = len(html) > MAX_HTML_CHARS
                    runtime_charts: list[dict[str, Any]] = []
                    runtime_grids: list[dict[str, Any]] = []
                    page_year: int | None = None
                    page_years: list[int] = []
                    # Year1 is the source-of-truth period selector on the NISE
                    # Cell/Module detail partials. Read it even when expensive
                    # runtime chart inspection is disabled; otherwise month-only
                    # xValue labels (Jan..Dec) cannot be assigned to a reporting
                    # year and the normalizer drops valid production points.
                    try:
                        selected_years_from_dom = page.evaluate("""() => {
                          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
                          const selects = Array.from(document.querySelectorAll('select'));
                          const target = selects.find(el => clean(el.id).toLowerCase() === 'year1' ||
                            /year/i.test(`${el.name || ''} ${el.getAttribute('aria-label') || ''} ${el.labels?.[0]?.innerText || ''}`));
                          if (!target) return [];
                          const values = Array.from(target.selectedOptions || []).map(option =>
                            `${option.value || ''} ${option.textContent || ''}`);
                          const years = values.flatMap(value => Array.from(value.matchAll(/\\b(20\\d{2})\\b/g), match => Number(match[1])));
                          return [...new Set(years)].filter(year => year >= 2022 && year <= 2099).sort((a,b) => a-b);
                        }""") or []
                        page_years = [int(value) for value in selected_years_from_dom if str(value).isdigit()]
                        page_year = page_years[0] if len(page_years) == 1 else None
                    except Exception:
                        pass
                    if capture_runtime_charts:
                        runtime_capture: dict[str, Any] = {}
                        try:
                            runtime_capture = page.evaluate("""async (captureRuntimeGridFullLoad) => {
                              // NISE initializes DevExtreme charts asynchronously after its MVC partials load.
                              // Wait briefly for the widget DataSources and SVG point models to settle;
                              // this does not interact with additional page controls.
                              await new Promise(resolve => setTimeout(resolve, 650));
                              const out = [];
                              const chartDiagnostics = [];
                              const gridOut = [];
                              const gridDiagnostics = [];
                              const safe = (value) => {
                                try { return JSON.parse(JSON.stringify(value)); }
                                catch (_) { return null; }
                              };
                              const selectedYear = () => {
                                for (const select of document.querySelectorAll('select')) {
                                  const meta = `${select.id || ''} ${select.name || ''} ${select.getAttribute('aria-label') || ''} ${select.labels?.[0]?.innerText || ''}`;
                                  if (!/year/i.test(meta)) continue;
                                  const option = select.selectedOptions?.[0];
                                  const value = `${option?.textContent || ''} ${select.value || ''}`;
                                  const match = value.match(/\\b(20\\d{2})\\b/);
                                  if (match) return Number(match[1]);
                                }
                                const match = (document.body?.innerText || '').match(/(?:select(?:ed)?\\s+year|year)\\s*[:\\-]?\\s*(20\\d{2})/i);
                                return match ? Number(match[1]) : null;
                              };
                              const periodYear = selectedYear();
                              const readAxisTitle = (axis) => {
                                if (!axis) return '';
                                if (Array.isArray(axis)) axis = axis[0];
                                const title = axis?.title;
                                if (typeof title === 'string') return title;
                                return String(title?.text || title?.textStr || axis?.name || axis?.axisTitle?.textStr || '');
                              };
                              const nearbyMeta = (element) => {
                                if (!element) return { title: '', unit: '', text: '' };
                                let title = '';
                                let text = '';
                                let node = element;
                                for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
                                  const nodeText = String(node.innerText || node.textContent || '').slice(0, 4000);
                                  if (!text && nodeText) text = nodeText;
                                  const heading = node.querySelector?.('h1,h2,h3,h4,h5,h6,.card-title,.panel-title,[data-chart-title],[data-title]');
                                  const headingText = String(heading?.innerText || heading?.textContent || '').trim();
                                  if (headingText && headingText.length < 180) { title = headingText; break; }
                                }
                                const unit = (text.match(/\b(?:MW|GW|kW)\b/gi) || []).join(' ');
                                return { title, unit, text };
                              };
                              const formatCategory = (value) => {
                                if (value instanceof Date && !Number.isNaN(value.getTime())) {
                                  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}`;
                                }
                                if (typeof value === 'number' && value > 1000000000) {
                                  const date = new Date(value > 100000000000 ? value : value * 1000);
                                  if (!Number.isNaN(date.getTime())) return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
                                }
                                return value == null ? '' : String(value);
                              };
                              const normalizeLabelsAndSets = (labels, datasets) => {
                                const rawSets = Array.isArray(datasets) ? datasets.slice(0, 20) : [];
                                let safeLabels = Array.isArray(labels) ? labels.slice(0, 2500).map(formatCategory) : [];
                                const cleanSets = rawSets.map((set, index) => {
                                  const rawData = set?.data || set?.values || set?.yData || [];
                                  const data = Array.isArray(rawData) ? rawData.map(point => {
                                    if (point && typeof point === 'object') return point.y ?? point.value ?? point.v ?? point.originalValue ?? null;
                                    return point;
                                  }) : [];
                                  if (!safeLabels.length && Array.isArray(set?.labels)) {
                                    const supplied = set.labels.slice(0, 2500).map(formatCategory);
                                    if (supplied.some(Boolean)) safeLabels = supplied;
                                  }
                                  if (!safeLabels.length && Array.isArray(rawData)) {
                                    const inferred = rawData.map(point => point && typeof point === 'object'
                                      ? formatCategory(point.argument ?? point.originalArgument ?? point.x ?? point.label ?? point.name ?? point.category ?? point.date ?? point.month ?? '') : '');
                                    if (inferred.some(Boolean)) safeLabels = inferred;
                                  }
                                  return { label: String(set?.label || set?.name || `Series ${index + 1}`), data };
                                });
                                return { labels: safeLabels, datasets: cleanSets };
                              };
                              const add = (library, chart, labels, datasets, title = '', unit = '', element = null) => {
                                const normalized = normalizeLabelsAndSets(labels, datasets);
                                const meta = nearbyMeta(element);
                                const safeLabels = normalized.labels;
                                const cleanSets = normalized.datasets;
                                if (!safeLabels.length || !cleanSets.some(set => Array.isArray(set.data) && set.data.length >= 1)) return;
                                const axisUnit = String(unit || '');
                                const elementId = String(element?.id || '');
                                const elementClass = String(element?.className?.baseVal || element?.className || '');
                                const containerText = String(meta.text || '').slice(0, 1600);
                                out.push({ library, title: String(title || meta.title || chart || ''), labels: safe(safeLabels), datasets: cleanSets,
                                  unit: axisUnit || meta.unit, period_year: periodYear, element_id: elementId,
                                  element_class: elementClass.slice(0, 300), container_heading: meta.title || '',
                                  container_text: containerText });
                              };
                              // Chart.js, including v3/v4 instances exposed through getChart().
                              try {
                                const instances = window.Chart?.instances;
                                const charts = Array.isArray(instances) ? instances : Object.values(instances || {});
                                if (!charts.length && window.Chart?.getChart) {
                                  for (const canvas of document.querySelectorAll('canvas')) {
                                    const instance = window.Chart.getChart(canvas);
                                    if (instance) charts.push(instance);
                                  }
                                }
                                for (const chart of charts.slice(0, 30)) {
                                  const data = chart?.data || {};
                                  const scales = Object.values(chart?.options?.scales || {});
                                  const unit = scales.map(scale => readAxisTitle(scale)).filter(Boolean).join(' ');
                                  const title = chart?.options?.plugins?.title?.text || chart?.canvas?.getAttribute?.('aria-label') || chart?.canvas?.id || '';
                                  add('chartjs', chart?.canvas?.id, data.labels || [], data.datasets || [], title, unit, chart?.canvas);
                                }
                              } catch (_) {}
                              // ECharts.
                              try {
                                if (window.echarts?.getInstanceByDom) {
                                  for (const el of document.querySelectorAll('div,section,article')) {
                                    const chart = window.echarts.getInstanceByDom(el);
                                    if (!chart) continue;
                                    const option = chart.getOption?.() || {};
                                    const axis = Array.isArray(option.xAxis) ? option.xAxis[0] : option.xAxis;
                                    const yAxes = Array.isArray(option.yAxis) ? option.yAxis : [option.yAxis];
                                    let labels = axis?.data || [];
                                    let datasets = (option.series || []).map((series, index) => ({
                                      label: series?.name || `Series ${index + 1}`,
                                      data: (series?.data || []).map(point => point && typeof point === 'object'
                                        ? (Array.isArray(point.value) ? (point.value[1] ?? null) : (point.value ?? point.y ?? point.v ?? null))
                                        : point)
                                    }));
                                    if (!labels.length) {
                                      // Some NISE drilldowns expose ECharts series as
                                      // [{name, value}] without xAxis categories. Keep
                                      // the company/month labels from the points.
                                      const categorySeries = (option.series || []).find(series =>
                                        Array.isArray(series?.data) && series.data.length &&
                                        series.data.some(point => point && typeof point === 'object' &&
                                          (point.name !== undefined || point.label !== undefined || point.x !== undefined || point.category !== undefined))
                                      );
                                      if (categorySeries) {
                                        labels = categorySeries.data.map(point =>
                                          formatCategory(point?.name ?? point?.label ?? point?.x ?? point?.category ?? '')
                                        );
                                      }
                                      if (!labels.length) {
                                        const mapSeries = (option.series || []).find(series => series?.type === 'map' && Array.isArray(series?.data) && series.data.length);
                                        if (mapSeries) labels = mapSeries.data.map(point => point?.name || '');
                                      }
                                      if (!labels.length) {
                                        const valueSeries = (option.series || []).find(series => Array.isArray(series?.data) && series.data.some(point => Array.isArray(point?.value) && point.value.length > 1));
                                        if (valueSeries) labels = valueSeries.data.map(point => formatCategory(point?.value?.[0] ?? ''));
                                      }
                                    }
                                    const unit = yAxes.map(item => readAxisTitle(item)).filter(Boolean).join(' ');
                                    add('echarts', el.id || el.className || 'chart', labels, datasets, option.title?.[0]?.text || option.title?.text || el.getAttribute('aria-label') || '', unit, el);
                                  }
                                }
                              } catch (_) {}
                              // Highcharts.
                              try {
                                for (const chart of (window.Highcharts?.charts || []).filter(Boolean).slice(0, 30)) {
                                  const categories = chart.xAxis?.[0]?.categories || [];
                                  const firstPoints = chart.series?.[0]?.points || [];
                                  const labels = categories.length ? categories : firstPoints.map((point, index) => {
                                    if (point?.category !== undefined) return point.category;
                                    if (point?.name) return point.name;
                                    const x = point?.x ?? chart.series?.[0]?.xData?.[index];
                                    if (typeof x === 'number' && chart.xAxis?.[0]?.dateTime) {
                                      const date = new Date(x);
                                      return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
                                    }
                                    return x ?? index + 1;
                                  });
                                  const datasets = (chart.series || []).map((series, index) => ({
                                    label: series?.name || `Series ${index + 1}`,
                                    data: series?.yData || (series?.points || []).map(point => point?.y)
                                  }));
                                  const unit = (chart.yAxis || []).map(axis => axis?.axisTitle?.textStr || axis?.options?.title?.text || '').filter(Boolean).join(' ');
                                  add('highcharts', chart.renderTo?.id, labels, datasets, chart.title?.textStr || chart.subtitle?.textStr || chart.renderTo?.getAttribute?.('aria-label') || '', unit, chart.renderTo);
                                }
                              } catch (_) {}
                              // ApexCharts (instances are exposed by some versions through Apex._chartInstances).
                              try {
                                const instances = window.Apex?._chartInstances || [];
                                for (const item of instances.slice(0, 30)) {
                                  const chart = item?.chart || item;
                                  const config = chart?.w?.config || chart?.config || {};
                                  const labels = config.xaxis?.categories || [];
                                  const datasets = (config.series || []).map((series, index) => ({ label: series?.name || `Series ${index + 1}`, data: series?.data || [] }));
                                  const axis = Array.isArray(config.yaxis) ? config.yaxis : [config.yaxis];
                                  const unit = axis.map(readAxisTitle).filter(Boolean).join(' ');
                                  add('apexcharts', chart?.el?.id || '', labels, datasets, config.title?.text || '', unit, chart?.el);
                                }
                              } catch (_) {}
                              // CanvasJS.
                              try {
                                for (const chart of (window.CanvasJS?.charts || []).slice(0, 30)) {
                                  const options = chart?.options || {};
                                  const datasets = (options.data || []).map((series, index) => ({
                                    label: series?.name || `Series ${index + 1}`,
                                    data: (series?.dataPoints || []).map(point => ({ x: point?.x instanceof Date
                                      ? `${point.x.getFullYear()}-${String(point.x.getMonth() + 1).padStart(2, '0')}`
                                      : (point?.x ?? point?.label ?? point?.name ?? ''), y: point?.y ?? point?.value ?? null }))
                                  }));
                                  const labels = datasets[0]?.data?.map(point => point.x) || [];
                                  add('canvasjs', chart?.container?.id || '', labels, datasets, options.title?.text || '', options.axisY?.title || '', chart?.container);
                                }
                              } catch (_) {}
                              // Legacy AmCharts v3.
                              try {
                                for (const chart of (window.AmCharts?.charts || []).slice(0, 30)) {
                                  const provider = chart?.dataProvider || [];
                                  const categoryField = chart?.categoryField || 'category';
                                  const graphs = chart?.graphs || [];
                                  const labels = provider.map(row => row?.[categoryField] ?? '');
                                  const datasets = graphs.map((graph, index) => ({
                                    label: graph?.title || graph?.valueField || `Series ${index + 1}`,
                                    data: provider.map(row => row?.[graph?.valueField])
                                  }));
                                  const unit = graphs.map(graph => graph?.balloonText || graph?.labelText || '').join(' ');
                                  add('amcharts', chart?.div?.id || '', labels, datasets, chart?.title || '', unit, chart?.div);
                                }
                              } catch (_) {}
                              // Kendo UI charts.
                              try {
                                if (window.jQuery) {
                                  for (const el of document.querySelectorAll('[data-role="chart"]')) {
                                    const chart = window.jQuery(el).data('kendoChart');
                                    if (!chart) continue;
                                    const options = chart.options || {};
                                    const firstSeries = options.series?.[0] || {};
                                    const view = chart.dataSource?.view?.() || [];
                                    const categories = options.categoryAxis?.categories || view.map(row => row?.[options.categoryField || 'category'] ?? '');
                                    const datasets = (options.series || []).map((series, index) => ({
                                      label: series?.name || `Series ${index + 1}`,
                                      data: series?.data || view.map(row => row?.[series.field])
                                    }));
                                    add('kendo', el.id || '', categories, datasets, options.title?.text || '', options.valueAxis?.title?.text || '', el);
                                  }
                                }
                              } catch (_) {}
                              // DevExtreme / DevExpress charts. NISE returns MVC partials
                              // and initializes dxChart after the AJAX response. Read the live
                              // widget instance, loaded DataSource rows, runtime series points,
                              // and instance internals (configuration objects alone contain no
                              // rendered values for many MVC-generated charts).
                              try {
                                const dxValue = (value) => {
                                  if (typeof value === 'string' || typeof value === 'number') return String(value);
                                  if (value && typeof value === 'object') return value.text || value.title || value.name || '';
                                  return '';
                                };
                                const callValue = (object, method) => {
                                  try {
                                    const fn = object?.[method];
                                    return typeof fn === 'function' ? fn.call(object) : null;
                                  } catch (_) { return null; }
                                };
                                const asRows = (value) => {
                                  if (Array.isArray(value)) return value.filter(row => row && typeof row === 'object').slice(0, 5000);
                                  if (value && typeof value === 'object') {
                                    for (const key of ['items', '_items', '_array', 'data', 'rows', 'array', 'result']) {
                                      const child = value[key];
                                      if (Array.isArray(child)) {
                                        const rows = child.filter(row => row && typeof row === 'object').slice(0, 5000);
                                        if (rows.length) return rows;
                                      }
                                    }
                                  }
                                  return [];
                                };
                                const dxRows = (instance, options, seriesOptions) => {
                                  const candidates = [];
                                  try { candidates.push(instance?.getDataSource?.()); } catch (_) {}
                                  try { candidates.push(instance?._dataSource); } catch (_) {}
                                  try { candidates.push(options?.dataSource); } catch (_) {}
                                  try { candidates.push(seriesOptions?.dataSource); } catch (_) {}
                                  for (const source of candidates) {
                                    if (!source) continue;
                                    const rowsDirect = asRows(source);
                                    if (rowsDirect.length) return rowsDirect;
                                    try {
                                      const rows = asRows(typeof source.items === 'function' ? source.items() : source.items);
                                      if (rows.length) return rows;
                                    } catch (_) {}
                                    try {
                                      const store = typeof source.store === 'function' ? source.store() : source.store;
                                      const rows = asRows(store);
                                      if (rows.length) return rows;
                                      const storeItems = asRows(store?._array || store?._items || store?.array || store?.items);
                                      if (storeItems.length) return storeItems;
                                    } catch (_) {}
                                    for (const candidate of [source?._items, source?._array, source?._store?._array, source?._store?._items]) {
                                      const rows = asRows(candidate);
                                      if (rows.length) return rows;
                                    }
                                  }
                                  return [];
                                };
                                const getDxInstance = (el, widgetName) => {
                                  try {
                                    const widget = window.DevExpress?.viz?.[widgetName]
                                      || window.DevExpress?.ui?.[widgetName];
                                    const instance = widget?.getInstance?.(el);
                                    if (instance) return instance;
                                  } catch (_) {}
                                  try {
                                    const wrapped = window.jQuery?.(el);
                                    const instance = wrapped?.[widgetName]?.('instance');
                                    if (instance) return instance;
                                  } catch (_) {}
                                  try {
                                    const data = window.jQuery?.(el)?.data?.() || {};
                                    for (const [key, value] of Object.entries(data)) {
                                      if (!value || typeof value !== 'object') continue;
                                      // A DataGrid also exposes getDataSource(); only a chart
                                      // instance with the chart-series API belongs here.
                                      if (widgetName === 'dxChart' || widgetName === 'dxPieChart') {
                                        if (typeof value.getAllSeries === 'function') return value;
                                      }
                                      if (key.toLowerCase().includes(widgetName.toLowerCase())) return value;
                                    }
                                  } catch (_) {}
                                  try {
                                    const data = window.jQuery?.data?.(el) || {};
                                    for (const value of Object.values(data)) {
                                      if (value && typeof value === 'object' && typeof value.getAllSeries === 'function') return value;
                                    }
                                  } catch (_) {}
                                  return null;
                                };
                                const pointArgument = (point, row, argumentField) => {
                                  let value = point?.argument ?? point?.originalArgument
                                    ?? callValue(point, 'getArgument') ?? point?.argumentText
                                    ?? point?.category ?? point?.label ?? point?.name ?? point?.x;
                                  if (value == null && row && argumentField) value = row[argumentField];
                                  if (value == null && row) value = row.month ?? row.period ?? row.date ?? row.manufacturer ?? row.company ?? row.name;
                                  return formatCategory(value);
                                };
                                const pointValue = (point, row, valueField) => {
                                  let value = point?.value ?? point?.originalValue
                                    ?? point?.initialValue ?? point?.y
                                    ?? callValue(point, 'getValue');
                                  if (value == null && row && valueField) value = row[valueField];
                                  if (value == null && point?.data && valueField) value = point.data[valueField];
                                  if (value && typeof value === 'object') value = value.value ?? value.y ?? value[1] ?? null;
                                  if (typeof value === 'string') {
                                    const numeric = Number(value.replace(/,/g, '').replace(/\\s/g, ''));
                                    return Number.isFinite(numeric) ? numeric : value;
                                  }
                                  return value;
                                };
                                const runtimePoints = (series) => {
                                  for (const method of ['getAllPoints', 'getVisiblePoints', 'getPoints']) {
                                    const points = callValue(series, method);
                                    if (Array.isArray(points) && points.length) return points;
                                  }
                                  for (const prop of ['_points', '_visiblePoints', '_allPoints', 'points', '_dataPoints']) {
                                    const points = series?.[prop];
                                    if (Array.isArray(points) && points.length) return points;
                                  }
                                  return [];
                                };
                                const rowField = (keys, pattern, exclude) => keys.find(key =>
                                  new RegExp(pattern, 'i').test(String(key).replace(/[^a-z0-9]/gi, '')) &&
                                  !(exclude && new RegExp(exclude, 'i').test(String(key).replace(/[^a-z0-9]/gi, '')))
                                );
                                const candidates = new Set();
                                for (const el of document.querySelectorAll('[id], [class], [data-options]')) {
                                  const meta = `${el.id || ''} ${String(el.className?.baseVal || el.className || '')} ${el.getAttribute?.('data-options') || ''}`;
                                  if (/chart|plot|graph|dx-widget|dx-visual/i.test(meta)) candidates.add(el);
                                }
                                // Some DevExtreme MVC wrappers do not put chart in the root
                                // element's class/id, but the jQuery data store still owns it.
                                if (window.jQuery) {
                                  for (const el of document.querySelectorAll('body *')) {
                                    if (candidates.size >= 1600) break;
                                    const data = window.jQuery(el).data?.() || {};
                                    if (Object.values(data).some(value => value && typeof value === 'object' &&
                                        (typeof value.getAllSeries === 'function' || typeof value.getDataSource === 'function'))) candidates.add(el);
                                  }
                                }
                                const seenInstances = new Set();
                                for (const el of candidates) {
                                  for (const widgetName of ['dxChart', 'dxPieChart']) {
                                    const instance = getDxInstance(el, widgetName);
                                    if (!instance || seenInstances.has(instance)) continue;
                                    seenInstances.add(instance);
                                    let options = {};
                                    try { options = instance.option?.() || instance._options || {}; } catch (_) {}
                                    if (!options || typeof options !== 'object') options = {};
                                    let configuredSeries = Array.isArray(options.series) ? options.series : [];
                                    if (!configuredSeries.length && options.series && typeof options.series === 'object') configuredSeries = [options.series];
                                    let runtimeSeries = [];
                                    try { runtimeSeries = instance.getAllSeries?.() || []; } catch (_) {}
                                    if (!Array.isArray(runtimeSeries) || !runtimeSeries.length) {
                                      runtimeSeries = Array.isArray(instance._series) ? instance._series : [];
                                    }
                                    const sourceSeries = runtimeSeries.length ? runtimeSeries : configuredSeries;
                                    const commonSettings = options.commonSeriesSettings || options.common_series_settings || {};
                                    const rows = dxRows(instance, options, null);
                                    const rowKeys = rows.length ? Object.keys(rows[0] || {}) : [];
                                    const inferredArgumentField = rowField(rowKeys,
                                      'month|date|period|category|label|argument|manufacturer|company|maker',
                                      'id|code|serial|index');
                                    const inferredValueField = rowField(rowKeys,
                                      'manufactur|production|produced|output|capacity|quantity|qty|amount|total|value|mw|power',
                                      'id|code|serial|index|year|month|date|period|unit|uom|name|company|manufacturer|state');
                                    const datasets = sourceSeries.slice(0, 24).map((series, index) => {
                                      const config = configuredSeries[index] || {};
                                      const seriesName = callValue(series, 'name') || series?.name || config?.name || config?.valueField || `Series ${index + 1}`;
                                      const argumentField = config.argumentField || series?.argumentField || commonSettings.argumentField || options.argumentField || inferredArgumentField;
                                      const valueField = config.valueField || series?.valueField || config.field || commonSettings.valueField || options.valueField || inferredValueField;
                                      const seriesRows = dxRows(instance, options, config);
                                      const useRows = seriesRows.length ? seriesRows : rows;
                                      const points = runtimePoints(series);
                                      let data = [];
                                      let labels = [];
                                      if (points.length) {
                                        data = points.map((point, pointIndex) => {
                                          const row = useRows[pointIndex] || point?.data || null;
                                          return pointValue(point, row, valueField);
                                        });
                                        labels = points.map((point, pointIndex) => pointArgument(point, useRows[pointIndex] || point?.data, argumentField));
                                      }
                                      if (!data.some(value => value !== undefined && value !== null) && useRows.length && valueField) {
                                        data = useRows.map(row => row?.[valueField] ?? null);
                                        labels = useRows.map(row => row?.[argumentField] ?? row?.month ?? row?.period ?? row?.date ?? row?.manufacturer ?? row?.company ?? row?.name ?? '');
                                      }
                                      if (!data.length && Array.isArray(config.data)) {
                                        data = config.data.map(point => pointValue(point, null, valueField));
                                        labels = config.data.map(point => pointArgument(point, null, argumentField));
                                      }
                                      return {
                                        label: dxValue(seriesName), data, labels,
                                        argument_field: argumentField || '', value_field: valueField || '',
                                        point_count: data.filter(value => value !== null && value !== undefined && Number.isFinite(Number(String(value).replace(/,/g, '')))).length,
                                        data_source: points.length ? 'live_series_points' : (useRows.length ? 'loaded_data_source' : 'widget_config'),
                                      };
                                    });
                                    let labels = datasets.find(item => item.labels?.some(Boolean))?.labels || [];
                                    if (!labels.length && rows.length && inferredArgumentField) labels = rows.map(row => row?.[inferredArgumentField] ?? '');
                                    if (!labels.length) {
                                      const axis = options.argumentAxis || options.argument_axis || {};
                                      const axisOptions = Array.isArray(axis) ? axis[0] : axis;
                                      const categories = axisOptions?.categories || options.categories || [];
                                      if (Array.isArray(categories)) labels = categories.map(formatCategory);
                                    }
                                    const yearKey = rowKeys.find(key => /^(year|fy)$/i.test(String(key).replace(/[^a-z]/gi, '')));
                                    if (labels.length) {
                                      labels = labels.map((label, index) => {
                                        const rowYear = yearKey ? rows[index]?.[yearKey] : null;
                                        const year = rowYear || periodYear;
                                        return year && !/\\b20\\d{2}\\b/.test(String(label)) ? `${label} ${year}` : formatCategory(label);
                                      });
                                    }
                                    const cleanDatasets = datasets.map(item => ({ ...item, data: item.data, labels: item.labels }));
                                    const axis = options.valueAxis || options.valueAxes || {};
                                    const axisList = Array.isArray(axis) ? axis : [axis];
                                    const axisUnit = axisList.map(item => dxValue(item?.title) || dxValue(item?.name)).filter(Boolean).join(' ');
                                    const title = dxValue(options.title) || el.getAttribute('aria-label') || el.id || '';
                                    const meta = nearbyMeta(el);
                                    const numericPoints = cleanDatasets.reduce((total, item) => total + item.point_count, 0);
                                    const diagnostic = {
                                      library: 'devextreme', element_id: String(el.id || ''),
                                      element_class: String(el.className?.baseVal || el.className || '').slice(0, 180),
                                      title: String(title || meta.title || '').slice(0, 180),
                                      container_heading: meta.title || '',
                                      container_text: String(meta.text || '').slice(0, 900),
                                      unit: axisUnit || meta.unit || '', labels_count: labels.length,
                                      labels_preview: labels.slice(0, 16), series_count: cleanDatasets.length,
                                      numeric_point_count: numericPoints, data_source_row_count: rows.length,
                                      runtime_series_count: runtimeSeries.length,
                                      series_fields: cleanDatasets.map(item => ({
                                        name: item.label, argument_field: item.argument_field,
                                        value_field: item.value_field, point_count: item.point_count,
                                        data_source: item.data_source,
                                      })),
                                      raw_data_source: safe(rows.slice(0, 80)),
                                    };
                                    chartDiagnostics.push(diagnostic);
                                    // Keep the raw runtime chart even when it is not yet usable;
                                    // diagnostics reveal whether the source rendered rows/points.
                                    if (labels.length && numericPoints > 0) {
                                      const previousCount = out.length;
                                      add('devextreme', el.id || widgetName, labels, cleanDatasets, title || meta.title, axisUnit || meta.unit, el);
                                      if (out.length > previousCount) {
                                        const captured = out[out.length - 1];
                                        captured.data_source_row_count = rows.length;
                                        captured.runtime_series_count = runtimeSeries.length;
                                        captured.series_fields = diagnostic.series_fields;
                                      }
                                    }
                                  }
                                }
                              } catch (dxError) {
                                chartDiagnostics.push({ library: 'devextreme', error: String(dxError).slice(0, 300) });
                              }
                              // DevExtreme DataGrid / MVC grids. NISE renders some
                              // manufacturer directories and state summaries as dxDataGrid
                              // widgets rather than native <table> elements. Capture both
                              // DataSource records and the rendered DOM fallback, retaining
                              // pagination/coverage metadata without clicking controls.
                              try {
                                const gridInstanceFor = (el) => {
                                  try {
                                    const instance = window.DevExpress?.ui?.dxDataGrid?.getInstance?.(el);
                                    if (instance) return instance;
                                  } catch (_) {}
                                  try {
                                    const instance = window.jQuery?.(el)?.dxDataGrid?.('instance');
                                    if (instance) return instance;
                                  } catch (_) {}
                                  try {
                                    const data = window.jQuery?.(el)?.data?.() || window.jQuery?.data?.(el) || {};
                                    for (const [key, value] of Object.entries(data)) {
                                      if (!value || typeof value !== 'object') continue;
                                      if (key.toLowerCase().includes('dxdatagrid') ||
                                          (typeof value.getVisibleRows === 'function' && typeof value.getDataSource === 'function')) return value;
                                    }
                                  } catch (_) {}
                                  return null;
                                };
                                const domGridRows = (el) => {
                                  const headerNodes = Array.from(el.querySelectorAll(
                                    '.dx-header-row [role="columnheader"], .dx-header-row td, [role="columnheader"]'
                                  ));
                                  const headers = headerNodes.map((cell, index) => {
                                    const text = String(cell.innerText || cell.textContent || '').trim();
                                    const field = String(cell.getAttribute('data-field') || cell.getAttribute('aria-label') || text || `Column ${index + 1}`).trim();
                                    return { field, title: text || field, data_type: '' };
                                  }).filter(column => column.field || column.title);
                                  const rowNodes = Array.from(el.querySelectorAll(
                                    '.dx-datagrid-rowsview .dx-data-row, .dx-datagrid-content .dx-data-row, [role="row"].dx-data-row'
                                  ));
                                  const rows = rowNodes.slice(0, 5000).map(row => {
                                    const cells = Array.from(row.querySelectorAll('td,[role="gridcell"]'));
                                    const result = {};
                                    cells.forEach((cell, index) => {
                                      const column = headers[index];
                                      if (!column) return;
                                      const value = String(cell.innerText || cell.textContent || '').trim();
                                      if (value) result[column.field] = value;
                                    });
                                    return result;
                                  }).filter(row => Object.keys(row).length);
                                  return { columns: headers, rows };
                                };
                                const gridElements = new Set(document.querySelectorAll(
                                  '.dx-datagrid, [class*="dx-datagrid"], [data-role="grid"]'
                                ));
                                // Some DevExtreme MVC wrappers keep the widget instance only
                                // in jQuery data and omit the normal root class.
                                if (window.jQuery) {
                                  for (const el of document.querySelectorAll('[id], [class]')) {
                                    if (gridElements.size >= 1000) break;
                                    try {
                                      const data = window.jQuery(el).data?.() || {};
                                      if (Object.entries(data).some(([key, value]) =>
                                        key.toLowerCase().includes('dxdatagrid') ||
                                        (value && typeof value.getVisibleRows === 'function' && typeof value.getDataSource === 'function')
                                      )) gridElements.add(el);
                                    } catch (_) {}
                                  }
                                }
                                const seenGrids = new Set();
                                for (const el of gridElements) {
                                  const instance = gridInstanceFor(el);
                                  if (instance && seenGrids.has(instance)) continue;
                                  if (instance) seenGrids.add(instance);
                                  const ds = instance ? (callValue(instance, 'getDataSource') || instance._dataSource || null) : null;
                                  let rows = [];
                                  try { rows = asRows(callValue(ds, 'items')); } catch (_) {}
                                  if (!rows.length) {
                                    try {
                                      const store = typeof ds?.store === 'function' ? ds.store() : ds?.store;
                                      rows = asRows(store?._array || store?._items || store?.array || store?.items);
                                    } catch (_) {}
                                  }
                                  if (!rows.length && instance) {
                                    try {
                                      const visible = callValue(instance, 'getVisibleRows') || [];
                                      rows = visible.filter(row => row && (!row.rowType || row.rowType === 'data') && row.data)
                                        .map(row => row.data).slice(0, 5000);
                                    } catch (_) {}
                                  }
                                  let totalCount = null;
                                  let pageIndex = null;
                                  let pageSize = null;
                                  const numericMeta = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
                                  try { totalCount = numericMeta(callValue(ds, 'totalCount')); } catch (_) {}
                                  try { pageIndex = numericMeta(callValue(ds, 'pageIndex')); } catch (_) {}
                                  try { pageSize = numericMeta(callValue(ds, 'pageSize')); } catch (_) {}
                                  let fullPageLoadAttempted = false;
                                  let fullPageLoadError = '';
                                  // Ask the grid's own Store for the complete result set when
                                  // the source reports a manageable total. This reuses the
                                  // existing data endpoint, filter and sort; it does not
                                  // interact with the page's visible filter controls.
                                  if (captureRuntimeGridFullLoad && ds && totalCount !== null && totalCount > rows.length && totalCount <= 500) {
                                    try {
                                      const store = typeof ds.store === 'function' ? ds.store() : ds.store;
                                      if (store && typeof store.load === 'function') {
                                        fullPageLoadAttempted = true;
                                        const loadOptions = { skip: 0, take: totalCount, requireTotalCount: true };
                                        const currentFilter = callValue(ds, 'filter');
                                        const currentSort = callValue(ds, 'sort');
                                        if (currentFilter) loadOptions.filter = currentFilter;
                                        if (currentSort) loadOptions.sort = currentSort;
                                        const loadPromise = Promise.resolve(store.load(loadOptions));
                                        let timeoutHandle = null;
                                        const timeoutPromise = new Promise((_, reject) => {
                                          timeoutHandle = setTimeout(() => reject(new Error('grid full-load timed out after 12s')), 12000);
                                        });
                                        let result;
                                        try { result = await Promise.race([loadPromise, timeoutPromise]); }
                                        finally { if (timeoutHandle !== null) clearTimeout(timeoutHandle); }
                                        const allRows = asRows(result);
                                        if (allRows.length > rows.length) rows = allRows;
                                        const resultTotal = numericMeta(result?.totalCount);
                                        if (resultTotal !== null) totalCount = Math.max(totalCount, resultTotal);
                                      }
                                    } catch (loadError) { fullPageLoadError = String(loadError).slice(0, 240); }
                                  }
                                  let columns = [];
                                  if (instance) {
                                    try {
                                      const visibleColumns = callValue(instance, 'getVisibleColumns') || [];
                                      columns = visibleColumns.map(column => ({
                                        field: String(column?.dataField || column?.name || ''),
                                        title: String(column?.caption || column?.dataField || column?.name || ''),
                                        data_type: String(column?.dataType || ''),
                                      })).filter(column => column.field || column.title);
                                    } catch (_) {}
                                    if (!columns.length) {
                                      try {
                                        const configured = instance.option?.('columns') || [];
                                        columns = configured.map(column => ({
                                          field: String(column?.dataField || column?.name || ''),
                                          title: String(column?.caption || column?.dataField || column?.name || ''),
                                          data_type: String(column?.dataType || ''),
                                        })).filter(column => column.field || column.title);
                                      } catch (_) {}
                                    }
                                  }
                                  let domFallbackUsed = false;
                                  if (!columns.length || !rows.length) {
                                    const dom = domGridRows(el);
                                    if (!columns.length && dom.columns.length) columns = dom.columns;
                                    if (!rows.length && dom.rows.length) { rows = dom.rows; domFallbackUsed = true; }
                                  }
                                  // DevExtreme may not expose totalCount for a CustomStore.
                                  // Read the pager text as a conservative count fallback.
                                  if (totalCount === null) {
                                    const pagerText = String(el.querySelector('.dx-info,.dx-datagrid-pager')?.innerText || '');
                                    const pagerMatch = pagerText.match(/\\bof\\s+([\\d,]+)\\s+(?:items?|records?)\\b/i)
                                      || pagerText.match(/\\bof\\s+([\\d,]+)\\b/i);
                                    if (pagerMatch) totalCount = numericMeta(pagerMatch[1].replace(/,/g, ''));
                                  }
                                  if (pageSize === null) {
                                    const sizeText = String(el.querySelector('.dx-page-size.dx-selection,.dx-page-sizes .dx-selection')?.innerText || '');
                                    pageSize = numericMeta(sizeText);
                                  }
                                  const firstRow = rows[0] || {};
                                  if (!columns.length) columns = Object.keys(firstRow).map(key => ({ field: key, title: key, data_type: '' }));
                                  const meta = nearbyMeta(el);
                                  const title = String(meta.title || el.getAttribute('aria-label') || el.id || 'Manufacturer data');
                                  const fieldNames = columns.map(column => column.field || column.title);
                                  if (!rows.length || !columns.length) {
                                    gridDiagnostics.push({
                                      library: 'devextreme', element_id: String(el.id || ''), title: title.slice(0, 180),
                                      row_count: 0, total_count: totalCount, page_index: pageIndex, page_size: pageSize,
                                      column_count: columns.length, columns: fieldNames.slice(0, 80),
                                      full_page_load_attempted: fullPageLoadAttempted,
                                      full_page_load_error: fullPageLoadError || null,
                                      dom_fallback_used: domFallbackUsed, error: 'No accessible grid rows or columns',
                                    });
                                    continue;
                                  }
                                  const isPartial = totalCount !== null ? totalCount > rows.length : false;
                                  gridDiagnostics.push({
                                    library: instance ? 'devextreme' : 'devextreme_dom', element_id: String(el.id || ''), title: title.slice(0, 180),
                                    row_count: rows.length, total_count: totalCount, page_index: pageIndex,
                                    page_size: pageSize, column_count: columns.length,
                                    columns: fieldNames.slice(0, 80), full_page_load_attempted: fullPageLoadAttempted,
                                    full_page_load_error: fullPageLoadError || null, dom_fallback_used: domFallbackUsed,
                                    is_partial: isPartial,
                                  });
                                  gridOut.push({
                                    library: instance ? 'devextreme' : 'devextreme_dom', element_id: String(el.id || ''),
                                    title: title.slice(0, 180), container_heading: String(meta.title || '').slice(0, 180),
                                    state_text: String(meta.text || '').slice(0, 1200),
                                    columns: columns.slice(0, 100), rows: safe(rows.slice(0, 5000)),
                                    loaded_row_count: rows.length, total_count: totalCount,
                                    page_index: pageIndex, page_size: pageSize,
                                    full_page_load_attempted: fullPageLoadAttempted,
                                    full_page_load_error: fullPageLoadError || null,
                                    dom_fallback_used: domFallbackUsed, is_partial: isPartial,
                                  });
                                }
                              } catch (gridError) {
                                gridDiagnostics.push({ library: 'devextreme', error: String(gridError).slice(0, 300) });
                              }
                              // Plotly.
                              try {
                                for (const el of document.querySelectorAll('.js-plotly-plot')) {
                                  const traces = el.data || [];
                                  const first = traces[0] || {};
                                  const labels = first.x || el.layout?.xaxis?.categoryarray || [];
                                  const datasets = traces.slice(0, 20).map((trace, index) => ({ label: trace?.name || `Series ${index + 1}`, data: trace?.y || [] }));
                                  const axisTitle = el.layout?.yaxis?.title?.text || el.layout?.yaxis?.title || '';
                                  add('plotly', el.id || 'plotly-chart', labels, datasets, el.layout?.title?.text || el.layout?.title || '', axisTitle, el);
                                }
                              } catch (_) {}
                              return { charts: out.slice(0, 80), diagnostics: chartDiagnostics.slice(0, 120),
                                grids: gridOut.slice(0, 80), grid_diagnostics: gridDiagnostics.slice(0, 120),
                                selected_year: periodYear, selected_years: periodYears, chart_count: out.length,
                                chart_libraries: [...new Set(out.map(item => item.library))], grid_count: gridOut.length };
                            }""", capture_runtime_grid_full_load) or {}
                            if isinstance(runtime_capture, dict):
                                page_year = runtime_capture.get("selected_year")
                                page_years = runtime_capture.get("selected_years") or ([page_year] if page_year else [])
                                runtime_charts = runtime_capture.get("charts") or []
                                runtime_grids = runtime_capture.get("grids") or []
                            else:
                                runtime_charts = []
                        except Exception as runtime_exc:
                            browser_errors.append(f"runtime-chart-snapshot:{label}:{type(runtime_exc).__name__}:{runtime_exc}")
                    snapshots.append({
                        "label": label,
                        "url": page.url,
                        "html": html[:MAX_HTML_CHARS],
                        "html_chars": len(html),
                        "html_truncated": truncated,
                        "runtime_charts": runtime_charts,
                        "runtime_chart_count": len(runtime_charts),
                        "runtime_grids": runtime_grids,
                        "runtime_grid_count": len(runtime_grids),
                        "runtime_grid_diagnostics": (runtime_capture.get("grid_diagnostics") or []) if capture_runtime_charts and isinstance(runtime_capture, dict) else [],
                        "runtime_chart_libraries": (runtime_capture.get("chart_libraries") or []) if capture_runtime_charts and isinstance(runtime_capture, dict) else [],
                        "runtime_chart_diagnostics": (runtime_capture.get("diagnostics") or []) if capture_runtime_charts and isinstance(runtime_capture, dict) else [],
                        "selected_year": page_year,
                        "selected_years": page_years,
                        "network_response_count": len(responses),
                        "captured_at": utc_now().isoformat(),
                    })
                except Exception as exc:
                    browser_errors.append(f"snapshot:{label}:{type(exc).__name__}:{exc}")

            snapshot("initial")
            logger.info("SOURCE_BROWSER_INITIAL_SNAPSHOT url=%s html_chars=%d network_items=%d", url, len(snapshots[-1].get("html", "")) if snapshots else 0, len(responses))

            # Some public dashboards expose important view controls as buttons
            # or Bootstrap tabs but do not give them accessible tab roles. Sweep
            # those visible navigation controls in addition to the source-owned
            # click list; this is how e.g. VAHAN's Registration/Revenue/Permit
            # views and NISE's summary/map views are preserved as separate states.
            tab_labels: list[str] = []
            try:
                for selector in (
                    "[role='tab']:visible", ".nav-tabs a:visible", ".nav-pills a:visible",
                    "[data-toggle='tab']:visible", "[data-bs-toggle='tab']:visible",
                    "button:visible", "[role='button']:visible",
                ):
                    locator = page.locator(selector)
                    for index in range(min(locator.count(), 100)):
                        node = locator.nth(index)
                        try:
                            label = clean_text(node.inner_text(timeout=500) or node.get_attribute("aria-label") or node.get_attribute("title") or "")
                        except Exception:
                            continue
                        if not label or len(label) > 70:
                            continue
                        low = label.casefold()
                        if any(token in low for token in ("apply filter", "reset all", "login", "sign in", "subscribe", "download app", "search maker", "search option")):
                            continue
                        if label not in tab_labels:
                            tab_labels.append(label)
            except Exception as exc:
                browser_errors.append(f"tab-discovery:{type(exc).__name__}:{exc}")

            # Sweep native <select> filters one dimension at a time. This never
            # cross-joins dimensions and never fabricates combinations: every
            # snapshot is the actual response from the site for that selected
            # option. The optional after-click pass is important for dashboards
            # (like NISE) where a Year selector exists only inside a drilldown.
            # Some dashboards expose a source filter only after a KPI drilldown.
            # Capture native <select> controls in that selected view so the
            # frontend can mirror the publisher's actual filter choices.
            select_sweep_audit: list[dict[str, Any]] = []

            def discover_source_selects(state_label: str) -> list[dict[str, Any]]:
                """Discover filter controls in the current NISE source view.

                Keys are scoped to the view so a Summary filter and a detail-view
                filter with the same DOM id cannot overwrite one another.
                """
                try:
                    discovered = page.evaluate("""async (stateLabel) => {
                      const visible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                      };
                      const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                      const scope = `${clean(stateLabel)}::`;
                      const filterWords = /(search|filter|year|month|date|period|company|manufacturer|maker|state|district|region|location|type|technology|capacity|status|category|product|cell|module)/i;
                      const labelFor = (el, parent) => {
                        const labels = Array.from(el.labels || []).map(x => clean(x.innerText)).filter(Boolean);
                        return labels[0] || clean(el.getAttribute('aria-label')) || clean(el.getAttribute('title')) ||
                          clean(el.getAttribute('placeholder')) || clean(el.name) || clean(el.id) ||
                          (parent ? clean(parent.innerText).split('\\n').filter(Boolean)[0] : '');
                      };
                      const parentFor = (el) => el.closest('.form-group,.filter-group,.input-group,.row,.col,.card,.panel,.dx-field,.dx-field-item') || el.parentElement;
                      const identity = (type, id, name, value, label, index) => {
                        const labelKey = clean(label).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 48) || 'filter';
                        const raw = id ? `id:${id}` : (name ? `name:${name}${value != null ? `:value:${value}` : ''}` : `index:${index}`);
                        return `${scope}${type}|label:${labelKey}|${raw}`;
                      };
                      const controls = [];
                      const selects = Array.from(document.querySelectorAll('select'));
                      selects.forEach((el, index) => {
                        const parent = parentFor(el);
                        // Include hidden native selects when their visible custom
                        // widget wrapper is present (e.g. Select2/DevExtreme).
                        if (!visible(el) && !(parent && visible(parent))) return;
                        const id = clean(el.id), name = clean(el.name);
                        const label = labelFor(el, parent);
                        const context = `${label} ${name} ${id} ${parent?.innerText || ''}`;
                        if (!label || !filterWords.test(context)) return;
                        const options = Array.from(el.options || []).map(option => ({
                          value: String(option.value ?? '') === '' ? '__NISE_EMPTY__' : String(option.value),
                          label: clean(option.textContent), disabled: Boolean(option.disabled)
                        })).filter(option => option.label && !option.disabled);
                        if (options.length < 2) return;
                        const selectedValues = Array.from(el.selectedOptions || []).map(option => String(option.value ?? '') === '' ? '__NISE_EMPTY__' : String(option.value));
                        const selectedValue = el.multiple ? JSON.stringify(selectedValues) : (selectedValues[0] || '__NISE_EMPTY__');
                        controls.push({ key: identity('select', id, name, null, label, index), label, type: el.multiple ? 'multiselect' : 'select', multiple: Boolean(el.multiple), input_type: null, id, name, index,
                          value: selectedValue, selected_label: clean(Array.from(el.selectedOptions || []).map(option => clean(option.textContent)).join(', ')), options: options.slice(0, 150) });
                      });
                      const inputs = Array.from(document.querySelectorAll('input'));
                      inputs.forEach((el, index) => {
                        const inputType = String(el.type || 'text').toLowerCase();
                        if (!visible(el) || el.disabled) return;
                        const parent = parentFor(el);
                        const id = clean(el.id), name = clean(el.name);
                        const label = labelFor(el, parent);
                        const context = `${label} ${name} ${id} ${el.placeholder || ''} ${parent?.innerText || ''}`;
                        if (!filterWords.test(context)) return;
                        if (['text','search','date','month','number'].includes(inputType)) {
                          if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox') return;
                          controls.push({ key: identity('input', id, name, null, label, index), label: label || 'Source filter', type: 'input', input_type: inputType, id, name, index,
                            value: String(el.value ?? ''), selected_label: '', options: [] });
                        } else if (inputType === 'checkbox') {
                          const optionLabel = Array.from(el.labels || []).map(x => clean(x.innerText)).filter(Boolean)[0] || label;
                          controls.push({ key: identity('checkbox', id, name, String(el.value ?? ''), optionLabel, index), label: optionLabel || 'Source filter', type: 'checkbox', input_type: 'checkbox', id, name, index,
                            value: String(el.value ?? 'on'), checked: Boolean(el.checked), selected_label: '', options: [] });
                        }
                      });
                      // Present radio inputs as a single source filter with the
                      // same options the publisher displays, rather than a list
                      // of unrelated radio controls.
                      const radioGroups = new Map();
                      Array.from(document.querySelectorAll('input[type="radio"]')).forEach((el, index) => {
                        if (!visible(el) || el.disabled) return;
                        const parent = parentFor(el);
                        const name = clean(el.name);
                        const fieldset = el.closest('fieldset');
                        const groupLabel = clean(fieldset?.querySelector('legend')?.innerText) ||
                          clean(parent?.querySelector('label,.control-label,.field-label')?.innerText) ||
                          clean(name || parent?.innerText?.split('\\n')?.[0]);
                        const context = `${groupLabel} ${name} ${parent?.innerText || ''}`;
                        if (!filterWords.test(context)) return;
                        const groupKey = name || `radio-${groupLabel.toLowerCase()}`;
                        if (!radioGroups.has(groupKey)) radioGroups.set(groupKey, { key: identity('radio', '', name, null, groupLabel, index), label: groupLabel || 'Source filter', type: 'radio', input_type: 'radio', name, index, value: '', selected_label: '', options: [] });
                        const group = radioGroups.get(groupKey);
                        const optionLabel = Array.from(el.labels || []).map(x => clean(x.innerText)).filter(Boolean)[0] || clean(el.value);
                        const optionValue = String(el.value ?? optionLabel);
                        group.options.push({ value: optionValue, label: optionLabel || optionValue });
                        if (el.checked) { group.value = optionValue; group.selected_label = optionLabel; }
                      });
                      controls.push(...radioGroups.values());
                      // Support custom select widgets (notably DevExtreme
                      // dxSelectBox and ARIA listboxes) which have no native
                      // <select>. Open each filter briefly to read the source's
                      // own options, then close it without choosing anything.
                      const customCombos = Array.from(document.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"]'));
                      for (let index = 0; index < customCombos.length; index += 1) {
                        const el = customCombos[index];
                        if (!visible(el) || el.tagName === 'SELECT') continue;
                        const parent = parentFor(el);
                        const id = clean(el.id), name = clean(el.getAttribute('name'));
                        const label = labelFor(el, parent);
                        const context = `${label} ${name} ${id} ${el.getAttribute('aria-label') || ''} ${parent?.innerText || ''}`;
                        if (!label || !filterWords.test(context)) continue;
                        try {
                          el.click();
                          await new Promise(resolve => setTimeout(resolve, 180));
                        } catch (_) {}
                        const controlsId = clean(el.getAttribute('aria-controls') || el.getAttribute('aria-owns'));
                        const listbox = controlsId ? document.getElementById(controlsId) : null;
                        const optionNodes = listbox
                          ? Array.from(listbox.querySelectorAll('[role="option"],.dx-list-item,.select2-results__option'))
                          : Array.from(document.querySelectorAll('[role="option"]:not([aria-hidden="true"]),.dx-list-item,.select2-results__option'));
                        const options = optionNodes.filter(option => visible(option)).map(option => {
                          const optionLabel = clean(option.innerText || option.textContent || option.getAttribute('aria-label'));
                          const optionValue = clean(option.getAttribute('data-value') || option.getAttribute('data-key') || option.getAttribute('value') || optionLabel);
                          return { value: optionValue, label: optionLabel, disabled: option.getAttribute('aria-disabled') === 'true' || option.classList.contains('dx-state-disabled') };
                        }).filter(option => option.value && option.label && !option.disabled);
                        try { el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); } catch (_) {}
                        if (options.length < 2) continue;
                        const currentLabel = clean(el.getAttribute('aria-valuetext') || el.getAttribute('data-value') || el.value || el.innerText);
                        const selected = options.find(option => option.value === currentLabel || option.label === currentLabel);
                        controls.push({ key: identity('custom_select', id, name, null, label, index), label, type: 'custom_select', input_type: null, id, name, index,
                          value: selected?.value || currentLabel, selected_label: selected?.label || currentLabel, options: options.slice(0, 150) });
                      }
                      return controls.slice(0, 60);
                    }""", state_label) or []
                except Exception as exc:
                    browser_errors.append(f"source-filter-discovery:{state_label}:{type(exc).__name__}:{exc}")
                    return []
                cleaned = []
                for item in discovered:
                    if not isinstance(item, dict) or not item.get("key"):
                        continue
                    control_type = str(item.get("type") or "select")
                    cleaned.append({
                        "key": str(item.get("key"))[:240],
                        "state": clean_text(state_label)[:120],
                        "label": clean_text(item.get("label"))[:120],
                        "type": control_type if control_type in {"select", "multiselect", "custom_select", "input", "checkbox", "radio"} else "select",
                        "multiple": bool(item.get("multiple")),
                        "input_type": clean_text(item.get("input_type"))[:20] or None,
                        "id": clean_text(item.get("id"))[:160] or None,
                        "name": clean_text(item.get("name"))[:160] or None,
                        "index": int(item.get("index") or 0),
                        "value": str(item.get("value") or "")[:300],
                        "checked": bool(item.get("checked")),
                        "selected_label": clean_text(item.get("selected_label"))[:160],
                        "options": [
                            {"value": str(option.get("value") or "")[:300], "label": clean_text(option.get("label"))[:160]}
                            for option in (item.get("options") or [])[:150]
                            if isinstance(option, dict) and option.get("value") is not None and clean_text(option.get("label"))
                        ],
                    })
                interactive_filters_by_state[state_label] = cleaned
                logger.info("SOURCE_FILTERS_DISCOVERED url=%s state=%s controls=%d labels=%s", url, state_label, len(cleaned), [item.get("label") for item in cleaned])
                return cleaned

            def apply_selected_source_filters(state_label: str, controls: list[dict[str, Any]]) -> None:
                """Replay only the filters belonging to the current source view."""
                response_start_index = len(responses)
                prefix = f"{clean_text(state_label)}::"
                requested = {
                    str(item.get("key")): str(item.get("value"))
                    for item in apply_source_filters
                    if isinstance(item, dict) and item.get("key") is not None
                    and item.get("value") is not None and str(item.get("key")).startswith(prefix)
                }
                if not requested:
                    return
                by_key = {str(item.get("key")): item for item in controls}
                any_applied = False
                search_input_to_submit = None

                def locator_matches(candidate: Any, descriptor: dict[str, Any], control_index: int) -> bool:
                    candidate_id = str(candidate.get_attribute("id") or "")
                    candidate_name = str(candidate.get_attribute("name") or "")
                    descriptor_id = str(descriptor.get("id") or "")
                    descriptor_name = str(descriptor.get("name") or "")
                    if descriptor_id:
                        return candidate_id == descriptor_id
                    if descriptor_name:
                        if candidate_name != descriptor_name:
                            return False
                        if descriptor.get("type") == "checkbox":
                            return str(candidate.get_attribute("value") or "on") == str(descriptor.get("value") or "on")
                        return True
                    return control_index == int(descriptor.get("index") or 0)

                for key, value in requested.items():
                    descriptor = by_key.get(key)
                    if not descriptor:
                        source_filter_audit.append({"key": key, "status": "not_found_in_source_view", "state": state_label})
                        logger.warning("SOURCE_FILTER_APPLY_NOT_FOUND url=%s state=%s key=%s", url, state_label, key)
                        continue
                    control_type = descriptor.get("type")
                    try:
                        if control_type in {"select", "multiselect"}:
                            selected_values = [value]
                            if control_type == "multiselect":
                                try:
                                    decoded_values = json.loads(value)
                                    selected_values = [str(item) for item in decoded_values] if isinstance(decoded_values, list) else []
                                except Exception:
                                    selected_values = []
                            valid_values = {str(option.get("value")) for option in descriptor.get("options") or []}
                            if not selected_values or any(item not in valid_values for item in selected_values):
                                source_filter_audit.append({"key": key, "label": descriptor.get("label"), "value": value, "status": "invalid_option", "state": state_label})
                                logger.warning("SOURCE_FILTER_APPLY_INVALID_OPTION url=%s state=%s label=%s value=%s", url, state_label, descriptor.get("label"), value)
                                continue
                            candidates = page.locator("select")
                            target = None
                            for control_index in range(min(candidates.count(), 150)):
                                candidate = candidates.nth(control_index)
                                if locator_matches(candidate, descriptor, control_index):
                                    target = candidate
                                    break
                            if target is None:
                                raise RuntimeError("source select element no longer exists")
                            actual_values = ["" if item == "__NISE_EMPTY__" else item for item in selected_values]
                            target.select_option(value=actual_values if control_type == "multiselect" else actual_values[0], timeout=2500)
                            # NISE's Year1 control is a Select2 multi-select.
                            # Its page handler listens to `select2:close`, not
                            # only the native change event. Playwright's
                            # select_option dispatches change/input but does not
                            # close Select2, so the source never fires CellChart /
                            # WaferToCell (or PnlChart / PnlMfgChart) for a filter
                            # replay unless this event is explicitly emitted.
                            if str(descriptor.get("id") or "").casefold() == "year1" or str(descriptor.get("name") or "").casefold() == "year1":
                                target.evaluate("""el => {
                                  const jq = window.jQuery;
                                  if (jq) {
                                    jq(el).trigger('change.select2');
                                    jq(el).trigger('select2:close');
                                  }
                                }""")
                        elif control_type == "custom_select":
                            selected_option = next((option for option in descriptor.get("options") or [] if str(option.get("value")) == value), None)
                            if not selected_option:
                                raise RuntimeError("requested value is not in the source custom-select options")
                            candidates = page.locator('[role="combobox"], [aria-haspopup="listbox"]')
                            target = None
                            for control_index in range(min(candidates.count(), 100)):
                                candidate = candidates.nth(control_index)
                                if locator_matches(candidate, descriptor, control_index):
                                    target = candidate
                                    break
                            if target is None:
                                raise RuntimeError("source custom-select element no longer exists")
                            target.click(timeout=2500)
                            page.wait_for_timeout(180)
                            option_locator = page.locator('[role="option"]:visible, .dx-list-item:visible, .select2-results__option:visible')
                            option_target = None
                            for option_index in range(min(option_locator.count(), 300)):
                                candidate = option_locator.nth(option_index)
                                candidate_label = clean_text(candidate.inner_text(timeout=500))
                                candidate_value = str(candidate.get_attribute("data-value") or candidate.get_attribute("data-key") or candidate.get_attribute("value") or candidate_label)
                                if candidate_value == value or candidate_label.casefold() == str(selected_option.get("label") or "").casefold():
                                    option_target = candidate
                                    break
                            if option_target is None:
                                raise RuntimeError("requested option not found in the open source custom-select")
                            option_target.click(timeout=2500)
                        elif control_type == "input":
                            candidates = page.locator("input")
                            target = None
                            for control_index in range(min(candidates.count(), 150)):
                                candidate = candidates.nth(control_index)
                                if locator_matches(candidate, descriptor, control_index):
                                    target = candidate
                                    break
                            if target is None:
                                raise RuntimeError("source input element no longer exists")
                            target.fill(value, timeout=2500)
                            if descriptor.get("input_type") == "search":
                                search_input_to_submit = target
                        elif control_type == "checkbox":
                            checked = value.casefold() in {"1", "true", "yes", "on", "checked"}
                            candidates = page.locator("input")
                            target = None
                            for control_index in range(min(candidates.count(), 150)):
                                candidate = candidates.nth(control_index)
                                if str(candidate.get_attribute("type") or "").casefold() != "checkbox":
                                    continue
                                if locator_matches(candidate, descriptor, control_index):
                                    target = candidate
                                    break
                            if target is None:
                                raise RuntimeError("source checkbox element no longer exists")
                            target.set_checked(checked, timeout=2500)
                        elif control_type == "radio":
                            name = str(descriptor.get("name") or "")
                            candidates = page.locator("input[type='radio']")
                            target = None
                            for control_index in range(min(candidates.count(), 150)):
                                candidate = candidates.nth(control_index)
                                if str(candidate.get_attribute("name") or "") == name and str(candidate.get_attribute("value") or "") == value:
                                    target = candidate
                                    break
                            if target is None:
                                raise RuntimeError("source radio option no longer exists")
                            target.check(timeout=2500)
                        else:
                            continue
                        source_filter_audit.append({"key": key, "label": descriptor.get("label"), "value": value, "status": "applied", "state": state_label})
                        any_applied = True
                        logger.info("SOURCE_FILTER_APPLIED url=%s state=%s label=%s value=%s", url, state_label, descriptor.get("label"), value)
                    except Exception as exc:
                        source_filter_audit.append({"key": key, "label": descriptor.get("label"), "value": value, "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:240], "state": state_label})
                        logger.warning("SOURCE_FILTER_APPLY_ERROR url=%s state=%s label=%s error=%s", url, state_label, descriptor.get("label"), exc)
                if any_applied:
                    action_clicked = False
                    for action_label in ("Apply Filters", "Apply Filter", "Apply", "Refresh", "Submit", "Search"):
                        try:
                            action = page.get_by_role("button", name=re.compile(rf"^{re.escape(action_label)}$", re.I)).first
                            if action.count() and action.is_visible():
                                action.click(timeout=1200)
                                action_clicked = True
                                break
                        except Exception:
                            continue
                    if not action_clicked and search_input_to_submit is not None:
                        try:
                            search_input_to_submit.press("Enter", timeout=1200)
                        except Exception:
                            pass
                    if network_action_allowlist and clean_text(state_label).casefold() != "initial":
                        # Once inside a NISE drilldown, the source filters update
                        # the two manufacturing charts through XHR. Wait on those
                        # exact chart actions instead of every portal request.
                        chart_actions = network_action_required
                        wait_for_source_actions(
                            response_start_index,
                            expected=chart_actions,
                            timeout_ms=8_000,
                            require_all=True,
                        )
                    else:
                        try:
                            page.wait_for_load_state("networkidle", timeout=7000)
                        except Exception:
                            pass
                    page.wait_for_timeout(settle_ms)
                    # Store the post-application values, not the defaults that
                    # were visible before the user selected a source filter.
                    discover_source_selects(state_label)

            def sweep_visible_selects(state_prefix: str = "") -> None:
                nonlocal total_sweep_options, active_capture_state
                for sweep_index, wanted in enumerate(sweep_selects, start=1):
                    # `year:latest` captures only the newest Year option;
                    # plain `year` captures each available source year.
                    wanted_label, _, selection_mode = wanted.partition(":")
                    latest_only = selection_mode.casefold() == "latest"
                    wanted_low = wanted_label.casefold()
                    logger.info("SOURCE_FILTER_SWEEP_START url=%s filter=%s mode=%s state=%s index=%d/%d elapsed_seconds=%.1f total_options=%d", url, wanted_label, selection_mode or "all", state_prefix or "initial", sweep_index, len(tuple(sweep_selects)), time.monotonic() - render_started, total_sweep_options)
                    if time.monotonic() >= sweep_deadline or total_sweep_options >= max_total_sweep_options:
                        select_sweep_audit.append({"filter": wanted_label, "mode": selection_mode or "all", "state": state_prefix or "initial", "status": "skipped_budget", "captured_states": 0})
                        logger.warning("SOURCE_FILTER_SWEEP_BUDGET_EXHAUSTED url=%s filter=%s state=%s elapsed_seconds=%.1f total_options=%d max_options=%d", url, wanted, state_prefix or "initial", time.monotonic() - render_started, total_sweep_options, max_total_sweep_options)
                        continue
                    try:
                        # Select2 hides the original <select> (the visible UI is
                        # a sibling span). NISE's handler is attached to the original
                        # #Year1 element, so include hidden selects and prefer its ID.
                        selects = page.locator("select")
                        found_select = None
                        found_label = ""
                        found_index = -1
                        select_count = min(selects.count(), 80)
                        for select_index in range(select_count):
                            select = selects.nth(select_index)
                            select_id = str(select.get_attribute("id") or "")
                            select_name = str(select.get_attribute("name") or "")
                            select_label = str(select.get_attribute("aria-label") or select.get_attribute("title") or select_name or select_id)
                            if select_id:
                                try:
                                    label_node = page.locator(f"label[for='{select_id}']").first
                                    if label_node.count():
                                        select_label = clean_text(label_node.inner_text(timeout=400)) or select_label
                                except Exception:
                                    pass
                            if not select_label:
                                try:
                                    select_label = clean_text(select.locator("xpath=..").inner_text(timeout=400))[:100]
                                except Exception:
                                    pass
                            id_name_match = wanted_low == "year" and (
                                select_id.casefold() == "year1" or select_name.casefold() == "year1"
                            )
                            if id_name_match or wanted_low in select_label.casefold():
                                found_select, found_label, found_index = select, select_label or wanted, select_index
                                if id_name_match:
                                    break
                        if found_select is None:
                            select_sweep_audit.append({"filter": wanted_label, "mode": selection_mode or "all", "state": state_prefix or "initial", "status": "not_found"})
                            continue
                        options = found_select.locator("option")
                        option_items: list[tuple[str, str]] = []
                        for option_index in range(min(options.count(), 500)):
                            option = options.nth(option_index)
                            value = option.get_attribute("value")
                            label = clean_text(option.inner_text(timeout=400))
                            if value is None or not label or label.startswith("---") or label.casefold().startswith("select "):
                                continue
                            if value not in {existing for existing, _ in option_items}:
                                option_items.append((value, label))
                        available_option_count = len(option_items)
                        if latest_only:
                            year_options = []
                            for option_value, option_label in option_items:
                                year_match = re.search(r"\b(20\d{2})\b", f"{option_label} {option_value}")
                                if year_match:
                                    year_options.append((int(year_match.group(1)), option_value, option_label))
                            if year_options:
                                latest_year = max(item[0] for item in year_options)
                                option_items = [next(item[1:] for item in year_options if item[0] == latest_year)]
                            else:
                                # With unlabelled options, keep the page's
                                # existing selection rather than guessing order.
                                selected_value = found_select.input_value(timeout=800)
                                option_items = [item for item in option_items if item[0] == selected_value]
                        limits = {"state": 20, "fuel": 12, "category": 12, "class": 12, "emission": 10, "norm": 10, "maker": 25, "manufacturer": 25, "view": 6, "month": 12, "year": 12, "technology": 8}
                        limit = next((value for token, value in limits.items() if token in wanted_low), 10)
                        remaining_budget = max(0, max_total_sweep_options - total_sweep_options)
                        option_items = option_items[:min(limit, remaining_budget)]
                        selected_option_labels = [label for _, label in option_items]
                        logger.info("SOURCE_FILTER_OPTIONS url=%s filter=%s mode=%s state=%s selector=%s available=%d scheduled=%d total_budget_remaining=%d", url, wanted_label, selection_mode or "all", state_prefix or "initial", found_label, available_option_count, len(option_items), remaining_budget)
                        original_value = found_select.input_value(timeout=800)
                        captures_done = 0
                        for option_index, (option_value, option_label) in enumerate(option_items, start=1):
                            if time.monotonic() >= sweep_deadline:
                                logger.warning("SOURCE_FILTER_SWEEP_TIME_LIMIT url=%s filter=%s state=%s captured=%d scheduled=%d elapsed_seconds=%.1f", url, wanted, state_prefix or "initial", captures_done, len(option_items), time.monotonic() - render_started)
                                break
                            try:
                                base_state = state_prefix or "initial"
                                safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", option_label).strip("_")[:45]
                                active_capture_state = f"{base_state}:{wanted}:{option_label}"
                                option_response_start = len(responses)
                                found_select.select_option(value=option_value, timeout=1800)
                                if wanted_low == "year":
                                    # NISE wires its AJAX refresh to the Select2
                                    # close event, not just the native change event.
                                    # select_option() updates the hidden select but
                                    # does not close Select2, so explicitly replay
                                    # the same jQuery events as the real UI.
                                    found_select.evaluate("""(el) => {
                                        const jq = window.jQuery || window.$;
                                        if (jq) {
                                            jq(el).trigger('change');
                                            jq(el).trigger('select2:close');
                                        } else {
                                            el.dispatchEvent(new Event('change', { bubbles: true }));
                                        }
                                    }""")
                                for action_label in ("Apply Filters", "Apply Filter", "Apply", "Refresh Table", "Refresh", "Submit"):
                                    try:
                                        action = page.get_by_role("button", name=re.compile(rf"^{re.escape(action_label)}$", re.I)).first
                                        if action.count() and action.is_visible():
                                            action.click(timeout=900)
                                            break
                                    except Exception:
                                        continue
                                page.wait_for_timeout(250)
                                if network_action_allowlist:
                                    # A Year1 change is useful only once the
                                    # corresponding manufacturing XHR has
                                    # returned. Wait for that request directly
                                    # instead of waiting on unrelated portal
                                    # traffic or animations.
                                    chart_actions = network_action_required
                                    wait_for_source_actions(
                                        option_response_start,
                                        expected=chart_actions,
                                        timeout_ms=max(1_500, int(network_action_timeout_ms)),
                                        require_all=True,
                                    )
                                    page.wait_for_timeout(200)
                                elif sweep_selects_after_clicks:
                                    # NISE updates its production chart through AJAX
                                    # after Year1 changes. Give the MVC partial and
                                    # its DevExtreme chart time to receive and render
                                    # the response before taking the snapshot.
                                    page.wait_for_timeout(180)
                                # Keep the snapshot state identical to the state
                                # attached to its AJAX responses. The normalizer
                                # uses this exact key to associate month-only rows
                                # with the selected Year1 value.
                                snapshot(active_capture_state)
                                captures_done += 1
                                total_sweep_options += 1
                                if option_index == 1 or option_index % 5 == 0 or option_index == len(option_items):
                                    logger.info("SOURCE_FILTER_SWEEP_PROGRESS url=%s filter=%s state=%s progress=%d/%d total_options=%d snapshots=%d network_items=%d elapsed_seconds=%.1f", url, wanted, base_state, captures_done, len(option_items), total_sweep_options, len(snapshots), len(responses), time.monotonic() - render_started)
                            except Exception as exc:
                                browser_errors.append(f"filter-sweep:{state_prefix}:{wanted}:{option_label}:{type(exc).__name__}:{exc}")
                        try:
                            if original_value is not None and not latest_only:
                                # A restore request is not part of any selected
                                # Year state. Give its XHR a neutral label so a
                                # month-only response cannot inherit the last
                                # swept year and be misattributed by the parser.
                                active_capture_state = "source-filter-restore"
                                found_select.select_option(value=original_value, timeout=1000)
                                for action_label in ("Apply Filters", "Apply Filter", "Apply", "Refresh Table", "Refresh", "Submit"):
                                    try:
                                        action = page.get_by_role("button", name=re.compile(rf"^{re.escape(action_label)}$", re.I)).first
                                        if action.count() and action.is_visible():
                                            action.click(timeout=700)
                                            break
                                    except Exception:
                                        continue
                        except Exception:
                            pass
                        select_sweep_audit.append({"filter": wanted_label, "mode": selection_mode or "all", "state": state_prefix or "initial", "label": found_label, "option_count": available_option_count, "selected_options": selected_option_labels, "captured_states": captures_done})
                        logger.info("SOURCE_FILTER_SWEEP_COMPLETE url=%s filter=%s mode=%s state=%s captured=%d scheduled=%d total_options=%d elapsed_seconds=%.1f", url, wanted_label, selection_mode or "all", state_prefix or "initial", captures_done, len(option_items), total_sweep_options, time.monotonic() - render_started)
                    except Exception as exc:
                        select_sweep_audit.append({"filter": wanted_label, "mode": selection_mode or "all", "state": state_prefix or "initial", "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
                        logger.exception("SOURCE_FILTER_SWEEP_ERROR url=%s filter=%s state=%s", url, wanted, state_prefix or "initial")

            if sweep_selects and sweep_selects_before_clicks:
                sweep_visible_selects()

            # Discover and replay Summary-level controls before the KPI drilldown.
            # Some NISE filters (e.g. Company type / State) can live on the
            # Summary view, while additional Year / search controls may appear
            # only after entering the Cell or Module manufacturer view.
            if discover_source_filters_after_clicks:
                summary_controls = discover_source_selects("initial")
                apply_selected_source_filters("initial", summary_controls)
                if any(item.get("state") == "initial" and item.get("status") == "applied" for item in source_filter_audit):
                    active_capture_state = "initial"
                    snapshot("initial:filtered")

            for click_index, label in enumerate(clicks):
                if reload_between_clicks and click_index:
                    # Some KPI tiles replace the active dashboard body with a
                    # detail grid. Reload the public summary before the next KPI
                    # so each drilldown is collected independently in one
                    # browser session rather than clicking through a modal.
                    active_capture_state = f"reload-before-{label}"
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=min(timeout_ms, 45_000))
                        try:
                            page.wait_for_load_state("networkidle", timeout=2500)
                        except Exception:
                            pass
                        page.wait_for_timeout(settle_ms)
                    except Exception as exc:
                        browser_errors.append(f"reload-before-click:{label}:{type(exc).__name__}:{exc}")
                active_capture_state = clean_text(label) or "initial"
                response_count_before_click = len(responses)
                clicked = False
                click_method = ""
                # Buttons and tabs first; this avoids accidentally clicking table
                # cells with the same text as the action label.
                for role in ("button", "tab", "menuitem"):
                    try:
                        locator = page.get_by_role(role, name=label, exact=True).first
                        if locator.count():
                            locator.click(timeout=min(7000, max(1500, timeout_ms // 10)))
                            clicked = True
                            click_method = f"role:{role}"
                            break
                    except Exception:
                        continue
                if not clicked:
                    # Some sites implement tabs as anchors or divs with tab roles
                    # omitted. Restrict fallback to visible nodes that look like a
                    # navigational tab/action rather than any arbitrary text node.
                    try:
                        locator = page.locator("[role='tab'], .nav-link, .nav-item a, .tab, [data-toggle='tab']").filter(has_text=re.compile(rf"^{re.escape(label)}$", re.I)).first
                        if locator.count() and locator.is_visible():
                            locator.click(timeout=min(7000, max(1500, timeout_ms // 10)))
                            clicked = True
                            click_method = "tab-like-selector"
                    except Exception:
                        pass
                if not clicked:
                    # NISE's KPI cards are clickable `.mini-stats-wid` cards.
                    # The visible caption is commonly rendered as
                    # "NO. OF SOLAR MODULE MANUFACTURERS", while the caller's
                    # semantic label is "Solar Module Manufacturer". Find the
                    # matching caption case-insensitively and click its nearest
                    # KPI card rather than the first arbitrary partial-text node.
                    try:
                        product_match = re.search(r"\b(cell|module)\b", label, re.I)
                        product_word = product_match.group(1) if product_match else ""
                        card_pattern = re.compile(
                            rf"(?:no\.?\s+of\s+)?solar\s+{re.escape(product_word)}\s+manufacturers?",
                            re.I,
                        ) if product_word else re.compile(re.escape(label), re.I)
                        candidates = page.get_by_text(card_pattern, exact=False)
                        candidate_count = min(candidates.count(), 40)
                        ranked = []
                        for candidate_index in range(candidate_count):
                            candidate = candidates.nth(candidate_index)
                            try:
                                if not candidate.is_visible():
                                    continue
                                candidate_text = clean_text(candidate.inner_text(timeout=500))
                                if not card_pattern.search(candidate_text):
                                    continue
                                card = candidate.locator(
                                    "xpath=ancestor-or-self::*[contains(concat(' ', normalize-space(@class), ' '), ' mini-stats-wid ')][1]"
                                ).first
                                if not card.count():
                                    card = candidate.locator(
                                        "xpath=ancestor-or-self::*[contains(concat(' ', normalize-space(@class), ' '), ' card ')][1]"
                                    ).first
                                target = card if card.count() and card.is_visible() else candidate
                                score = (0 if re.search(r"no\.?\s+of", candidate_text, re.I) else 1, len(candidate_text))
                                ranked.append((score, target))
                            except Exception:
                                continue
                        if ranked:
                            ranked.sort(key=lambda item: item[0])
                            ranked[0][1].click(timeout=min(7000, max(1500, timeout_ms // 10)))
                            clicked = True
                            click_method = "manufacturer-kpi-card"
                    except Exception:
                        pass
                if not clicked:
                    # Generic fallback for other explicitly requested dashboard actions.
                    try:
                        locator = page.get_by_text(label, exact=True).first
                        if not locator.count():
                            locator = page.get_by_text(label, exact=False).first
                        if locator.count() and locator.is_visible():
                            locator.click(timeout=min(7000, max(1500, timeout_ms // 10)))
                            clicked = True
                            click_method = "visible-text"
                    except Exception:
                        pass
                if clicked:
                    if network_action_allowlist:
                        # The screenshots identify the exact AJAX actions that
                        # populate these two KPI drilldowns. Wait for the
                        # manufacturing actions themselves; unrelated portal
                        # requests (map tiles, analytics, etc.) cannot hold this
                        # collection open.
                        wait_for_source_actions(
                            response_count_before_click,
                            expected=network_action_required,
                            timeout_ms=max(1_000, int(network_action_timeout_ms)),
                            require_all=True,
                        )
                        page.wait_for_timeout(250)
                    else:
                        try:
                            page.wait_for_timeout(settle_ms)
                            page.wait_for_load_state("networkidle", timeout=min(12_000, max(3_000, settle_ms * 4)))
                        except Exception:
                            pass
                        page.wait_for_timeout(min(2_000, max(500, settle_ms)))
                    if discover_source_filters_after_clicks:
                        source_controls = discover_source_selects(label)
                        apply_selected_source_filters(label, source_controls)
                    snapshot(label)
                    if sweep_selects_after_clicks and sweep_selects:
                        sweep_visible_selects(label)
                        active_capture_state = label
                    state_responses = [item for item in responses if item.get("state") == label or str(item.get("state") or "").startswith(f"{label}:")]
                    state_json = [item for item in state_responses if "json" in str(item.get("content_type") or "").casefold() or str(item.get("body") or "").lstrip().startswith(("{", "["))]
                    click_audit.append({
                        "label": label,
                        "clicked": True,
                        "method": click_method,
                        "new_network_items": len(responses) - response_count_before_click,
                        "state_network_items": len(state_responses),
                        "state_json_items": len(state_json),
                    })
                    logger.info(
                        "SOURCE_CLICK_ACTION_COMPLETE url=%s label=%s method=%s new_network_items=%d state_network_items=%d state_json_items=%d elapsed_seconds=%.1f",
                        url, label, click_method, len(responses) - response_count_before_click,
                        len(state_responses), len(state_json), time.monotonic() - render_started,
                    )
                else:
                    click_audit.append({"label": label, "clicked": False, "method": None, "new_network_items": 0, "state_network_items": 0, "state_json_items": 0})
                    logger.warning("SOURCE_CLICK_ACTION_NOT_FOUND url=%s label=%s; no DOM action was performed", url, label)

            # Capture visible buttons/tabs that weren't matched by the static
            # labels. Exclude obvious actions that submit, reset or sign users in.
            attempted = {clean_text(x).casefold() for x in clicks}
            for label in (tab_labels if auto_sweep_tabs else []):
                if label.casefold() in attempted or any(token in label.casefold() for token in ("apply", "reset", "load more", "next", "refresh", "search", "login", "subscribe")):
                    continue
                try:
                    clicked = False
                    for role in ("tab", "button"):
                        locator = page.get_by_role(role, name=label, exact=True).first
                        if locator.count() and locator.is_visible():
                            locator.click(timeout=800)
                            clicked = True
                            break
                    if not clicked:
                        locator = page.locator("[data-toggle='tab'], [data-bs-toggle='tab'], .nav-tabs a, .nav-pills a").filter(has_text=re.compile(rf"^{re.escape(label)}$", re.I)).first
                        if locator.count() and locator.is_visible():
                            locator.click(timeout=800)
                            clicked = True
                    if clicked:
                        page.wait_for_timeout(400)
                        snapshot(f"auto-tab-{label}")
                except Exception as exc:
                    logger.debug("Auto tab sweep skipped label=%s error=%s", label, exc)

            final_html = page.content()
            logger.info("SOURCE_BROWSER_COMPLETE url=%s elapsed_seconds=%.1f snapshots=%d network_items=%d network_chars=%d sweep_options=%d sweep_audit=%s errors=%d", url, time.monotonic() - render_started, len(snapshots), len(responses), response_chars, total_sweep_options, select_sweep_audit, len(browser_errors))
            if page.url in {"", "about:blank"}:
                raise SourceUnavailable(f"Browser did not navigate to a document for {url}")
            return {
                "html": final_html[:MAX_HTML_CHARS],
                "html_chars": len(final_html),
                "html_truncated": len(final_html) > MAX_HTML_CHARS,
                "responses": responses,
                "network_failures": network_failures,
                "snapshots": snapshots,
                "final_url": page.url,
                "browser_errors": browser_errors,
                "network_response_chars": response_chars,
                "captured_network_actions": sorted({
                    str(item.get("endpoint_action")) for item in responses if item.get("endpoint_action")
                }),
                "discovered_tab_labels": tab_labels,
                "click_audit": click_audit,
                "select_sweep_audit": select_sweep_audit,
                "interactive_filters_by_state": interactive_filters_by_state,
                "source_filter_audit": source_filter_audit,
            }
        finally:
            try:
                browser.close()
            except Exception:
                pass


async def render_page_snapshot(
    url: str,
    *,
    timeout_ms: int = 120_000,
    clicks: Iterable[str] = (),
    sweep_selects: Iterable[str] = (),
    capture_network: bool = True,
    auto_sweep_tabs: bool = True,
    settle_ms: int = 900,
    capture_runtime_charts: bool = False,
    reload_between_clicks: bool = False,
    sweep_selects_after_clicks: bool = False,
    sweep_selects_before_clicks: bool = True,
    apply_source_filters: Iterable[dict[str, Any]] = (),
    discover_source_filters_after_clicks: bool = False,
    network_action_allowlist: Iterable[str] = (),
    network_action_required: Iterable[str] = (),
    network_action_timeout_ms: int = 18_000,
    network_idle_timeout_ms: int | None = None,
    capture_runtime_grid_full_load: bool = True,
) -> dict[str, Any]:
    # Internal Playwright timeouts control the actual work. Do not wrap this in
    # asyncio.wait_for: a timed-out thread would continue running in the background.
    return await asyncio.to_thread(
        _render_page_sync,
        url,
        timeout_ms=int(timeout_ms),
        clicks=tuple(clicks),
        sweep_selects=tuple(sweep_selects),
        capture_network=capture_network,
        auto_sweep_tabs=auto_sweep_tabs,
        settle_ms=int(settle_ms),
        capture_runtime_charts=bool(capture_runtime_charts),
        reload_between_clicks=bool(reload_between_clicks),
        sweep_selects_after_clicks=bool(sweep_selects_after_clicks),
        sweep_selects_before_clicks=bool(sweep_selects_before_clicks),
        apply_source_filters=tuple(item for item in apply_source_filters if isinstance(item, dict)),
        discover_source_filters_after_clicks=bool(discover_source_filters_after_clicks),
        network_action_allowlist=tuple(str(item) for item in network_action_allowlist if str(item).strip()),
        network_action_required=tuple(str(item) for item in network_action_required if str(item).strip()),
        network_action_timeout_ms=max(1_000, int(network_idle_timeout_ms if network_idle_timeout_ms is not None else network_action_timeout_ms)),
        network_idle_timeout_ms=network_idle_timeout_ms,
        capture_runtime_grid_full_load=bool(capture_runtime_grid_full_load),
    )


def _extract_tables(soup: BeautifulSoup, *, page_url: str, state: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for table_index, table in enumerate(soup.find_all("table")[:MAX_TABLES_PER_DOCUMENT], start=1):
        matrix: list[list[str]] = []
        for tr in table.find_all("tr")[: MAX_ROWS_PER_TABLE + 1]:
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
            if cells:
                matrix.append(cells)
        if not matrix:
            continue
        table_rows = table.find_all("tr")[: MAX_ROWS_PER_TABLE + 1]
        th_idx = next((i for i, tr in enumerate(table_rows[:20]) if tr.find_all("th")), None)
        first_row = [clean_text(value).lower() for value in matrix[0]]
        header_terms = (
            "state", "month", "year", "quarter", "period", "date", "product", "commodity",
            "price", "count", "growth", "registration", "permit", "manufacturer", "fuel",
            "category", "value", "quantity", "volume", "sales", "total", "effective",
        )
        looks_like_header = bool(first_row) and sum(
            1 for cell in first_row if any(term in cell for term in header_terms)
        ) >= max(1, min(2, len(first_row) // 2))
        # Some JavaScript dashboards render column headings as <td>, while others
        # render the first data row without any header row. Only consume a row as
        # headings when <th> exists or the text itself looks like column labels.
        has_header = th_idx is not None or looks_like_header
        header_idx = th_idx if th_idx is not None else 0
        if has_header:
            headers = matrix[min(header_idx, len(matrix) - 1)]
            raw_rows = matrix[min(header_idx, len(matrix) - 1) + 1:]
        else:
            width = max(len(row) for row in matrix)
            first_value = matrix[0][0] if matrix[0] else ""
            title_hint = clean_text((table.find("caption") or {}).get_text(" ", strip=True)) if table.find("caption") else ""
            nearby_node = table.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
            nearby_hint = clean_text(nearby_node.get_text(" ", strip=True)) if nearby_node else ""
            title_hint = (title_hint or nearby_hint).lower()
            if "summary" in title_hint and first_value and not parse_number(first_value):
                headers = ["State", "Cell Mfg #", "Module Mfg #", "Reseller #"] + [f"Column {i+1}" for i in range(max(0, width - 4))]
            elif first_value and not parse_number(first_value):
                headers = ["Label"] + [f"Value {i+1}" for i in range(max(0, width - 1))]
            else:
                headers = [f"Column {i+1}" for i in range(width)]
            raw_rows = matrix
        headers = [name or f"Column {i + 1}" for i, name in enumerate(headers)]
        columns: list[str] = []
        seen: dict[str, int] = {}
        for i, name in enumerate(headers):
            base = clean_text(name) or f"Column {i + 1}"
            seen[base] = seen.get(base, 0) + 1
            columns.append(base if seen[base] == 1 else f"{base} ({seen[base]})")
        rows: list[dict[str, Any]] = []
        for row_index, raw in enumerate(raw_rows):
            item = {columns[i]: raw[i] if i < len(raw) else None for i in range(len(columns))}
            if any(v not in (None, "") for v in item.values()):
                item["_row_number"] = row_index + 1
                rows.append(item)
        caption = clean_text((table.find("caption") or {}).get_text(" ", strip=True)) if table.find("caption") else ""
        nearby = ""
        for node in table.find_all_previous(["h1", "h2", "h3", "h4", "h5", "h6"] , limit=2):
            nearby = clean_text(node.get_text(" ", strip=True))
            if nearby:
                break
        title = caption or nearby or f"Table {table_index}"
        output.append({
            "key": f"{state.lower().replace(' ', '-')}-table-{table_index}",
            "title": title,
            "caption": caption or None,
            "state": state,
            "url": page_url,
            "columns": columns,
            "rows": rows,
            "raw_rows": matrix[:MAX_ROWS_PER_TABLE + 1],
            "row_count": len(raw_rows),
            "truncated": len(matrix) > MAX_ROWS_PER_TABLE,
        })
    return output


def _extract_kpis(soup: BeautifulSoup, *, page_url: str, state: str) -> list[dict[str, Any]]:
    selectors = (
        "[class*='kpi' i]", "[class*='stat' i]", "[class*='metric' i]",
        "[class*='counter' i]", "[class*='summary-card' i]", "[class*='info-box' i]",
        "[data-testid*='kpi' i]", "[data-testid*='metric' i]", ".card",
    )
    candidates: list[Any] = []
    seen_nodes: set[int] = set()
    for selector in selectors:
        try:
            for node in soup.select(selector):
                identity = id(node)
                if identity in seen_nodes:
                    continue
                seen_nodes.add(identity)
                candidates.append(node)
        except Exception:
            continue
    result: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for node in candidates:
        text = clean_text(node.get_text(" ", strip=True))
        lowered_text = text.casefold()
        if (not text or len(text) > 260 or len(text) < 3 or text in seen_text
                or any(token in lowered_text for token in (
                    "load in batches", "select an option", "choose an option", "click here",
                    "read more", "view more", "download", "search maker", "filters available",
                ))):
            continue
        # Keep nodes that look like a value card, not every arbitrary UI card.
        descendants = node.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "span", "small", "label"], limit=16)
        numeric = parse_number(text)
        if numeric is None and len(descendants) < 2:
            continue
        labels = [clean_text(x.get_text(" ", strip=True)) for x in descendants if clean_text(x.get_text(" ", strip=True))]
        label = next((x for x in labels if parse_number(x) is None and len(x) <= 100), "")
        value_text = next((x for x in labels if parse_number(x) is not None), text)
        value = parse_number(value_text)
        if value is None:
            value = parse_number(text)
        if value is None and len(labels) < 2:
            continue
        result.append({
            "key": f"kpi-{len(result)+1}",
            "label": label or text[:100],
            "value": value if value is not None else value_text,
            "display_value": value_text[:120],
            "text": text,
            "url": page_url,
            "state": state,
        })
        seen_text.add(text)
        if len(result) >= MAX_KPIS_PER_DOCUMENT:
            break
    return result


def _extract_filters(soup: BeautifulSoup, *, page_url: str, state: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, node in enumerate(soup.select("select, input[type='checkbox'], input[type='radio'], input[type='range'], input[type='date'], input[type='month'], input[type='number']")[:MAX_FILTERS_PER_DOCUMENT], start=1):
        attrs = {key: clean_text(node.get(key, "")) for key in ("id", "name", "type", "value", "aria-label", "placeholder", "title") if node.get(key) is not None}
        label = clean_text(node.get("aria-label") or node.get("title") or node.get("placeholder") or node.get("name") or node.get("id") or "")
        if not label:
            lab = soup.find("label", attrs={"for": node.get("id")}) if node.get("id") else node.find_parent("label")
            label = clean_text(lab.get_text(" ", strip=True)) if lab else f"Filter {index}"
        options = []
        if node.name == "select":
            for option in node.find_all("option")[:500]:
                options.append({
                    "label": clean_text(option.get_text(" ", strip=True)),
                    "value": clean_text(option.get("value") or option.get_text(" ", strip=True)),
                    "selected": bool(option.has_attr("selected")),
                    "disabled": bool(option.has_attr("disabled")),
                })
        elif node.get("type", "").lower() in {"checkbox", "radio"}:
            options = [{"label": label, "value": attrs.get("value", "on"), "selected": bool(node.has_attr("checked"))}]
        result.append({
            "key": attrs.get("id") or attrs.get("name") or f"filter-{index}",
            "label": label,
            "type": attrs.get("type") or node.name,
            "value": attrs.get("value"),
            "options": options,
            "attributes": attrs,
            "state": state,
            "url": page_url,
        })
    return result


def _extract_charts_and_json(soup: BeautifulSoup, *, page_url: str, state: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    charts: list[dict[str, Any]] = []
    embedded: list[dict[str, Any]] = []
    for index, node in enumerate(soup.find_all("script")[:200], start=1):
        content = node.string or node.get_text() or ""
        content = content.strip()
        if not content:
            continue
        script_type = str(node.get("type") or "").lower()
        script_id = str(node.get("id") or "")
        looks_json = "json" in script_type or script_id.lower().find("json") >= 0
        parsed = None
        if looks_json or content[:1] in "[{":
            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                parsed = None
        if parsed is not None:
            if len(embedded) < 120:
                embedded.append({"key": f"embedded-json-{len(embedded)+1}", "script_id": script_id or None, "script_type": script_type or None, "url": page_url, "state": state, "data": parsed})
            _find_chart_models(parsed, charts, page_url=page_url, state=state, context=f"script-{index}")
        elif re.search(r"highcharts|echarts|plotly|apexcharts|chart\.js|new Chart\(|series\s*[:=]|datasets\s*[:=]", content, re.I):
            if len(charts) < 100:
                charts.append({
                    "key": f"chart-script-{len(charts)+1}",
                    "title": script_id or f"Chart script {index}",
                    "library": _chart_library(content),
                    "url": page_url,
                    "state": state,
                    "script_text": content[:MAX_SCRIPT_CHARS],
                    "script_chars": len(content),
                    "truncated": len(content) > MAX_SCRIPT_CHARS,
                })
    for index, svg in enumerate(soup.find_all("svg")[:80], start=1):
        labels = [clean_text(x.get_text(" ", strip=True)) for x in svg.find_all(["text", "title", "desc"])[:100]]
        labels = [x for x in labels if x]
        charts.append({"key": f"dom-svg-{index}", "title": clean_text(svg.get("aria-label") or svg.get("title") or (labels[0] if labels else f"SVG chart {index}")), "type": "svg", "labels": labels, "url": page_url, "state": state})
    for index, canvas in enumerate(soup.find_all("canvas")[:80], start=1):
        charts.append({"key": f"dom-canvas-{index}", "title": clean_text(canvas.get("aria-label") or canvas.get("title") or canvas.get("id") or f"Canvas chart {index}"), "type": "canvas", "width": canvas.get("width"), "height": canvas.get("height"), "url": page_url, "state": state})
    return charts, embedded


def _find_chart_models(value: Any, output: list[dict[str, Any]], *, page_url: str, state: str, context: str, depth: int = 0) -> None:
    if depth > 10 or len(output) >= 100:
        return
    if isinstance(value, dict):
        keys = {str(k).lower() for k in value.keys()}
        has_series = bool(keys & {"series", "datasets", "traces"})
        has_categories = bool(keys & {"categories", "labels", "xaxis", "x_axis", "dates", "periods"})
        has_record_source = bool(keys & {"datasource", "data_source", "rows", "items"})
        if has_series and (has_categories or "options" in keys or "data" in keys or has_record_source):
            output.append({"key": f"chart-model-{len(output)+1}", "title": str(value.get("title") or value.get("name") or context), "type": "config", "library": _chart_library(json.dumps(value, default=str)[:10000]), "url": page_url, "state": state, "model": value})
            if len(output) >= 100:
                return
        for key, child in value.items():
            _find_chart_models(child, output, page_url=page_url, state=state, context=str(key), depth=depth + 1)
    elif isinstance(value, list):
        for i, child in enumerate(value[:1000]):
            _find_chart_models(child, output, page_url=page_url, state=state, context=f"{context}[{i}]", depth=depth + 1)


def _chart_library(text: str) -> str:
    low = text.lower()
    for token, label in (("highcharts", "Highcharts"), ("echarts", "ECharts"), ("plotly", "Plotly"), ("apexcharts", "ApexCharts"), ("chart.js", "Chart.js")):
        if token in low:
            return label
    return "unknown"


def _extract_links(soup: BeautifulSoup, *, page_url: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_suffixes = {".pdf", ".csv", ".xlsx", ".xls", ".json", ".xml", ".zip"}
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page_url, str(anchor.get("href") or "").strip())
        parsed = urlparse(href)
        suffix = PurePosixPath(parsed.path.lower()).suffix
        title = clean_text(anchor.get_text(" ", strip=True) or anchor.get("title") or anchor.get("aria-label") or "")
        if href in seen or parsed.scheme not in {"http", "https"}:
            continue
        if suffix not in allowed_suffixes and not re.search(r"(report|download|export|data|history|price|benchmark|monthly|annual|dashboard)", f"{parsed.path} {title}", re.I):
            continue
        seen.add(href)
        output.append({"url": href, "title": title or PurePosixPath(parsed.path).name or href, "suffix": suffix, "page_url": page_url})
        if len(output) >= 300:
            break
    return output


def extract_document(html: str, *, page_url: str, method: str, state: str = "initial", content_type: str = "text/html", status_code: int | None = None) -> dict[str, Any]:
    stored_html = str(html or "")
    html_truncated = len(stored_html) > MAX_HTML_CHARS
    stored_html = stored_html[:MAX_HTML_CHARS]
    soup = BeautifulSoup(stored_html, "html.parser")
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    visible_text = clean_text(soup.get_text(" ", strip=True))[:MAX_TEXT_CHARS]
    tables = _extract_tables(soup, page_url=page_url, state=state)
    kpis = _extract_kpis(soup, page_url=page_url, state=state)
    filters = _extract_filters(soup, page_url=page_url, state=state)
    charts, embedded_json = _extract_charts_and_json(soup, page_url=page_url, state=state)
    # A JSON API response may itself be the source document (no HTML <script>).
    stripped = stored_html.lstrip()
    if "json" in str(content_type).lower() or stripped.startswith(("{", "[")):
        try:
            payload = json.loads(stored_html)
            embedded_json.insert(0, {"key": f"{state}-root-json", "url": page_url, "state": state, "context": "document-body", "data": payload})
            _find_chart_models(payload, charts, page_url=page_url, state=state, context="document-body")
        except Exception:
            pass
    links = _extract_links(soup, page_url=page_url)
    return {
        "document": {
            "url": page_url,
            "method": method,
            "state": state,
            "title": title,
            "content_type": content_type,
            "status_code": status_code,
            "captured_at": utc_now().isoformat(),
            "html": stored_html,
            "html_chars": len(html),
            "html_truncated": html_truncated,
            "text": visible_text,
        },
        "tables": tables,
        "kpi_cards": kpis,
        "filters": filters,
        "charts": charts,
        "embedded_json": embedded_json,
        "links": links,
        "warnings": ["HTML truncated at configured capture limit"] if html_truncated else [],
    }


def _decode_linked_resource(content: bytes, *, url: str, content_type: str, title: str) -> dict[str, Any]:
    parsed = urlparse(url)
    suffix = PurePosixPath(parsed.path.lower()).suffix
    base: dict[str, Any] = {"url": url, "title": title, "content_type": content_type, "size_bytes": len(content), "captured_at": utc_now().isoformat()}
    if suffix == ".pdf" or "pdf" in content_type.lower():
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            pages = []
            for number, page in enumerate(reader.pages[:250], start=1):
                text = clean_text(page.extract_text() or "")
                if text:
                    pages.append({"page": number, "text": text[:100_000]})
            base.update({"kind": "pdf", "page_count": len(reader.pages), "pages": pages, "text": "\n\n".join(p["text"] for p in pages)[:1_500_000]})
        except Exception as exc:
            base.update({"kind": "pdf", "parse_error": f"{type(exc).__name__}: {exc}"})
        return base
    if suffix == ".csv" or "csv" in content_type.lower():
        text = content.decode("utf-8-sig", errors="replace")[:MAX_HTML_CHARS]
        try:
            rows = list(csv.reader(io.StringIO(text)))[:MAX_ROWS_PER_TABLE]
        except Exception:
            rows = []
        base.update({"kind": "csv", "text": text, "rows": rows})
        return base
    if suffix in {".xlsx", ".xls"} or "spreadsheet" in content_type.lower() or "excel" in content_type.lower():
        try:
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            sheets = []
            for sheet in workbook.worksheets[:80]:
                rows = [[cell for cell in row] for row in sheet.iter_rows(values_only=True, max_row=MAX_ROWS_PER_TABLE)]
                sheets.append({"name": sheet.title, "rows": rows})
            base.update({"kind": "spreadsheet", "sheets": sheets})
        except Exception as exc:
            base.update({"kind": "spreadsheet", "parse_error": f"{type(exc).__name__}: {exc}"})
        return base
    text = content.decode("utf-8", errors="replace")[:MAX_HTML_CHARS]
    if suffix == ".json" or "json" in content_type.lower():
        try:
            base.update({"kind": "json", "data": json.loads(text)})
        except Exception:
            base.update({"kind": "json", "text": text})
        return base
    if suffix == ".xml" or "xml" in content_type.lower():
        base.update({"kind": "xml", "text": text})
    else:
        base.update({"kind": "text", "text": text})
    return base


def _same_host(base_url: str, candidate_url: str) -> bool:
    base_host = (urlparse(base_url).hostname or "").lower()
    candidate_host = (urlparse(candidate_url).hostname or "").lower()
    return bool(base_host and candidate_host and (candidate_host == base_host or candidate_host.endswith("." + base_host)))


async def fetch_linked_resources(
    links: list[dict[str, Any]],
    *,
    base_url: str,
    timeout: float = 20.0,
    limit: int = MAX_LINKED_RESOURCES,
    include_path_patterns: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    if int(limit) <= 0:
        return [], []
    patterns = [re.compile(p, re.I) for p in include_path_patterns]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in links:
        url = str(link.get("url") or "")
        if not url or url in seen or not _same_host(base_url, url):
            continue
        suffix = str(link.get("suffix") or PurePosixPath(urlparse(url).path.lower()).suffix)
        path = urlparse(url).path
        document_like = suffix in {".pdf", ".csv", ".xlsx", ".xls", ".json", ".xml", ".zip"}
        detail_like = any(pattern.search(path) for pattern in patterns)
        if not document_like and not detail_like:
            continue
        candidates.append(link)
        seen.add(url)
        if len(candidates) >= min(MAX_LINKED_RESOURCES, max(0, int(limit))):
            break

    semaphore = asyncio.Semaphore(4)
    warnings: list[str] = []

    async def one(link: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            url = str(link["url"])
            try:
                async with httpx.AsyncClient(headers=_headers(base_url), follow_redirects=True, timeout=timeout) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    if len(response.content) > MAX_LINK_BYTES:
                        return {"url": url, "title": link.get("title"), "status": "skipped", "reason": f"resource exceeds {MAX_LINK_BYTES} bytes", "size_bytes": len(response.content)}
                    content_type = response.headers.get("content-type", "")
                    suffix = PurePosixPath(urlparse(str(response.url)).path.lower()).suffix
                    if suffix in {".html", ".htm", ""} and "html" in content_type.lower():
                        # Commodity detail pages are captured as structured HTML,
                        # but unrelated linked report pages are not crawled without
                        # an explicit source-specific path pattern.
                        if not any(p.search(urlparse(url).path) for p in patterns):
                            return {"url": url, "title": link.get("title"), "status": "skipped", "reason": "HTML link not selected by the sector's detail-page rule"}
                        doc = extract_document(response.text, page_url=str(response.url), method="linked_http", state="linked-detail", content_type=content_type, status_code=response.status_code)
                        return {"url": str(response.url), "title": link.get("title"), "status": "success", "kind": "html", "document": doc["document"], "tables": doc["tables"], "kpi_cards": doc["kpi_cards"], "filters": doc["filters"], "charts": doc["charts"], "embedded_json": doc["embedded_json"], "text": doc["document"]["text"]}
                    resource = _decode_linked_resource(response.content, url=str(response.url), content_type=content_type, title=str(link.get("title") or ""))
                    resource["status"] = "success"
                    return resource
            except Exception as exc:
                return {"url": url, "title": link.get("title"), "status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    values = await asyncio.gather(*(one(link) for link in candidates))
    for value in values:
        if value.get("status") != "success":
            warnings.append(f"linked resource {value.get('url')}: {value.get('error') or value.get('reason') or 'not captured'}")
    return values, warnings


def _looks_like_access_challenge(source_data: dict[str, Any]) -> bool:
    """Detect common anti-bot/access-denied shells that otherwise look like HTTP 200 successes."""
    text_parts: list[str] = []
    for item in source_data.get("raw_documents") or []:
        if not isinstance(item, dict):
            continue
        text_parts.append(str(item.get("title") or ""))
        text_parts.append(str(item.get("text") or "")[:12_000])
    body = " ".join(text_parts).lower()
    markers = (
        "verify you are human", "checking your browser", "enable javascript and cookies to continue",
        "attention required! | cloudflare", "sorry, you have been blocked", "automated requests",
        "unusual traffic from your computer network", "complete the security check",
        "please verify you are a human", "captcha challenge", "access denied",
    )
    structured = any(source_data.get(key) for key in ("tables", "kpi_cards", "filters", "charts", "embedded_json", "network_resources", "linked_resources"))
    return (not structured) and any(marker in body for marker in markers)


async def capture_source(
    url: str,
    *,
    source_name: str,
    category: str,
    timeout_seconds: float = 45.0,
    browser_timeout_ms: int = 120_000,
    browser: bool = True,
    clicks: Iterable[str] = (),
    sweep_selects: Iterable[str] = (),
    follow_link_limit: int = 20,
    detail_link_patterns: Iterable[str] = (),
    auto_sweep_tabs: bool = True,
    settle_ms: int = 900,
    capture_runtime_charts: bool = False,
    reload_between_clicks: bool = False,
    sweep_selects_after_clicks: bool = False,
    sweep_selects_before_clicks: bool = True,
    apply_source_filters: Iterable[dict[str, Any]] = (),
    discover_source_filters_after_clicks: bool = False,
    network_action_allowlist: Iterable[str] = (),
    network_action_required: Iterable[str] = (),
    network_action_timeout_ms: int = 18_000,
    network_idle_timeout_ms: int | None = None,
    fetch_http_page: bool = True,
    capture_runtime_grid_full_load: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Capture all publicly exposed, relevant source structures and derive rows.

    Browser failure is non-fatal when the HTTP page was captured. Both failures
    are fatal because saving an empty success would hide a source outage.
    """
    started = utc_now()
    http_info: dict[str, Any] = {"status": "not_attempted"}
    browser_info: dict[str, Any] = {"status": "not_requested"}
    source_data: dict[str, Any] = {
        "capture_version": 2,
        "source_url": url,
        "source_name": source_name,
        "category": category,
        "started_at": started.isoformat(),
        "captured_at": None,
        "fetch": {},
        "raw_documents": [],
        "tables": [],
        "kpi_cards": [],
        "filters": [],
        "charts": [],
        "embedded_json": [],
        "network_resources": [],
        # Complete AJAX partials are kept separately from parsed page documents.
        # Each entry has raw HTML, inline scripts, chart target IDs and transport metadata.
        "network_fragments": [],
        "runtime_chart_artifacts": [],
        "runtime_chart_diagnostics": [],
        "runtime_grid_artifacts": [],
        "runtime_grid_diagnostics": [],
        "links": [],
        "linked_resources": [],
        "capture_completeness": {"warnings": [], "truncations": [], "counts": {}},
        "collection_policy": {
            "mode": "public_source_exhaustive_best_effort",
            "preserve_raw_documents": True,
            "preserve_network_payloads": True,
            "preserve_embedded_json": True,
            "preserve_tables_kpis_charts_filters": True,
            "preserve_source_urls": True,
            "monthly_snapshot_enabled": True,
            "synthetic_values_allowed": False,
        },
    }
    links: list[dict[str, Any]] = []
    http_html = ""
    logger.info("SOURCE_CAPTURE_START source=%s category=%s url=%s browser=%s timeout=%.1fs", source_name, category, url, browser, timeout_seconds)
    try:
        if not fetch_http_page:
            http_info = {"status": "skipped", "reason": "browser-first source capture"}
        else:
            logger.info("SOURCE_HTTP_FETCH_START source=%s url=%s", source_name, url)
            response = await fetch_http(url, timeout=timeout_seconds)
            http_info = {"status": "success", "status_code": response["status_code"], "content_type": response["content_type"], "final_url": response["url"], "body_bytes": response["body_bytes"], "headers": response["headers"]}
            http_html = str(response.get("html") or "")
            logger.info("SOURCE_HTTP_FETCH_COMPLETE source=%s status=%s bytes=%s content_type=%s elapsed_seconds=%.1f", source_name, response.get("status_code"), response.get("body_bytes"), response.get("content_type"), (utc_now() - started).total_seconds())
            if http_html:
                doc = extract_document(http_html, page_url=response["url"], method="http", state="http", content_type=response.get("content_type", ""), status_code=response.get("status_code"))
                source_data["raw_documents"].append(doc["document"])
                for name in ("tables", "kpi_cards", "filters", "charts", "embedded_json", "links"):
                    source_data[name].extend(doc[name])
                source_data["capture_completeness"]["warnings"].extend(doc["warnings"])
                links.extend(doc["links"])
    except Exception as exc:
        http_info = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        source_data["capture_completeness"]["warnings"].append(f"HTTP capture failed: {type(exc).__name__}: {exc}")

    if browser:
        try:
            logger.info("SOURCE_BROWSER_CAPTURE_START source=%s url=%s timeout_ms=%d", source_name, url, browser_timeout_ms)
            rendered = await render_page_snapshot(url, timeout_ms=browser_timeout_ms, clicks=clicks, sweep_selects=sweep_selects,
                                                  capture_network=True, auto_sweep_tabs=auto_sweep_tabs, settle_ms=settle_ms,
                                                  capture_runtime_charts=capture_runtime_charts,
                                                  reload_between_clicks=reload_between_clicks,
                                                  sweep_selects_after_clicks=sweep_selects_after_clicks,
                                                  sweep_selects_before_clicks=sweep_selects_before_clicks,
                                                  apply_source_filters=apply_source_filters,
                                                  discover_source_filters_after_clicks=discover_source_filters_after_clicks,
                                                  network_action_allowlist=network_action_allowlist,
                                                  network_action_required=network_action_required,
                                                  network_action_timeout_ms=network_action_timeout_ms,
                                                  network_idle_timeout_ms=network_idle_timeout_ms,
                                                  capture_runtime_grid_full_load=capture_runtime_grid_full_load)
            browser_info = {"status": "success", "final_url": rendered.get("final_url"), "snapshot_count": len(rendered.get("snapshots") or []), "network_resource_count": len(rendered.get("responses") or []), "network_response_chars": rendered.get("network_response_chars", 0), "captured_network_actions": rendered.get("captured_network_actions") or [], "network_failures": rendered.get("network_failures") or [], "errors": rendered.get("browser_errors") or [], "discovered_tab_labels": rendered.get("discovered_tab_labels") or [], "select_sweep_audit": rendered.get("select_sweep_audit") or [], "click_audit": rendered.get("click_audit") or [], "interactive_filters_by_state": rendered.get("interactive_filters_by_state") or {}, "source_filter_audit": rendered.get("source_filter_audit") or [], "runtime_chart_counts_by_state": {str(item.get("label") or "rendered"): int(item.get("runtime_chart_count") or len(item.get("runtime_charts") or [])) for item in (rendered.get("snapshots") or [])}, "runtime_chart_diagnostics_by_state": {str(item.get("label") or "rendered"): item.get("runtime_chart_diagnostics") or [] for item in (rendered.get("snapshots") or [])}}
            logger.info("SOURCE_BROWSER_CAPTURE_COMPLETE source=%s snapshots=%d network_items=%d network_chars=%d elapsed_seconds=%.1f", source_name, browser_info["snapshot_count"], browser_info["network_resource_count"], browser_info["network_response_chars"], (utc_now() - started).total_seconds())
            snapshots = rendered.get("snapshots") or [{"label": "rendered", "url": rendered.get("final_url") or url, "html": rendered.get("html") or ""}]
            for browser_error in rendered.get("browser_errors") or []:
                message = f"Browser capture warning: {browser_error}"
                if message not in source_data["capture_completeness"]["warnings"]:
                    source_data["capture_completeness"]["warnings"].append(message)
            seen_state_html: set[tuple[str, str]] = set()
            for item in snapshots:
                state = str(item.get("label") or "rendered")
                page_url = str(item.get("url") or rendered.get("final_url") or url)
                html = str(item.get("html") or "")
                for diagnostic in item.get("runtime_chart_diagnostics") or []:
                    if isinstance(diagnostic, dict):
                        source_data["runtime_chart_diagnostics"].append({
                            **diagnostic,
                            "snapshot_state": state,
                            "page_url": page_url,
                            "snapshot_year": item.get("selected_year"),
                        })
                for chart_index, chart_payload in enumerate(item.get("runtime_charts") or []):
                    if isinstance(chart_payload, dict):
                        artifact = {
                            **chart_payload,
                            "snapshot_state": state,
                            "page_url": page_url,
                            "snapshot_year": item.get("selected_year"),
                            "captured_at": item.get("captured_at"),
                        }
                        source_data["runtime_chart_artifacts"].append(artifact)
                        source_data["embedded_json"].append({
                            "key": f"runtime-chart-{state}-{chart_index + 1}",
                            "url": page_url,
                            "state": state,
                            "context": "runtime-chart",
                            "data": artifact,
                        })
                for diagnostic in item.get("runtime_grid_diagnostics") or []:
                    if isinstance(diagnostic, dict):
                        source_data["runtime_grid_diagnostics"].append({
                            **diagnostic, "snapshot_state": state, "page_url": page_url,
                        })
                for grid_index, grid_payload in enumerate(item.get("runtime_grids") or []):
                    if not isinstance(grid_payload, dict):
                        continue
                    artifact = {
                        **grid_payload,
                        "snapshot_state": state,
                        "page_url": page_url,
                        "snapshot_year": item.get("selected_year"),
                        "captured_at": item.get("captured_at"),
                    }
                    source_data["runtime_grid_artifacts"].append(artifact)
                if item.get("selected_year") or item.get("selected_years"):
                    selected_years = item.get("selected_years") or ([item.get("selected_year")] if item.get("selected_year") else [])
                    source_data["embedded_json"].append({
                        "key": f"runtime-page-context-{state}",
                        "url": page_url,
                        "state": state,
                        "data": {"selected_year": item.get("selected_year"), "selected_years": selected_years},
                    })
                signature = (state, html[:500])
                if not html or signature in seen_state_html:
                    continue
                seen_state_html.add(signature)
                doc = extract_document(html, page_url=page_url, method="browser", state=state, content_type="text/html")
                if item.get("html_truncated"):
                    doc["document"]["html_truncated"] = True
                source_data["raw_documents"].append(doc["document"])
                for name in ("tables", "kpi_cards", "filters", "charts", "embedded_json", "links"):
                    source_data[name].extend(doc[name])
                source_data["capture_completeness"]["warnings"].extend(doc["warnings"])
                links.extend(doc["links"])
            network_total = 0
            network_html_fragments = 0
            for item in rendered.get("responses") or []:
                body = str(item.get("body") or "")
                network_total += len(body)
                content = body.strip()
                parsed_json: Any = _decode_json_response_body(content)
                resource_state = str(item.get("state") or "network")
                resource_url = str(item.get("url") or "")
                resource_type = str(item.get("resource_type") or "").casefold()
                resource = {
                    "url": resource_url, "state": resource_state,
                    "method": item.get("method") or "GET",
                    "content_type": item.get("content_type"), "resource_type": item.get("resource_type"),
                    "status_code": item.get("status_code"),
                    "request_headers": item.get("request_headers") or {},
                    "response_headers": item.get("response_headers") or {},
                    "request_payload_fields": item.get("request_payload_fields") or {},
                    "endpoint_action": item.get("endpoint_action"),
                    "request_id": item.get("request_id"),
                    "received_at": item.get("received_at"),
                    "body": body, "body_chars": item.get("body_chars"), "truncated": item.get("truncated", False),
                }
                if isinstance(parsed_json, (dict, list)):
                    resource["data"] = parsed_json
                    source_data["embedded_json"].append({"key": f"network-json-{len(source_data['embedded_json'])+1}", "url": resource_url, "state": resource_state, "data": parsed_json})
                source_data["network_resources"].append(resource)

                # NISE's KPI clicks call MVC endpoints that may return a partial
                # view directly OR JSON that wraps HTML in `d`, `result`, `data`,
                # `html`, etc. The main page can contain only empty chart nodes;
                # parse each returned fragment as its own document so the same
                # table, KPI, chart-script and embedded-JSON extractors are used.
                nested_fragments = _embedded_html_fragments(parsed_json) if parsed_json is not None else []
                # Require an HTML fragment at the beginning of the response.
                # Searching anywhere in a JS bundle for "<div" caused minified
                # assets such as jquery-ui.js to be misclassified as HTML and
                # inflated document count/normalization time.
                html_prefix = content.lstrip("\ufeff\r\n\t ")[:4000]
                raw_html_fragment = bool(re.match(
                    r"^(?:<!doctype\s+html\b|<!--|<(?:html|head|body|div|section|article|table|script|svg|canvas|form|main|ul|ol|p|span|tr|tbody|thead|select|button)\b)",
                    html_prefix, re.I,
                ))
                if raw_html_fragment:
                    nested_fragments.insert(0, body)
                seen_fragment_signatures: set[str] = set()
                for fragment_index, fragment_body in enumerate(nested_fragments[:20], start=1):
                    signature = fragment_body[:2000]
                    if signature in seen_fragment_signatures or len(fragment_body) > MAX_HTML_CHARS:
                        continue
                    seen_fragment_signatures.add(signature)
                    try:
                        if resource_type in {"xhr", "fetch"}:
                            source_data["network_fragments"].append(_network_fragment_artifact(
                                fragment_body,
                                url=resource_url,
                                state=resource_state,
                                method=str(resource.get("method") or "GET"),
                                status_code=resource.get("status_code"),
                                content_type=str(resource.get("content_type") or "text/html"),
                                resource_type=resource_type,
                                request_headers=resource.get("request_headers") or {},
                                response_headers=resource.get("response_headers") or {},
                                request_payload_fields=resource.get("request_payload_fields") or {},
                                endpoint_action=resource.get("endpoint_action"),
                                request_id=resource.get("request_id"),
                                received_at=resource.get("received_at"),
                            ))
                        fragment = extract_document(
                            fragment_body,
                            page_url=resource_url or url,
                            method="browser-network-fragment",
                            state=resource_state,
                            content_type="text/html",
                            status_code=item.get("status_code"),
                        )
                        fragment_doc = fragment["document"]
                        fragment_doc["network_resource"] = True
                        fragment_doc["network_resource_type"] = resource_type
                        source_data["raw_documents"].append(fragment_doc)
                        for name in ("tables", "kpi_cards", "filters", "charts", "embedded_json"):
                            source_data[name].extend(fragment.get(name) or [])
                        source_data["capture_completeness"]["warnings"].extend(fragment.get("warnings") or [])
                        network_html_fragments += 1
                        logger.info(
                            "SOURCE_NETWORK_HTML_FRAGMENT_PARSED source=%s state=%s path=%s fragment=%d bytes=%d tables=%d charts=%d embedded_json=%d",
                            source_name, resource_state, urlparse(resource_url).path[:180], fragment_index, len(fragment_body),
                            len(fragment.get("tables") or []), len(fragment.get("charts") or []), len(fragment.get("embedded_json") or []),
                        )
                    except Exception as fragment_exc:
                        logger.warning(
                            "SOURCE_NETWORK_HTML_FRAGMENT_PARSE_FAILED source=%s state=%s url=%s error=%s: %s",
                            source_name, resource_state, resource_url, type(fragment_exc).__name__, str(fragment_exc)[:240],
                        )

                # Some chart endpoints return JavaScript snippets rather than
                # HTML/JSON; retain them in network_resources for the sector
                # normalizer's chart-config parser.
                if item.get("truncated"):
                    source_data["capture_completeness"]["truncations"].append(f"Network response truncated: {item.get('url')}")
            browser_info["html_fragment_count"] = network_html_fragments
            logger.info(
                "SOURCE_NETWORK_EXTRACTION_COMPLETE source=%s network_items=%d html_fragments=%d ajax_artifacts=%d inline_scripts=%d chart_targets=%d documents=%d charts=%d embedded_json=%d",
                source_name, len(rendered.get("responses") or []), network_html_fragments,
                len(source_data["network_fragments"]),
                sum(len(item.get("inline_scripts") or []) for item in source_data["network_fragments"]),
                sum(len(item.get("chart_target_ids") or []) for item in source_data["network_fragments"]),
                len(source_data["raw_documents"]), len(source_data["charts"]), len(source_data["embedded_json"]),
            )
            if network_total >= MAX_NETWORK_TOTAL_CHARS:
                source_data["capture_completeness"]["truncations"].append("Network response capture reached the configured total character budget")
            links.extend({"url": item.get("url"), "title": "Network resource", "suffix": PurePosixPath(urlparse(str(item.get("url") or "")).path).suffix, "page_url": url} for item in rendered.get("responses") or [])
        except Exception as exc:
            browser_info = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            source_data["capture_completeness"]["warnings"].append(f"Browser capture failed: {type(exc).__name__}: {exc}")

    source_data["fetch"] = {"http": http_info, "browser": browser_info}
    # De-duplicate links but preserve distinct view states for tables and KPIs.
    unique_links: dict[str, dict[str, Any]] = {}
    for item in links:
        link_url = str(item.get("url") or "")
        if link_url and link_url not in unique_links:
            unique_links[link_url] = item
    source_data["links"] = list(unique_links.values())
    linked, link_warnings = await fetch_linked_resources(
        source_data["links"],
        base_url=url,
        timeout=min(max(timeout_seconds, 5), 30),
        limit=follow_link_limit,
        include_path_patterns=detail_link_patterns,
    )
    source_data["linked_resources"] = linked
    source_data["capture_completeness"]["warnings"].extend(link_warnings)
    # Linked HTML detail pages can hold the historical rows absent from the
    # landing page. Keep the complete linked document as well as exposing its
    # parsed structures in the common tables/KPI/filter/chart collections.
    for resource in linked:
        if resource.get("kind") != "html" or resource.get("status") != "success":
            continue
        document = resource.get("document")
        if isinstance(document, dict):
            source_data["raw_documents"].append(document)
        for field in ("tables", "kpi_cards", "filters", "charts", "embedded_json"):
            source_data[field].extend(resource.get(field) or [])
    _dedupe_captured_structures(source_data)
    source_data["captured_at"] = utc_now().isoformat()
    source_data["capture_completeness"]["counts"] = {
        "documents": len(source_data["raw_documents"]),
        "tables": len(source_data["tables"]),
        "kpi_cards": len(source_data["kpi_cards"]),
        "filters": len(source_data["filters"]),
        "charts": len(source_data["charts"]),
        "embedded_json": len(source_data["embedded_json"]),
        "network_resources": len(source_data["network_resources"]),
        "network_fragments": len(source_data["network_fragments"]),
        "runtime_chart_artifacts": len(source_data["runtime_chart_artifacts"]),
        "runtime_chart_diagnostics": len(source_data["runtime_chart_diagnostics"]),
        "linked_resources": len(source_data["linked_resources"]),
    }
    capture_warnings = source_data["capture_completeness"].get("warnings") or []
    capture_truncations = source_data["capture_completeness"].get("truncations") or []
    if http_info.get("status") != "success" and browser_info.get("status") != "success":
        capture_status = "failed"
    elif capture_truncations or capture_warnings or browser_info.get("status") == "failed":
        capture_status = "partial"
    else:
        capture_status = "captured"
    source_data["capture_completeness"]["status"] = capture_status
    if http_info.get("status") == "failed" and browser_info.get("status") == "failed":
        raise SourceUnavailable(f"Both HTTP and browser capture failed for {url}: {http_info.get('error')} | {browser_info.get('error')}")
    if _looks_like_access_challenge(source_data):
        message = "Source returned an access challenge / anti-bot page and exposed no structured market data."
        source_data["capture_completeness"]["status"] = "blocked"
        source_data["capture_completeness"]["warnings"].append(message)
        raise SourceUnavailable(f"{message} URL={url}; HTTP={http_info.get('status_code')} browser={browser_info.get('status')}")
    observations = records_from_capture(source_data, source_name=source_name, category=category)
    source_data["capture_completeness"]["numeric_observation_count"] = len(observations)
    source_data["capture_completeness"]["source_url"] = url
    source_data["capture_completeness"]["public_source_only"] = True
    if not observations:
        source_data["capture_completeness"]["warnings"].append(
            "No numeric observations normalized from this capture; inspect raw documents, embedded JSON, access status and parser coverage."
        )
        source_data["capture_completeness"]["status"] = "empty" if source_data["capture_completeness"].get("status") != "blocked" else "blocked"
    return source_data, observations


def _dedupe_captured_structures(data: dict[str, Any]) -> None:
    for field in ("tables", "kpi_cards", "filters", "charts", "embedded_json"):
        found: dict[str, dict[str, Any]] = {}
        for item in data.get(field) or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or json.dumps(item, sort_keys=True, default=str)[:300])
            # Same content can appear in server HTML and rendered HTML. Retain
            # separate table states when their state labels differ.
            if field == "tables":
                key = f"{key}:{item.get('state')}:{item.get('url')}"
            found[key] = item
        data[field] = list(found.values())
    # Network bodies can contain secrets/tokens; remove common authorization
    # strings without modifying numeric source data.
    for item in data.get("network_resources") or []:
        item.pop("set-cookie", None)


def _period_from_row(row: dict[str, Any], fallback: datetime) -> tuple[datetime, str | None]:
    for key, value in row.items():
        low = str(key).lower()
        if not any(token in low for token in ("date", "period", "month", "year", "quarter", "time", "as of", "fy")):
            continue
        parsed = parse_date(value, reference=fallback)
        if parsed:
            label = "monthly" if "month" in low else "quarterly" if "quarter" in low else "yearly" if "year" in low or "fy" in low else "daily"
            return parsed, label
        # Public dashboards frequently expose year-only cells (e.g. "Year Wise: 2026").
        text = clean_text(value)
        year_match = re.fullmatch(r"(?:FY[- ]?)?(20\d{2})(?:[-/]\d{2})?", text, re.I)
        if year_match and ("year" in low or "fy" in low):
            return datetime(int(year_match.group(1)), 1, 1, tzinfo=timezone.utc), "yearly"
        quarter_match = re.search(r"\bQ([1-4])\s*[-/]?\s*(20\d{2})\b", text, re.I)
        if quarter_match:
            q, year = int(quarter_match.group(1)), int(quarter_match.group(2))
            return datetime(year, (q - 1) * 3 + 1, 1, tzinfo=timezone.utc), "quarterly"
    return fallback, None


def _row_label(row: dict[str, Any], numeric_column: str) -> str:
    ignore = {numeric_column, "_row_number"}
    for key, value in row.items():
        if key in ignore or value in (None, ""):
            continue
        if parse_number(value) is None and not parse_date(value):
            text = clean_text(value)
            if text and len(text) <= 180:
                return text
    return ""


def records_from_capture(source_data: dict[str, Any], *, source_name: str, category: str) -> list[dict[str, Any]]:
    """Derive normalized observations from captured tables/KPI/chart JSON.

    Source-specific parsers may enrich this set in their own sector module.
    These rows are a convenience for charts/filters; the raw capture is retained
    separately even when no value can safely be normalized.
    """
    output: list[dict[str, Any]] = []
    now = daily_stamp()

    def valid_metric_label(value: Any) -> bool:
        label = clean_text(value)
        low = label.casefold()
        if not label or not re.search(r"[a-zA-Z]", label):
            return False
        return not any(token in low for token in (
            "publish at", "archive - reports", "load in batches", "click here", "read more",
            "view more", "subscribe", "subscribers", "search maker", "select category",
            "select permit", "filter ", "javascript", "chart script", "svg chart",
        ))

    def add(name: str, value: Any, unit: str | None, recorded: datetime, dimensions: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None, source_url: str | None = None) -> None:
        numeric = parse_number(value)
        if not valid_metric_label(name) or numeric is None:
            return
        output.append({
            "source_name": source_name, "category": category, "metric_name": clean_text(name),
            "metric_value": numeric, "unit": unit, "recorded_at": recorded,
            "dimensions": dimensions or {}, "source_url": source_url or source_data.get("source_url"),
            "source_metadata": metadata or {},
        })

    for card in source_data.get("kpi_cards") or []:
        if not valid_metric_label(card.get("label") or card.get("key")):
            continue
        value = card.get("value")
        unit_match = re.search(r"(GWh|MWh|registrations?|vehicles?|units?|MW|GW|INR|USD|Rs\.?|₹|%)(?![A-Za-z])", str(card.get("display_value") or card.get("text") or ""), re.I)
        unit = unit_match.group(1) if unit_match else None
        add(str(card.get("label") or card.get("key") or "KPI"), value, unit, now, {"source_state": card.get("state")} if card.get("state") else {}, {"kind": "kpi_card", "display_value": card.get("display_value")}, card.get("url"))

    month_order = {name: index for index, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), start=1)}
    for table in source_data.get("tables") or []:
        rows = table.get("rows") or []
        cols = table.get("columns") or []
        title_low = clean_text(table.get("title")).casefold()
        cols_low = " ".join(str(col).casefold() for col in cols)
        if not rows or not cols or not valid_metric_label(table.get("title") or ""):
            continue
        if "publish at" in cols_low or "archive - reports" in title_low:
            continue
        # Wide monthly tables (common on PPAC) must become date-indexed records,
        # not 12 unrelated metrics stamped with the scrape date.
        month_columns = [(col, month_order[next((m for m in month_order if m.casefold() == str(col).strip().casefold()), "")]) for col in cols if str(col).strip().casefold() in {m.casefold() for m in month_order}]
        if len(month_columns) >= 4 and any(token in cols_low for token in ("product", "commodity", "fuel", "consumption")):
            fiscal_start = fallback_year = now.year if now.month >= 4 else now.year - 1
            for row in rows:
                if not isinstance(row, dict):
                    continue
                label_col = next((col for col in cols if any(token in str(col).casefold() for token in ("product", "commodity", "fuel", "name", "item"))), cols[0])
                item_label = clean_text(row.get(label_col))
                if not valid_metric_label(item_label):
                    continue
                year_hint = next((int(m.group(1)) for v in row.values() if (m := re.search(r"FY\s*(20\d{2})", str(v), re.I))), fiscal_start)
                for month_col, month_num in month_columns:
                    numeric = parse_number(row.get(month_col))
                    if numeric is None:
                        continue
                    year = year_hint if month_num >= 4 else year_hint + 1
                    recorded = datetime(year, month_num, 1, tzinfo=timezone.utc)
                    metric = f"{clean_text(table.get('title'))} | {item_label}"
                    unit = "TMT" if "metric tonne" in title_low or "000 metric ton" in cols_low else None
                    add(metric, numeric, unit, recorded, {"product": item_label, "timeline": "monthly", "source_table": table.get("key")}, {"kind": "monthly_table", "period_type": "monthly", "table_title": table.get("title")}, table.get("url"))
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_date, period_kind = _period_from_row(row, now)
            numeric_cols = [(str(key), value) for key, value in row.items() if key != "_row_number" and parse_number(value) is not None]
            for col_name, value in numeric_cols:
                label = _row_label(row, col_name)
                # A date column is a period, not a metric in its own right.
                col_low = col_name.lower()
                period_value = parse_date(value, reference=now)
                if any(x in col_low for x in ("date", "period", "month", "year", "quarter", "time", "fy")):
                    if period_value or ("year" in col_low and re.fullmatch(r"(?:FY[- ]?)?20\d{2}(?:[-/]\d{2})?", clean_text(value), re.I)):
                        continue
                if len(numeric_cols) == 1 and label and col_name.lower() in {"value", "current", "price", "count", "total", "amount"}:
                    metric_name = f"{table.get('title') or 'Source table'} | {label}"
                else:
                    metric_name = " | ".join(x for x in (table.get("title") or "Source table", label, col_name) if x)
                dimensions = {"source_table": table.get("key"), "source_state": table.get("state")}
                if label:
                    dimensions["source_label"] = label
                add(metric_name, value, col_name if re.search(r"%|price|amount|revenue|mw|gw|gwh|mwh|count|registration|sales|stock|volume|value|units", col_name, re.I) else None, row_date, dimensions, {"kind": "table", "table_title": table.get("title"), "row": row, "period_type": period_kind}, table.get("url"))

    # Chart data are not always materialized as HTML tables. Walk common JSON
    # shapes and extract explicitly dated/named numeric data only.
    seen_obj: set[int] = set()
    def walk_json(value: Any, path: tuple[str, ...] = (), depth: int = 0) -> None:
        if depth > 12 or len(output) >= 150_000 or id(value) in seen_obj:
            return
        if isinstance(value, (dict, list)):
            seen_obj.add(id(value))
        if isinstance(value, dict):
            date_key = next((k for k in value if any(tok in str(k).lower() for tok in ("date", "period", "month", "year", "time", "quarter")) and parse_date(value[k], reference=now)), None)
            value_key = next((k for k in value if str(k).lower() in {"value", "price", "count", "total", "amount", "sales", "registrations", "mw", "gw", "gwh", "mwh", "close", "y", "actual", "current"} and parse_number(value[k]) is not None), None)
            if date_key and value_key:
                name = next((clean_text(value[k]) for k in ("metric_name", "metric", "name", "label", "title", "commodity", "series_name") if k in value and clean_text(value[k]) and parse_number(value[k]) is None), " | ".join(path[-3:]) or "Source series")
                period, kind = _period_from_row(value, now)
                add(name, value[value_key], value.get("unit") or value.get("currency") or None, period, {}, {"kind": "json_series", "json_path": list(path), "period_type": kind}, source_data.get("source_url"))
            for key, child in value.items():
                # Handle labels/categories + list of parallel series, e.g. Chart.js.
                if str(key).lower() in {"data", "series", "datasets", "traces", "observations", "history", "results", "items", "rows", "values"}:
                    walk_json(child, path + (str(key),), depth + 1)
                elif isinstance(child, (dict, list)):
                    walk_json(child, path + (str(key),), depth + 1)
        elif isinstance(value, list):
            for i, child in enumerate(value[:20_000]):
                if isinstance(child, (dict, list)):
                    walk_json(child, path + (str(i),), depth + 1)

    json_items = list(source_data.get("embedded_json") or [])
    json_items += [item for item in (source_data.get("network_resources") or []) if isinstance(item.get("data"), (dict, list))]
    for item in json_items:
        payload = item.get("data")
        if payload is not None:
            walk_json(payload, (str(item.get("key") or item.get("url") or "json"),))

    # Chart configurations frequently store dates/categories separately from the
    # numeric series (Highcharts, Chart.js, ApexCharts). Convert only explicitly
    # dated points; never assign the scrape date to an undated historical value.
    chart_seen: set[int] = set()
    def walk_parallel_chart(value: Any, path: tuple[str, ...] = (), depth: int = 0) -> None:
        if depth > 12 or not isinstance(value, (dict, list)) or id(value) in chart_seen:
            return
        chart_seen.add(id(value))
        if isinstance(value, dict):
            categories: list[Any] = []
            xaxis = value.get("xAxis") or value.get("xaxis") or value.get("x_axis")
            if isinstance(xaxis, dict):
                raw_categories = xaxis.get("categories") or xaxis.get("data")
                if isinstance(raw_categories, list):
                    categories = raw_categories
            for key in ("categories", "labels", "dates", "periods", "timestamps"):
                if not categories and isinstance(value.get(key), list):
                    categories = value[key]
            series = value.get("series") or value.get("datasets") or value.get("traces")
            if isinstance(series, list):
                for series_index, item in enumerate(series[:500]):
                    if not isinstance(item, dict):
                        continue
                    series_name = clean_text(item.get("name") or item.get("label") or item.get("title") or item.get("key") or "")
                    data = item.get("data") or item.get("values") or item.get("y")
                    if not series_name or not isinstance(data, list):
                        continue
                    emitted = 0
                    for point_index, point in enumerate(data[:20_000]):
                        period_value = None
                        numeric_value = None
                        if isinstance(point, dict):
                            period_value = next((point[k] for k in ("x", "date", "datetime", "period", "timestamp", "time") if k in point), None)
                            numeric_value = next((point[k] for k in ("y", "value", "price", "count", "total", "amount") if k in point and parse_number(point[k]) is not None), None)
                        elif isinstance(point, (list, tuple)) and len(point) >= 2:
                            period_value, numeric_value = point[0], point[1]
                        elif point_index < len(categories):
                            period_value, numeric_value = categories[point_index], point
                        period = parse_date(period_value, reference=now)
                        number = parse_number(numeric_value)
                        if period is None or number is None:
                            continue
                        add(
                            series_name, number, item.get("unit") or value.get("unit"), period,
                            {"timeline": "monthly" if period.day == 1 else "dated", "chart_series": series_name},
                            {"kind": "parallel_chart_series", "json_path": list(path), "series_index": series_index, "point_index": point_index, "period_type": "month" if period.day == 1 else "date"},
                            next((source.get("url") for source in json_items if source.get("data") is value), source_data.get("source_url")),
                        )
                        emitted += 1
                    # An empty series is still preserved in raw chart JSON, but it
                    # must not be materialized as a fabricated observation.
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    walk_parallel_chart(child, path + (str(key),), depth + 1)
        else:
            for index, child in enumerate(value[:20_000]):
                if isinstance(child, (dict, list)):
                    walk_parallel_chart(child, path + (str(index),), depth + 1)

    for item in json_items:
        payload = item.get("data")
        if payload is not None:
            walk_parallel_chart(payload, (str(item.get("key") or item.get("url") or "json"),))

    # Retain all numeric observations without duplicates; source state and table
    # name remain part of identity to avoid conflating similar tables.
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in output:
        dt = row["recorded_at"]
        iso = dt.isoformat() if isinstance(dt, datetime) else str(dt)
        dims = json.dumps(row.get("dimensions") or {}, sort_keys=True, default=str)
        key = (row["metric_name"], iso, row["metric_value"], row.get("unit"), dims)
        unique[key] = row
    return list(unique.values())


def merge_observations(*groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for group in groups:
        for row in group:
            if not isinstance(row, dict):
                continue
            try:
                value = float(row.get("metric_value"))
                recorded = parse_date(row.get("recorded_at")) or daily_stamp()
            except (TypeError, ValueError):
                continue
            name = clean_text(row.get("metric_name"))
            if not name:
                continue
            row = dict(row)
            row["metric_value"] = value
            row["recorded_at"] = recorded
            key = (name, recorded.isoformat(), value, row.get("unit"), json.dumps(row.get("dimensions") or {}, sort_keys=True, default=str))
            unique[key] = row
    return list(unique.values())


def compact_capture(source_data: dict[str, Any], *, rows_per_table: int = 250) -> dict[str, Any]:
    """Return a bounded, raw-HTML-free payload appropriate for compact daily dashboard projections."""
    tables = []
    for table in source_data.get("tables") or []:
        rows = table.get("rows") or []
        tables.append({**table, "rows": rows[:rows_per_table], "preview_truncated": len(rows) > rows_per_table})
    resources = []
    for item in source_data.get("linked_resources") or []:
        resource = {k: v for k, v in item.items() if k not in {"text", "pages", "sheets", "document", "data", "rows"}}
        resources.append(resource)
    return {
        "source_url": source_data.get("source_url"),
        "source_name": source_data.get("source_name"),
        "captured_at": source_data.get("captured_at"),
        "fetch": source_data.get("fetch"),
        "capture_completeness": source_data.get("capture_completeness"),
        "tables": tables,
        "kpi_cards": source_data.get("kpi_cards") or [],
        "filters": source_data.get("filters") or [],
        "charts": source_data.get("charts") or [],
        "embedded_json_count": len(source_data.get("embedded_json") or []),
        "network_resources": [{k: v for k, v in item.items() if k not in {"body", "data"}} for item in (source_data.get("network_resources") or [])],
        "linked_resources": resources,
    }


# Shared generic helpers migrated from the previous collector dispatcher.
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _daily_stamp() -> datetime:
    # Daily snapshots are stored at a stable UTC boundary so reruns the same day
    # overwrite rather than create duplicate observations.
    now = _now_utc()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)

def _month_start(year: int, month: int) -> datetime:
    return datetime(year=year, month=month, day=1, tzinfo=timezone.utc)

def _parse_date_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        if 500_000_000 <= number <= 4_000_000_000:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        return None
    text = str(value).strip()
    if not text:
        return None
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in (
        "%d, %b %Y", "%d %b %Y", "%d-%b-%Y", "%d/%m/%Y",
        "%Y-%m-%d", "%Y/%m/%d", "%b %Y", "%B %Y",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    ym = _year_month(text)
    if ym:
        return _month_start(*ym)
    q = re.search(r"\bQ([1-4])\s*[-/]?\s*(20\d{2})\b", text, re.I)
    if q:
        return _quarter_end(int(q.group(2)), int(q.group(1)))
    return None

def _quarter_start_end_months(count: int = 2) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    current_q = ((now.month - 1) // 3) + 1
    current_year = now.year
    # Previous completed quarter is current_q-1. Two quarters means six months.
    end_q = current_q - 1
    end_y = current_year
    if end_q == 0:
        end_q = 4
        end_y -= 1
    start_q = end_q - (count - 1)
    start_y = end_y
    while start_q <= 0:
        start_q += 4
        start_y -= 1
    return _month_start(start_y, (start_q - 1) * 3 + 1), _quarter_end(end_y, end_q)

def _within_history(dt: datetime, *, months: int = 6) -> bool:
    start, _ = _quarter_start_end_months(2 if months <= 6 else max(2, (months + 2) // 3))
    return start <= dt <= _now_utc() + timedelta(days=2)

def _walk_json_history(
    obj: Any,
    *,
    aliases: tuple[str, ...] = (),
    history_months: int = 6,
) -> list[tuple[datetime, float]]:
    """Extract dated numeric observations from common API/chart JSON shapes.

    The six source sites use several chart libraries. Some return explicit
    {date,value} rows; others return Highcharts/Recharts-style labels + series.
    This helper intentionally accepts both without assuming a proprietary API.
    """
    found: list[tuple[datetime, float]] = []
    aliases_lower = tuple(a.lower() for a in aliases if a)

    def add_series(labels: list[Any], values: list[Any], hint: str = "") -> None:
        if not labels or not values or len(labels) != len(values):
            return
        for label, raw in zip(labels, values):
            dt = _parse_date_value(label)
            if dt is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if _within_history(dt, months=history_months):
                if aliases_lower and hint and not any(alias in hint.lower() for alias in aliases_lower):
                    continue
                found.append((dt, value))

    def visit(node: Any, hint: str = "") -> None:
        if isinstance(node, dict):
            lower = {str(k).lower(): k for k in node.keys()}

            # labels/categories + datasets/series is a very common chart shape.
            labels_key = next((lower[k] for k in ("labels", "categories", "xaxis") if k in lower), None)
            series_key = next((lower[k] for k in ("datasets", "series") if k in lower), None)
            if labels_key is not None and series_key is not None and isinstance(node.get(labels_key), list):
                labels = node.get(labels_key) or []
                series = node.get(series_key) or []
                if isinstance(series, list):
                    for series_item in series:
                        if isinstance(series_item, dict):
                            name = str(series_item.get("name") or series_item.get("label") or "")
                            values = series_item.get("data")
                            if isinstance(values, list):
                                add_series(labels, values, f"{hint} {name}")

            # {series:[{name, data:[[timestamp,value], ...]}]} / similar.
            if isinstance(node.get("series"), list):
                for series_item in node.get("series") or []:
                    if not isinstance(series_item, dict):
                        continue
                    name = str(series_item.get("name") or series_item.get("label") or "")
                    data = series_item.get("data")
                    if not isinstance(data, list):
                        continue
                    for point in data:
                        if isinstance(point, (list, tuple)) and len(point) >= 2:
                            dt = _parse_date_value(point[0])
                            try:
                                value = float(point[1])
                            except (TypeError, ValueError):
                                continue
                            if dt is not None and _within_history(dt, months=history_months):
                                if not aliases_lower or any(alias in f"{hint} {name}".lower() for alias in aliases_lower):
                                    found.append((dt, value))

            date_key = next((lower[k] for k in ("date", "datetime", "timestamp", "time", "recorded_at", "period", "x") if k in lower), None)
            value_key = next((lower[k] for k in ("value", "price", "index", "close", "y", "amount", "metric_value") if k in lower), None)
            if date_key is not None and value_key is not None:
                dt = _parse_date_value(node.get(date_key))
                raw = node.get(value_key)
                if dt is not None:
                    try:
                        val = float(raw)
                    except (TypeError, ValueError):
                        val = None
                    if val is not None and _within_history(dt, months=history_months):
                        if not aliases_lower or any(alias in hint.lower() for alias in aliases_lower):
                            found.append((dt, val))

            for key, value in node.items():
                visit(value, f"{hint} {key}")
        elif isinstance(node, list):
            # Lists like [{x:..., y:...}] are handled recursively.
            for item in node:
                visit(item, hint)

    try:
        visit(obj)
    except Exception:
        return []
    return found

def _extract_named_series_from_snapshot(
    snapshot: dict[str, Any],
    *,
    mappings: tuple[tuple[str, tuple[str, ...], str], ...],
    source_name: str,
    category: str,
    history_months: int = 6,
) -> list[dict[str, Any]]:
    """Map generic chart-series names to canonical metric names."""
    out: list[dict[str, Any]] = []
    payloads: list[tuple[str, str]] = [(snapshot.get("html", ""), "")]
    payloads.extend((item.get("body", ""), item.get("url", "")) for item in snapshot.get("responses", []))

    for body, resource_url in payloads:
        if not body:
            continue
        parsed_objects: list[Any] = []
        try:
            parsed_objects.append(json.loads(body))
        except Exception:
            pass

        # Look for chart objects embedded in script text.
        for raw_match in re.finditer(r"(?:labels|categories)\s*[:=]\s*(\[[^\]]{1,20000}\]).{0,20000}?(?:datasets|series)\s*[:=]\s*(\[[^\]]{1,50000}\])", body, re.I | re.S):
            try:
                parsed_objects.append({
                    "labels": json.loads(raw_match.group(1).replace("'", '"')),
                    "series": json.loads(raw_match.group(2).replace("'", '"')),
                })
            except Exception:
                continue

        for obj in parsed_objects:
            for canonical, aliases, unit in mappings:
                points = _walk_json_history(obj, aliases=aliases, history_months=history_months)
                if points:
                    dedup = {dt: value for dt, value in points}
                    out.extend(
                        _rows_to_metrics(
                            (
                                {
                                    "metric_name": canonical,
                                    "metric_value": value,
                                    "unit": unit,
                                    "recorded_at": dt,
                                }
                                for dt, value in sorted(dedup.items())
                            ),
                            source_name=source_name,
                            category=category,
                            recorded_at=_daily_stamp(),
                        )
                    )
    return out

def _extract_history_from_snapshot(
    snapshot: dict[str, Any],
    *,
    metric_name: str,
    aliases: tuple[str, ...] = (),
    unit: str | None = None,
    source_name: str,
    category: str,
    history_months: int = 6,
) -> list[dict[str, Any]]:
    aliases_lower = tuple(a.lower() for a in aliases)
    payloads: list[tuple[str, str]] = [(snapshot.get("html", ""), "")]
    payloads.extend((item.get("body", ""), item.get("url", "")) for item in snapshot.get("responses", []))
    points: list[tuple[datetime, float]] = []
    for body, resource_url in payloads:
        if not body:
            continue
        candidates: list[tuple[datetime, float]] = []
        try:
            parsed = json.loads(body)
            candidates.extend(_walk_json_history(parsed))
        except Exception:
            pass
        # Some sites embed chart series in JS without valid JSON. Look for
        # compact {date:..., value:...} / {timestamp:...,price:...} objects.
        # Filter *each* JS match by its local context or request URL. The old
        # implementation appended every numeric date/value pair from every
        # script on the page, cross-contaminating one GPU/commodity series with
        # unrelated charts embedded in the same JS bundle.
        url_text = str(resource_url or "").casefold()
        url_matches_metric = not aliases_lower or any(alias in url_text for alias in aliases_lower)
        for match in re.finditer(
            r"(?:date|datetime|timestamp|time|recorded_at)\s*[:=]\s*[\"']([^\"']+)[\"'].{0,120}?(?:value|price|index|close|y)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
            body,
            re.I | re.S,
        ):
            context = body[max(0, match.start() - 180):min(len(body), match.end() + 180)].casefold()
            if not url_matches_metric and aliases_lower and not any(alias in context for alias in aliases_lower):
                continue
            dt = _parse_date_value(match.group(1))
            if dt is not None:
                points.append((dt, float(match.group(2))))
        if candidates:
            body_text = body.casefold()
            # When the URL isn't metric-specific, demand a matching series label
            # near the data as well; merely mentioning H100 somewhere in a large
            # downloaded bundle is not sufficient evidence for every data point.
            if url_matches_metric or not aliases_lower or any(alias in body_text for alias in aliases_lower):
                points.extend(candidates)

    dedup: dict[datetime, float] = {}
    for dt, value in points:
        if _within_history(dt, months=history_months):
            dedup[dt] = value
    return _rows_to_metrics(
        (
            {
                "metric_name": metric_name,
                "metric_value": value,
                "unit": unit,
                "recorded_at": dt,
            }
            for dt, value in sorted(dedup.items())
        ),
        source_name=source_name,
        category=category,
        recorded_at=_daily_stamp(),
    )

def _quarter_end(year: int, quarter: int) -> datetime:
    month = quarter * 3
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    from datetime import timedelta

    return next_month - timedelta(days=1)

def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def _number(value: str) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    text = text.replace("₹", "").replace("Rs.", "").replace("Rs", "")
    text = re.sub(r"[^0-9.\-]", "", text)
    if text in {"", ".", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None

def _year_month(text: str) -> tuple[int, int] | None:
    if not text:
        return None
    month_map = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    match = re.search(
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"[\s\-/]+(20\d{2})\b",
        text,
        flags=re.I,
    )
    if not match:
        return None
    month = month_map[match.group(1)[:3].lower()]
    return int(match.group(2)), month

def _parse_visible_number(text: str, label: str, *, max_chars: int = 120) -> float | None:
    pattern = re.compile(
        rf"{re.escape(label)}[^0-9]{{0,{max_chars}}}([0-9][0-9,]*(?:\.[0-9]+)?)",
        flags=re.I,
    )
    match = pattern.search(text or "")
    return _number(match.group(1)) if match else None

async def _http_get(url: str, *, timeout: float, allow_redirects: bool = True) -> httpx.Response:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=allow_redirects,
                headers=headers,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response
        except Exception as exc:  # pragma: no cover - network dependent
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(1.0)
    raise SourceUnavailable(f"Unable to fetch {url}: {last_error}") from last_error

async def _sync_urllib_get(url: str, *, timeout: float | None = None) -> str:
    """Windows-safe synchronous HTTP fallback for public dashboards."""
    def _fetch() -> str:
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-IN,en;q=0.8",
                "Cache-Control": "no-cache",
            },
        )
        with urlopen(request, timeout=(None if timeout is None or float(timeout) <= 0 else float(timeout))) as response:
            raw = response.read()
            encoding = response.headers.get_content_charset() or "utf-8"
            return raw.decode(encoding, errors="replace")

    return await asyncio.to_thread(_fetch)

def _text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    return _clean_text(soup.get_text(" ", strip=True))

def _tables(html: str) -> list[list[list[str]]]:
    soup = BeautifulSoup(html, "lxml")
    tables: list[list[list[str]]] = []
    for table in soup.find_all("table"):
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [
                _clean_text(cell.get_text(" ", strip=True))
                for cell in tr.find_all(["th", "td"])
            ]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables

def _rows_to_metrics(
    rows: Iterable[dict[str, Any]],
    *,
    source_name: str,
    category: str,
    recorded_at: datetime,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        value = row.get("metric_value")
        if value is None:
            continue
        output.append(
            {
                "source_name": str(row.get("source_name") or source_name),
                "category": str(row.get("category") or category),
                "metric_name": row["metric_name"],
                "metric_value": float(value),
                "unit": row.get("unit"),
                "recorded_at": row.get("recorded_at") or recorded_at,
                "dimensions": dict(row.get("dimensions") or {}),
                "source_url": row.get("source_url"),
                "source_metadata": dict(row.get("source_metadata") or {}),
            }
        )
    return output

async def _render_page_snapshot(
    url: str, *, timeout_ms: int = 120_000, clicks: Iterable[str] = (), capture_network: bool = True
) -> dict[str, Any]:
    """Compatibility spelling used by source-specific render strategies."""
    return await render_page_snapshot(url, timeout_ms=timeout_ms, clicks=clicks, capture_network=capture_network)


def snapshot_from_capture(source_data: dict[str, Any]) -> dict[str, Any]:
    """Build the common history-parser input from preserved source capture."""
    documents = source_data.get("raw_documents") or []
    html = "\n".join(str(item.get("html") or "") for item in documents if isinstance(item, dict))
    responses = []
    for item in source_data.get("network_resources") or []:
        if isinstance(item, dict):
            responses.append({"url": item.get("url", ""), "body": item.get("body", ""), "content_type": item.get("content_type", "")})
    for item in source_data.get("embedded_json") or []:
        if isinstance(item, dict) and item.get("data") is not None:
            responses.append({"url": item.get("url", ""), "body": json.dumps(item.get("data"), default=str), "content_type": "application/json"})
    return {"html": html, "responses": responses, "final_url": source_data.get("source_url")}


def merge_captures(target: dict[str, Any], extra: dict[str, Any], *, source_role: str | None = None) -> None:
    """Merge captures from source-owned supplementary pages without losing raw material."""
    for field in ("raw_documents", "tables", "kpi_cards", "filters", "charts", "embedded_json", "network_resources", "links", "linked_resources"):
        dest = target.setdefault(field, [])
        for value in extra.get(field) or []:
            if isinstance(value, dict):
                item = dict(value)
                if source_role and field == "raw_documents":
                    item.setdefault("source_role", source_role)
                dest.append(item)
    target.setdefault("capture_completeness", {}).setdefault("warnings", []).extend(
        (extra.get("capture_completeness") or {}).get("warnings") or []
    )
    target.setdefault("capture_completeness", {}).setdefault("truncations", []).extend(
        (extra.get("capture_completeness") or {}).get("truncations") or []
    )
    target["capture_completeness"]["counts"] = {
        "documents": len(target.get("raw_documents") or []),
        "raw_documents": len(target.get("raw_documents") or []),
        **{key: len(target.get(key) or []) for key in
           ("tables", "kpi_cards", "filters", "charts", "embedded_json", "network_resources", "links", "linked_resources")}
    }
    target["capture_completeness"]["supplementary_source_count"] = int(target["capture_completeness"].get("supplementary_source_count") or 0) + 1
    # merge_captures used to merge diagnostics but leave the base page's status
    # unchanged. A clean landing page could therefore hide a failed/truncated
    # supplementary source and still be reported as fully captured.
    captures = target["capture_completeness"]
    warnings = list(dict.fromkeys(str(x) for x in (captures.get("warnings") or []) if x))
    truncations = list(dict.fromkeys(str(x) for x in (captures.get("truncations") or []) if x))
    captures["warnings"] = warnings
    captures["truncations"] = truncations
    current_status = str(captures.get("status") or "captured").casefold()
    extra_status = str((extra.get("capture_completeness") or {}).get("status") or "captured").casefold()
    if current_status == "blocked" or extra_status == "blocked":
        captures["status"] = "blocked"
    elif truncations or warnings or current_status in {"partial", "failed", "empty"} or extra_status in {"partial", "failed", "empty", "blocked"}:
        captures["status"] = "partial"
    else:
        captures["status"] = "captured"
