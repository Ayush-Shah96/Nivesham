from __future__ import annotations

"""Structured Sector Screener extraction.

Captures public report pages, then emits only report data primitives for the
first-party dashboard: KPI records, chart datasets, tables and section names.
Publisher navigation, headers, ads, chat widgets and page chrome are excluded.
"""

import ast
import json
import logging
import re
from typing import Any, Iterable
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from ..common import capture_source, clean_text, parse_number, _find_chart_models

logger = logging.getLogger(__name__)

_EXCLUDED = (
    "chat", "chatbot", "assistant", "cookie", "consent", "advert", "ads-", "ad-",
    "popup", "modal", "sticky-header", "site-header", "site-footer", "navigation",
    "navbar", "menu", "breadcrumb", "social-share", "subscribe", "newsletter",
)
_KPI_SELECTORS = (
    "[data-kpi]", "[data-metric]", "[class*='kpi-card' i]", "[class*='kpi-item' i]",
    "[class*='stat-card' i]", "[class*='metric-card' i]", "[class*='summary-card' i]",
    "[class*='indicator-card' i]", "[class*='value-card' i]", "[class*='highlight-card' i]",
)
_VALUE_SELECTORS = (
    "[class*='kpi-value' i]", "[class*='stat-value' i]", "[class*='metric-value' i]",
    "[class*='value' i]", "[class*='number' i]", "[data-value]", "strong", "b",
)
_LABEL_SELECTORS = (
    "[class*='kpi-label' i]", "[class*='stat-label' i]", "[class*='metric-label' i]",
    "[class*='label' i]", "[class*='title' i]", "small", "h3", "h4", "h5", "h6",
)
_UNIT_SELECTORS = ("[class*='unit' i]", "[class*='suffix' i]", "[data-unit]", "sup")
_CHANGE_SELECTORS = ("[class*='change' i]", "[class*='trend' i]", "[class*='yoy' i]", "[class*='growth' i]")


def _is_excluded(node: Tag) -> bool:
    # Unadorned structural elements such as <html>, <body>, <main> and
    # headings are content containers, not exclusions. The old implementation
    # treated every attribute-free element as excluded and decomposed the entire
    # document before looking for KPI cards.
    if not isinstance(node, Tag):
        return False
    if node.name in {"nav", "header", "footer", "aside"}:
        return True
    attrs = getattr(node, "attrs", None) or {}
    values = " ".join([
        str(node.name or ""), str(attrs.get("id") or ""), " ".join(attrs.get("class") or []),
        str(attrs.get("role") or ""), str(attrs.get("aria-label") or ""),
    ]).casefold()
    return any(token in values for token in _EXCLUDED)


def _number_text(value: str) -> bool:
    return parse_number(value) is not None and bool(re.search(r"\d", value))


def _first_text(node: Tag, selectors: Iterable[str], *, max_len: int = 120) -> str:
    for selector in selectors:
        try:
            for child in node.select(selector)[:8]:
                text = clean_text(child.get_text(" ", strip=True))
                # Labels often contain reporting years (e.g. FY 2024-25);
                # reject only pure numeric value strings, not labels with digits.
                if text and len(text) <= max_len and not ( _number_text(text) and not re.search(r"[A-Za-z]", text) ):
                    return text
        except Exception:
            continue
    return ""


def _first_value(node: Tag) -> str:
    for selector in _VALUE_SELECTORS:
        try:
            for child in node.select(selector)[:12]:
                text = clean_text(child.get("data-value") or child.get_text(" ", strip=True))
                if text and _number_text(text) and len(text) <= 100:
                    return text
        except Exception:
            continue
    # The whole candidate must be concise; this prevents analyst notes and
    # descriptive panels containing incidental numbers from becoming KPIs.
    text = clean_text(node.get_text(" ", strip=True))
    if len(text) <= 150:
        matches = re.findall(r"(?:₹|Rs\.?\s*)?[-+]?\d[\d,]*(?:\.\d+)?\s*(?:%|\$B|USD\s?Bn|INR|Cr|Lakh\s?Cr|000'?MT|MT|MW|GW|units?)?", text, re.I)
        return matches[0].strip() if matches else ""
    return ""


