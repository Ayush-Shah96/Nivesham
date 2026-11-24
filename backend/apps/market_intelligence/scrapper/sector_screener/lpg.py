from __future__ import annotations

"""Focused PPAC petroleum / natural-gas collector.

This collector intentionally does *not* crawl PPAC's broader LPG/CNG ecosystem.
It captures only the source pages needed by the Petroleum Market Intelligence UI:

Overview
- PPAC Import/Export of Crude Oil and Petroleum Products (one combined table)
- PPAC LNG Imports (current + historical workbook, parsed in memory)

Production
- PPAC Petroleum Products Production
- PPAC Indigenous Crude Oil Production (aggregate only)
- PPAC Natural Gas Production (aggregate gross/net only)

Consumption
- PPAC Product-wise Domestic Petroleum Consumption
- PPAC Natural Gas Consumption

The collector never writes PPAC Excel files to disk. LNG workbooks are fetched into
memory and parsed directly so the dashboard does not depend on a browser download.
"""

import asyncio
import io
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

URL = "https://ppac.gov.in/import-export"

SOURCE_PAGES: dict[str, tuple[str, str]] = {
    "trade": (URL, "Import / Export of Crude Oil and Petroleum Products"),
    "lng_import": ("https://ppac.gov.in/natural-gas/import", "LNG Imports"),
    "product_production": ("https://ppac.gov.in/production/petroleum-products", "Petroleum Products Production"),
    "crude_production": ("https://ppac.gov.in/production/indigenous-crude-oil", "Indigenous Crude Oil Production"),
    "gas_production": ("https://ppac.gov.in/natural-gas/production", "Natural Gas Production"),
    "product_consumption": ("https://ppac.gov.in/consumption/products-wise", "Domestic Petroleum Products Consumption"),
    "gas_consumption": ("https://ppac.gov.in/natural-gas/consumption", "Natural Gas Consumption"),
}

PRODUCTS = (
    "Crude Oil", "LPG", "MS", "Naphtha", "ATF", "SKO", "HSD", "LOBS",
    "Fuel Oil", "Bitumen", "Petcoke", "Others",
)

MONTHS = (
    ("April", 4), ("May", 5), ("June", 6), ("July", 7), ("August", 8),
    ("September", 9), ("October", 10), ("November", 11), ("December", 12),
    ("January", 1), ("February", 2), ("March", 3),
)
MONTH_LOOKUP = {name.casefold(): (name, number) for name, number in MONTHS}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

MAX_YEARS = 12
HTTP_TIMEOUT = httpx.Timeout(35.0, connect=12.0, read=28.0, write=12.0, pool=12.0)


