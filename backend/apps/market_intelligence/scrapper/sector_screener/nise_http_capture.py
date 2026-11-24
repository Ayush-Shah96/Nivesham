from __future__ import annotations

"""Direct HTTP collector for the public NISE Solar DCR Summary endpoints.

The collector intentionally avoids browser automation. It opens one HTTP
session, calls the same form POST endpoints used by the portal, parses the
DevExtreme ArrayStore payloads, and returns normalized observations suitable
for the Sector Screener cache/API contract.
"""

import asyncio
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from infrastructure.config.settings import get_settings
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_settings = get_settings()
INDEX_URL = (getattr(_settings, "market_solar_url", None) or "https://solardcrportal.nise.res.in/Summary/index").strip()
_url_parts = urlsplit(INDEX_URL)
BASE_URL = f"{_url_parts.scheme}://{_url_parts.netloc}"
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=20.0)
MAX_RETRIES = 4
MAX_PARALLEL_REQUESTS = 4
DEFAULT_START_YEAR = 2022

PRODUCT_CONFIG = {
    "Cell": {
        "annual_endpoint": "/Summary/CellChart",
        "monthly_endpoint": "/Summary/WaferToCell",
        "metric": "cell_manufactured_mw",
        "unit": "MW",
    },
    "Module": {
        "annual_endpoint": "/Summary/PnlChart",
        "monthly_endpoint": "/Summary/PnlMfgChart",
        "metric": "module_manufactured_mw",
        "unit": "MWp",
    },
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    text = str(value).replace("\u00a0", " ").strip()
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _month_number(value: Any) -> int | None:
    text = _clean(value).casefold()
    for token, month in _MONTHS.items():
        if re.search(rf"\b{re.escape(token)}\b", text):
            return month
    match = re.search(r"(?:^|\D)(0?[1-9]|1[0-2])(?:\D|$)", text)
    if match:
        return int(match.group(1))
    return None


def _array_from_json_object(value: Any) -> list[dict[str, Any]] | None:
    """Find a DevExtreme data array in a decoded JSON object."""
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        for child in value.values():
            found = _array_from_json_object(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _array_from_json_object(child)
            if found is not None:
                return found
    return None



def _single_quoted_strings_to_json(value: str) -> str:
    """Convert single-quoted JS strings while preserving double-quoted JSON."""
    output: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    for char in value:
        if in_double:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_double = False
        elif in_single:
            if escaped:
                if char == "'":
                    output.append("'")
                elif char == '"':
                    output.append('\\"')
                else:
                    output.append("\\" + char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                output.append('"')
                in_single = False
            elif char == '"':
                output.append('\\"')
            else:
                output.append(char)
        elif char == '"':
            output.append(char)
            in_double = True
        elif char == "'":
            output.append('"')
            in_single = True
        else:
            output.append(char)
    if escaped:
        output.append("\\")
    return "".join(output)

def extract_arraystore_data(body: str) -> list[dict[str, Any]]:
    """Extract ArrayStore rows from JSON or HTML/JavaScript partial responses.

    Unlike the first-pass scraper, this accepts whitespace around the data
    marker and JSON envelopes as well as HTML containing inline initializers.
    The bracket scanner respects quoted strings and escaped characters.
    """
    text = body or ""
    try:
        decoded = json.loads(text)
        rows = _array_from_json_object(decoded)
        if rows is not None:
            return rows
    except (TypeError, ValueError):
        pass

    marker = re.search(r"(?:[\"']data[\"']|\bdata)\s*:\s*\[", text)
    if not marker:
        raise ValueError("NISE response did not contain a DevExtreme data array")
    array_start = text.find("[", marker.start())
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    for position in range(array_start, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in {"'", '"'}:
            in_string = True
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                data_text = text[array_start:position + 1]
                # NISE may serialize JavaScript Date constructors inside the
                # ArrayStore initializer; those are not valid JSON literals.
                data_text = re.sub(r"new\s+Date\([^)]*\)", "null", data_text)
                data_text = _single_quoted_strings_to_json(data_text)
                decoded_rows = json.loads(data_text)
                if not isinstance(decoded_rows, list):
                    break
                return [item for item in decoded_rows if isinstance(item, dict)]
    raise ValueError("NISE ArrayStore data array was unterminated or invalid")


def _stat_cards(index_html: str) -> dict[str, float]:
    """Read the two manufacturer KPI cards across old/new NISE markup."""
    soup = BeautifulSoup(index_html or "", "html.parser")
    aliases = {
        "solarcellmanufacturer": "Cell", "solarcellmanufacturers": "Cell",
        "cellmanufacturer": "Cell", "cellmanufacturers": "Cell",
        "noofsolarcellmanufacturers": "Cell", "noofcellmanufacturers": "Cell",
        "solarmodulemanufacturer": "Module", "solarmodulemanufacturers": "Module",
        "modulemanufacturer": "Module", "modulemanufacturers": "Module",
        "noofsolarmodulemanufacturers": "Module", "noofmodulemanufacturers": "Module",
    }
    found: dict[str, float] = {}
    for node in soup.find_all(string=True):
        label = re.sub(r"[^a-z0-9]", "", _clean(node).casefold())
        product = aliases.get(label)
        if not product or product in found:
            continue
        # Walk outward from the label until a compact KPI card containing a
        # numeric value is found. NISE has changed card classes over time.
        parent = node.parent
        for _ in range(6):
            if parent is None:
                break
            text = _clean(parent.get_text(" ", strip=True))
            value = _number(text.replace(_clean(node), "", 1))
            if value is not None and len(text) < 180:
                found[product] = value
                break
            parent = parent.parent
    return found


def _available_years(index_html: str) -> list[int]:
    soup = BeautifulSoup(index_html or "", "html.parser")
    years: set[int] = set()
    for select in soup.find_all("select"):
        name = _clean(select.get("name") or "").casefold()
        element_id = _clean(select.get("id") or "").casefold()
        if "year" not in name and "year" not in element_id:
            continue
        for option in select.find_all("option"):
            raw = _clean(option.get("value") or option.get_text(" ", strip=True))
            match = re.search(r"\b(20\d{2})\b", raw)
            if match:
                years.add(int(match.group(1)))
    current_year = datetime.now(timezone.utc).year
    if not years:
        years = set(range(DEFAULT_START_YEAR, current_year + 1))
    return sorted(year for year in years if DEFAULT_START_YEAR <= year <= current_year)


async def _load_ready_index(client: httpx.AsyncClient, source_index_url: str) -> httpx.Response:
    """Wait for the NISE summary document to expose its KPI/detail controls.

    Direct HTTP does not execute JavaScript, but it must establish the portal
    session and receive the complete summary HTML before firing chart POSTs.
    Some NISE edge responses briefly return a thin shell; retry those rather
    than treating them as a valid zero-data snapshot.
    """
    last_response = None
    for attempt in range(4):
        response = await _request_with_retry(client, "GET", source_index_url)
        last_response = response
        html = response.text or ""
        lowered = html.casefold()
        ready_markers = ("solar cell manufacturer", "solar module manufacturer", "view details")
        if len(html) > 2500 and sum(marker in lowered for marker in ready_markers) >= 2:
            # Allow the portal session/cookies to settle before dependent POSTs.
            await asyncio.sleep(1.25)
            return response
        if attempt < 3:
            await asyncio.sleep(1.5 * (attempt + 1))
    if last_response is None:
        raise RuntimeError("NISE Summary page did not return a response")
    raise RuntimeError(
        f"NISE Summary page did not become ready after 4 attempts "
        f"(HTTP {last_response.status_code}, {len(last_response.text or '')} chars)"
    )


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    data: dict[str, str] | None = None,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.request(method, url, data=data)
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response
            if attempt >= MAX_RETRIES:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            delay = min(10.0, 0.8 * (2 ** attempt))
            if retry_after:
                try:
                    delay = min(20.0, max(0.5, float(retry_after)))
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                        delay = min(20.0, max(0.5, (retry_at - datetime.now(retry_at.tzinfo)).total_seconds()))
                    except Exception:
                        pass
            await asyncio.sleep(delay)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt >= MAX_RETRIES:
                raise
            await asyncio.sleep(min(10.0, 0.8 * (2 ** attempt)))
    if last_error:
        raise last_error
    raise RuntimeError(f"NISE request failed: {method} {url}")


def _manufacturer_record(item: dict[str, Any], year: int) -> dict[str, Any] | None:
    agency_id = _clean(item.get("AgencyId") or item.get("MfgId") or item.get("agencyId"))
    name = _clean(item.get("tagValue") or item.get("company_name") or item.get("AgencyName") or item.get("name"))
    value = _number(item.get("yValue") if item.get("yValue") is not None else item.get("value"))
    if not name or value is None:
        return None
    return {
        "agency_id": agency_id or None,
        "name": name,
        "year": year,
        "value_mw": value,
        "rank": item.get("xValue"),
        "is_almm": item.get("isAlmm"),
    }


def _monthly_points(rows: list[dict[str, Any]], year: int, product: str, *, agency_id: str | None = None, company_name: str | None = None) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in rows:
        month = _month_number(item.get("xValue") or item.get("month") or item.get("label"))
        value = _number(item.get("yValue") if item.get("yValue") is not None else item.get("value"))
        if month is None or value is None or month in seen:
            continue
        seen.add(month)
        output.append({
            "product": product,
            "observation_type": "manufacturer_monthly" if agency_id else "monthly",
            "manufacturer": company_name,
            "manufacturer_id": agency_id,
            "year": year,
            "month": month,
            "period": f"{year}-{month:02d}",
            "manufactured_mw": value,
            "source_endpoint": PRODUCT_CONFIG[product]["monthly_endpoint"],
        })
    return sorted(output, key=lambda row: row["month"])


async def _post_arraystore(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    year: int,
    agency_id: str | None = None,
    base_url: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = {"Year1[]": str(year), "RecaptchaToken": ""}
    if agency_id is not None:
        payload["MfgId"] = agency_id
    url = f"{(base_url or BASE_URL).rstrip('/')}{endpoint}"
    response = await _request_with_retry(client, "POST", url, data=payload)
    body = response.text
    rows = extract_arraystore_data(body)
    return rows, {
        "path": endpoint,
        "method": "POST",
        "status": response.status_code,
        "body_chars": len(body),
        "rows": len(rows),
        "year": year,
        "agency_id": agency_id,
    }


async def scrape_product(product: str, *, index_url: str | None = None) -> dict[str, Any]:
    """Collect a complete Cell or Module product snapshot using direct HTTP."""
    product = "Module" if str(product).casefold() in {"module", "solar module", "panel"} else "Cell"
    config = PRODUCT_CONFIG[product]
    # solar.scrape passes the configured source URL explicitly. Accept it here
    # and derive the matching host for all subsequent POST requests. Previously
    # this function accepted only ``product`` while the caller supplied
    # ``index_url``; every refresh therefore failed with TypeError before the
    # first request was sent to NISE.
    source_index_url = (index_url or INDEX_URL).strip()
    source_parts = urlsplit(source_index_url)
    source_base_url = f"{source_parts.scheme}://{source_parts.netloc}"
    if source_parts.scheme not in {"http", "https"} or not source_parts.netloc:
        raise ValueError(f"Invalid NISE Solar index URL: {source_index_url!r}")
    captured_at = datetime.now(timezone.utc).isoformat()
    limits = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": source_index_url,
        "Origin": source_base_url,
    }

    async with httpx.AsyncClient(headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        index_response = await _load_ready_index(client, source_index_url)
        index_html = index_response.text
        years = _available_years(index_html)
        manufacturer_counts = _stat_cards(index_html)

        async def limited_post(endpoint: str, year: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            async with limits:
                return await _post_arraystore(
                    client, endpoint, year=year,
                    agency_id="" if endpoint == config["monthly_endpoint"] else None,
                    base_url=source_base_url,
                )

        async def fetch_year(year: int) -> dict[str, Any]:
            annual_result, monthly_result = await asyncio.gather(
                limited_post(config["annual_endpoint"], year),
                limited_post(config["monthly_endpoint"], year),
                return_exceptions=True,
            )
            return {"year": year, "annual": annual_result, "monthly": monthly_result}

        year_results = await asyncio.gather(*(fetch_year(year) for year in years))

    manufacturers: dict[str, dict[str, Any]] = {}
    monthly_observations: list[dict[str, Any]] = []
    yearly_manufacturer_observations: list[dict[str, Any]] = []
    endpoint_audit: list[dict[str, Any]] = [{
        "path": "/Summary/index", "method": "GET", "status": index_response.status_code,
        "body_chars": len(index_html), "rows": 0,
    }]
    failed_requests: list[dict[str, Any]] = []
    successful_years: set[int] = set()

    for result in year_results:
        year = result["year"]
        annual_result = result["annual"]
        monthly_result = result["monthly"]
        annual_rows: list[dict[str, Any]] = []
        month_rows: list[dict[str, Any]] = []
        if isinstance(annual_result, Exception):
            failed_requests.append({"year": year, "endpoint": config["annual_endpoint"], "error": f"{type(annual_result).__name__}: {annual_result}"[:300]})
        else:
            annual_rows, audit = annual_result
            endpoint_audit.append(audit)
        if isinstance(monthly_result, Exception):
            failed_requests.append({"year": year, "endpoint": config["monthly_endpoint"], "error": f"{type(monthly_result).__name__}: {monthly_result}"[:300]})
        else:
            month_rows, audit = monthly_result
            endpoint_audit.append(audit)

        if not isinstance(annual_result, Exception) and not isinstance(monthly_result, Exception):
            successful_years.add(year)

        for item in annual_rows:
            record = _manufacturer_record(item, year)
            if not record:
                continue
            agency_id = record.get("agency_id")
            if not agency_id:
                # A name-only row is retained as a labelled observation but is
                # not offered for the lazy company-series endpoint.
                agency_id = f"name:{re.sub(r'[^a-z0-9]+', '-', record['name'].casefold()).strip('-')}"
            company = manufacturers.setdefault(agency_id, {
                "agency_id": agency_id,
                "name": record["name"],
                "is_almm": record.get("is_almm"),
                "yearly_values": {},
            })
            company["yearly_values"][str(year)] = record["value_mw"]
            yearly_manufacturer_observations.append({
                "product": product,
                "observation_type": "manufacturer_yearly",
                "manufacturer": record["name"],
                "manufacturer_id": agency_id,
                "year": year,
                "month": None,
                "period": str(year),
                "manufactured_mw": record["value_mw"],
                "source_endpoint": config["annual_endpoint"],
            })

        monthly_observations.extend(_monthly_points(month_rows, year, product))

    manufacturer_list = sorted(
        manufacturers.values(),
        key=lambda item: (item["name"].casefold(), str(item["agency_id"])),
    )
    for company in manufacturer_list:
        values = list(company.get("yearly_values", {}).values())
        company["latest_yearly_mw"] = values[-1] if values else None

    # Aggregate yearly observations from monthly data. Twelve observations are
    # required for a full-year total; incomplete years are explicitly YTD.
    monthly_by_year: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in monthly_observations:
        monthly_by_year[int(row["year"])].append(row)
    yearly_total_observations: list[dict[str, Any]] = []
    for year in sorted(monthly_by_year):
        month_rows = monthly_by_year[year]
        total = round(sum(float(row["manufactured_mw"]) for row in month_rows), 3)
        month_count = len({row["month"] for row in month_rows})
        if month_count == 12:
            label = str(year)
        elif year == datetime.now(timezone.utc).year:
            label = f"{year} YTD"
        else:
            label = f"{year} Partial"
        yearly_total_observations.append({
            "product": product,
            "observation_type": "yearly",
            "manufacturer": None,
            "manufacturer_id": None,
            "year": year,
            "month": None,
            "period": label,
            "manufactured_mw": total,
            "source_endpoint": config["monthly_endpoint"],
        })

    annual_totals: dict[int, float] = defaultdict(float)
    for row in yearly_manufacturer_observations:
        annual_totals[int(row["year"])] += float(row["manufactured_mw"])
    existing_years = {int(row["year"]) for row in yearly_total_observations}
    for year, total in sorted(annual_totals.items()):
        if year in existing_years:
            continue
        yearly_total_observations.append({
            "product": product,
            "observation_type": "yearly",
            "manufacturer": None,
            "manufacturer_id": None,
            "year": year,
            "month": None,
            "period": str(year),
            "manufactured_mw": round(total, 3),
            "source_endpoint": config["annual_endpoint"],
        })
    yearly_total_observations.sort(key=lambda row: int(row["year"]))

    available_total_years = set(monthly_by_year) | {int(row["year"]) for row in yearly_total_observations}
    latest_year = max(available_total_years) if available_total_years else None
    current_total = None
    period_basis = None
    if latest_year is not None:
        if latest_year in monthly_by_year:
            current_months = {row["month"] for row in monthly_by_year[latest_year]}
            current_total = round(sum(float(row["manufactured_mw"]) for row in monthly_by_year[latest_year]), 3)
            coverage_label = "full year" if len(current_months) == 12 else ("YTD" if latest_year == datetime.now(timezone.utc).year else "partial period")
            period_basis = f"{latest_year} {coverage_label} · derived from NISE monthly manufacturing observations"
        else:
            match = next((row for row in yearly_total_observations if row["year"] == latest_year), None)
            current_total = match.get("manufactured_mw") if match else None
            period_basis = f"{latest_year} · derived from NISE manufacturer observations"

    manufacturer_count = manufacturer_counts.get(product)
    count_source = "NISE Summary KPI"
    if manufacturer_count is None:
        manufacturer_count = float(len(manufacturer_list)) if manufacturer_list else None
        count_source = "Distinct manufacturers returned by NISE manufacturer endpoint"

    unit = config["unit"]
    kpis = [
        {
            "key": f"solar-{product.casefold()}-manufacturers",
            "label": f"No. of Solar {product} Manufacturers",
            "value": int(manufacturer_count) if manufacturer_count is not None else None,
            "display_value": f"{int(manufacturer_count):,}" if manufacturer_count is not None else "Not available",
            "unit": "manufacturers",
            "state": "NISE DCR",
            "source_basis": count_source,
        },
        {
            "key": f"solar-{product.casefold()}-manufactured-mw",
            "label": f"Solar {product} Manufactured",
            "value": current_total,
            "display_value": f"{current_total:,.3f} {unit}" if current_total is not None else "Not available",
            "unit": unit,
            "period_basis": period_basis,
            "state": "NISE DCR",
            "source_basis": "Derived from monthly NISE manufacturing observations",
        },
    ]

    monthly_chart = {
        "key": f"nise-{product.casefold()}-monthwise-manufactured",
        "title": f"Solar {product} Manufactured Month-wise",
        "kind": "bar",
        "unit": unit,
        "state": "NISE DCR",
        "series": [{
            "name": f"Solar {product} Manufactured ({unit})",
            "data": [{"label": row["period"], "value": row["manufactured_mw"]} for row in monthly_observations],
        }] if monthly_observations else [],
    }
    yearly_chart = {
        "key": f"nise-{product.casefold()}-yearwise-manufactured",
        "title": f"Solar {product} Manufactured Year-wise",
        "kind": "bar",
        "unit": unit,
        "state": "NISE DCR",
        "series": [{
            "name": f"Solar {product} Manufactured ({unit})",
            "data": [{"label": row["period"], "value": row["manufactured_mw"]} for row in yearly_total_observations],
        }] if yearly_total_observations else [],
    }
    # This chart is retained in the API contract for backwards compatibility;
    # the React view uses the normalized manufacturer directory and lazy
    # company-monthly endpoint for the multi-company line chart.
    manufacturer_chart = {
        "key": f"nise-{product.casefold()}-manufacturer-manufactured",
        "title": f"Solar {product} Manufactured Company-wise",
        "kind": "bar",
        "unit": unit,
        "state": "NISE DCR",
        "series": [{
            "name": f"Solar {product} Manufacturers",
            "data": [
                {"label": item["name"], "value": max(item.get("yearly_values", {}).values(), default=0), "agency_id": item["agency_id"], "by_year": item.get("yearly_values", {})}
                for item in sorted(manufacturer_list, key=lambda entry: max(entry.get("yearly_values", {}).values(), default=0), reverse=True)
            ],
        }] if manufacturer_list else [],
    }
    charts = [chart for chart in (monthly_chart, yearly_chart, manufacturer_chart) if chart["series"]]

    observations = [*monthly_observations, *yearly_total_observations, *yearly_manufacturer_observations]
    audit_expected = len(years) * 2 + 1
    audit_captured = len(endpoint_audit)
    missing_years = sorted(set(years) - successful_years)
    has_company_ids = any(not str(item["agency_id"]).startswith("name:") for item in manufacturer_list)
    audit_complete = (
        index_response.status_code == 200
        and not failed_requests
        and not missing_years
        and bool(monthly_observations)
        and bool(yearly_manufacturer_observations)
        and bool(manufacturer_list)
        and has_company_ids
    )
    warnings: list[str] = []
    if missing_years:
        warnings.append("NISE data was not collected for reporting years: " + ", ".join(map(str, missing_years)))
    if not has_company_ids:
        warnings.append("NISE manufacturer rows did not include stable AgencyId values; monthly company comparisons cannot be fetched for those rows.")
    if len(monthly_by_year) and latest_year is not None and len({row["month"] for row in monthly_by_year[latest_year]}) < 12:
        warnings.append(f"{latest_year} monthly manufacturing data is partial/YTD and is not a complete calendar year.")

    return {
        "source_name": "NISE_DCR_DIRECT_HTTP",
        "source_label": "National Institute of Solar Energy — DCR Verification Portal",
        "source_url": source_index_url,
        "captured_at": captured_at,
        "selected_product": product,
        "selected_period": "yearly",
        "available_years": years,
        "selected_years": years,
        "filters": {"product": ["Cell", "Module"], "timeline": ["monthly", "yearly"], "years": years},
        "kpi_cards": kpis,
        "charts": charts,
        "tables": [],
        "sections": [f"Solar {product} Manufacturing"],
        "manufacturers": manufacturer_list,
        "manufacturing_observations": observations,
        "company_monthly_observations": [],
        "company_series_coverage": [],
        "observation_count": len(observations),
        "source_snapshot_version": "nise-direct-http-v2",
        "capture_audit": {
            "method": "direct_http_post",
            "expected_responses": audit_expected,
            "captured_responses": audit_captured,
            "successful_years": sorted(successful_years),
            "missing_years": missing_years,
            "failed_requests": failed_requests[:60],
            "endpoints": endpoint_audit[:80],
            "complete": audit_complete,
        },
        "capture_completeness": {
            "status": "complete" if audit_complete and not warnings else ("partial" if observations else "failed"),
            "warnings": warnings,
            "counts": {
                "manufacturers": len(manufacturer_list),
                "monthly_observations": len(monthly_observations),
                "yearly_manufacturer_observations": len(yearly_manufacturer_observations),
                "years_requested": len(years),
                "years_captured": len(successful_years),
            },
        },
        "data_quality_notes": warnings,
        "source_diagnostics": [{
            "source": "NISE DCR Direct HTTP",
            "url": source_index_url,
            "method": "GET + targeted POST requests",
            "endpoints": endpoint_audit[:40],
            "failed_requests": failed_requests[:40],
        }],
    }


async def scrape_company_monthly(
    product: str,
    agency_ids: list[str],
    years: list[int],
    *,
    pairs: list[tuple[str, int]] | None = None,
    index_url: str | None = None,
) -> dict[str, Any]:
    """Fetch monthly manufacturing only for explicitly selected manufacturers."""
    product = "Module" if str(product).casefold() in {"module", "solar module", "panel"} else "Cell"
    config = PRODUCT_CONFIG[product]
    ids = list(dict.fromkeys(str(value).strip() for value in agency_ids if str(value).strip()))[:12]
    normalized_years = sorted({int(value) for value in years if 2022 <= int(value) <= datetime.now(timezone.utc).year})[:12]
    requested_pairs = (
        list(dict.fromkeys((str(agency_id), int(year)) for agency_id, year in (pairs or [])
                           if str(agency_id) in ids and int(year) in normalized_years))
        if pairs is not None
        else [(agency_id, year) for agency_id in ids for year in normalized_years]
    )
    source_index_url = (index_url or INDEX_URL).strip()
    source_parts = urlsplit(source_index_url)
    source_base_url = f"{source_parts.scheme}://{source_parts.netloc}"
    if source_parts.scheme not in {"http", "https"} or not source_parts.netloc:
        raise ValueError(f"Invalid NISE Solar index URL: {source_index_url!r}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": source_index_url,
        "Origin": source_base_url,
    }
    semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)
    captured_at = datetime.now(timezone.utc).isoformat()
    observations: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    async with httpx.AsyncClient(headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        index_response = await _load_ready_index(client, source_index_url)

        async def fetch_one(agency_id: str, year: int) -> None:
            async with semaphore:
                try:
                    rows, audit = await _post_arraystore(
                        client, config["monthly_endpoint"], year=year, agency_id=agency_id,
                        base_url=source_base_url,
                    )
                    company_name = None
                    points = _monthly_points(rows, year, product, agency_id=agency_id, company_name=company_name)
                    observations.extend(points)
                    coverage.append({
                        "agency_id": agency_id, "year": year, "status": "captured",
                        "point_count": len(points), "captured_at": captured_at,
                        "endpoint": audit["path"],
                    })
                except Exception as exc:
                    failures.append({
                        "agency_id": agency_id, "year": year,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    })
                    coverage.append({
                        "agency_id": agency_id, "year": year, "status": "failed",
                        "point_count": 0, "captured_at": captured_at,
                        "endpoint": config["monthly_endpoint"],
                    })

        await asyncio.gather(*(fetch_one(agency_id, year) for agency_id, year in requested_pairs))

    return {
        "product": product,
        "captured_at": captured_at,
        "index_status": index_response.status_code,
        "observations": observations,
        "coverage": coverage,
        "failures": failures,
    }