def _extract_kpis(html: str, *, page_url: str, state: str) -> list[dict[str, Any]]:
    """Extract KPI cards without expensive CSS selector evaluation.

    Parivahan snapshots can contain a very large/deep DOM. Repeated SoupSieve
    substring/attribute selectors over that tree were the exact hot path seen
    in the production KeyboardInterrupt traceback. A single linear DOM pass
    is predictable and avoids that pathological selector matching cost.
    """
    import time
    started = time.monotonic()
    html = html or ""
    if not html:
        logger.info("KPI_EXTRACT_SKIP state=%s reason=empty_html", state)
        return []

    logger.info("KPI_EXTRACT_START state=%s html_chars=%s", state, len(html))
    soup = BeautifulSoup(html, "lxml")
    all_nodes = soup.find_all(True)
    for index, node in enumerate(all_nodes):
        if _is_excluded(node):
            node.decompose()
        if index and index % 5000 == 0:
            logger.debug("KPI_EXTRACT_DOM_PROGRESS state=%s scanned=%s elapsed=%.2fs", state, index, time.monotonic()-started)

    def attr_text(node: Tag) -> str:
        attrs = node.attrs or {}
        classes = attrs.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        return " ".join([str(attrs.get("id") or ""), " ".join(map(str, classes)),
                          str(attrs.get("role") or ""), str(attrs.get("aria-label") or "")]).casefold()

    def is_kpi_candidate(node: Tag) -> bool:
        attrs = node.attrs or {}
        marker = attr_text(node)
        return (
            "data-kpi" in attrs or "data-metric" in attrs
            or any(token in marker for token in ("kpi-card", "kpi-item", "stat-card", "metric-card",
                   "summary-card", "indicator-card", "value-card", "highlight-card"))
        )

    candidates = [node for node in soup.find_all(True) if is_kpi_candidate(node) and not _is_excluded(node)]
    # Keep leaf-most KPI cards; wrappers containing individual cards are omitted.
    candidate_ids = {id(node) for node in candidates}
    candidates = [node for node in candidates if not any(id(child) in candidate_ids for child in node.find_all(True))]

    if not candidates:
        # Conservative fallback: inspect at most 250 generic card nodes using
        # Python attribute checks instead of SoupSieve CSS selectors.
        for node in soup.find_all(True):
            marker = attr_text(node)
            if "card" not in marker:
                continue
            text_value = clean_text(node.get_text(" ", strip=True))
            if 8 <= len(text_value) <= 130 and _first_value(node) and _first_text(node, _LABEL_SELECTORS):
                candidates.append(node)
                if len(candidates) >= 250:
                    break

    logger.info("KPI_EXTRACT_CANDIDATES state=%s candidates=%s dom_nodes=%s elapsed=%.2fs", state, len(candidates), len(all_nodes), time.monotonic()-started)
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, node in enumerate(candidates):
        text_value = clean_text(node.get_text(" ", strip=True))
        if not text_value or len(text_value) > 220 or any(token in text_value.casefold() for token in _EXCLUDED):
            continue
        value_text = _first_value(node)
        label = clean_text(node.get("data-kpi") or node.get("data-metric") or node.get("aria-label") or _first_text(node, _LABEL_SELECTORS))
        unit = _first_text(node, _UNIT_SELECTORS, max_len=45)
        change = _first_text(node, _CHANGE_SELECTORS, max_len=80)
        if not change:
            remainder = text_value.replace(label, "", 1).replace(value_text, "", 1)
            change_match = re.search(r"(?:[▲▼↑↓+−-]\s*)?\d+(?:\.\d+)?\s*%\s*(?:YoY|MoM|QoQ|growth)?", remainder, re.I)
            if change_match:
                change = clean_text(change_match.group(0))
        display_value = value_text + (f" {unit}" if unit and unit.casefold() not in value_text.casefold() else "")
        if not value_text or not label or label.casefold() == value_text.casefold() or len(label) > 100 or len(value_text) > 80:
            continue
        identity = (label.casefold(), value_text.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        output.append({"key": f"{state.lower().replace(' ', '-')}-kpi-{len(output)+1}",
                       "label": label, "display_value": display_value, "value": parse_number(value_text),
                       "change": change or None, "unit": unit or None, "text": text_value,
                       "state": state, "url": page_url})
        if index and index % 10 == 0:
            logger.debug("KPI_EXTRACT_PROGRESS state=%s processed=%s emitted=%s elapsed=%.2fs", state, index, len(output), time.monotonic()-started)
    logger.info("KPI_EXTRACT_COMPLETE state=%s emitted=%s elapsed=%.2fs", state, len(output), time.monotonic()-started)
    return output[:80]


def _balanced_expression(text: str, start: int) -> str | None:
    """Return one balanced JS object/array starting at or after start."""
    opening = next((i for i in range(start, len(text)) if text[i] in "{["), None)
    if opening is None:
        return None
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    i = opening
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if line_comment:
            if ch == "\n": line_comment = False
            i += 1; continue
        if block_comment:
            if ch == "*" and nxt == "/": block_comment = False; i += 2; continue
            i += 1; continue
        if quote:
            if escaped: escaped = False
            elif ch == "\\": escaped = True
            elif ch == quote: quote = None
            i += 1; continue
        if ch == "/" and nxt == "/": line_comment = True; i += 2; continue
        if ch == "/" and nxt == "*": block_comment = True; i += 2; continue
        if ch in "'\"`": quote = ch; i += 1; continue
        if ch in "{[": stack.append(ch)
        elif ch in "}]":
            if not stack: return None
            expected = "{" if ch == "}" else "["
            if stack[-1] != expected: return None
            stack.pop()
            if not stack:
                return text[opening:i + 1]
        i += 1
    return None


def _load_js_object(text: str) -> Any:
    if not text:
        return None
    try:
        import json5  # optional parser for JS object literals
        return json5.loads(text)
    except Exception:
        try:
            return json.loads(text)
        except Exception:
            return None


def _resolve_js_literal(expression: str, script: str, *, depth: int = 0) -> Any:
    """Resolve a literal or a simple variable used by a chart initializer.

    DevExtreme MVC views often declare ``var chartData = [...]`` in one
    script block and then configure ``dataSource: chartData`` in another.
    Parsing each initializer in isolation leaves the chart with no rows.
    This intentionally resolves only bounded literal declarations and
    JSON.parse(string-literal), never evaluates JavaScript.
    """
    if depth > 4:
        return None
    candidate = str(expression or "").strip().rstrip(";,")
    if not candidate:
        return None
    parsed = _load_js_object(candidate)
    if parsed is not None:
        return parsed

    json_parse = re.fullmatch(r"""JSON\s*\.\s*parse\s*\(\s*(['"])(.*)\1\s*\)""", candidate, re.S | re.I)
    if json_parse:
        encoded = json_parse.group(2)
        delimiter = json_parse.group(1)
        try:
            decoded = ast.literal_eval(delimiter + encoded + delimiter)
        except Exception:
            try:
                decoded = json.loads('"' + encoded + '"')
            except Exception:
                decoded = encoded
        nested = _load_js_object(decoded) if isinstance(decoded, str) else None
        if nested is not None:
            return nested

    # Resolve a plain identifier or window/global property assignment from
    # this same script bundle. This covers `dataSource: chartData` without
    # interpreting arbitrary expressions or executing page code.
    identifier = re.fullmatch(r"(?:window\s*\.\s*)?([A-Za-z_$][\w$]*)", candidate)
    if identifier:
        name = re.escape(identifier.group(1))
        declarations = list(re.finditer(
            rf"(?:\b(?:var|let|const)\s+{name}|(?:window\s*\.\s*{name}))\s*=\s*",
            script,
        ))
        if declarations:
            declaration = declarations[-1]
            literal = _balanced_expression(script, declaration.end())
            if literal:
                return _resolve_js_literal(literal, script, depth=depth + 1)

    # DataSource / ArrayStore wrappers used by DevExtreme MVC may contain
    # `store: { data: rows }` or `new ArrayStore({ data: rows })`. Resolve the
    # actual bounded data property, not the surrounding executable expression.
    nested_data_match = re.search(r"(?:[\"\']?data[\"\']?)\s*:\s*", candidate, re.I)
    if nested_data_match:
        nested_expression = _js_property_expression(candidate, nested_data_match.end())
        nested_value = _resolve_js_literal(nested_expression, script, depth=depth + 1)
        if isinstance(nested_value, (dict, list)):
            return nested_value

    # Some MVC templates use a zero-argument factory for the local data array.
    function_return = re.match(r"function\s*\([^)]*\)\s*\{", candidate, re.S)
    if function_return:
        return_match = re.search(r"\breturn\s+", candidate[function_return.end():], re.S)
        if return_match:
            start = function_return.end() + return_match.end()
            literal = _balanced_expression(candidate, start)
            if literal:
                return _resolve_js_literal(literal, script, depth=depth + 1)
    return None


def _js_property_expression(text: str, start: int) -> str:
    """Read one JS object-property expression without consuming later options."""
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    expression_start = index
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    while index < len(text):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"`":
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if char == "}" and not stack:
                break
            if stack:
                stack.pop()
        elif char == "," and not stack:
            break
        index += 1
    return text[expression_start:index].strip()


def _chart_configs_from_script(script: str) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    markers = (
        r"new\s+Chart\s*\(", r"Highcharts\.(?:stockChart|chart)\s*\(",
        r"(?:echartsInstance|myChart|chart)\.setOption\s*\(", r"\.setOption\s*\(",
        r"new\s+ApexCharts\s*\(", r"Plotly\.newPlot\s*\(",
        # NISE's Summary drilldowns use DevExtreme MVC chart initializers.
        # Those are ordinary jQuery widget calls, not Chart.js/Highcharts
        # constructors, so the generic engine previously missed them.
        r"\.dx(?:Pie)?Chart\s*\(",
        r"DevExpress\.viz\.dx(?:Pie)?Chart\s*\(",
    )
    for pattern in markers:
        for match in re.finditer(pattern, script, re.I):
            expression = _balanced_expression(script, match.end())
            model = _load_js_object(expression or "")
            if isinstance(model, dict):
                # Chart.js wraps the actual datasets below data; the other
                # supported libraries generally expose series at the root.
                if any(key in model for key in ("series", "data", "datasets", "options", "dataSource", "data_source")):
                    models.append(model)
            elif isinstance(model, list):
                models.append({"data": model})
    # Even when a chart options object contains callback functions that are
    # not valid JSON5, its numeric series and category arrays are usually plain
    # data literals. Parse those sub-arrays independently as a fallback.
    for series_match in re.finditer(r"(?:series|datasets)\s*:\s*", script, re.I):
        parsed_series = _load_js_object(_balanced_expression(script, series_match.end()) or "")
        if not isinstance(parsed_series, list) or not parsed_series:
            continue
        labels_match = re.search(r"(?:labels|categories)\s*:\s*", script, re.I)
        parsed_labels = _load_js_object(_balanced_expression(script, labels_match.end()) or "") if labels_match else None
        chart_type_match = re.search(r"(?:type|chart\s*:\s*\{[^}]*type)\s*:\s*['\"](bar|line|area|pie|donut|doughnut|scatter)['\"]", script, re.I)
        models.append({"type": chart_type_match.group(1).lower() if chart_type_match else "bar",
                       "labels": parsed_labels if isinstance(parsed_labels, list) else [],
                       "series": parsed_series})
        if len(models) >= 100:
            break

    # DevExtreme MVC initializers sometimes contain callbacks that JSON5 cannot
    # parse as one object. Resolve each dxChart initializer independently and
    # recover its literal dataSource/series options, including variables declared
    # in another script block within the same HTML fragment.
    for dx_match in re.finditer(r"\.dx(Pie)?Chart\s*\(", script, re.I):
        expression = _balanced_expression(script, dx_match.end())
        if not expression:
            continue
        direct_model = _load_js_object(expression)
        if isinstance(direct_model, dict) and any(
            key in direct_model for key in ("series", "dataSource", "data_source")
        ):
            direct_model.setdefault("type", "pie" if dx_match.group(1) else "bar")
            direct_model.setdefault("library", "DevExtreme")
            models.append(direct_model)
            continue

        data_source_match = re.search(r"(?:[\"\']?dataSource[\"\']?)\s*:\s*", expression, re.I)
        series_match = re.search(r"(?:[\"\']?series[\"\']?)\s*:\s*", expression, re.I)
        template_match = re.search(r"(?:[\"\']?seriesTemplate[\"\']?)\s*:\s*", expression, re.I)
        if not data_source_match or not (series_match or template_match):
            continue
        source_expression = _js_property_expression(expression, data_source_match.end())
        parsed_source = _resolve_js_literal(source_expression, script)
        parsed_series = None
        if series_match:
            series_expression = _js_property_expression(expression, series_match.end())
            parsed_series = _resolve_js_literal(series_expression, script)
        parsed_template = None
        if template_match:
            template_expression = _js_property_expression(expression, template_match.end())
            parsed_template = _resolve_js_literal(template_expression, script)
        common_match = re.search(r"(?:[\"\']?commonSeriesSettings[\"\']?)\s*:\s*", expression, re.I)
        parsed_common = _resolve_js_literal(
            _js_property_expression(expression, common_match.end()), script
        ) if common_match else None
        value_axis_match = re.search(r"(?:[\"\']?valueAxis[\"\']?)\s*:\s*", expression, re.I)
        parsed_value_axis = _resolve_js_literal(
            _js_property_expression(expression, value_axis_match.end()), script
        ) if value_axis_match else None
        title_match = re.search(
            r"(?:[\"\']?title[\"\']?)\s*:\s*(?:\{\s*(?:[\"\']?text[\"\']?)\s*:\s*)?['\"]([^'\"]{1,180})",
            expression,
            re.I,
        )
        has_explicit_series = isinstance(parsed_series, (dict, list))
        has_template = isinstance(parsed_template, dict) and bool(parsed_template.get("nameField"))
        if isinstance(parsed_source, (dict, list)) and (has_explicit_series or has_template):
            model = {
                "type": "pie" if dx_match.group(1) else "bar",
                "title": title_match.group(1) if title_match else "",
                "dataSource": parsed_source,
                "series": parsed_series if has_explicit_series else [],
                "commonSeriesSettings": parsed_common if isinstance(parsed_common, dict) else {},
                "valueAxis": parsed_value_axis,
                "library": "DevExtreme",
            }
            if has_template:
                model["seriesTemplate"] = parsed_template
            models.append(model)

    # Named data/config objects are common when a report uses a helper factory.
    # Keep this as a conservative fallback and require actual chart-like keys.
    for match in re.finditer(r"(?:const|let|var)\s+[\w$]*(?:chart|series|dataset|trend|plot)[\w$]*\s*=\s*", script, re.I):
        expression = _balanced_expression(script, match.end())
        model = _load_js_object(expression or "")
        if isinstance(model, dict) and any(k in model for k in ("series", "datasets", "labels", "categories")):
            models.append(model)
        elif isinstance(model, list) and len(model) >= 2:
            models.append({"labels": [str(i + 1) for i in range(len(model))], "series": [{"name": "Value", "data": model}]})
    return models[:100]


def _normalise_chart(
    model: dict[str, Any],
    *,
    title: str,
    state: str,
    page_url: str,
    period_year: int | None = None,
) -> dict[str, Any] | None:
    """Normalize common chart-library configs, including DevExtreme dataSource rows."""
    # Unwrap common framework/config wrappers.
    for wrapper in ("option", "options", "config"):
        nested = model.get(wrapper)
        if isinstance(nested, dict) and any(
            key in nested for key in ("series", "data", "datasets", "dataSource", "data_source")
        ):
            if wrapper == "options" and model.get("data"):
                break
            model = nested
            break

    def records_from(source: Any, depth: int = 0) -> list[dict[str, Any]]:
        if depth > 6:
            return []
        if isinstance(source, list):
            dict_rows = [row for row in source[:5000] if isinstance(row, dict)]
            if len(dict_rows) >= 1:
                return dict_rows
            # DevExtreme can also use tuples/arrays with an explicitly supplied
            # category list. These are converted later when labels are present.
            return []
        if isinstance(source, dict):
            for key in ("items", "rows", "data", "store", "_array", "array", "result", "d"):
                if key in source:
                    found = records_from(source.get(key), depth + 1)
                    if found:
                        return found
        return []

    def field_match(keys: list[str], pattern: str, *, exclude: str = "") -> str | None:
        for key in keys:
            normalized = re.sub(r"[^a-z0-9]+", "", key.casefold())
            if re.search(pattern, normalized) and (not exclude or not re.search(exclude, normalized)):
                return key
        return None

    def numeric_value(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", str(value).replace("\u00a0", " "))
            if not match:
                return None
            try:
                number = float(match.group(0).replace(",", ""))
            except ValueError:
                return None
        return number if number == number and abs(number) != float("inf") else None

    data_obj = model.get("data") if isinstance(model.get("data"), dict) else {}
    options = model.get("options") if isinstance(model.get("options"), dict) else {}
    xaxis = model.get("xAxis") or model.get("xaxis") or model.get("x_axis") or options.get("xaxis") or options.get("xAxis") or {}
    if isinstance(xaxis, list):
        xaxis = xaxis[0] if xaxis else {}
    if not isinstance(xaxis, dict):
        xaxis = {}
    labels = model.get("labels") or data_obj.get("labels") or xaxis.get("categories") or xaxis.get("data") or options.get("labels") or []
    if not isinstance(labels, list):
        labels = []

    raw_series = model.get("series") or data_obj.get("datasets") or model.get("datasets") or []
    if isinstance(raw_series, dict):
        raw_series = [raw_series]
    if not isinstance(raw_series, list):
        raw_series = []
    # Plotly traces have x and y rather than categories and data.
    if not raw_series and isinstance(model.get("data"), list):
        raw_series = model["data"]
    if raw_series and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in raw_series):
        raw_series = [{"name": "Value", "data": raw_series}]

    # DevExtreme charts keep row data under dataSource and point fields under
    # series[].argumentField/valueField (or commonSeriesSettings). NISE also
    # uses seriesTemplate.nameField to group rows; expand those groups below.
    data_source = (
        model.get("dataSource")
        or model.get("data_source")
        or options.get("dataSource")
        or data_obj.get("dataSource")
    )
    rows = records_from(data_source)
    common_settings = model.get("commonSeriesSettings") or model.get("common_series_settings") or {}
    if not isinstance(common_settings, dict):
        common_settings = {}

    # NISE's DevExtreme MVC fragments use `seriesTemplate: { nameField:
    # "SeriesText" }` rather than an explicit `series` array. The template
    # creates one visible series for each distinct SeriesText value in the
    # ArrayStore data. Materialize those groups as ordinary series descriptors
    # so the existing row-to-point normalizer can process the real XHR payload.
    series_template = model.get("seriesTemplate") or model.get("series_template") or {}
    if rows and not raw_series and isinstance(series_template, dict):
        name_field = clean_text(series_template.get("nameField") or series_template.get("name_field"))
        argument_field = clean_text(common_settings.get("argumentField") or model.get("argumentField"))
        value_field = clean_text(common_settings.get("valueField") or model.get("valueField"))
        if name_field and argument_field and value_field and all(
            field in rows[0] for field in (name_field, argument_field, value_field)
        ):
            group_names = list(dict.fromkeys(
                clean_text(row.get(name_field)) for row in rows
                if clean_text(row.get(name_field))
            ))[:32]
            raw_series = [{
                "name": group_name,
                "argumentField": argument_field,
                "valueField": value_field,
                "_series_template_filter": {name_field: group_name},
                "type": common_settings.get("type") or model.get("type") or "bar",
            } for group_name in group_names]

    if rows and raw_series:
        converted_series: list[dict[str, Any]] = []
        keys = list(dict.fromkeys(key for row in rows[:100] for key in row.keys()))
        default_argument = (
            common_settings.get("argumentField")
            or model.get("argumentField")
            or field_match(keys, r"month|date|period|category|label|argument|manufacturer|company|maker")
        )
        # NISE CellChart/PnlMfgChart use xValue as a display rank ("01", "02",
        # ...) but expose the actual manufacturer name separately in tagValue.
        # DevExtreme's tooltip reads tagValue even though the category axis uses
        # xValue. For market-intelligence output the company name is the useful
        # category. Only switch when tagValue is populated for most rows and is
        # clearly non-period text; WaferToCell has null tagValue and keeps xValue.
        tag_value_field = next((key for key in keys if re.sub(r"[^a-z0-9]+", "", key.casefold()) == "tagvalue"), None)
        if tag_value_field and rows:
            tag_values = [clean_text(row.get(tag_value_field)) for row in rows[:500] if clean_text(row.get(tag_value_field))]
            if tag_values:
                period_like = sum(1 for value in tag_values if re.fullmatch(
                    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|0?[1-9]|1[0-2]|20\d{2}(?:[-/.]\d{1,2})?)",
                    value, re.I,
                ))
                populated_ratio = len(tag_values) / max(1, min(len(rows), 500))
                if populated_ratio >= 0.5 and period_like < max(1, len(tag_values) // 3):
                    default_argument = tag_value_field
                    # The series-template expansion above has already copied
                    # commonSeriesSettings.argumentField into each descriptor.
                    # Override that copied xValue for NISE company chart rows.
                    for generated_series in raw_series:
                        if isinstance(generated_series, dict):
                            generated_series["argumentField"] = tag_value_field
        year_field = field_match(keys, r"year|fy")
        for index, item in enumerate(raw_series[:16]):
            if not isinstance(item, dict):
                continue
            value_field = item.get("valueField") or common_settings.get("valueField") or model.get("valueField")
            argument_field = item.get("argumentField") or default_argument
            if value_field not in keys:
                value_field = field_match(
                    keys,
                    r"manufactur|produ|production|output|capacity|quantity|qty|amount|total|value|mw|power|cell|module|panel",
                    exclude=r"id|code|serial|index|year|month|date|period|unit|uom|name|company|manufacturer|state",
                )
            if not value_field:
                continue
            points: list[dict[str, Any]] = []
            template_filter = item.get("_series_template_filter") if isinstance(item.get("_series_template_filter"), dict) else {}
            for row in rows[:5000]:
                if template_filter and any(clean_text(row.get(key)) != clean_text(expected) for key, expected in template_filter.items()):
                    continue
                label_value = row.get(argument_field) if argument_field else None
                if label_value is None:
                    candidate_field = field_match(list(row.keys()), r"month|date|period|category|label|argument|manufacturer|company|maker")
                    label_value = row.get(candidate_field) if candidate_field else None
                if label_value is None:
                    continue
                label = clean_text(label_value)
                row_year = row.get(year_field) if year_field else None
                if row_year is None:
                    row_year = period_year
                # Append the selected year only to month-like categories. Do not
                # corrupt company names (e.g. "Alpha Cell 2026") on the
                # manufacturer chart simply because the AJAX request carried
                # Year1=2026.
                is_month_label = bool(re.fullmatch(
                    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|0?[1-9]|1[0-2])",
                    label.strip(), re.I,
                ))
                if is_month_label and row_year and re.search(r"\b20\d{2}\b", str(row_year)) and not re.search(r"\b20\d{2}\b", label):
                    label = f"{label} {clean_text(row_year)}"
                value = numeric_value(row.get(value_field))
                if value is not None and label:
                    point = {"label": label, "value": value}
                    agency_field = next((key for key in row if re.sub(r"[^a-z0-9]+", "", str(key).casefold()) in {"agencyid", "manufacturerid", "mfgid"}), None)
                    if agency_field and row.get(agency_field):
                        point["agency_id"] = clean_text(row.get(agency_field))[:100]
                    if default_argument == tag_value_field:
                        rank_field = next((key for key in row if re.sub(r"[^a-z0-9]+", "", str(key).casefold()) in {"xvalue", "rank", "serialno", "srno"}), None)
                        if rank_field and row.get(rank_field) is not None:
                            point["source_rank"] = clean_text(row.get(rank_field))[:40]
                    points.append(point)
            if points:
                converted_series.append({
                    "name": clean_text(item.get("name") or item.get("valueField") or f"Series {index + 1}"),
                    "data": points,
                })
        if converted_series:
            raw_series = converted_series
            labels = []

    if not raw_series:
        return None

    series_out: list[dict[str, Any]] = []
    for index, item in enumerate(raw_series[:16]):
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name") or item.get("label") or item.get("title") or f"Series {index + 1}")
        values = item.get("data") or item.get("values") or item.get("y")
        point_labels = item.get("x") if isinstance(item.get("x"), list) else labels
        if not isinstance(values, list):
            continue
        points = []
        for point_idx, point in enumerate(values[:2500]):
            label: Any = None
            value: Any = None
            if isinstance(point, dict):
                label = point.get("name") or point.get("label") or point.get("x") or point.get("category")
                value = point.get("y") if point.get("y") is not None else point.get("value")
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                label, value = point[0], point[1]
            elif isinstance(point, (int, float)) and not isinstance(point, bool):
                value = point
            if value is None:
                continue
            numeric = numeric_value(value)
            if numeric is None:
                continue
            if label is None and point_idx < len(point_labels):
                label = point_labels[point_idx]
            if label is None:
                label = str(point_idx + 1)
            label = clean_text(label)
            if period_year and label and not re.search(r"\b20\d{2}\b", label):
                if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b(?:0?[1-9]|1[0-2])\b", label, re.I):
                    label = f"{label} {int(period_year)}"
            point_out = {"label": label, "value": numeric}
            if isinstance(point, dict):
                if point.get("agency_id"):
                    point_out["agency_id"] = clean_text(point.get("agency_id"))[:100]
                if point.get("source_rank") is not None:
                    point_out["source_rank"] = clean_text(point.get("source_rank"))[:40]
            points.append(point_out)
        if points:
            series_out.append({"name": name, "data": points})

    if not series_out:
        return None

    chart_meta = model.get("chart") if isinstance(model.get("chart"), dict) else {}
    layout = model.get("layout") if isinstance(model.get("layout"), dict) else {}
    first_raw_series = raw_series[0] if raw_series and isinstance(raw_series[0], dict) else {}
    chart_type = clean_text(
        model.get("type") or chart_meta.get("type") or first_raw_series.get("type") or common_settings.get("type") or ""
    ).lower()
    if not chart_type:
        chart_type = "line" if "line" in title.lower() else "bar"
    if chart_type in {"doughnut", "donut"}:
        chart_type = "pie"
    if chart_type in {"column", "histogram", "stackedbar", "fullstackedbar"}:
        chart_type = "bar"
    if chart_type not in {"bar", "line", "area", "pie", "scatter"}:
        chart_type = "bar"

    raw_title = model.get("title") or layout.get("title") or title or "Chart"
    if isinstance(raw_title, dict):
        raw_title = raw_title.get("text") or raw_title.get("title") or raw_title.get("name") or title or "Chart"
    if isinstance(raw_title, list):
        raw_title = " ".join(clean_text(value) for value in raw_title if value)

    raw_unit = model.get("unit") or model.get("yAxisTitle") or model.get("subtitle")
    if isinstance(raw_unit, dict):
        raw_unit = raw_unit.get("text") or raw_unit.get("title")
    if raw_unit is None:
        y_axis = model.get("yAxis") or model.get("yaxis") or model.get("valueAxis") or options.get("valueAxis")
        if isinstance(y_axis, list):
            y_axis = y_axis[0] if y_axis else {}
        if isinstance(y_axis, dict):
            axis_title = y_axis.get("title")
            raw_unit = axis_title.get("text") if isinstance(axis_title, dict) else axis_title
    return {
        "key": f"{state}-{len(series_out)}-{abs(hash((title, page_url))) % 1000000}",
        "title": clean_text(raw_title)[:180] or "Chart",
        "kind": chart_type,
        "state": state,
        "url": page_url,
        "series": series_out,
        "unit": clean_text(raw_unit)[:100] if raw_unit else None,
    }