def _clean(value: Any) -> str:
    """Normalize any PPAC cell/value into a scalar string.

    PPAC can expose table cells as nested arrays when the underlying data is
    assembled by JavaScript. Never let those nested structures reach a
    ``str.join`` call or numeric parser as raw lists.
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        parts = [_clean(child) for child in value.values()]
        return re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()
    if isinstance(value, (list, tuple, set)):
        parts = [_clean(child) for child in value]
        return re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()
    return re.sub(r"\s+", " ", str(value)).strip()


def _join_cells(values: Any) -> str:
    """Join arbitrary source cells after scalar normalization."""
    if values is None:
        return ""
    try:
        iterator = values if isinstance(values, (list, tuple, set)) else [values]
        return " ".join(part for part in (_clean(value) for value in iterator) if part)
    except Exception:
        return _clean(values)


def _num(value: Any) -> float | None:
    text = _clean(value).replace(",", "")
    if not text or text in {"-", "—", "na", "n.a.", "nil"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _source_unit(text: str, default: str) -> str:
    value = _clean(text).casefold()
    if "thousand metric" in value or "000 mt" in value or "000 metric" in value:
        return "Thousand Metric Tonnes"
    if "mmscm" in value or "million standard cubic" in value:
        return "MMSCM"
    return default


def _calendar_year(financial_year: str, month_number: int) -> int | None:
    match = re.search(r"20(\d{2})\s*[-–/]\s*(?:20)?\d{2}", financial_year or "")
    if not match:
        year_match = re.search(r"20\d{2}", financial_year or "")
        return int(year_match.group(0)) if year_match else None
    start_year = int("20" + match.group(1))
    return start_year + (1 if month_number <= 3 else 0)


def _period_label(financial_year: str, month_name: str) -> tuple[str, int] | None:
    month = MONTH_LOOKUP.get(month_name.casefold())
    if not month:
        return None
    calendar_year = _calendar_year(financial_year, month[1])
    if calendar_year is None:
        return None
    return f"{month[0][:3]} {calendar_year}", calendar_year * 100 + month[1]


def _normalize_fy(value: str) -> str:
    text = _clean(value)
    match = re.search(r"20\d{2}\s*[-–/]\s*(?:20)?\d{2}", text)
    if match:
        raw = re.sub(r"\s+", "", match.group(0)).replace("–", "-").replace("/", "-")
        start, end = raw.split("-")
        if len(end) == 4:
            end = end[-2:]
        return f"FY {start}-{end}"
    year = re.search(r"20\d{2}", text)
    if year:
        start = int(year.group(0))
        return f"FY {start}-{str(start + 1)[-2:]}"
    return text or "FY (latest)"


def _year_rank(fy: str) -> int:
    match = re.search(r"20\d{2}", fy or "")
    return int(match.group(0)) if match else 0


def _current_financial_year() -> str:
    now = datetime.now(timezone.utc)
    start_year = now.year if now.month >= 4 else now.year - 1
    return f"FY {start_year}-{str(start_year + 1)[-2:]}"


def _canonical_product(value: str) -> str | None:
    text = re.sub(r"[^a-z0-9]+", " ", _clean(value).casefold()).strip()
    if not text:
        return None
    if "crude oil" in text or text == "crude":
        return "Crude Oil"
    if text == "lpg" or text.startswith("lpg "):
        return "LPG"
    if text in {"ms", "ms petrol", "ms petrol motor spirit", "motor spirit"} or text.startswith("ms "):
        return "MS"
    if "naphtha" in text:
        return "Naphtha"
    if text == "atf" or "aviation turbine" in text:
        return "ATF"
    if text == "sko" or "superior kerosene" in text:
        return "SKO"
    if text == "hsd" or "high speed diesel" in text:
        return "HSD"
    if "lobs" in text or "lube oil" in text or "lubricants" in text or "greases" in text:
        return "LOBS"
    if "fuel oil" in text or "fo & lshs" in text or "lshs" in text:
        return "Fuel Oil"
    if "bitumen" in text:
        return "Bitumen"
    if "petcoke" in text or "petroleum coke" in text or "cbfs" in text:
        return "Petcoke"
    if text.startswith("other"):
        return "Others"
    return None


def _fy_from_text(value: str) -> str | None:
    text = _clean(value)
    match = re.search(r"20\d{2}\s*[-–/]\s*(?:20)?\d{2}", text)
    if match:
        return _normalize_fy(match.group(0))
    return None


def _parse_table_rows(rows: list[list[Any]]) -> list[list[str]]:
    """Normalize browser/HTTP table rows, including nested list-shaped cells."""
    cleaned: list[list[str]] = []
    for row in rows or []:
        if isinstance(row, (list, tuple)):
            cells = list(row)
        elif isinstance(row, dict):
            cells = list(row.values())
        else:
            cells = [row]
        values = [_clean(cell) for cell in cells]
        while values and not values[-1]:
            values.pop()
        if any(values):
            cleaned.append(values)
    return cleaned


def _extract_html_tables(html: str) -> list[list[list[str]]]:
    soup = BeautifulSoup(html or "", "lxml")
    tables: list[list[list[str]]] = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"], recursive=False)]
            if cells:
                rows.append(cells)
        rows = _parse_table_rows(rows)
        if len(rows) >= 2:
            tables.append(rows)
    return tables


def _find_month_header(rows: list[list[str]]) -> tuple[int, list[tuple[int, str]] | None]:
    best_index = -1
    best_months: list[tuple[int, str]] = []
    for index, row in enumerate(rows[:20]):
        found = []
        for column_index, cell in enumerate(row):
            cell_text = _clean(cell)
            key = re.sub(r"[^a-z]", "", cell_text.casefold())
            for month_name, _ in MONTHS:
                if key == re.sub(r"[^a-z]", "", month_name.casefold()):
                    found.append((column_index, month_name))
                    break
        if len(found) > len(best_months):
            best_index = index
            best_months = found
    return best_index, (best_months or None)


def _find_year_select_info(selects: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in selects:
        options = item.get("options") or []
        if not options:
            continue
        year_options = [option for option in options if re.search(r"20\d{2}", str(option.get("text") or ""))]
        if len(year_options) >= 2:
            return {"index": item.get("index"), "options": year_options}
    return None


def _select_years(select_info: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not select_info:
        return [{"text": "FY (latest)", "value": None}]
    options = select_info.get("options") or []
    unique: dict[str, dict[str, Any]] = {}
    for option in options:
        label = _normalize_fy(str(option.get("text") or ""))
        unique.setdefault(label, option)
    ordered = sorted(unique.values(), key=lambda option: _year_rank(_normalize_fy(str(option.get("text") or ""))), reverse=True)
    return ordered[:MAX_YEARS]


async def _launch_browser(playwright):
    errors = []
    for launch_args in (
        {"channel": "chrome", "headless": True},
        {"channel": "msedge", "headless": True},
        {"headless": True},
    ):
        try:
            return await playwright.chromium.launch(**launch_args), None
        except Exception as exc:
            errors.append(f"{launch_args}: {type(exc).__name__}: {_clean(exc)[:160]}")
    return None, "Chromium launch failed: " + _join_cells(errors[:3])


async def _render_page(page, url: str) -> tuple[list[list[list[str]]], list[dict[str, Any]], list[str]]:
    diagnostics: list[str] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(1_200)
    except Exception as exc:
        return [], [], [f"Page load failed {url}: {type(exc).__name__}: {_clean(exc)[:240]}"]

    async def read_tables() -> list[list[list[str]]]:
        result = await page.locator("table").evaluate_all(
            """
            tables => tables.map(table => Array.from(table.querySelectorAll('tr')).map(tr =>
              Array.from(tr.querySelectorAll(':scope > th, :scope > td')).map(cell => (cell.innerText || '').trim())
            ))
            """
        )
        return [_parse_table_rows(rows) for rows in result if rows]

    tables = await read_tables()
    select_rows = await page.locator("select").evaluate_all(
        """
        selects => selects.map((el, index) => ({
          index,
          name: el.name || '',
          id: el.id || '',
          options: Array.from(el.options || []).map(option => ({text: (option.text || '').trim(), value: option.value || ''}))
        }))
        """
    )
    return tables, select_rows, diagnostics


async def _capture_dom_source(browser, url: str) -> tuple[list[tuple[str, list[list[str]]]], list[str], bool]:
    """Capture current + recent financial years from one PPAC table page."""
    context = await browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-IN", timezone_id="Asia/Kolkata")
    page = await context.new_page()
    diagnostics: list[str] = []
    outputs: list[tuple[str, list[list[str]]]] = []
    try:
        tables, selects, page_diagnostics = await _render_page(page, url)
        diagnostics.extend(page_diagnostics)
        year_select = _find_year_select_info(selects)
        years = _select_years(year_select)
        if tables:
            latest_fy = _normalize_fy(str(years[0].get("text") or "FY (latest)")) if years else "FY (latest)"
            for table in tables:
                if len(table) >= 2:
                    outputs.append((latest_fy, table))

        if year_select and year_select.get("index") is not None:
            select = page.locator("select").nth(int(year_select["index"]))
            for option in years:
                value = option.get("value")
                label = _normalize_fy(str(option.get("text") or ""))
                if not value:
                    continue
                try:
                    await select.select_option(value=str(value), timeout=7_000)
                    await page.wait_for_timeout(750)
                    current_tables = await page.locator("table").evaluate_all(
                        """
                        tables => tables.map(table => Array.from(table.querySelectorAll('tr')).map(tr =>
                          Array.from(tr.querySelectorAll(':scope > th, :scope > td')).map(cell => (cell.innerText || '').trim())
                        ))
                        """
                    )
                    for table_rows in current_tables:
                        parsed_rows = _parse_table_rows(table_rows)
                        if len(parsed_rows) >= 2:
                            outputs.append((label or "FY (latest)", parsed_rows))
                except Exception as exc:
                    diagnostics.append(f"Year {label} selection failed for {url}: {type(exc).__name__}: {_clean(exc)[:180]}")
        return outputs, diagnostics, True
    finally:
        await context.close()


def _table_with_months_for_years(raw: list[tuple[str, list[list[str]]]]) -> list[tuple[str, list[list[str]]]]:
    out = []
    for fy, rows in raw or []:
        normalized_rows = _parse_table_rows(rows)
        header_index, month_columns = _find_month_header(normalized_rows)
        if header_index >= 0 and month_columns:
            out.append((_normalize_fy(_clean(fy)), normalized_rows))
    return out


def _wide_table_chart(
    raw_tables: list[tuple[str, list[list[str]]]],
    *,
    title: str,
    section: str,
    unit_hint: str,
    row_filter,
    canonicalize_label,
    scale: float = 1.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    series_map: dict[str, dict[str, dict[str, Any]]] = {}
    captured_units: set[str] = set()
    rows_for_table: list[dict[str, Any]] = []

    for fy_raw, rows in _table_with_months_for_years(raw_tables):
        fy = _normalize_fy(fy_raw)
        header_index, month_columns = _find_month_header(rows)
        if not month_columns:
            continue
        header = rows[header_index]
        unit = _source_unit(_join_cells(rows[:header_index + 1][:3]), unit_hint)
        captured_units.add(unit)
        for source_row in rows[header_index + 1:]:
            if not source_row:
                continue
            raw_label = source_row[0] if source_row else ""
            label = canonicalize_label(raw_label) if canonicalize_label else _clean(raw_label)
            if not label or not row_filter(raw_label, source_row):
                continue
            series = series_map.setdefault(label, {})
            for column_index, month_name in month_columns:
                if column_index >= len(source_row):
                    continue
                value = _num(source_row[column_index])
                if value is None:
                    continue
                period = _period_label(fy, month_name)
                if not period:
                    continue
                point_label, sort_key = period
                scaled = value * scale
                series[point_label] = {"label": point_label, "value": scaled, "sort_key": sort_key, "financial_year": fy}
                rows_for_table.append({
                    "Financial Year": fy,
                    "Period": point_label,
                    "Metric": label,
                    "Value": scaled,
                    "Unit": "Metric Tonnes" if scale != 1.0 else (next(iter(captured_units)) if captured_units else unit_hint),
                })

    ordered_series = []
    for name in PRODUCTS if title == "Import / Export" else sorted(series_map):
        points = list(series_map.get(name, {}).values())
        points.sort(key=lambda point: point["sort_key"])
        for point in points:
            point.pop("sort_key", None)
        if points or title != "Import / Export":
            ordered_series.append({"name": name, "data": points})

    chart = {
        "key": re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-"),
        "title": title,
        "kind": "line",
        "state": "PPAC",
        "section": section,
        "unit": "Metric Tonnes" if scale != 1.0 else (next(iter(captured_units)) if captured_units else unit_hint),
        "series": ordered_series,
    }
    return chart, rows_for_table


def _extract_trade_chart(raw_tables: list[tuple[str, list[list[str]]]]) -> tuple[dict[str, Any], dict[str, Any]]:
    import_map: dict[str, dict[str, Any]] = {product: {} for product in PRODUCTS}
    export_map: dict[str, dict[str, Any]] = {product: {} for product in PRODUCTS}
    source_years: set[str] = set()
    diagnostics = []

    for fy_raw, rows in raw_tables:
        fy = _normalize_fy(fy_raw)
        source_years.add(fy)
        header_index, month_columns = _find_month_header(rows)
        if not month_columns:
            continue
        mode = ""
        for row in rows[header_index + 1:]:
            first = _clean(row[0] if row else "")
            upper = first.casefold()
            if upper.startswith("import"):
                mode = "import"
                continue
            if "product export" in upper or upper.startswith("export") or upper == "export":
                mode = "export"
                continue
            if upper in {"products", "product", "total import", "product import", "total product export", "net import", "total", "ldo"}:
                continue
            product = _canonical_product(first)
            if not product or mode not in {"import", "export"}:
                continue
            target = import_map if mode == "import" else export_map
            for column_index, month_name in month_columns:
                if column_index >= len(row):
                    continue
                value = _num(row[column_index])
                if value is None:
                    continue
                period = _period_label(fy, month_name)
                if not period:
                    continue
                point_label, sort_key = period
                target[product][point_label] = {
                    "label": point_label,
                    "value": value * 1000.0,
                    "sort_key": sort_key,
                    "financial_year": fy,
                }

    def to_chart(name: str, data_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
        series = []
        for product in PRODUCTS:
            points = list(data_map.get(product, {}).values())
            points.sort(key=lambda point: point["sort_key"])
            for point in points:
                point.pop("sort_key", None)
            # Keep the same product list for import/export. Missing source rows
            # remain absent rather than being changed into invented zeroes.
            series.append({"name": product, "data": points})
        return {
            "key": f"ppac-{name.casefold()}-petroleum-trade",
            "title": name,
            "kind": "line",
            "state": "PPAC",
            "section": "overview",
            "unit": "Metric Tonnes",
            "series": series,
        }

    import_chart = to_chart("Imports", import_map)
    export_chart = to_chart("Exports", export_map)
    import_chart["source_url"] = SOURCE_PAGES["trade"][0]
    export_chart["source_url"] = SOURCE_PAGES["trade"][0]
    if not any(series["data"] for series in import_chart["series"]):
        diagnostics.append("No PPAC import rows matched the requested product taxonomy")
    if not any(series["data"] for series in export_chart["series"]):
        diagnostics.append("No PPAC export rows matched the requested product taxonomy")
    return import_chart, export_chart


def _extract_limited_rows(raw_tables: list[tuple[str, list[list[str]]]], *, title: str, section: str, selector) -> dict[str, Any]:
    """Build a compact chart from rows selected by a source-specific predicate."""
    series_map: dict[str, list[dict[str, Any]]] = {}
    for fy_raw, rows in _table_with_months_for_years(raw_tables):
        fy = _normalize_fy(fy_raw)
        header_index, month_columns = _find_month_header(rows)
        if not month_columns:
            continue
        for row in rows[header_index + 1:]:
            if not row:
                continue
            name = selector(row[0] if row else "", row)
            if not name:
                continue
            points = series_map.setdefault(name, [])
            for column_index, month_name in month_columns:
                if column_index >= len(row):
                    continue
                value = _num(row[column_index])
                if value is None:
                    continue
                period = _period_label(fy, month_name)
                if not period:
                    continue
                label, sort_key = period
                points.append({"label": label, "value": value, "sort_key": sort_key, "financial_year": fy})

    series = []
    for name, points in sorted(series_map.items()):
        unique = {(point["label"], point["value"]): point for point in points}
        ordered = list(unique.values())
        ordered.sort(key=lambda point: point["sort_key"])
        for point in ordered:
            point.pop("sort_key", None)
        series.append({"name": name, "data": ordered})

    return {
        "key": re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-"),
        "title": title,
        "kind": "line",
        "state": "PPAC",
        "section": section,
        "unit": "Metric Tonnes",
        "series": series,
    }


def _extract_gas_rows(raw_tables: list[tuple[str, list[list[str]]]], *, title: str, section: str, allowed: dict[str, str]) -> dict[str, Any]:
    """Extract only the requested natural-gas rows.

    Natural-gas production is a special PPAC table: the Gross and Net sections
    reuse the same ``Total (A+B), incl. CBM`` row label. The parser therefore
    tracks the section heading instead of trying to infer Gross/Net from the
    total-row text alone.
    """
    series_map: dict[str, list[dict[str, Any]]] = {name: [] for name in allowed.values()}
    is_production = "production" in title.casefold()

    for fy_raw, rows in _table_with_months_for_years(raw_tables):
        fy = _normalize_fy(fy_raw)
        header_index, month_columns = _find_month_header(rows)
        if not month_columns:
            continue

        production_mode: str | None = "Gross Production" if is_production and "Gross Production" in series_map else None
        for row in rows[header_index + 1:]:
            label_text = _clean(row[0] if row else "").casefold()
            if not label_text:
                continue

            if is_production:
                if label_text.startswith("gross production"):
                    production_mode = "Gross Production"
                    continue
                if label_text.startswith("net production"):
                    production_mode = "Net Production"
                    continue
                if "total (a+b)" in label_text or label_text.startswith("total production"):
                    target = production_mode
                else:
                    target = None
            else:
                target = None
                # Match the explicit total-consumption row before the
                # ``net production`` token embedded inside its label.
                if label_text.startswith("total consumption") and "total consumption" in allowed:
                    target = allowed["total consumption"]
                elif label_text.startswith("lng import") and "lng import" in allowed:
                    target = allowed["lng import"]
                elif label_text.startswith("net production") and "net production" in allowed:
                    target = allowed["net production"]
                else:
                    for needle, display in allowed.items():
                        if needle in label_text:
                            target = display
                            break

            if not target or target not in series_map:
                continue

            for column_index, month_name in month_columns:
                if column_index >= len(row):
                    continue
                value = _num(row[column_index])
                if value is None:
                    continue
                period = _period_label(fy, month_name)
                if not period:
                    continue
                label, sort_key = period
                series_map[target].append({"label": label, "value": value, "sort_key": sort_key, "financial_year": fy})

    series = []
    for name, points in series_map.items():
        seen: set[tuple[str, float]] = set()
        ordered = []
        for point in sorted(points, key=lambda item: item["sort_key"]):
            key = (point["label"], point["value"])
            if key in seen:
                continue
            seen.add(key)
            point.pop("sort_key", None)
            ordered.append(point)
        if ordered:
            series.append({"name": name, "data": ordered})
    return {
        "key": re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-"),
        "title": title,
        "kind": "line",
        "state": "PPAC",
        "section": section,
        "unit": "MMSCM",
        "series": series,
    }


def _excel_rows(content: bytes) -> list[tuple[str, list[list[Any]]]]:
    """Read XLS/XLSX bytes without creating a local file."""
    output: list[tuple[str, list[list[Any]]]] = []
    if zipfile.is_zipfile(io.BytesIO(content)):
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        for sheet in workbook.worksheets:
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            output.append((sheet.title, rows))
        workbook.close()
        return output

    import xlrd

    workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
    try:
        for sheet in workbook.sheets():
            rows = [sheet.row_values(index) for index in range(sheet.nrows)]
            output.append((sheet.name, rows))
    finally:
        workbook.release_resources()
    return output


def _excel_lng_series(content: bytes) -> list[dict[str, Any]]:
    """Extract only LNG-import quantity from PPAC's current/historical workbooks."""
    data_points: dict[str, dict[str, Any]] = {}

    def put_point(label: str, value: float, *, financial_year: str, sort_key: int) -> None:
        data_points[str(label)] = {
            "label": str(label),
            "value": value,
            "sort_key": sort_key,
            "financial_year": financial_year,
        }

    for sheet_name, raw_rows in _excel_rows(content):
        rows = _parse_table_rows([[_clean(cell) for cell in row] for row in raw_rows])
        if not rows:
            continue

        sheet_fy = _fy_from_text(sheet_name) or next((
            _fy_from_text(_join_cells(row[:4])) for row in rows[:12]
            if _fy_from_text(_join_cells(row[:4]))
        ), None)

        # Shape 1: a wide monthly table (Month across columns).
        header_index, month_columns = _find_month_header(rows)
        if month_columns:
            for row in rows[header_index + 1:]:
                label = _clean(row[0] if row else "").casefold()
                if not label or not any(token in label for token in ("lng", "import")):
                    continue
                fy = sheet_fy or _fy_from_text(_join_cells(row)) or _current_financial_year()
                for column_index, month_name in month_columns:
                    if column_index >= len(row):
                        continue
                    value = _num(row[column_index])
                    if value is None:
                        continue
                    period = _period_label(fy, month_name)
                    if not period:
                        continue
                    label_text, sort_key = period
                    put_point(label_text, value, financial_year=fy, sort_key=sort_key)

        # Shape 2: historical/annual workbook with an explicit MMSCM column.
        # This avoids accidentally taking MMT, MMBtu, USD value, or price columns.
        mmscm_header = None
        for index, row in enumerate(rows[:20]):
            for column_index, cell in enumerate(row):
                text = _clean(cell).casefold()
                if "mmscm" in text:
                    mmscm_header = (index, column_index)
                    break
            if mmscm_header:
                break
        if mmscm_header:
            header_row, value_column = mmscm_header
            for row in rows[header_row + 1:]:
                if value_column >= len(row):
                    continue
                row_label = _clean(row[0] if row else "")
                # Annual rows are either FY labels (2019-20) or period labels
                # such as Apr-Sep 2025. Do not accept arbitrary page text.
                fy_match = re.search(r"20\d{2}\s*[-–/]\s*(?:20)?\d{2}", row_label)
                half_match = re.search(r"Apr[- ]Sep\s+20\d{2}", row_label, re.I)
                if not (fy_match or half_match):
                    continue
                value = _num(row[value_column])
                if value is None:
                    continue
                if fy_match:
                    fy = _normalize_fy(fy_match.group(0))
                    label = fy
                    sort_key = _year_rank(fy) * 100
                else:
                    year = int(re.search(r"20\d{2}", row_label).group(0))
                    fy = f"FY {year}-{str(year + 1)[-2:]}"
                    label = _clean(row_label)
                    sort_key = year * 100 + 3
                put_point(label, value, financial_year=fy, sort_key=sort_key)

        # Shape 3: long-form monthly table (one month per row).
        for row in rows:
            cells = [_clean(cell) for cell in row]
            month_index = next((index for index, cell in enumerate(cells) if cell.casefold() in MONTH_LOOKUP), None)
            if month_index is None:
                continue
            numeric = [(index, _num(cell)) for index, cell in enumerate(cells) if index != month_index and _num(cell) is not None]
            if not numeric:
                continue
            fy = next((_fy_from_text(cell) for cell in cells if _fy_from_text(cell)), sheet_fy or _current_financial_year())
            month_name = cells[month_index]
            period = _period_label(fy, month_name)
            if not period:
                continue
            label_text, sort_key = period
            # Prefer the numeric cell closest to the month column.
            value = min(numeric, key=lambda item: abs(item[0] - month_index))[1]
            put_point(label_text, value, financial_year=fy, sort_key=sort_key)

    ordered = list(data_points.values())
    ordered.sort(key=lambda point: point["sort_key"])
    for point in ordered:
        point.pop("sort_key", None)
    return ordered


