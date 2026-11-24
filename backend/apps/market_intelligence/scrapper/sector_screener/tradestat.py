from __future__ import annotations

"""TradeStat MEIDB collector for watched HS codes.

Uses the public monthly commodity-wise forms. Form controls are discovered from
labels/names at runtime instead of assuming a fixed ASP.NET field name. The
collector never invents values: when the portal form or result table changes it
returns diagnostics and an incomplete capture.
"""

import asyncio
import calendar
import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
TRADESTAT_DIAG_VERSION = "TRADESTAT-SPECIFIC-HSCODE-V3"
BASE = "https://tradestat.commerce.gov.in/meidb/commoditywise_{trade}"
IMPORT_URL = BASE.format(trade="import")
EXPORT_URL = BASE.format(trade="export")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
MONTHS = {name.casefold(): index for index, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.casefold(): index for index, name in enumerate(calendar.month_abbr) if name})
TRADESTAT_JOB_CONCURRENCY = 4
TRADESTAT_JOB_TIMEOUT_SECONDS = 75


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _number(value: Any) -> float | None:
    text = _clean(value).replace(",", "").replace("₹", "").replace("$", "")
    text = re.sub(r"\(([^)]+)\)", r"-\1", text)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _published_period(html: str) -> date | None:
    """Read the latest reporting month only from MEIDB's published coverage banner."""
    if not html:
        return None
    text = _clean(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    match = re.search(r"data\s+available\s*:\s*(.{0,220})", text, re.I)
    if not match:
        return None
    candidates: list[date] = []
    for month, year in re.findall(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+(20\d{2})\b", match.group(1), re.I):
        try:
            candidates.append(date(int(year), MONTHS[month.casefold()[:3]], 1))
        except Exception:
            continue
    return max(candidates) if candidates else None


async def _browser_discover_period(url: str) -> tuple[date | None, str | None]:
    """Read MEIDB's coverage banner from a rendered page if the HTTP page is blocked."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = None
            errors: list[str] = []
            for args in ({"channel": "chrome", "headless": True}, {"channel": "msedge", "headless": True}, {"headless": True}):
                try:
                    browser = await playwright.chromium.launch(**args)
                    break
                except Exception as exc:
                    errors.append(f"{args}: {type(exc).__name__}: {_clean(exc)[:130]}")
            if browser is None:
                return None, "Could not launch a browser to read the MEIDB reporting-period banner: " + " | ".join(errors[:3])
            try:
                page = await browser.new_page(user_agent=HEADERS["User-Agent"], locale="en-IN", timezone_id="Asia/Kolkata")
                response = await page.goto(url, wait_until="domcontentloaded", timeout=35000, referer="https://tradestat.commerce.gov.in/meidb/")
                if response and response.status >= 400:
                    return None, f"MEIDB rendered landing page returned HTTP {response.status}"
                try:
                    await page.wait_for_load_state("networkidle", timeout=7000)
                except Exception:
                    pass
                period = _published_period(await page.content())
                return period, None if period else "Rendered MEIDB page did not expose its Data available coverage banner"
            finally:
                await browser.close()
    except Exception as exc:
        return None, f"MEIDB browser period discovery failed: {type(exc).__name__}: {_clean(exc)[:250]}"


def _last_periods(latest: date, count: int = 12) -> list[date]:
    output = []
    year, month = latest.year, latest.month
    for _ in range(count):
        output.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return list(reversed(output))


def _label_for_control(control, form) -> str:
    """Resolve the nearest label, without inheriting every label in a form row."""
    ident = control.get("id")
    if ident:
        label = form.find("label", attrs={"for": ident})
        if label:
            return _clean(label.get_text(" ", strip=True)).casefold()
    aria = control.get("aria-label") or control.get("title")
    if aria:
        return _clean(aria).casefold()
    parent_label = control.find_parent("label")
    if parent_label:
        return _clean(parent_label.get_text(" ", strip=True)).casefold()
    # When a label is adjacent to its input but lacks a for/id association,
    # prefer the nearest sibling label. Do not take the last label from a shared
    # row: both radio buttons could otherwise inherit the same wrapper text.
    previous = control.find_previous_sibling("label")
    following = control.find_next_sibling("label")
    if following:
        return _clean(following.get_text(" ", strip=True)).casefold()
    if previous:
        return _clean(previous.get_text(" ", strip=True)).casefold()
    parent = control.parent
    if parent:
        direct_labels = parent.find_all("label", recursive=False)
        if len(direct_labels) == 1:
            return _clean(direct_labels[0].get_text(" ", strip=True)).casefold()
    return ""


def _placeholder_option(values: list[tuple[str, str]]) -> str:
    for value, text in values:
        if not value or text.casefold().strip() in {"select", "select one", "--select--", "choose"}:
            return value
    return ""


_MONTH_WORD = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


def _is_cumulative_header(header: Any) -> bool:
    """Identify a month-range/YTD column without excluding a single April month."""
    text = _clean(header).casefold()
    if any(token in text for token in ("ytd", "cumulative", "year to date", "upto", "up to")):
        return True
    return bool(re.search(rf"\b{_MONTH_WORD}\s*(?:-|–|—|to)\s*{_MONTH_WORD}\b", text, re.I))


def _selected_unit_label(form) -> str:
    """Read the selected value denomination from the live report form."""
    for node in form.find_all("select"):
        marker = f"{node.get('name') or ''} {node.get('id') or ''} {_label_for_control(node, form)}".casefold()
        if "value" not in marker or "in" not in marker:
            continue
        selected = node.find("option", selected=True)
        if selected:
            return _clean(selected.get_text(" ", strip=True))
        current = next((option for option in node.find_all("option") if option.get("value") == node.get("value")), None)
        if current:
            return _clean(current.get_text(" ", strip=True))
    return ""


def _canonical_header(value: Any) -> str:
    """Normalize a MEIDB grid heading (``HSCode``/``HS Code``/``HS_Code``)."""
    return re.sub(r"[^a-z0-9]", "", _clean(value).casefold())


def _is_hs_header(value: Any) -> bool:
    canonical = _canonical_header(value)
    return canonical in {"hscode", "itshscode", "indiantradeclassificationhscode"} or (
        canonical.startswith("hscode")
    )


def _parse_source_tables(body: str, content_type: str = "", hs_code: str = "") -> list[dict[str, Any]]:
    """Parse either the document HTML response or a JSON/XHR representation."""
    if not body:
        return []
    tables = _parse_result_tables(body, hs_code)
    if tables:
        return tables
    content = str(content_type or "").casefold()
    if "json" in content or body.lstrip().startswith(("{", "[")):
        return _parse_json_result_tables(body, hs_code)
    return []


def _form_payload(
    form,
    code: str,
    period: date,
    page_url: str,
    level_digits: int | None = None,
) -> tuple[dict[str, str], str, str, list[str]]:
    """Build the exact Commodity-wise MEIDB form payload.

    A watched HS code must use the portal's *Specific HSCode* radio option and
    its adjacent HS-code field. The All-Commodities radio and digit-level
    selector are used only for an intentionally unfiltered request.
    """
    payload: dict[str, str] = {}
    warnings: list[str] = []
    hs_set = False
    month_set = False
    year_set = False
    level_set = False
    all_mode_set = False
    specific_mode_set = False
    radio_count = len(form.find_all("input", attrs={"type": "radio"}))
    year_type_text = ""
    use_specific_hs = bool(re.sub(r"\D", "", str(code or "")))
    requested_code = re.sub(r"\D", "", str(code or ""))

    # Find the specific-code text box independently of the radio button. The
    # portal's label is commonly attached to the radio, while the text input is
    # adjacent and may only expose an ``hscode`` name/id/placeholder.
    hs_input_name: str | None = None
    if use_specific_hs:
        ranked_inputs: list[tuple[int, Any]] = []
        for candidate in form.find_all("input"):
            typ = str(candidate.get("type") or "text").casefold()
            if typ not in {"text", "search", "number", "tel"} or candidate.has_attr("disabled"):
                continue
            marker = " ".join((
                str(candidate.get("name") or ""), str(candidate.get("id") or ""),
                str(candidate.get("placeholder") or ""), _label_for_control(candidate, form),
                str(candidate.get("aria-label") or ""),
            )).casefold()
            score = 0
            if "hscode" in _canonical_header(marker) or "hs_code" in marker or "hs code" in marker:
                score += 8
            if "specific" in marker:
                score += 5
            if "description" in marker:
                score -= 10
            if "search" in marker:
                score -= 3
            if candidate.get("name") and score > 0:
                ranked_inputs.append((score, candidate))
        if ranked_inputs:
            ranked_inputs.sort(key=lambda item: item[0], reverse=True)
            hs_input_name = str(ranked_inputs[0][1].get("name") or "") or None

    # Resolve the reporting-year convention before selecting the year value.
    # For January-March, a Financial Year report for calendar year 2026 is FY
    # 2025-26; selecting 2026-27 would silently request the wrong period.
    for year_type_node in form.find_all("select"):
        year_type_marker = f"{year_type_node.get('name') or ''} {year_type_node.get('id') or ''} {_label_for_control(year_type_node, form)}".casefold()
        if not any(token in year_type_marker for token in ("year type", "yeartype", "year_type")):
            continue
        year_type_options = [
            (str(option.get("value") or option.get_text(" ", strip=True)), _clean(option.get_text(" ", strip=True)))
            for option in year_type_node.find_all("option")
        ]
        preferred = next((item for item in year_type_options if "financial" in item[1].casefold() or "fiscal" in item[1].casefold()), None)
        preferred = preferred or next((item for item in year_type_options if "calendar" in item[1].casefold()), None)
        if preferred:
            year_type_name = year_type_node.get("name")
            if year_type_name:
                payload[str(year_type_name)] = preferred[0]
            year_type_text = preferred[1]

    for node in form.find_all(["input", "select", "textarea", "button"]):
        name = node.get("name")
        if not name or node.has_attr("disabled"):
            continue
        label = _label_for_control(node, form)
        ident = str(node.get("id") or "")
        placeholder = str(node.get("placeholder") or "")
        marker = f"{name} {ident} {placeholder} {label}".casefold()
        tag = node.name

        if tag == "input":
            typ = (node.get("type") or "text").casefold()
            value = str(node.get("value") or "")
            if typ == "hidden":
                payload[name] = value
                continue
            if typ in {"submit", "button", "image"}:
                # Do not submit the HS-code lookup button as the report action.
                button_text = _clean(node.get("value") or node.get("alt") or "").casefold()
                if typ == "submit" and "search" not in button_text and "hscode" not in button_text:
                    payload[name] = value or "Submit"
                continue
            if use_specific_hs and name == hs_input_name:
                payload[name] = requested_code
                hs_set = True
            elif typ == "radio":
                radio_label = _label_for_control(node, form)
                radio_context = ""
                if not radio_label and node.parent is not None and node.parent is not form:
                    candidate_context = _clean(node.parent.get_text(" ", strip=True)).casefold()
                    has_all = "all commodit" in candidate_context
                    has_specific = "specific hs" in candidate_context or "hscode" in candidate_context
                    if has_all != has_specific:
                        radio_context = candidate_context
                radio_marker = f"{name} {ident} {placeholder} {radio_label} {radio_context}".casefold()
                is_all_mode = "all commodit" in radio_marker
                is_specific_mode = "specific hs" in radio_marker or "hscode" in _canonical_header(radio_marker)
                if use_specific_hs and is_specific_mode:
                    payload[name] = value or "on"
                    specific_mode_set = True
                elif not use_specific_hs and is_all_mode:
                    payload[name] = value or "on"
                    all_mode_set = True
                elif is_all_mode or is_specific_mode:
                    continue
                elif node.has_attr("checked") and name not in payload:
                    payload[name] = value or "on"
            elif typ == "checkbox":
                if node.has_attr("checked"):
                    payload[name] = value or "on"
            elif value:
                payload[name] = value

        elif tag == "textarea":
            payload[name] = _clean(node.get_text())

        elif tag == "button":
            button_text = _clean(node.get("value") or node.get_text(" ", strip=True)).casefold()
            if (node.get("type") or "submit").casefold() == "submit" and "search" not in button_text and "hscode" not in button_text:
                payload[name] = str(node.get("value") or _clean(node.get_text(" ", strip=True)) or "Submit")

        elif tag == "select":
            options = node.find_all("option")
            values = [(str(option.get("value") or option.get_text(" ", strip=True)), _clean(option.get_text(" ", strip=True))) for option in options]
            if use_specific_hs and any(re.search(r"\b(?:2|4|6|8)\s*digit(?:\s+level)?\b", text, re.I) for _, text in values):
                # This selector is meaningful only in All-Commodities mode.
                continue
            selected = next((value for value, _ in values if option_is_selected(node, value)), None)
            if selected is None:
                selected = values[0][0] if values else ""

            # The 2/4/6/8 digit selector belongs to All-Commodities mode. Do not
            # submit it with Specific HSCode: the two modes are mutually exclusive.
            if not use_specific_hs and level_digits and any(re.search(r"\b(?:2|4|6|8)\s*digit\b", text, re.I) for _, text in values):
                level_text = re.compile(rf"\b{int(level_digits)}\s*digit\b", re.I)
                level_option = next((value for value, text in values if level_text.search(text) or level_text.search(value)), None)
                if level_option is not None:
                    selected = level_option
                    payload[name] = selected
                    level_set = True
                    continue

            if not use_specific_hs and ("all commod" in marker or ("commodity" in marker and "specific" not in marker and "hs" not in marker)):
                if not code:
                    all_option = next((value for value, text in values if "all commodit" in text.casefold()), None)
                    if all_option is not None:
                        selected = all_option
                    elif selected is None or not selected.strip() or str(next((text for value, text in values if value == selected), "")).casefold() in {"select", "select one", "--select--", "choose"}:
                        selected = next((value for value, text in values if value and text.casefold() not in {"select", "select one", "--select--", "choose"}), selected)
                if selected is not None:
                    payload[name] = selected
                    all_mode_set = True
                continue

            if "month" in marker:
                month_name = calendar.month_name[period.month].casefold()
                match = next((value for value, text in values if text.casefold().startswith(month_name[:3]) or text.strip() == str(period.month) or value.strip() == str(period.month)), None)
                if match is not None:
                    selected = match
                    month_set = True
            elif "year" in marker and "type" not in marker:
                financial_year = "financial" in year_type_text.casefold() or "fiscal" in year_type_text.casefold()
                desired_year = period.year - 1 if financial_year and period.month < 4 else period.year
                match = next((value for value, text in values if text.strip() == str(desired_year) or value.strip() == str(desired_year)), None)
                if match is None:
                    match = next((value for value, text in values if re.search(rf"\b{desired_year}(?:[-/ ]\d{{2,4}})?\b", text) or re.search(rf"\b{desired_year}(?:[-/ ]\d{{2,4}})?\b", value)), None)
                if match is not None:
                    selected = match
                    year_set = True
            elif "year type" in marker or "yeartype" in marker or "year_type" in marker:
                match = next((value for value, text in values if "financial" in text.casefold() or "financial" in value.casefold()), None)
                if match is None:
                    match = next((value for value, text in values if "calendar" in text.casefold() or "calendar" in value.casefold()), None)
                if match is not None:
                    selected = match
            elif "value" in marker and "in" in marker:
                # Keep MEIDB's source denomination consistent across every code
                # and month. Prefer US dollars in millions when offered, otherwise
                # retain the form's selected value.
                current = next((value for value, _ in values if option_is_selected(node, value)), None)
                preferred = next((value for value, text in values if (
                    ("us $" in text.casefold() or "us dollar" in text.casefold() or "usd" in text.casefold())
                    and "million" in text.casefold()
                )), None)
                selected = preferred or current or selected

            if selected is not None:
                payload[name] = selected

    if use_specific_hs and not hs_set:
        warnings.append("Could not identify the Specific HSCode input on the TradeStat form")
    if radio_count and use_specific_hs and not specific_mode_set:
        warnings.append("Could not identify/select the Specific HSCode radio mode on the TradeStat form")
    if radio_count and not use_specific_hs and not all_mode_set:
        warnings.append("Could not identify/select the All Commodities mode on the TradeStat form")
    if not use_specific_hs and level_digits and not level_set:
        warnings.append(f"Could not identify the {level_digits}-digit HS level selector on the TradeStat form")
    if not month_set:
        warnings.append("Month control was not identified or did not expose the requested month")
    if not year_set:
        warnings.append("Year control was not identified or did not expose the requested year")

    action = urljoin(page_url, str(form.get("action") or page_url))
    method = str(form.get("method") or "get").casefold()
    return payload, action, method, warnings

def option_is_selected(select, value: str) -> bool:
    option = select.find("option", attrs={"value": value})
    return bool(option and option.has_attr("selected"))


def _parse_result_tables(html: str, hs_code: str = "") -> list[dict[str, Any]]:
    """Parse the actual HTML commodity table returned by MEIDB.

    The live Commodity-wise Export/Import response contains a table like:
    S.No | HS Code | Commodity | <prior month> (R) | <current month> (F) | % | ...
    The table response is the authoritative payload; no HS lookup modal is needed.
    """
    soup = BeautifulSoup(html or "", "lxml")
    tables: list[dict[str, Any]] = []
    target_code = re.sub(r"\D", "", str(hs_code or ""))

    for index, table in enumerate(soup.find_all("table")):
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [_clean(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"], recursive=False)]
            if cells:
                rows.append(cells)
        if len(rows) < 2:
            continue

        header_idx = None
        for i, row in enumerate(rows[:10]):
            canonical = [_canonical_header(cell) for cell in row]
            has_hs = any(_is_hs_header(cell) for cell in row)
            has_commodity = any("commodity" in cell or "description" in cell for cell in canonical)
            if has_hs and has_commodity:
                header_idx = i
                break
        if header_idx is None:
            continue

        raw_headers = [cell or f"Column {i+1}" for i, cell in enumerate(rows[header_idx])]
        # MEIDB uses duplicate `%` headers: the first is monthly growth and the
        # second belongs to the cumulative Apr-Sep comparison. Preserve both.
        header_counts: dict[str, int] = {}
        headers = []
        for header in raw_headers:
            count = header_counts.get(header, 0) + 1
            header_counts[header] = count
            headers.append(header if count == 1 else f"{header} ({count})")
        width = len(headers)
        if width < 3:
            continue
        if not any(_is_hs_header(header) for header in headers) or not any(
            "commodity" in _canonical_header(header) or "description" in _canonical_header(header)
            for header in headers
        ):
            continue

        normalized = []
        hs_columns = [i for i, header in enumerate(headers) if _is_hs_header(header)]
        for raw_row in rows[header_idx + 1:]:
            if not any(raw_row):
                continue
            row = raw_row[:width] + [""] * max(0, width - len(raw_row))
            row_dict = {headers[col]: row[col] for col in range(width)}
            # The report contains an India total row without an HS code. It is
            # not a commodity observation and must not enter the watchlist data.
            hs_value = ""
            for idx in hs_columns:
                match = re.search(r"(?<!\d)(\d{2,8})(?!\d)", _clean(row[idx]))
                if match:
                    hs_value = match.group(1)
                    break
            if target_code and hs_value != target_code:
                continue
            if not hs_value:
                continue
            normalized.append(row_dict)

        if not normalized:
            continue

        tables.append({
            "key": f"tradestat-{index+1}",
            "title": "TradeStat Commodity-wise",
            "columns": headers,
            "rows": normalized,
            "row_count": len(normalized),
            "captured_row_count": len(normalized),
        })

    return tables

def _parse_json_result_tables(raw_text: str, hs_code: str) -> list[dict[str, Any]]:
    """Convert MEIDB JSON row payloads to the same table shape as its HTML grid."""
    try:
        root = json.loads(raw_text)
    except Exception:
        return []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_records(records: list[dict[str, Any]], path: str) -> None:
        if not records:
            return
        keys: list[str] = []
        for record in records:
            for key in record:
                if str(key) not in keys:
                    keys.append(str(key))
        if len(keys) < 2:
            return
        has_hs = any(_is_hs_header(key) for key in keys)
        has_commodity = any("commodity" in _canonical_header(key) or "description" in _canonical_header(key) for key in keys)
        has_trade_measure = any(
            any(token in _canonical_header(key) for token in ("value", "amount", "trade", "quantity"))
            or bool(re.search(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\D*20\d{2}", str(key), re.I))
            for key in keys
        )
        if not (has_hs and (has_trade_measure or has_commodity)):
            return
        rows = [{key: record.get(key, "") for key in keys} for record in records]
        hs_columns = [key for key in keys if _is_hs_header(key)]
        if hs_columns:
            target = re.sub(r"\D", "", str(hs_code or ""))
            if target:
                rows = [row for row in rows if any(re.sub(r"\D", "", str(row.get(key, ""))) == target for key in hs_columns)]
        if not rows:
            return
        signature = f"{path}:{','.join(keys)}:{len(rows)}"
        if signature in seen:
            return
        seen.add(signature)
        found.append({"key": f"tradestat-xhr-{len(found)+1}", "title": "TradeStat AJAX commodity result",
                      "columns": keys, "rows": rows[:500], "row_count": len(rows),
                      "captured_row_count": min(len(rows), 500)})

    def walk(value: Any, path: str = "data", depth: int = 0) -> None:
        if depth > 8 or len(found) >= 20:
            return
        if isinstance(value, dict):
            keys = {str(key).casefold() for key in value}
            # Some responses return one row object instead of an array of rows.
            if any(_is_hs_header(key) for key in keys) and any(
                any(token in _canonical_header(key) for token in ("value", "amount", "trade", "quantity"))
                or bool(re.search(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\D*20\d{2}", key, re.I))
                for key in keys
            ):
                add_records([value], path)
                return
            for key, child in value.items():
                walk(child, str(key), depth + 1)
            return
        if isinstance(value, str):
            fragment = value.strip()
            if "<table" in fragment.casefold():
                for table in _parse_result_tables(fragment, hs_code):
                    found.append(table)
                return
            if fragment.startswith(("{", "[")):
                try:
                    walk(json.loads(fragment), path, depth + 1)
                except Exception:
                    pass
            return
        if not isinstance(value, list) or not value:
            return
        if all(isinstance(item, dict) for item in value):
            add_records(value, path)
            return
        if all(isinstance(item, (list, tuple)) for item in value):
            rows = [[_clean(cell) for cell in item] for item in value]
            if len(rows) < 2:
                return
            headers = rows[0]
            if not any(any(token in str(header).casefold() for token in ("hs", "commodity", "value", "unit", "quantity")) for header in headers):
                return
            normalized = []
            hs_columns = [i for i, header in enumerate(headers) if _is_hs_header(header)]
            for cells in rows[1:]:
                cells = cells[:len(headers)] + [""] * max(0, len(headers) - len(cells))
                row = {str(headers[i]): cells[i] for i in range(len(headers))}
                if hs_columns and not any(re.sub(r"\D", "", str(cells[i])).startswith(hs_code) for i in hs_columns):
                    continue
                normalized.append(row)
            if normalized:
                found.append({"key": f"tradestat-xhr-{len(found)+1}", "title": "TradeStat AJAX commodity result",
                              "columns": [str(item) for item in headers], "rows": normalized[:500],
                              "row_count": len(normalized), "captured_row_count": min(len(normalized), 500)})
            return
        for index, child in enumerate(value[:30]):
            walk(child, f"{path}[{index}]", depth + 1)

    walk(root)
    return found


def _monthly_value_fields(row: dict[str, Any], period: date) -> dict[str, Any]:
    """Extract the current-month and same-month-prior-year values from a MEIDB row.

    The live Commodity-wise response contains two monthly comparison columns,
    e.g. ``Sep-2025 (R)`` and ``Sep-2026 (F)``. These are the values the UI needs
    for the monitor table and YoY bar chart. Cumulative Apr-Sep columns and growth
    columns are deliberately ignored.
    """
    current_year = int(period.year)
    prior_year = current_year - 1
    month_abbr = calendar.month_abbr[period.month].casefold()
    month_name = calendar.month_name[period.month].casefold()

    def header_matches(header: str, year: int) -> bool:
        text = _clean(header).casefold()
        if str(year) not in text:
            return False
        if month_abbr not in text and month_name not in text:
            return False
        if _is_cumulative_header(text) or "%" in text or "growth" in text:
            return False
        return True

    candidates: list[tuple[str, float, str]] = []
    for key, raw in row.items():
        value = _number(raw)
        if value is None:
            continue
        key_text = _clean(key)
        lower = key_text.casefold()
        # Exclude serial/HS/commodity text and cumulative/growth columns.
        if any(token in lower for token in ("hs code", "hscode", "growth", "%", "ytd", "unit")) or _is_cumulative_header(lower):
            continue
        candidates.append((key_text, value, lower))

    def pick(year: int) -> tuple[float | None, str | None]:
        # Prefer exact month/year headers, then month/year headers containing a
        # revision marker. Never select cumulative or growth columns.
        for key, value, _ in candidates:
            if header_matches(key, year):
                return value, key
        return None, None

    previous_value, previous_field = pick(prior_year)
    current_value, current_field = pick(current_year)

    # MEIDB places the monthly YoY growth column immediately after the current
    # month value, before the cumulative Apr-Sep comparison columns. Keep the
    # source-published percentage rather than recomputing it.
    growth_value = None
    growth_field = None
    keys = list(row.keys())
    if current_field in keys:
        current_index = keys.index(current_field)
        for key in keys[current_index + 1:]:
            lower = _clean(key).casefold()
            if _is_cumulative_header(lower):
                break
            if lower.startswith("%") or lower in {"% growth", "growth %", "growth", "percentage growth"} or "growth" in lower:
                candidate = _number(row.get(key))
                if candidate is not None:
                    growth_value = candidate
                    growth_field = key
                    break

    return {
        "previous_year": previous_value,
        "previous_year_field": previous_field,
        "current_year": current_value,
        "current_year_field": current_field,
        "growth_percent": growth_value,
        "growth_field": growth_field,
        "previous_year_label": f"{calendar.month_abbr[period.month]}-{prior_year}",
        "current_year_label": f"{calendar.month_abbr[period.month]}-{current_year}",
    }


def _numeric_fields(row: dict[str, Any], period: date) -> tuple[float | None, str]:
    candidates = [(key, _number(value)) for key, value in row.items()]
    candidates = [(key, value) for key, value in candidates if value is not None]
    excluded_tokens = ("hs code", "hscode", "growth", "percent", "%", "ytd", "unit")

    def is_excluded_header(key: str) -> bool:
        text = key.casefold()
        if any(token in text for token in excluded_tokens) or _is_cumulative_header(text):
            return True
        # Do not reject "Value of commodity quantity in INR" merely because
        # the value column describes the commodity quantity. Only standalone
        # quantity fields are excluded.
        if text.strip() in {"quantity", "qty", "quantity of commodity", "commodity quantity"}:
            return True
        return False

    month_tokens = (calendar.month_abbr[period.month].casefold(), calendar.month_name[period.month].casefold())
    exact_month = next(((key, value) for key, value in candidates
                        if str(period.year) in key and any(token in key.casefold() for token in month_tokens)
                        and not is_excluded_header(key)), None)
    preferred = next(((key, value) for key, value in candidates
                      if any(token in key.casefold() for token in ("value", "usd", "inr", "amount"))
                      and not is_excluded_header(key)), None)
    fallback = next(((key, value) for key, value in reversed(candidates)
                     if not is_excluded_header(key)), None)
    chosen = exact_month or preferred or fallback or (None, None)
    return chosen[1], chosen[0] or ""


async def _fetch_month(
    client: httpx.AsyncClient,
    url: str,
    code: str,
    period: date,
    *,
    level_digits: int | None = None,
) -> dict[str, Any]:
    """Submit one direction-specific, Specific-HSCode MEIDB report via HTTP.

    The URL determines Import vs Export; the response heading is checked before
    form submission so a server redirect/default tab cannot silently mix data.
    """
    trade = "import" if "commoditywise_import" in str(url).casefold() else "export"
    code = re.sub(r"\D", "", str(code or ""))
    page = await client.get(url, headers={"Referer": "https://tradestat.commerce.gov.in/meidb/"})
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "lxml")
    page_text = _clean(soup.get_text(" ", strip=True)).casefold()
    expected_heading = "imports commodity wise" if trade == "import" else "exports commodity wise"
    canonical_page_text = re.sub(r"[^a-z0-9]+", " ", page_text)
    if expected_heading not in canonical_page_text:
        return {
            "ok": False,
            "warning": f"MEIDB trade tab mismatch: requested {trade.upper()} but page heading did not confirm '{expected_heading}'",
            "period": period.isoformat()[:7],
            "trade_type": trade,
            "requested_url": url,
            "final_url": str(page.url),
            "page_title_excerpt": page_text[:500],
        }

    forms = [item for item in soup.find_all("form") if item.find("select") or item.find("input")]

    def form_score(candidate):
        controls = " ".join(
            f"{node.get('name') or ''} {node.get('id') or ''} {node.get('placeholder') or ''} {_label_for_control(node, candidate)}"
            for node in candidate.find_all(["input", "select"])
        )
        text = controls.casefold()
        return (3 if "month" in text else 0) + (3 if "year" in text else 0) + (5 if ("hs" in text or "commodity" in text) else 0)

    form = max(forms, key=form_score) if forms else None
    if form is None or form_score(form) < 4:
        return {"ok": False, "warning": "TradeStat MEIDB report form was not found", "period": period.isoformat()[:7], "trade_type": trade}

    payload, action, method, warnings = _form_payload(form, code, period, str(page.url), level_digits=None)
    blocking_prefixes = (
        "Month control", "Year control", "Could not identify the Specific HSCode",
        "Could not identify/select the Specific HSCode",
    )
    blocking_warnings = [item for item in warnings if item.startswith(blocking_prefixes)]
    if blocking_warnings:
        return {
            "ok": False, "warning": "; ".join(blocking_warnings), "period": period.isoformat()[:7],
            "trade_type": trade, "form_warnings": warnings,
        }

    meta = soup.find("meta", attrs={"name": re.compile(r"csrf-token", re.I)})
    csrf = str(meta.get("content") or "") if meta else ""
    if csrf:
        payload.setdefault("_token", csrf)
    parsed = urljoin(str(page.url), action)
    headers = {
        "Referer": str(page.url),
        "Origin": "https://tradestat.commerce.gov.in",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if csrf:
        headers["X-CSRF-TOKEN"] = csrf
    cookie_map = dict(client.cookies)
    xsrf_cookie = cookie_map.get("XSRF-TOKEN") or cookie_map.get("xsrf-token")
    if xsrf_cookie:
        headers["X-XSRF-TOKEN"] = str(xsrf_cookie)

    if method == "post":
        response = await client.post(parsed, data=payload, headers=headers)
    else:
        response = await client.get(parsed, params=payload, headers=headers)

    if response.status_code == 419:
        refresh = await client.get(url, headers={"Referer": "https://tradestat.commerce.gov.in/meidb/"})
        if refresh.status_code < 400:
            refresh_soup = BeautifulSoup(refresh.text, "lxml")
            refresh_text = re.sub(r"[^a-z0-9]+", " ", _clean(refresh_soup.get_text(" ", strip=True)).casefold())
            if expected_heading not in refresh_text:
                return {"ok": False, "warning": f"MEIDB session retry landed on the wrong tab for {trade}", "period": period.isoformat()[:7], "trade_type": trade}
            refresh_forms = [item for item in refresh_soup.find_all("form") if item.find("select") or item.find("input")]
            refresh_form = max(refresh_forms, key=form_score) if refresh_forms else None
            if refresh_form is not None:
                payload, action, method, warnings = _form_payload(refresh_form, code, period, str(refresh.url), level_digits=None)
                blocking_warnings = [item for item in warnings if item.startswith(blocking_prefixes)]
                if blocking_warnings:
                    return {"ok": False, "warning": "MEIDB session retry form invalid: " + "; ".join(blocking_warnings), "period": period.isoformat()[:7], "trade_type": trade}
                parsed = urljoin(str(refresh.url), action)
                fresh_token_node = refresh_soup.find("input", attrs={"name": "_token"})
                fresh_token = str(fresh_token_node.get("value") or "") if fresh_token_node else ""
                fresh_meta = refresh_soup.find("meta", attrs={"name": re.compile(r"csrf-token", re.I)})
                fresh_token = fresh_token or (str(fresh_meta.get("content") or "") if fresh_meta else "")
                if fresh_token:
                    payload["_token"] = fresh_token
                    headers["X-CSRF-TOKEN"] = fresh_token
                cookie_map = dict(client.cookies)
                if cookie_map.get("XSRF-TOKEN"):
                    headers["X-XSRF-TOKEN"] = str(cookie_map["XSRF-TOKEN"])
                if method == "post":
                    response = await client.post(parsed, data=payload, headers=headers)
                else:
                    response = await client.get(parsed, params=payload, headers=headers)

    response.raise_for_status()
    content_type = str(response.headers.get("content-type") or "")
    tables = _parse_source_tables(response.text, content_type, hs_code=code)
    if not tables:
        return {
            "ok": False,
            "warning": f"No HSCode/Commodity result table returned for {trade.upper()} HS {code} {period:%Y-%m}; HTTP {response.status_code}, content_type={content_type}, response={_clean(response.text)[:700]}",
            "period": period.isoformat()[:7], "trade_type": trade, "hs_code": code,
            "response_status": response.status_code, "response_content_type": content_type,
            "form_warnings": warnings, "form_fields": sorted(payload.keys()),
        }

    unit_label = _selected_unit_label(form)
    records = []
    for table in tables:
        for row in table.get("rows") or []:
            hs_value = _clean(next((value for key, value in row.items() if _is_hs_header(key)), ""))
            hs_match = re.search(r"(?<!\d)(\d{2,8})(?!\d)", hs_value)
            hs_value = hs_match.group(1) if hs_match else ""
            if code and hs_value != code:
                continue
            value, value_field = _numeric_fields(row, period)
            if not hs_value or value is None:
                continue
            monthly = _monthly_value_fields(row, period)
            # Require the report's exact requested month/year column. This
            # prevents an old/stale table or Apr-Sep cumulative amount from
            # being accepted as the monthly observation.
            if monthly.get("current_year") is None:
                continue
            value = monthly["current_year"]
            value_field = monthly.get("current_year_field") or value_field
            records.append({
                "period": period.isoformat()[:7], "hs_code": hs_value, "trade_value": value,
                "value_field": value_field,
                "previous_year_value": monthly.get("previous_year"),
                "previous_year_field": monthly.get("previous_year_field"),
                "current_year_value": monthly.get("current_year"),
                "current_year_field": monthly.get("current_year_field"),
                "growth_percent": monthly.get("growth_percent"),
                "growth_field": monthly.get("growth_field"),
                "previous_year_label": monthly.get("previous_year_label"),
                "current_year_label": monthly.get("current_year_label"),
                "unit": unit_label or "Source selected unit", "source_row": row,
                "source_status": _status_from_row(row), "source_level": len(hs_value),
                "trade_type": trade,
            })
    if not records:
        return {
            "ok": False,
            "warning": f"MEIDB returned a table but no numeric observation matched exact HSCode {code} for {period:%Y-%m}; content_type={content_type}",
            "period": period.isoformat()[:7], "trade_type": trade, "hs_code": code,
            "response_status": response.status_code, "response_content_type": content_type,
        }
    return {
        "ok": True, "records": records, "tables": tables, "period": period.isoformat()[:7],
        "capture_method": "http-specific-hscode-form", "selected_hs_code": code,
        "trade_type": trade, "unit": unit_label, "form_warnings": warnings,
        "response_content_type": content_type, "response_status": response.status_code,
        "selected_filters": {"month": calendar.month_name[period.month], "year": period.year,
                              "year_type": "Financial Year", "values_in": unit_label,
                              "mode": "Specific HSCode", "hs_code": code},
    }


async def _browser_fetch_month(
    browser,
    url: str,
    level_digits: int | None,
    period: date,
    *,
    hs_code: str = "",
) -> dict[str, Any]:
    """Use MEIDB's visible Trade Type tab and submit one specific HS-code report.

    The browser follows the same sequence a human uses: open the Commodity-wise
    portal, click IMPORTS or EXPORTS, select Specific HSCode, fill the code,
    choose Month/Year/Values-in/Year-Type, and click Submit.
    """
    trade = "import" if "commoditywise_import" in str(url).casefold() else "export"
    code = re.sub(r"\D", "", str(hs_code or ""))
    if not code:
        raise ValueError("A specific HS code is required for TradeStat collection")

    context = await browser.new_context(
        user_agent=HEADERS["User-Agent"], locale="en-IN", timezone_id="Asia/Kolkata"
    )
    page = await context.new_page()
    response_events: list[str] = []
    response_payloads: list[dict[str, Any]] = []
    pending: set[asyncio.Task] = set()
    capture_enabled = {"value": False}

    async def capture_response(response) -> None:
        try:
            resource_type = response.request.resource_type
            if resource_type not in {"xhr", "fetch", "document"}:
                return
            content_type = str(response.headers.get("content-type") or "")
            event = f"{response.status} {resource_type} {response.request.method} {response.url[:190]} [{content_type[:80]}]"
            response_events.append(event)
            if not capture_enabled["value"] or response.status >= 400:
                return
            body = await response.body()
            if len(body) <= 12 * 1024 * 1024:
                response_payloads.append({"url": response.url, "content_type": content_type, "body": body})
        except Exception as exc:
            response_events.append(f"capture error: {type(exc).__name__}: {_clean(exc)[:160]}")

    def on_response(response) -> None:
        task = asyncio.create_task(capture_response(response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    page.on("response", on_response)

    try:
        # Start from the portal's TradeStat commodity-wise page and click the
        # direction tab. This deliberately verifies the user's suspected Import
        # vs Export tab-state issue instead of trusting a stale form/session.
        landing = await page.goto(EXPORT_URL, wait_until="domcontentloaded", timeout=35000)
        if landing and landing.status >= 400:
            raise RuntimeError(f"MEIDB portal entry page returned HTTP {landing.status}")
        try:
            await page.wait_for_load_state("networkidle", timeout=7000)
        except Exception:
            pass

        tab_name = "IMPORTS" if trade == "import" else "EXPORTS"
        tab = page.get_by_role("link", name=re.compile(rf"^\s*{tab_name}\s*$", re.I)).first
        if await tab.count():
            try:
                await tab.click(timeout=8000)
            except Exception as exc:
                # A tab may point to the page that is already open; direct route
                # navigation below is the deterministic fallback.
                response_events.append(f"trade tab click fallback: {type(exc).__name__}: {_clean(exc)[:120]}")
        expected_path = f"commoditywise_{trade}"
        if expected_path not in str(page.url).casefold():
            response = await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            if response and response.status >= 400:
                raise RuntimeError(f"MEIDB {trade.upper()} tab returned HTTP {response.status}")

        try:
            await page.wait_for_load_state("networkidle", timeout=7000)
        except Exception:
            pass
        body_before = _clean(await page.locator("body").inner_text())
        heading = f"{trade.title()} :: Commodity Wise"
        if not re.search(rf"{trade}s?\s*::\s*Commodity\s*Wise", body_before, re.I):
            raise RuntimeError(
                f"MEIDB trade tab verification failed: expected '{heading}', "
                f"URL={page.url}, body={body_before[:600]}"
            )

        forms = page.locator("form")
        chosen = None
        summaries = []
        for form_index in range(await forms.count()):
            candidate = forms.nth(form_index)
            marker = " ".join(await candidate.locator("input,select,textarea,button").evaluate_all("""nodes => nodes.map(el => {
              const byId = el.id && document.querySelector(`label[for='${CSS.escape(el.id)}']`);
              const parentLabel = el.closest('label');
              const siblingLabel = (el.nextElementSibling?.tagName === 'LABEL' ? el.nextElementSibling : null) ||
                (el.previousElementSibling?.tagName === 'LABEL' ? el.previousElementSibling : null);
              const label = byId || parentLabel || siblingLabel;
              return `${el.tagName} ${el.name||''} ${el.id||''} ${el.placeholder||''} ${label?.innerText||''} ${el.value||''}`;
            })"""))
            summaries.append(f"form {form_index}: {marker[:500]}")
            if "month" in marker.casefold() and "year" in marker.casefold() and "hs" in marker.casefold():
                chosen = candidate
                break
        if chosen is None:
            raise RuntimeError("MEIDB Commodity-wise form not identified: " + " | ".join(summaries[:4])[:1800])

        mode_result = await chosen.evaluate("""(form, targetCode) => {
          const clean = v => String(v || '').replace(/\\s+/g, ' ').trim();
          const norm = v => clean(v).toLowerCase().replace(/[^a-z0-9]/g, '');
          const labelFor = el => {
            if (el.labels && el.labels.length) return clean([...el.labels].map(x => x.textContent).join(' '));
            if (el.id) {
              const l = document.querySelector(`label[for='${CSS.escape(el.id)}']`);
              if (l) return clean(l.textContent);
            }
            const nested = el.closest('label');
            if (nested) return clean(nested.textContent);
            const sibling = (el.nextElementSibling?.tagName === 'LABEL' ? el.nextElementSibling : null) ||
              (el.previousElementSibling?.tagName === 'LABEL' ? el.previousElementSibling : null);
            if (sibling) return clean(sibling.textContent);
            return clean(el.parentElement?.textContent || '');
          };
          const radios = [...form.querySelectorAll('input[type=radio]')];
          const specific = radios.find(r => /specific.*hs|specific.*hscode/i.test(labelFor(r) + ' ' + r.name + ' ' + r.id + ' ' + r.value));
          if (!specific) return {ok:false,reason:'Specific HSCode radio not found',radioCount:radios.length};
          specific.click();
          specific.checked = true;
          specific.dispatchEvent(new Event('input',{bubbles:true}));
          specific.dispatchEvent(new Event('change',{bubbles:true}));
          const textInputs = [...form.querySelectorAll('input:not([type=radio]):not([type=hidden]):not([type=submit]):not([type=button])')];
          const scored = textInputs.map(input => {
            const marker = norm([input.name,input.id,input.placeholder,input.getAttribute('aria-label'),labelFor(input)].join(' '));
            let score = marker.includes('hscode') ? 10 : (marker.includes('hs') && marker.includes('code') ? 8 : 0);
            if (marker.includes('description')) score -= 10;
            if (marker.includes('search')) score -= 3;
            return {input,score};
          }).sort((a,b) => b.score-a.score);
          const codeInput = scored[0]?.score > 0 ? scored[0].input : null;
          if (!codeInput) return {ok:false,reason:'Specific HSCode text input not found',inputs:textInputs.map(x=>({name:x.name,id:x.id,placeholder:x.placeholder}))};
          codeInput.focus();
          codeInput.value = targetCode;
          codeInput.dispatchEvent(new Event('input',{bubbles:true}));
          codeInput.dispatchEvent(new Event('change',{bubbles:true}));
          let disabledLevelSelectors = 0;
          for (const select of [...form.querySelectorAll('select')]) {
            if ([...select.options].some(o => /(?:2|4|6|8)\\s*digit(?:\\s+level)?/i.test(clean(o.textContent)))) {
              select.disabled = true;
              disabledLevelSelectors += 1;
            }
          }
          return {ok:true,radio:{name:specific.name,value:specific.value},input:{name:codeInput.name,id:codeInput.id,value:codeInput.value},disabledLevelSelectors};
        }""", code)
        if not mode_result.get("ok"):
            raise RuntimeError(f"MEIDB Specific HSCode selection failed: {mode_result}")

        # Discover all select controls from their actual label/name/id, then set
        # the reporting month, reporting year, financial/calendar year type and
        # denomination. This mirrors the visible form filter fields.
        selects = await chosen.locator("select").evaluate_all("""nodes => nodes.map((el,i) => {
          const byId = el.id && document.querySelector(`label[for='${CSS.escape(el.id)}']`);
          const nested = el.closest('label');
          const sibling = (el.previousElementSibling?.tagName === 'LABEL' ? el.previousElementSibling : null) ||
            (el.nextElementSibling?.tagName === 'LABEL' ? el.nextElementSibling : null);
          const label = byId || nested || sibling;
          return {i,name:el.name||'',id:el.id||'',label:label?.innerText||'',
            value:el.value||'',options:[...el.options].map(o=>({value:o.value,text:(o.textContent||'').trim(),selected:o.selected}))};
        })""")

        year_type_text = ""
        year_type_index = None
        for item in selects:
            marker = f"{item['name']} {item['id']} {item['label']}".casefold().replace("_", " ")
            if not any(token in marker for token in ("year type", "yeartype", "year_type")):
                continue
            options = item.get("options") or []
            target = next((o for o in options if "financial" in str(o["text"]).casefold() or "fiscal" in str(o["text"]).casefold()), None)
            target = target or next((o for o in options if "calendar" in str(o["text"]).casefold()), None)
            if target:
                await chosen.locator("select").nth(item["i"]).select_option(value=str(target["value"]))
                year_type_text = str(target["text"])
                year_type_index = item["i"]
                break

        selected_filters = {"trade_type": trade, "hs_code": code}
        for item in selects:
            marker = f"{item['name']} {item['id']} {item['label']}".casefold().replace("_", " ")
            options = item.get("options") or []
            target = None
            selected_filter = None
            if "month" in marker:
                target = next((o["value"] for o in options if str(o["text"]).strip().casefold().startswith(calendar.month_name[period.month].casefold()[:3]) or str(o["value"]).strip() in {str(period.month), f"{period.month:02d}"}), None)
                selected_filter = "month"
            elif "year" in marker and "type" not in marker:
                is_financial = "financial" in year_type_text.casefold() or "fiscal" in year_type_text.casefold()
                desired_year = period.year if (not is_financial or period.month >= 4) else period.year - 1
                target = next((o["value"] for o in options if str(o["text"]).strip() == str(desired_year) or str(o["value"]).strip() == str(desired_year)), None)
                if target is None:
                    target = next((o["value"] for o in options if re.search(rf"\b{desired_year}(?:[-/ ]\d{{2,4}})?\b", str(o["text"])) or re.search(rf"\b{desired_year}(?:[-/ ]\d{{2,4}})?\b", str(o["value"]))), None)
                selected_filter = "year"
            elif "value" in marker and "in" in marker:
                target = next((o["value"] for o in options if (("us $" in str(o["text"]).casefold() or "us dollar" in str(o["text"]).casefold() or "usd" in str(o["text"]).casefold()) and "million" in str(o["text"]).casefold())), None)
                target = target if target is not None else next((o["value"] for o in options if o.get("selected")), None)
                selected_filter = "values_in"
            if target is not None:
                await chosen.locator("select").nth(item["i"]).select_option(value=str(target))
                selected_filters[selected_filter] = target

        if "month" not in selected_filters or "year" not in selected_filters:
            raise RuntimeError(f"MEIDB Month/Year filters could not be selected: {selected_filters}; controls={selects}")
        selected_filters["year_type"] = year_type_text or "Source default"
        await page.wait_for_timeout(200)

        form_meta = await chosen.locator("select").evaluate_all("""nodes => nodes.map(el => {
          const byId = el.id && document.querySelector(`label[for='${CSS.escape(el.id)}']`);
          const label = byId || el.closest('label');
          const marker = `${el.name||''} ${el.id||''} ${label?.innerText||''}`.toLowerCase();
          const selected = el.options[el.selectedIndex];
          return {marker,value:el.value||'',selectedText:selected?.textContent?.trim()||''};
        })""")
        unit_label = next((item["selectedText"] for item in form_meta if "value" in item["marker"] and "in" in item["marker"]), "")
        selected_filters["values_in"] = unit_label or selected_filters.get("values_in", "Source default")

        capture_enabled["value"] = True
        submit = None
        controls = chosen.locator("button,input[type='submit'],input[type='button']")
        for i in range(await controls.count()):
            control = controls.nth(i)
            label = await control.evaluate("el => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()")
            if re.fullmatch(r"submit", _clean(label), flags=re.I):
                submit = control
                break
        if submit is None:
            raise RuntimeError("MEIDB Commodity-wise Submit control not found")

        navigated = False
        navigation_html = ""
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=15000) as nav_info:
                await submit.click(timeout=7000)
            navigated = True
            nav_response = await nav_info.value
            if nav_response is not None:
                try:
                    navigation_html = await nav_response.text()
                except Exception:
                    navigation_html = ""
        except Exception:
            # AJAX/Javascript form submission is supported by the response listener.
            pass

        deadline = asyncio.get_running_loop().time() + 20.0
        parsed_tables: list[dict[str, Any]] = []
        while asyncio.get_running_loop().time() < deadline:
            if pending:
                await asyncio.gather(*list(pending), return_exceptions=True)
            parsed_tables = []
            if navigation_html:
                parsed_tables.extend(_parse_source_tables(navigation_html, "text/html", hs_code=code))
            for captured in response_payloads:
                body = captured.get("body")
                if not body:
                    continue
                text_body = body.decode("utf-8-sig", errors="replace") if isinstance(body, bytes) else str(body)
                parsed_tables.extend(_parse_source_tables(text_body, str(captured.get("content_type") or ""), hs_code=code))
            if not parsed_tables:
                try:
                    table_html = await page.locator("table").evaluate_all("tables => tables.map(t => t.outerHTML).join('\\n')")
                    parsed_tables.extend(_parse_result_tables(table_html, hs_code=code))
                except Exception:
                    parsed_tables = []

            unique = []
            seen = set()
            for table in parsed_tables:
                signature = (tuple(table.get("columns") or []), table.get("row_count"),
                             json.dumps((table.get("rows") or [])[:2], sort_keys=True, default=str))
                if signature in seen:
                    continue
                seen.add(signature)
                table["source_unit"] = unit_label
                unique.append(table)

            records = []
            for table in unique:
                for row in table.get("rows") or []:
                    hs_value = _clean(next((value for key, value in row.items() if _is_hs_header(key)), ""))
                    hs_match = re.search(r"(?<!\d)(\d{2,8})(?!\d)", hs_value)
                    hs_value = hs_match.group(1) if hs_match else ""
                    if hs_value != code:
                        continue
                    value, value_field = _numeric_fields(row, period)
                    monthly = _monthly_value_fields(row, period)
                    if value is None or monthly.get("current_year") is None:
                        continue
                    value = monthly["current_year"]
                    value_field = monthly.get("current_year_field") or value_field
                    records.append({
                        "period": period.isoformat()[:7], "hs_code": hs_value, "trade_value": value,
                        "value_field": value_field,
                        "previous_year_value": monthly.get("previous_year"),
                        "previous_year_field": monthly.get("previous_year_field"),
                        "current_year_value": monthly.get("current_year"),
                        "current_year_field": monthly.get("current_year_field"),
                        "growth_percent": monthly.get("growth_percent"),
                        "growth_field": monthly.get("growth_field"),
                        "previous_year_label": monthly.get("previous_year_label"),
                        "current_year_label": monthly.get("current_year_label"),
                        "unit": unit_label or "Source selected unit", "source_row": row,
                        "source_status": _status_from_row(row), "source_level": len(hs_value),
                        "trade_type": trade,
                    })
            if records:
                return {
                    "ok": True, "records": records, "tables": unique,
                    "period": period.isoformat()[:7],
                    "capture_method": "browser-specific-hscode-document" if navigated else "browser-specific-hscode-xhr",
                    "selected_hs_code": code, "selected_filters": selected_filters,
                    "trade_type": trade, "unit": unit_label,
                    "response_events": response_events[-60:],
                }
            await page.wait_for_timeout(500)

        body_preview = _clean(await page.locator("body").inner_text())[-1400:]
        raise RuntimeError(
            f"MEIDB {trade.upper()} Specific HSCode {code} was submitted but no matching numeric result was parsed "
            f"for {period:%Y-%m}; filters={selected_filters}; body={body_preview[:900]}; "
            f"responses={response_events[-15:]}"
        )
    finally:
        if pending:
            await asyncio.gather(*list(pending), return_exceptions=True)
        await context.close()


def _status_from_row(row: dict[str, Any]) -> str | None:
    text = " ".join(str(value) for value in row.values()).casefold()
    if re.search(r"\brevised\b", text) or re.search(r"(?:^|\s)R(?:$|\s)", text):
        return "R"
    if re.search(r"\bfinal\b", text) or re.search(r"(?:^|\s)F(?:$|\s)", text):
        return "F"
    return None



async def _scrape_impl(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    logger.info("TRADESTAT_SCRAPE_IMPL_START pid=%s filters=%s", os.getpid(), filters)
    filters = filters or {}

    def clean_codes(values: Any) -> list[str]:
        return list(dict.fromkeys(
            re.sub(r"\D", "", str(code))
            for code in (values or [])
            if re.sub(r"\D", "", str(code))
        ))[:30]

    trade_type = str(filters.get("trade_type") or "all").strip().casefold()
    focus_code = re.sub(r"\D", "", str(filters.get("focus_code") or ""))
    legacy_codes = clean_codes(filters.get("hs_codes"))
    import_codes = clean_codes(filters.get("import_hs_codes"))
    export_codes = clean_codes(filters.get("export_hs_codes"))
    if not import_codes and not export_codes and legacy_codes:
        import_codes = list(legacy_codes)
        export_codes = list(legacy_codes)
    if trade_type == "import":
        export_codes = []
    elif trade_type == "export":
        import_codes = []
    if focus_code:
        if trade_type == "export":
            export_codes = [focus_code]
            import_codes = []
        else:
            import_codes = [focus_code]
            export_codes = []

    codes_by_trade = {"import": import_codes[:30], "export": export_codes[:30]}
    all_codes = list(dict.fromkeys(import_codes + export_codes))
    detail_mode = bool(filters.get("history") or focus_code)

    base_payload = {
        "source_name": "TRADESTAT_MEIDB",
        "source_url": IMPORT_URL if trade_type == "import" else EXPORT_URL,
        "source_urls": {"import": IMPORT_URL, "export": EXPORT_URL},
    }
    if not all_codes:
        return {
            **base_payload,
            "kpi_cards": [{"key": "tradestat-watchlist-empty", "label": "Watched commodities", "display_value": "0", "value": 0}],
            "tables": [], "charts": [],
            "sector_payload": {
                "imports": [], "exports": [], "monitor_rows": [], "monitor_rows_by_trade": {"import": [], "export": []},
                "watchlist_empty": True, "diagnostics": ["Add HS codes to the separate Import or Export watchlist."],
            },
            "capture_completeness": {"status": "partial", "complete": False, "requested_queries": 0, "successful_queries": 0, "failed_queries": 0},
        }

    diagnostics: list[str] = []
    latest: date | None = None
    landing_html = ""
    period_source_url = IMPORT_URL if trade_type == "import" else EXPORT_URL
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(18.0, connect=7.0, read=12.0), follow_redirects=True) as client:
            landing = await client.get(period_source_url, headers={"Referer": "https://tradestat.commerce.gov.in/meidb/"})
            if landing.status_code < 400:
                landing_html = landing.text
                latest = _published_period(landing_html)
            else:
                diagnostics.append(f"TradeStat {trade_type} landing page returned HTTP {landing.status_code}: {period_source_url}")
    except Exception as exc:
        diagnostics.append(f"TradeStat {trade_type} landing HTTP failed: {type(exc).__name__}: {_clean(exc)[:240]}")

    if latest is None:
        browser_period, browser_period_error = await _browser_discover_period(period_source_url)
        if browser_period:
            latest = browser_period
            diagnostics.append(f"TradeStat published period discovered from browser: {latest:%Y-%m}")
        elif browser_period_error:
            diagnostics.append(browser_period_error)

    periods = _last_periods(latest, 12) if (detail_mode and latest) else ([latest] if latest else [])
    if not periods:
        diagnostics.append("TradeStat latest published period could not be determined")
        return {
            **base_payload,
            "kpi_cards": [{"key": "tradestat-count", "label": "Watched commodities", "value": len(all_codes), "display_value": str(len(all_codes))}],
            "tables": [], "charts": [],
            "sector_payload": {"imports": [], "exports": [], "monitor_rows": [], "monitor_rows_by_trade": {"import": [], "export": []}, "latest_period": None, "diagnostics": diagnostics},
            "capture_completeness": {"status": "failed", "complete": False, "requested_codes": len(all_codes), "requested_queries": 0, "successful_queries": 0, "failed_queries": 0, "diagnostics": diagnostics},
        }

    # Query each watched HS code in its own MEIDB Specific-HSCode form. The
    # source presents Import/Export as separate Trade Type tabs and the code
    # selector is mutually exclusive with All Commodities / digit-level mode.
    jobs = [
        (trade, code, period)
        for trade in ("import", "export")
        for code in codes_by_trade[trade]
        for period in periods
    ]
    logger.info(
        "TRADESTAT_SCRAPE_PLAN diag=%s pid=%s import_codes=%s export_codes=%s "
        "periods=%s mode=specific_hscode job_count=%d",
        TRADESTAT_DIAG_VERSION, os.getpid(), codes_by_trade.get("import"),
        codes_by_trade.get("export"), [p.strftime("%Y-%m") for p in periods], len(jobs),
    )
    results: dict[str, list[dict[str, Any]]] = {"import": [], "export": []}
    result_tables: dict[str, list[dict[str, Any]]] = {"import": [], "export": []}
    failed_jobs: list[tuple[str, str, date]] = []
    query_successes = 0
    query_failures = 0
    browser_used = False
    http_sem = asyncio.Semaphore(TRADESTAT_JOB_CONCURRENCY)

    async def http_collect_job(job):
        trade, hs_code, period = job
        url = IMPORT_URL if trade == "import" else EXPORT_URL
        job_started = time.monotonic()
        logger.info(
            "TRADESTAT_HTTP_JOB_START diag=%s pid=%s trade=%s hs_code=%s period=%s url=%s mode=specific_hscode",
            TRADESTAT_DIAG_VERSION, os.getpid(), trade, hs_code, period.strftime("%Y-%m"), url,
        )
        try:
            timeout = httpx.Timeout(28.0, connect=8.0, read=20.0, write=15.0, pool=10.0)
            async with http_sem:
                async with httpx.AsyncClient(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
                    result = await asyncio.wait_for(
                        _fetch_month(client, url, hs_code, period, level_digits=None),
                        timeout=TRADESTAT_JOB_TIMEOUT_SECONDS,
                    )
            logger.info(
                "TRADESTAT_HTTP_JOB_DONE pid=%s trade=%s hs_code=%s period=%s ok=%s rows=%d tables=%d "
                "method=%s status=%s content_type=%s duration_seconds=%.2f",
                os.getpid(), trade, hs_code, period.strftime("%Y-%m"), bool(result and result.get("ok")),
                len(result.get("records") or []) if isinstance(result, dict) else 0,
                len(result.get("tables") or []) if isinstance(result, dict) else 0,
                result.get("capture_method") if isinstance(result, dict) else None,
                result.get("response_status") if isinstance(result, dict) else None,
                result.get("response_content_type") if isinstance(result, dict) else None,
                time.monotonic() - job_started,
            )
            return trade, hs_code, period, result, None
        except Exception as exc:
            logger.exception(
                "TRADESTAT_HTTP_JOB_ERROR trade=%s hs_code=%s period=%s duration_seconds=%.2f",
                trade, hs_code, period.strftime("%Y-%m"), time.monotonic() - job_started,
            )
            return trade, hs_code, period, None, exc

    # The native HTTP form submit is preferred. Playwright retries only failed
    # requests and performs the same visible tab/radio/text-field interaction.
    http_results = await asyncio.gather(*(http_collect_job(job) for job in jobs), return_exceptions=False)
    for trade, hs_code, period, result, exc in http_results:
        if result and result.get("ok"):
            query_successes += 1
            results[trade].extend(result.get("records") or [])
            result_tables[trade].extend(result.get("tables") or [])
            diagnostics.append(
                f"HTTP {trade.title()} HS {hs_code} {period:%Y-%m}: {len(result.get('records') or [])} row(s); "
                f"method={result.get('capture_method')}; status={result.get('response_status')}; "
                f"content_type={result.get('response_content_type')}; unit={result.get('unit') or 'unknown'}"
            )
        else:
            failed_jobs.append((trade, hs_code, period))
            detail = (result or {}).get("warning") if isinstance(result, dict) else None
            diagnostics.append(
                f"HTTP {trade.title()} HS {hs_code} {period:%Y-%m} failed: "
                f"{type(exc).__name__}: {_clean(exc)[:220]}" if exc else
                f"HTTP {trade.title()} HS {hs_code} {period:%Y-%m} failed: {detail or 'no parsed observation'}"
            )

    if failed_jobs:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as playwright:
                browser = None
                launch_errors = []
                for launch_args in (
                    {"channel": "chrome", "headless": True},
                    {"channel": "msedge", "headless": True},
                    {"headless": True},
                ):
                    try:
                        browser = await playwright.chromium.launch(**launch_args)
                        break
                    except Exception as launch_exc:
                        launch_errors.append(f"{launch_args}: {type(launch_exc).__name__}: {_clean(launch_exc)[:100]}")
                if browser is None:
                    query_failures += len(failed_jobs)
                    diagnostics.append("TradeStat browser fallback unavailable: " + " | ".join(launch_errors[:3]))
                else:
                    browser_used = True
                    browser_sem = asyncio.Semaphore(2)

                    async def browser_job(job):
                        trade, hs_code, period = job
                        url = IMPORT_URL if trade == "import" else EXPORT_URL
                        job_started = time.monotonic()
                        logger.info(
                            "TRADESTAT_BROWSER_JOB_START pid=%s trade=%s hs_code=%s period=%s mode=specific_hscode",
                            os.getpid(), trade, hs_code, period.strftime("%Y-%m"),
                        )
                        async with browser_sem:
                            try:
                                result = await asyncio.wait_for(
                                    _browser_fetch_month_fast(browser, url, None, period, hs_code=hs_code),
                                    timeout=90.0,
                                )
                                return trade, hs_code, period, result, None
                            except Exception as browser_exc:
                                logger.exception(
                                    "TRADESTAT_BROWSER_JOB_ERROR trade=%s hs_code=%s period=%s duration_seconds=%.2f",
                                    trade, hs_code, period.strftime("%Y-%m"), time.monotonic() - job_started,
                                )
                                return trade, hs_code, period, None, browser_exc

                    browser_results = await asyncio.gather(*(browser_job(job) for job in failed_jobs))
                    for trade, hs_code, period, result, exc in browser_results:
                        if result and result.get("ok"):
                            query_successes += 1
                            results[trade].extend(result.get("records") or [])
                            result_tables[trade].extend(result.get("tables") or [])
                            response_summary = "; ".join((result.get("response_events") or [])[-4:])
                            diagnostics.append(
                                f"Browser {trade.title()} HS {hs_code} {period:%Y-%m}: "
                                f"{len(result.get('records') or [])} row(s); method={result.get('capture_method')}; "
                                f"unit={result.get('unit') or 'unknown'}; responses={response_summary or 'no response events'}"
                            )
                        else:
                            query_failures += 1
                            detail = _clean(exc)[:280] if exc else ((result or {}).get("warning") if isinstance(result, dict) else "no matching row")
                            diagnostics.append(f"Browser {trade.title()} HS {hs_code} {period:%Y-%m} failed: {detail}")
                    await browser.close()
        except Exception as exc:
            query_failures += len(failed_jobs)
            diagnostics.append(f"TradeStat browser fallback failed: {type(exc).__name__}: {_clean(exc)[:280]}")

    def dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen = set()
        out = []
        for row in rows:
            sig = (row.get("hs_code"), row.get("period"), row.get("trade_value"), row.get("previous_year_value"), row.get("current_year_value"), row.get("growth_percent"))
            if sig in seen:
                continue
            seen.add(sig)
            out.append(row)
        return sorted(out, key=lambda r: (str(r.get("period") or ""), str(r.get("hs_code") or "")))

    imports = dedupe([r for r in results["import"] if r.get("hs_code") in set(import_codes)])
    exports = dedupe([r for r in results["export"] if r.get("hs_code") in set(export_codes)])
    latest_key = latest.isoformat()[:7] if latest else None
    latest_label = latest.strftime("%b %Y") if latest else "Not available"
    if detail_mode and periods and periods[0] < date(2026, 4, 1) <= periods[-1]:
        diagnostics.append(
            "MEIDB warns that HS codes may be dropped/reallocated and units may change from April 2026. "
            "Historical points before and after April 2026 are not guaranteed to be directly comparable; "
            "the collector does not infer an HS-code crosswalk."
        )

    def build_monitor_rows(trade: str) -> list[dict[str, Any]]:
        rows = imports if trade == "import" else exports
        codes = import_codes if trade == "import" else export_codes
        by_key = {(r.get("hs_code"), r.get("period")): r for r in rows}
        output = []
        for code in codes:
            row = by_key.get((code, latest_key)) if latest_key else None
            if not row:
                continue
            source_row = row.get("source_row") or {}
            commodity = next((str(v) for k, v in source_row.items() if any(tok in str(k).casefold() for tok in ("commodity", "description")) and _clean(v)), f"HS {code}")
            output.append({
                "HSCode": code,
                "Commodity": commodity,
                "Previous Year": row.get("previous_year_value"),
                "Current Year": row.get("current_year_value") if row.get("current_year_value") is not None else row.get("trade_value"),
                "% Growth": row.get("growth_percent"),
                "Previous Year Label": row.get("previous_year_label") or (latest.strftime("%b-%Y") if latest else ""),
                "Current Year Label": row.get("current_year_label") or (latest.strftime("%b-%Y") if latest else ""),
                "Reporting period": latest_key,
                "Source unit": row.get("unit"),
                "Trade Type": trade,
            })
        return output

    monitor_by_trade = {"import": build_monitor_rows("import"), "export": build_monitor_rows("export")}
    monitor_rows = monitor_by_trade[trade_type] if trade_type in {"import", "export"} else monitor_by_trade["import"] + monitor_by_trade["export"]

    charts: list[dict[str, Any]] = []
    if detail_mode and focus_code:
        focused_rows = (imports if trade_type == "import" else exports)
        points = [{"label": r["period"], "value": r["trade_value"]} for r in focused_rows if r.get("trade_value") is not None]
        if points:
            charts.append({"key": f"tradestat-{trade_type}-history-{focus_code}", "title": f"{trade_type.title()} monthly history – HS {focus_code}", "kind": "line", "unit": "Source-reported value", "series": [{"name": trade_type.title(), "data": points}]})
        latest_row = next((r for r in focused_rows if r.get("period") == latest_key), None)
        if latest_row and latest_row.get("previous_year_value") is not None and latest_row.get("current_year_value") is not None:
            charts.append({"key": f"tradestat-{trade_type}-yoy-{focus_code}", "title": f"{trade_type.title()} latest-month YoY – HS {focus_code}", "kind": "bar", "unit": "Source-reported value", "series": [{"name": "Previous year", "data": [{"label": "Previous year", "value": latest_row.get("previous_year_value")} ]}, {"name": "Current year", "data": [{"label": "Current year", "value": latest_row.get("current_year_value")} ]}]})

    kpis = [
        {"key": "tradestat-watchlist-count", "label": "Watched commodities", "value": len(all_codes), "display_value": str(len(all_codes))},
        {"key": "tradestat-latest-period", "label": "Latest reported period", "display_value": latest_label},
        {"key": "tradestat-import-coverage", "label": "Import observations", "value": len(imports), "display_value": str(len(imports))},
        {"key": "tradestat-export-coverage", "label": "Export observations", "value": len(exports), "display_value": str(len(exports))},
    ]
    requested = len(jobs)
    complete = requested > 0 and query_successes == requested
    status = "ready" if complete else ("partial" if query_successes else "failed")
    detail_table = []
    if detail_mode and focus_code:
        focus_rows = imports if trade_type == "import" else exports
        detail_table = [{
            "Month": r.get("period"),
            "Value": r.get("trade_value"),
            "Previous Year": r.get("previous_year_value"),
            "Current Year": r.get("current_year_value"),
            "% Growth": r.get("growth_percent"),
            "Unit": r.get("unit"),
        } for r in focus_rows]

    tables = []
    if detail_table:
        tables.append({"key": "tradestat-history", "title": f"{trade_type.title()} monthly history – HS {focus_code}", "columns": list(detail_table[0].keys()), "rows": detail_table})
    elif monitor_rows:
        tables.append({"key": "tradestat-monitor", "title": "Commodity-wise Trade Monitor", "columns": list(monitor_rows[0].keys()), "rows": monitor_rows})

    logger.info("TRADESTAT_SCRAPE_IMPL_DONE pid=%s duration_seconds=%.2f import_records=%d export_records=%d import_tables=%d export_tables=%d query_successes=%d query_failures=%d diagnostics_count=%d", os.getpid(), time.monotonic()-started, len(results["import"]), len(results["export"]), len(result_tables["import"]), len(result_tables["export"]), query_successes, query_failures, len(diagnostics))
    return {
        **base_payload,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "kpi_cards": kpis,
        "charts": charts,
        "tables": tables,
        "sector_payload": {
            "latest_period": latest_key,
            "latest_period_label": latest_label,
            "imports": imports,
            "exports": exports,
            "monitor_rows": monitor_rows,
            "monitor_rows_by_trade": monitor_by_trade,
            "history_mode": detail_mode,
            "focus_code": focus_code or None,
            "trade_type": trade_type,
            "browser_used": browser_used,
            "query_successes": query_successes,
            "query_failures": max(query_failures, requested - query_successes),
            "diagnostics": diagnostics[:300],
            "periods": [p.isoformat()[:7] for p in periods],
            "collection_mode": "specific_hscode",
            "requested_hs_codes": codes_by_trade,
            "source_note": "TradeStat reports are collected by selecting the Import/Export tab and submitting each watched code in Specific HSCode mode; source monthly values, same-month prior-year values and YoY growth are preserved.",
        },
        "capture_completeness": {
            "status": status,
            "complete": complete,
            "requested_codes": len(all_codes),
            "requested_queries": requested,
            "successful_queries": query_successes,
            "failed_queries": max(query_failures, requested - query_successes),
            "import_rows": len(imports),
            "export_rows": len(exports),
            "diagnostics": diagnostics[:100],
        },
        "data_quality_notes": diagnostics[:60],
    }

async def _browser_fetch_month_fast(browser, url: str, level_digits: int | None, period: date, *, hs_code: str = "") -> dict[str, Any]:
    """Backward-compatible wrapper for the Specific-HSCode native-submit flow."""
    return await _browser_fetch_month(browser, url, level_digits, period, hs_code=hs_code)


async def scrape(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Public entry point; isolates Playwright on a Proactor loop on Windows."""
    from .playwright_compat import run_async_collector

    return await run_async_collector(_scrape_impl, filters)