def _title_near(soup: BeautifulSoup, node: Tag, fallback: str) -> str:
    heading = node.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
    if heading:
        title = clean_text(heading.get_text(" ", strip=True))
        if title and len(title) <= 140:
            return title
    for attr in ("aria-label", "title", "data-title"):
        value = clean_text(node.get(attr))
        if value and len(value) <= 140: return value
    return fallback


def _extract_charts(html: str, *, page_url: str, state: str, fallback_models: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "lxml")
    output: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = list(fallback_models or [])
    script_nodes = soup.find_all("script")[:350]
    script_parts: list[str] = []
    for index, script_node in enumerate(script_nodes, start=1):
        script = (script_node.string or script_node.get_text() or "").strip()
        if not script:
            continue
        script_parts.append(script)
        parsed = _load_js_object(script) if script[:1] in "[{" else None
        before_count = len(models)
        if parsed is not None:
            _find_chart_models(parsed, models, page_url=page_url, state=state, context=f"script-{index}")
        script_title_node = script_node.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
        script_title = clean_text(script_title_node.get_text(" ", strip=True)) if script_title_node else ""
        for item in models[before_count:]:
            if script_title and not item.get("_dom_title"):
                item["_dom_title"] = script_title
        for item in _chart_configs_from_script(script):
            if script_title:
                item["_dom_title"] = script_title
            models.append(item)

    # Re-scan the combined scripts so a dxChart in one script can resolve a
    # `dataSource` array declared in a preceding/adjacent script tag.
    combined_script = "\n".join(script_parts)
    if combined_script:
        for item in _chart_configs_from_script(combined_script):
            models.append(item)

    chart_nodes = soup.select("canvas, svg, [class*='chart' i], [id*='chart' i]")
    seen: set[str] = set()
    for index, model in enumerate(models[:250], start=1):
        if not isinstance(model, dict):
            continue
        raw_title = model.get("title") or model.get("name") or model.get("_dom_title") or ""
        if isinstance(raw_title, dict):
            raw_title = raw_title.get("text") or raw_title.get("title") or ""
        if not raw_title and isinstance(model.get("layout"), dict):
            raw_title = model["layout"].get("title") or ""
            if isinstance(raw_title, dict): raw_title = raw_title.get("text") or ""
        title = clean_text(raw_title)
        if title.casefold().startswith(("script-", "chart-model-", "network-json", "embedded-json")):
            title = ""
        if not title:
            # Chart models frequently carry a generic context name. Prefer a
            # real nearby heading, otherwise omit that chart title instead of
            # borrowing a random page heading or a fabricated chart number.
            if index - 1 < len(chart_nodes):
                candidate_title = _title_near(soup, chart_nodes[index - 1], "")
                title = candidate_title if candidate_title and not candidate_title.casefold().startswith("chart ") else ""
        chart_model = model.get("model") if isinstance(model.get("model"), dict) else model
        normalized = _normalise_chart(chart_model, title=title or "Data chart", state=state, page_url=page_url)
        if not normalized:
            continue
        # Chart.js configs are often discoverable through both the common
        # JSON walker and the direct JavaScript parser. Deduplicate by actual
        # plotted data, not by generated keys or DOM placeholder count.
        signature = json.dumps(
            [normalized.get("kind"), normalized.get("series")],
            sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
        )
        if signature in seen:
            continue
        seen.add(signature)
        output.append(normalized)
    return output