async def _get_page_links(client: httpx.AsyncClient, url: str) -> list[tuple[str, str]]:
    try:
        response = await client.get(url, headers={"Referer": URL})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        links = []
        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            label = _clean(anchor.get_text(" ", strip=True))
            if not href:
                continue
            absolute = urljoin(str(response.url), href)
            if any(token in f"{label} {absolute}".casefold() for token in ("current", "historical", "lng", "import")):
                if absolute.casefold().endswith((".xls", ".xlsx", ".xlsm")) or "download" in absolute.casefold() or "uploads" in absolute.casefold():
                    links.append((absolute, label or absolute.rsplit("/", 1)[-1]))
        unique = []
        seen = set()
        for link in links:
            if link[0] in seen:
                continue
            seen.add(link[0])
            unique.append(link)
        return unique[:6]
    except Exception as exc:
        logger.warning("PPAC link discovery failed url=%s error=%s", url, exc)
        return []


async def _collect_lng_imports(client: httpx.AsyncClient) -> tuple[dict[str, Any], list[str], bool]:
    url = SOURCE_PAGES["lng_import"][0]
    diagnostics: list[str] = []
    all_points: dict[str, dict[str, Any]] = {}
    links = await _get_page_links(client, url)
    if not links:
        return {
            "key": "ppac-lng-imports",
            "title": "LNG Imports",
            "kind": "line",
            "state": "PPAC",
            "section": "overview",
            "unit": "MMSCM",
            "series": [{"name": "LNG Import", "data": []}],
        }, ["PPAC LNG import page did not expose a public workbook link"], False

    for workbook_url, label in links:
        try:
            response = await client.get(workbook_url, headers={"Referer": url})
            response.raise_for_status()
            if not response.content:
                continue
            points = _excel_lng_series(response.content)
            if points:
                diagnostics.append(f"LNG workbook parsed in memory: {label}")
            for point in points:
                all_points[point["label"]] = point
        except Exception as exc:
            diagnostics.append(f"LNG workbook parse failed {label}: {type(exc).__name__}: {_clean(exc)[:180]}")

    ordered = list(all_points.values())
    ordered.sort(key=lambda point: (point.get("financial_year", ""), point.get("label", "")))
    for point in ordered:
        point.pop("sort_key", None)
    chart = {
        "key": "ppac-lng-imports",
        "title": "LNG Imports",
        "kind": "line",
        "state": "PPAC",
        "section": "overview",
        "unit": "MMSCM",
        "source_url": url,
        "series": [{"name": "LNG Import", "data": ordered}],
    }
    return chart, diagnostics, bool(ordered)


def _source_views() -> list[dict[str, str]]:
    return [{"label": label, "url": url} for url, label in SOURCE_PAGES.values()]


async def _scrape_impl() -> dict[str, Any]:
    logger.info("PPAC_SCRAPER_START source=PPAC_PETROLEUM_NATURAL_GAS datasets=%s", ",".join(SOURCE_PAGES.keys()))
    diagnostics: list[str] = []
    charts: list[dict[str, Any]] = []
    source_success: dict[str, bool] = {key: False for key in SOURCE_PAGES}

    # Static HTTP fallback is attempted first; Playwright is used only for PPAC
    # pages whose data table is populated by the site's year/view controls.
    page_results: dict[str, list[tuple[str, list[list[str]]]]] = {}
    async with httpx.AsyncClient(headers=HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True, trust_env=True, max_redirects=8) as client:
        for key in ("trade", "product_production", "crude_production", "gas_production", "product_consumption", "gas_consumption"):
            url, _title = SOURCE_PAGES[key]
            try:
                response = await client.get(url, headers={"Referer": URL})
                response.raise_for_status()
                tables = _extract_html_tables(response.text)
                matching = []
                for table in tables:
                    header_index, month_columns = _find_month_header(table)
                    if header_index >= 0 and month_columns:
                        matching.append(("FY (latest)", table))
                if matching:
                    page_results[key] = matching
                    source_success[key] = True
            except Exception as exc:
                diagnostics.append(f"PPAC HTTP fallback failed {key}: {type(exc).__name__}: {_clean(exc)[:180]}")

        lng_chart, lng_diag, lng_ok = await _collect_lng_imports(client)
        diagnostics.extend(lng_diag)
        charts.append(lng_chart)
        source_success["lng_import"] = lng_ok

    browser_used = False
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser, browser_error = await _launch_browser(playwright)
            if browser is None:
                diagnostics.append(browser_error or "Chromium launch failed")
            else:
                browser_used = True
                for key in ("trade", "product_production", "crude_production", "gas_production", "product_consumption", "gas_consumption"):
                    url, _title = SOURCE_PAGES[key]
                    try:
                        logger.info("PPAC_SOURCE_FETCH_START dataset=%s url=%s", key, url)
                        raw, page_diag, _ = await _capture_dom_source(browser, url)
                        diagnostics.extend(page_diag)
                        if raw:
                            page_results[key] = raw
                            source_success[key] = True
                            logger.info("PPAC_SOURCE_FETCH_OK dataset=%s tables=%d", key, len(raw))
                        else:
                            logger.warning("PPAC_SOURCE_FETCH_EMPTY dataset=%s url=%s", key, url)
                    except Exception as exc:
                        diagnostics.append(f"PPAC browser capture failed {key}: {type(exc).__name__}: {_clean(exc)[:220]}")
                        logger.exception("PPAC_SOURCE_FETCH_FAILED dataset=%s url=%s", key, url)
                await browser.close()
    except Exception as exc:
        diagnostics.append(f"Playwright runtime unavailable: {type(exc).__name__}: {_clean(exc)[:220]}")

    def _parser_failure(dataset: str, exc: Exception) -> None:
        message = f"PPAC {dataset} parser failed: {type(exc).__name__}: {_clean(exc)[:220]}"
        diagnostics.append(message)
        logger.exception(message)

    # One PPAC table drives both Imports and Exports. We explicitly split the
    # IMPORT and PRODUCT EXPORT blocks; totals are excluded from product-series.
    if page_results.get("trade"):
        try:
            import_chart, export_chart = _extract_trade_chart(page_results["trade"])
            charts.extend([import_chart, export_chart])
            source_success["trade"] = any(
                series.get("data") for chart in (import_chart, export_chart)
                for series in chart.get("series", [])
            )
        except Exception as exc:
            source_success["trade"] = False
            _parser_failure("Import/Export", exc)

    # Production: keep only the required aggregate/product-level rows.
    if page_results.get("product_production"):
        try:
            product_chart, _ = _wide_table_chart(
                page_results["product_production"],
                title="Petroleum Products Production", section="production",
                unit_hint="Thousand Metric Tonnes",
                row_filter=lambda _raw, row: _canonical_product(row[0] if row else "") in PRODUCTS and _canonical_product(row[0] if row else "") != "Crude Oil",
                canonicalize_label=_canonical_product, scale=1000.0,
            )
            product_chart["source_url"] = SOURCE_PAGES["product_production"][0]
            charts.append(product_chart)
            source_success["product_production"] = any(series.get("data") for series in product_chart.get("series", []))
        except Exception as exc:
            source_success["product_production"] = False
            _parser_failure("Petroleum Products Production", exc)

    if page_results.get("crude_production"):
        try:
            chart = _extract_limited_rows(
                page_results["crude_production"], title="Indigenous Crude Oil Production", section="production",
                selector=lambda raw, _row: "Crude Oil" if "total crude" in _clean(raw).casefold() or _clean(raw).casefold() == "crude oil" else None,
            )
            chart["unit"] = "Metric Tonnes"
            # The source is published in Thousand Metric Tonnes; convert dashboard
            # values to actual metric tonnes as requested by the UI.
            for series in chart["series"]:
                for point in series["data"]:
                    point["value"] *= 1000.0
            chart["source_url"] = SOURCE_PAGES["crude_production"][0]
            charts.append(chart)
            source_success["crude_production"] = any(series.get("data") for series in chart.get("series", []))
        except Exception as exc:
            source_success["crude_production"] = False
            _parser_failure("Indigenous Crude Oil Production", exc)

    if page_results.get("gas_production"):
        try:
            gas_production_chart = _extract_gas_rows(
                page_results["gas_production"], title="Natural Gas Production", section="production",
                allowed={"total (a+b)": "Gross Production", "net production": "Net Production"},
            )
            gas_production_chart["source_url"] = SOURCE_PAGES["gas_production"][0]
            charts.append(gas_production_chart)
            source_success["gas_production"] = any(series.get("data") for series in gas_production_chart.get("series", []))
        except Exception as exc:
            source_success["gas_production"] = False
            _parser_failure("Natural Gas Production", exc)

    # Consumption: all product rows from the PPAC source, excluding TOTAL.
    if page_results.get("product_consumption"):
        try:
            consumption_chart, _ = _wide_table_chart(
                page_results["product_consumption"],
                title="Domestic Petroleum Products Consumption", section="consumption",
                unit_hint="Thousand Metric Tonnes",
                row_filter=lambda raw, _row: _canonical_product(raw) is not None and _canonical_product(raw) != "Crude Oil",
                canonicalize_label=lambda raw: _canonical_product(raw), scale=1000.0,
            )
            consumption_chart["source_url"] = SOURCE_PAGES["product_consumption"][0]
            charts.append(consumption_chart)
            source_success["product_consumption"] = any(series.get("data") for series in consumption_chart.get("series", []))
        except Exception as exc:
            source_success["product_consumption"] = False
            _parser_failure("Domestic Petroleum Products Consumption", exc)

    if page_results.get("gas_consumption"):
        try:
            gas_consumption_chart = _extract_gas_rows(
                page_results["gas_consumption"], title="Natural Gas Consumption", section="consumption",
                allowed={"net production": "Net Production", "lng import": "LNG Import", "total consumption": "Total Consumption"},
            )
            gas_consumption_chart["source_url"] = SOURCE_PAGES["gas_consumption"][0]
            charts.append(gas_consumption_chart)
            source_success["gas_consumption"] = any(series.get("data") for series in gas_consumption_chart.get("series", []))
        except Exception as exc:
            source_success["gas_consumption"] = False
            _parser_failure("Natural Gas Consumption", exc)

    # Drop empty charts. Keep only the eight required analytical datasets:
    # 2 trade blocks + 1 LNG chart + 3 production charts + 2 consumption charts.
    charts = [chart for chart in charts if any(series.get("data") for series in chart.get("series") or [])]

    kpis: list[dict[str, Any]] = []
    for chart in charts:
        for series in chart.get("series") or []:
            points = series.get("data") or []
            if not points:
                continue
            latest = points[-1]
            kpis.append({
                "key": f"{chart['key']}-{re.sub(r'[^a-z0-9]+', '-', str(series['name']).casefold()).strip('-')}",
                "label": f"{chart['title']} · {series['name']}",
                "value": latest.get("value"),
                "display_value": f"{latest.get('value'):,.0f}" if isinstance(latest.get("value"), (int, float)) else str(latest.get("value")),
                "unit": chart.get("unit"),
                "period_basis": latest.get("label"),
                "section": chart.get("section"),
            })
            if len(kpis) >= 12:
                break
        if len(kpis) >= 12:
            break

    available_years = sorted({
        str(point.get("financial_year"))
        for chart in charts for series in chart.get("series") or []
        for point in series.get("data") or [] if point.get("financial_year")
    }, key=_year_rank, reverse=True)
    required_keys = set(SOURCE_PAGES)
    successful_sources = sum(1 for value in source_success.values() if value)
    required_chart_keys = {
        "ppac-imports-petroleum-trade",
        "ppac-exports-petroleum-trade",
        "ppac-lng-imports",
        "petroleum-products-production",
        "indigenous-crude-oil-production",
        "natural-gas-production",
        "domestic-petroleum-products-consumption",
        "natural-gas-consumption",
    }
    chart_keys = {str(chart.get("key") or "") for chart in charts}
    complete = (
        required_keys.issubset({key for key, ok in source_success.items() if ok})
        and required_chart_keys.issubset(chart_keys)
    )

    refreshed_at = datetime.now(timezone.utc).isoformat()
    logger.info(
        "PPAC_SCRAPER_COMPLETE complete=%s charts=%d successful_sources=%s failed_sources=%s",
        complete, len(charts),
        [key for key, ok in source_success.items() if ok],
        [key for key, ok in source_success.items() if not ok],
    )
    return {
        "source_name": "PPAC_PETROLEUM_NATURAL_GAS",
        "source_url": URL,
        "source_urls": {key: value[0] for key, value in SOURCE_PAGES.items()},
        "source_views": _source_views(),
        "page_title": "Petroleum & Natural Gas Market Intelligence",
        "captured_at": refreshed_at,
        "kpi_cards": kpis,
        "charts": charts,
        "tables": [],
        "sections": ["Overview", "Production", "Consumption"],
        "available_years": available_years,
        "sector_payload": {
            "schema_version": "ppac-petroleum-gas-v2",
            "sources": _source_views(),
            "scrape_method": "Targeted PPAC DOM capture for source tables + in-memory LNG workbook parsing",
            "browser_used": browser_used,
            "requested_datasets": [
                "PPAC combined import/export table",
                "PPAC LNG imports",
                "PPAC petroleum-products production",
                "PPAC indigenous crude-oil production aggregate",
                "PPAC natural-gas production aggregate",
                "PPAC petroleum-products domestic consumption",
                "PPAC natural-gas consumption",
            ],
            "successful_sources": [key for key, ok in source_success.items() if ok],
            "failed_sources": [key for key, ok in source_success.items() if not ok],
            "available_years": available_years,
            "diagnostics": diagnostics[:200],
            "refreshed_at": refreshed_at,
        },
        "capture_completeness": {
            "status": "ready" if complete else "partial" if charts else "failed",
            "complete": complete,
            "chart_count": len(charts),
            "required_chart_count": len(required_chart_keys),
            "successful_source_count": successful_sources,
            "attempted_source_count": len(SOURCE_PAGES),
            "successful_sources": [key for key, ok in source_success.items() if ok],
            "failed_sources": [key for key, ok in source_success.items() if not ok],
            "diagnostics": diagnostics[:120],
        },
        "data_quality_notes": [
            "Import and export are parsed from the single PPAC Import/Export table; summary rows are excluded from product series.",
            "PPAC export does not publish a crude-oil row in the product-export block; the dashboard does not invent a crude-oil export value.",
            "LNG workbook responses are parsed in memory and are never saved as files by this collector.",
            "Petroleum quantities reported by PPAC in Thousand Metric Tonnes are converted to Metric Tonnes for the chart payload.",
            *diagnostics[:20],
        ],
    }