def _extract_tables_from_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for doc in documents:
        html = str(doc.get("html") or "")
        if not html: continue
        soup = BeautifulSoup(html, "lxml")
        for index, table in enumerate(soup.find_all("table")[:200], start=1):
            rows = []
            for tr in table.find_all("tr")[:10001]:
                cells = [clean_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
                if cells: rows.append(cells)
            if len(rows) < 2 or max(map(len, rows), default=0) < 2: continue
            columns = rows[0]
            # Respect multi-row header only when source uses explicit TH rows.
            header_rows = table.find_all("tr")[:3]
            th_row_idx = next((i for i, tr in enumerate(header_rows) if tr.find_all("th")), None)
            if th_row_idx is not None:
                columns = rows[th_row_idx]
                body = rows[th_row_idx + 1:]
            else:
                body = rows[1:]
            if not columns or not body: continue
            title = ""
            caption = table.find("caption")
            if caption: title = clean_text(caption.get_text(" ", strip=True))
            if not title:
                heading = table.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
                title = clean_text(heading.get_text(" ", strip=True)) if heading else f"Table {index}"
            if title.casefold() in {"menu", "navigation", "header", "footer"}: continue
            columns = [col or f"Column {i+1}" for i, col in enumerate(columns)]
            data_rows = []
            for row in body[:10000]:
                if not any(row): continue
                data_rows.append({columns[i]: row[i] if i < len(row) else "" for i in range(len(columns))})
            signature = json.dumps([title, columns, data_rows[:4]], ensure_ascii=False, sort_keys=True, default=str)
            if signature in seen: continue
            seen.add(signature)
            output.append({"key": f"table-{len(output)+1}", "title": title, "state": doc.get("state") or "Overview",
                           "columns": columns, "rows": data_rows, "row_count": len(data_rows),
                           "truncated": len(body) > 10000, "url": doc.get("url")})
    return output



def _tables_from_json_payloads(payloads: list[dict[str, Any]], *, page_url: str, state: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    visited: set[int] = set()

    def add_table(title: str, columns: list[Any], raw_rows: list[Any]) -> None:
        safe_columns = [clean_text(x)[:160] or f"Column {i+1}" for i, x in enumerate(columns[:100])]
        if len(safe_columns) < 2 or not raw_rows:
            return
        rows: list[dict[str, Any]] = []
        for raw in raw_rows[:10000]:
            if isinstance(raw, dict):
                rows.append({column: raw.get(column) for column in safe_columns})
            elif isinstance(raw, (list, tuple)):
                rows.append({safe_columns[i]: raw[i] if i < len(raw) else "" for i in range(len(safe_columns))})
        if not rows:
            return
        signature = json.dumps([title, safe_columns, rows[:5]], ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen:
            return
        seen.add(signature)
        output.append({"key": f"json-table-{len(output)+1}", "title": clean_text(title)[:180] or "Data table",
                       "state": state, "columns": safe_columns, "rows": rows,
                       "row_count": len(raw_rows), "truncated": len(raw_rows) > 10000, "url": page_url})

    def walk(value: Any, path: tuple[str, ...] = (), depth: int = 0) -> None:
        if depth > 12 or len(output) >= 120 or not isinstance(value, (dict, list)) or id(value) in visited:
            return
        visited.add(id(value))
        if isinstance(value, dict):
            # Explicit table payload patterns used by common JS data-grid APIs.
            for col_key, row_key in (("columns", "rows"), ("headers", "rows"), ("columns", "data"), ("headers", "data")):
                cols, rows = value.get(col_key), value.get(row_key)
                if isinstance(cols, list) and isinstance(rows, list) and rows:
                    normalized_cols = [c.get("title") or c.get("name") or c.get("key") or c.get("field") or str(i+1) if isinstance(c, dict) else c for i, c in enumerate(cols)]
                    add_table(str(value.get("title") or value.get("name") or " ".join(path[-2:])), normalized_cols, rows)
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    walk(child, path + (str(key),), depth + 1)
        else:
            # Generic record arrays only qualify if the same set of fields is
            # shared by several rows and numeric values appear in the dataset.
            if len(value) >= 3 and all(isinstance(row, dict) for row in value[: min(20, len(value))]):
                rows = value[:10000]
                keys: list[str] = []
                for row in rows[:50]:
                    for key in row:
                        if str(key) not in keys: keys.append(str(key))
                if len(keys) >= 2 and len(keys) <= 100:
                    numeric_cols = sum(1 for key in keys if sum(1 for row in rows[:50] if parse_number(row.get(key)) is not None) >= 2)
                    if numeric_cols:
                        add_table(" ".join(path[-2:]) or "Data table", keys, rows)
            for index, child in enumerate(value[:1000]):
                if isinstance(child, (dict, list)):
                    walk(child, path + (str(index),), depth + 1)

    for item in payloads:
        if isinstance(item, dict) and item.get("data") is not None:
            walk(item["data"], (str(item.get("key") or item.get("url") or "Data"),))
    return output


def _js_data_tables(html: str, *, page_url: str, state: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "lxml")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for script_node in soup.find_all("script")[:350]:
        script = (script_node.string or script_node.get_text() or "").strip()
        if not script: continue
        patterns = (
            r"(?:const|let|var)\s+([\w$]*(?:table|rows|records|grid|dataset|data)[\w$]*)\s*=\s*",
            r"(?:window\.)?([\w$]*(?:Table|Rows|Records|Grid|Data)[\w$]*)\s*=\s*",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, script, re.I):
                expression = _balanced_expression(script, match.end())
                parsed = _load_js_object(expression or "")
                if isinstance(parsed, dict):
                    models = [parsed]
                elif isinstance(parsed, list):
                    models = [{"rows": parsed}]
                else:
                    continue
                for model in models:
                    if isinstance(model.get("rows"), list) and isinstance(model.get("columns"), list):
                        columns = [c.get("title") or c.get("name") or c.get("key") or c.get("field") or str(i+1) if isinstance(c, dict) else c for i, c in enumerate(model["columns"])]
                        rows = model["rows"]
                    elif isinstance(model.get("data"), list) and isinstance(model.get("columns") or model.get("headers"), list):
                        columns = model.get("columns") or model.get("headers")
                        rows = model["data"]
                    else:
                        rows = model.get("rows") or model.get("data") or []
                        columns = []
                        for row in rows[:30] if isinstance(rows, list) else []:
                            if isinstance(row, dict):
                                for key in row:
                                    if key not in columns: columns.append(key)
                    if len(columns) < 2 or not isinstance(rows, list) or not rows:
                        continue
                    numeric_cols = sum(1 for col in columns if sum(1 for row in rows[:30] if isinstance(row, dict) and parse_number(row.get(col)) is not None) >= 2)
                    if numeric_cols == 0: continue
                    normalized = []
                    for row in rows[:10000]:
                        if isinstance(row, dict): normalized.append({str(c): row.get(c) for c in columns})
                        elif isinstance(row, list): normalized.append({str(columns[i]): row[i] if i < len(row) else "" for i in range(len(columns))})
                    sig = json.dumps([match.group(1), columns, normalized[:4]], ensure_ascii=False, sort_keys=True, default=str)
                    if sig in seen: continue
                    seen.add(sig)
                    output.append({"key": f"js-table-{len(output)+1}", "title": re.sub(r"([a-z])([A-Z])", r"\1 \2", match.group(1)).strip(),
                                   "state": state, "columns": [str(x) for x in columns], "rows": normalized,
                                   "row_count": len(normalized), "truncated": len(rows) > 10000, "url": page_url})
    return output

def _extract_note(html: str) -> str | None:
    soup = BeautifulSoup(html or "", "lxml")
    for selector in ("[class*='analyst-note' i]", "[class*='insight' i]", "[class*='commentary' i]", "blockquote"):
        node = soup.select_one(selector)
        if node and not _is_excluded(node):
            text = clean_text(node.get_text(" ", strip=True))
            if 50 <= len(text) <= 3000 and "chatbot" not in text.casefold():
                return text
    return None


async def capture_report(
    url: str,
    *,
    source_name: str,
    sector_name: str,
    sections: Iterable[str] = (),
    filters: Iterable[str] = (),
    sweep_selects: Iterable[str] = (),
    timeout_seconds: float = 22.0,
    browser_timeout_seconds: float = 48.0,
    settle_ms: int = 900,
    reload_between_clicks: bool = False,
) -> dict[str, Any]:
    capture, _metric_rows = await capture_source(
        url, source_name=source_name, category=sector_name,
        timeout_seconds=timeout_seconds, browser_timeout_ms=int(browser_timeout_seconds * 1000),
        browser=True, clicks=tuple(sections), sweep_selects=tuple(sweep_selects),
        follow_link_limit=0, detail_link_patterns=(), auto_sweep_tabs=False,
        settle_ms=settle_ms, reload_between_clicks=reload_between_clicks,
    )
    documents = [doc for doc in (capture.get("raw_documents") or []) if isinstance(doc, dict) and doc.get("html")]
    # Script JSON embedded in each document is parsed alongside that document
    # below. Only separately captured JSON network responses are added here;
    # otherwise every page state would inherit every script chart from every
    # other state, creating duplicates and mislabelled chart groups.
    fallback_models: list[dict[str, Any]] = []
    for resource in capture.get("network_resources") or []:
        data = resource.get("data") if isinstance(resource, dict) else None
        if isinstance(data, (dict, list)):
            # Keep the click/view label attached to AJAX chart models. Without
            # this, charts returned only as JSON are flattened into Overview
            # and cannot be shown in the matching in-app manufacturer view.
            resource_state = clean_text(resource.get("state") or "Overview")
            _find_chart_models(data, fallback_models, page_url=str(resource.get("url") or url),
                               state=resource_state, context=str(resource.get("url") or "network"))
    kpis: list[dict[str, Any]] = []
    seen_kpis: set[tuple[str, str]] = set()
    charts: list[dict[str, Any]] = []
    seen_charts: set[str] = set()
    notes: list[str] = []
    sections_out: list[str] = []
    for doc in documents[:40]:
        html = str(doc.get("html") or "")
        state = clean_text(doc.get("state") or "Overview")
        page_url = str(doc.get("url") or url)
        if state not in sections_out: sections_out.append(state)
        for item in _extract_kpis(html, page_url=page_url, state=state):
            identity = (item["label"].casefold(), item["display_value"].casefold())
            if identity not in seen_kpis:
                seen_kpis.add(identity); kpis.append(item)
        for item in _extract_charts(html, page_url=page_url, state=state):
            signature = json.dumps([item["kind"], item["state"], item["series"]], sort_keys=True, ensure_ascii=False, default=str)
            if signature not in seen_charts:
                seen_charts.add(signature); charts.append(item)
        note = _extract_note(html)
        if note and note not in notes: notes.append(note)

    # Normalize API-fed chart models once while preserving the click state
    # recorded on each network resource, so in-app source views remain scoped.
    for model in fallback_models[:250]:
        if not isinstance(model, dict):
            continue
        chart_model = model.get("model") if isinstance(model.get("model"), dict) else model
        raw_title = model.get("title") or model.get("name") or ""
        if isinstance(raw_title, dict): raw_title = raw_title.get("text") or ""
        title = clean_text(raw_title) or "Data chart"
        item_url = str(model.get("url") or url)
        model_state = clean_text(model.get("state") or "Overview")
        normalized = _normalise_chart(chart_model, title=title, state=model_state, page_url=item_url)
        if normalized:
            signature = json.dumps([normalized["kind"], normalized["state"], normalized["series"]], sort_keys=True, ensure_ascii=False, default=str)
            if signature not in seen_charts:
                seen_charts.add(signature); charts.append(normalized)

    tables = _extract_tables_from_documents(documents)
    payloads = list(capture.get("embedded_json") or []) + [
        item for item in (capture.get("network_resources") or [])
        if isinstance(item, dict) and isinstance(item.get("data"), (dict, list))
    ]
    # JavaScript data-table objects are part of the page even when no separate
    # JSON/XHR resource was captured; parse them unconditionally.
    for doc in documents[:40]:
        doc_state = clean_text(doc.get("state") or "Overview")
        doc_url = str(doc.get("url") or url)
        for table in _js_data_tables(str(doc.get("html") or ""), page_url=doc_url, state=doc_state):
            tables.append(table)
    if payloads:
        # Preserve the clicked manufacturer state for JSON-backed data grids.
        # The NISE page returns several XHR payloads from the same base URL;
        # tagging every table as Overview would make the Cell/Module controls
        # show empty views even though the AJAX responses were captured.
        payloads_by_state: dict[str, list[dict[str, Any]]] = {}
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            payload_state = clean_text(payload.get("state") or "Overview")
            payloads_by_state.setdefault(payload_state, []).append(payload)
        for payload_state, state_payloads in payloads_by_state.items():
            state_url = next((str(item.get("url")) for item in state_payloads if item.get("url")), url)
            tables.extend(_tables_from_json_payloads(state_payloads, page_url=state_url, state=payload_state))
    if len(tables) > 1:
        table_seen: set[str] = set()
        unique_tables = []
        for table in tables:
            signature = json.dumps([table.get("title"), table.get("columns"), (table.get("rows") or [])[:5]], ensure_ascii=False, sort_keys=True, default=str)
            if signature not in table_seen:
                table_seen.add(signature); unique_tables.append(table)
        tables = unique_tables

    # If only the initial HTTP representation was captured, the browser may
    # have failed to render. A truthful empty chart list is better than generic
    # fake chart cards. The UI will explain that datasets have not been exposed.
    warnings = list((capture.get("capture_completeness") or {}).get("warnings") or [])
    report = {
        "source_name": source_name, "sector_name": sector_name,
        "source_url": str(capture.get("source_url") or url), "captured_at": capture.get("captured_at"),
        "fetch": capture.get("fetch") or {}, "kpi_cards": kpis,
        "charts": charts, "tables": tables, "sections": sections_out,
        "analyst_note": notes[0] if notes else None,
        "filters": capture.get("filters") or [], "embedded_json_count": len(capture.get("embedded_json") or []),
        "network_payload_count": len(capture.get("network_resources") or []),
        "capture_completeness": {
            "status": (capture.get("capture_completeness") or {}).get("status"),
            "warnings": warnings[:40],
            "counts": {"documents": len(documents), "kpis": len(kpis), "charts": len(charts), "tables": len(tables)},
        },
        "observation_count": len(_metric_rows),
    }
    source_views: list[dict[str, str]] = []
    seen_views: set[tuple[str, str]] = set()
    source_host = (urlsplit(url).hostname or "").casefold()
    for doc in documents:
        label = clean_text(doc.get("state") or "Overview")
        page_url = str(doc.get("url") or capture.get("source_url") or url).strip()
        try:
            parsed_url = urlsplit(page_url)
            # Source-page buttons must only link back to the publisher that
            # supplied the report; never expose arbitrary URLs captured in HTML.
            if parsed_url.scheme not in {"http", "https"} or (source_host and (parsed_url.hostname or "").casefold() != source_host):
                continue
        except Exception:
            continue
        identity = (label.casefold(), page_url)
        if identity in seen_views:
            continue
        seen_views.add(identity)
        source_views.append({"label": label, "url": page_url})
    report["source_views"] = source_views
    return report