async def scrape() -> dict[str, Any]:
    from .playwright_compat import run_async_collector

    try:
        return await run_async_collector(_scrape_impl)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Never let one malformed PPAC response crash the sector worker. Return
        # a valid partial payload so the service can retain the last-good cache
        # and schedule the next attempt.
        message = f"PPAC collector failed safely: {type(exc).__name__}: {_clean(exc)[:240]}"
        logger.exception(message)
        return {
            "source_name": "PPAC_PETROLEUM_NATURAL_GAS",
            "source_url": URL,
            "source_urls": {key: value[0] for key, value in SOURCE_PAGES.items()},
            "source_views": _source_views(),
            "page_title": "Petroleum & Natural Gas Market Intelligence",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "kpi_cards": [],
            "charts": [],
            "tables": [],
            "sections": ["Overview", "Production", "Consumption"],
            "available_years": [],
            "sector_payload": {
                "schema_version": "ppac-petroleum-gas-v2",
                "scrape_method": "Targeted PPAC capture",
                "successful_sources": [],
                "failed_sources": list(SOURCE_PAGES),
                "diagnostics": [message],
            },
            "capture_completeness": {
                "status": "failed",
                "complete": False,
                "chart_count": 0,
                "required_chart_count": 8,
                "successful_source_count": 0,
                "attempted_source_count": len(SOURCE_PAGES),
                "successful_sources": [],
                "failed_sources": list(SOURCE_PAGES),
                "diagnostics": [message],
            },
            "data_quality_notes": [message],
        }
