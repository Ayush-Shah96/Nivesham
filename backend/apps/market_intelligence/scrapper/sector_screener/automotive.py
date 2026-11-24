from __future__ import annotations

"""Source-backed Automotive Overview for the official Parivahan VAHAN portal.

The portal exposes public aggregate JSON endpoints.  We call those endpoints
with the same query parameters used by the dashboard instead of scraping chart
pixels or generating local allocations.  All values returned by this module
are either source values or clearly identified calculations over source values.
"""

import asyncio
import logging
import re
import time
import json
from bs4 import BeautifulSoup
from datetime import date, datetime
from typing import Any

import httpx

from infrastructure.config.settings import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()
URL = (
    getattr(_settings, "market_automotive_url", None)
    or "https://analytics.parivahan.gov.in/analytics/publicdashboard/vahan?lang=en"
).strip()
if "analytics.parivahan.gov.in/analytics/publicdashboard/vahan" not in URL:
    URL = "https://analytics.parivahan.gov.in/analytics/publicdashboard/vahan?lang=en"

_API_ROOT = "https://analytics.parivahan.gov.in/analytics/"
_API_TIMEOUT = httpx.Timeout(connect=15.0, read=45.0, write=15.0, pool=15.0)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": URL,
}

FLASHFINANCE_BASE = "https://flashfinance.news"
FLASH_REPORTS: dict[str, tuple[str, str]] = {
    "ev": ("EV", f"{FLASHFINANCE_BASE}/vahan/ev-sales-india"),
    "two_wheeler": ("2W", f"{FLASHFINANCE_BASE}/vahan/two-wheeler-sales-india"),
    "four_wheeler": ("4W", f"{FLASHFINANCE_BASE}/vahan/car-sales-india"),
    "commercial_vehicle": ("Commercial Vehicle", f"{FLASHFINANCE_BASE}/vahan/reports/commercial-vehicle-sales-november-2026"),
    "statewise": ("Statewise", f"{FLASHFINANCE_BASE}/vahan/state-wise-vehicle-registration"),
}
REPORTS = {"overview": ("Automotive Overview", URL), **FLASH_REPORTS}

# Dashboard-facing buckets are intentionally small and stable. They are mapped
# to the source's exact Category Group / Sub-Category / Fuel values below.
CATEGORY_FILTERS: dict[str, dict[str, str]] = {
    "2W": {"vehicleCategoryGroup": "Two Wheeler"},
    "4W": {"vehicleCategoryGroup": "Four Wheeler"},
    "3W": {"vehicleCategoryGroup": "Three Wheeler"},
    "Tractor": {"vehicleCategoryGroup": "Tractor"},
    # Parivahan does not expose one broad "Commercial Vehicle" group. The
    # nine goods/motor/passenger sub-categories below are the source's Heavy CV
    # + Light CV classes, combined into the user-facing Commercial Vehicle bucket.
    "Commercial Vehicle": {
        "vehicleSubCategories": ",".join([
            "HEAVY GOODS VEHICLE", "MEDIUM GOODS VEHICLE", "HEAVY MOTOR VEHICLE",
            "MEDIUM MOTOR VEHICLE", "HEAVY PASSENGER VEHICLE", "MEDIUM PASSENGER VEHICLE",
            "LIGHT GOODS VEHICLE", "LIGHT MOTOR VEHICLE", "LIGHT PASSENGER VEHICLE",
        ])
    },
    # EV is a propulsion bucket on the source, not a vehicle Category Group.
    "EV": {},
    "Bus": {"vehicleCategoryGroup": "Bus"},
}
CATEGORY_ALIASES = {
    "two wheeler": "2W", "two-wheelers": "2W", "2 wheeler": "2W", "2w": "2W",
    "four wheeler": "4W", "four-wheelers": "4W", "4 wheeler": "4W", "4w": "4W",
    "three wheeler": "3W", "three-wheelers": "3W", "3 wheeler": "3W", "3w": "3W",
    "goods vehicle": "Commercial Vehicle", "commercial vehicles": "Commercial Vehicle",
    "commercial vehicle": "Commercial Vehicle", "tractor": "Tractor", "bus": "Bus", "ev": "EV",
}

# These are the five fuel buckets shown in the dashboard. Every source fuel
# value is assigned to at most one bucket; unknown/other fuels are not silently
# forced into Petrol or Diesel.
FUEL_FILTERS: dict[str, list[str]] = {
    "Petrol": ["PETROL", "PETROL(E20)", "ETHANOL(E100)", "FLEX-FUEL(ETHANOL)", "PETROL/METHANOL", "PETROL/LPG"],
    "CNG": ["CNG ONLY", "BIO-CNG/BIO-GAS", "HCNG", "PETROL/CNG", "PETROL(E20)/CNG", "PETROL/HYBRID/CNG", "PETROL(E20)/HYBRID/CNG", "DUAL DIESEL/BIO CNG", "DUAL DIESEL/CNG"],
    "Diesel": ["DIESEL", "DUAL DIESEL/LNG", "FLEX-FUEL(BIO-DIESEL)", "LNG"],
    "Hybrid": ["DIESEL/HYBRID", "PETROL/HYBRID", "PETROL(E20)/HYBRID", "PLUG-IN HYBRID EV", "STRONG HYBRID EV"],
    "EV": ["ELECTRIC(BOV)", "PURE EV", "FUEL CELL HYDROGEN", "HYDROGEN(ICE)", "SOLAR"],
}
FUEL_OPTIONS = list(FUEL_FILTERS)
CATEGORY_OPTIONS = list(CATEGORY_FILTERS)
TIMELINE_OPTIONS = {"monthly": "3", "quarterly": "2", "yearly": "1"}

# The dropdown options endpoint is paged and supports search. Never harvest all
# thousands of maker names on every dashboard refresh.
_MAKER_CACHE: dict[str, tuple[float, list[str]]] = {}
_MAKER_CACHE_TTL = 300.0


def _normalise_filters(filters: dict[str, Any] | None) -> dict[str, str]:
    raw = filters or {}
    raw_category = str(raw.get("category") or "").strip()
    fuel = str(raw.get("fuel") or "").strip()
    maker = str(raw.get("maker") or "").strip()
    timeline = str(raw.get("timeline") or "monthly").strip().lower()
    result: dict[str, str] = {"timeline": timeline if timeline in TIMELINE_OPTIONS else "monthly"}
    if raw_category and raw_category.casefold() != "all":
        category = CATEGORY_ALIASES.get(raw_category.casefold())
        if category is None:
            category = next((name for name in CATEGORY_FILTERS if name.casefold() == raw_category.casefold()), None)
        if not category:
            raise ValueError(f"Unsupported Automotive category bucket: {raw_category}")
        result["category"] = category
    if fuel and fuel.casefold() != "all":
        match = next((name for name in FUEL_FILTERS if name.casefold() == fuel.casefold()), None)
        if not match:
            raise ValueError(f"Unsupported Automotive fuel bucket: {fuel}")
        result["fuel"] = match
    if maker and maker.casefold() != "all":
        if len(maker) > 160:
            raise ValueError("Manufacturer name is too long")
        result["maker"] = maker
    raw_makers = raw.get("makers") or []
    if isinstance(raw_makers, str):
        raw_makers = raw_makers.split(",")
    if not isinstance(raw_makers, (list, tuple, set)):
        raise ValueError("Makers must be supplied as a list")
    selected_makers = []
    for item in raw_makers:
        name = re.sub(r"\s+", " ", str(item or "")).strip()
        if not name or name.casefold() == "all":
            continue
        if len(name) > 160:
            raise ValueError("Manufacturer name is too long")
        if name.casefold() not in {value.casefold() for value in selected_makers}:
            selected_makers.append(name)
        if len(selected_makers) > 10:
            raise ValueError("Select no more than 10 manufacturers for comparison")
    if selected_makers:
        result["makers"] = ",".join(selected_makers)
    return result


def _base_params(calendar_type: str = "3", filters: dict[str, str] | None = None) -> dict[str, str]:
    """Build Parivahan's public filter contract for one request."""
    params = {
        "fromYear": "1970",
        "toYear": str(date.today().year),
        "stateCode": "",
        "rtoCode": "0",
        "vehicleClasses": "",
        "vehicleMakers": "",
        "vehicleSubCategories": "",
        "vehicleEmissions": "",
        "vehicleFuels": "",
        "timePeriod": "2",
        "vehicleCategoryGroup": "",
        "evType": "",
        "vehicleStatus": "",
        "vehicleOwnerType": "",
        "fitnessCheck": "0",
        "vehicleType": "",
        "archiveTypeAC": "ACTIVE_COMPLIANT",
        "archiveTypeANC": "ACTIVE_NON_COMPLIANT",
        "archiveTypePA": "",
        "archiveTypeTA": "",
        "archiveTypeNA": "",
        "calendarType": calendar_type,
    }
    filters = filters or {}
    category = filters.get("category")
    fuel = filters.get("fuel")
    if category and category != "EV":
        params.update(CATEGORY_FILTERS[category])
    selected_fuel_values = list(FUEL_FILTERS.get(fuel, [])) if fuel else []
    if category == "EV":
        ev_values = set(FUEL_FILTERS["EV"])
        selected_fuel_values = [item for item in (selected_fuel_values or FUEL_FILTERS["EV"]) if item in ev_values]
    if selected_fuel_values:
        params["vehicleFuels"] = ",".join(selected_fuel_values)
    # The Overview uses one shared multi-select maker filter. Parivahan's
    # vehicleMakers parameter accepts the same comma-separated contract used
    # by its dashboard filters. Keep the legacy single-maker key as fallback
    # for any older callers.
    selected_makers = filters.get("makers")
    if selected_makers:
        params["vehicleMakers"] = selected_makers
    elif filters.get("maker"):
        params["vehicleMakers"] = filters["maker"]
    return params


def _filter_scope_is_empty(filters: dict[str, str]) -> bool:
    return filters.get("category") == "EV" and filters.get("fuel") not in (None, "EV")


def _fuel_bucket(label: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(label or "")).strip().upper()
    if not text:
        return None
    if text in FUEL_FILTERS["EV"] or "ELECTRIC(BOV)" in text or text.startswith("PURE EV") or "FUEL CELL" in text or "HYDROGEN" in text or text == "SOLAR":
        return "EV"
    if "CNG" in text or "HCNG" in text or "BIO-CNG" in text:
        return "CNG"
    if "HYBRID" in text:
        return "Hybrid"
    if "DIESEL" in text or text == "LNG":
        return "Diesel"
    if "PETROL" in text or "ETHANOL" in text or "METHANOL" in text:
        return "Petrol"
    return None


def _bucket_fuel_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals = {name: 0.0 for name in FUEL_OPTIONS}
    for point in points:
        bucket = _fuel_bucket(point.get("label"))
        if bucket:
            totals[bucket] += float(point.get("value") or 0)
    return [{"label": name, "value": totals[name]} for name in FUEL_OPTIONS if totals[name] > 0]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "").replace("%", "")
        text = re.sub(r"[^0-9.+-]", "", text)
        if not text or text in {".", "+", "-"}:
            return None
        try:
            number = float(text)
        except (TypeError, ValueError):
            return None
    return number if number == number and abs(number) != float("inf") else None


def _unwrap_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "result", "results", "rows", "items", "content", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if isinstance(payload.get("labels"), list) and isinstance(payload.get("data"), list):
            return [{"label": label, "value": value} for label, value in zip(payload["labels"], payload["data"])]
    return []


def _chart_points(payload: Any, *, preferred_label_keys: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Normalize Parivahan chart payloads from DTO, Chart.js, Apex and map shapes.

    The two Top 5 endpoints have returned more than one JSON shape over time.
    In particular, Chart.js keeps category labels on the parent object while
    numeric values are nested under ``datasets[].data``. Treating each dataset
    as an independent list loses those labels and silently produced empty
    rankings even when the endpoint response contained five values.
    """
    label_keys = preferred_label_keys + (
        "label", "name", "key", "title", "text", "state", "stateName", "state_name",
        "state_name_en", "statename", "stateLabel", "stateDesc", "stateDescription",
        "nameOfState", "maker", "makerName", "maker_name", "makername", "makerDesc",
        "makerDescription", "manufacturer", "manufacturerName", "manufacturer_name",
        "manufacturerDesc", "vehicleMaker", "vehicleMakerName", "category", "categoryName",
        "fuel", "fuelType", "fuelName", "x", "xLabel", "displayName", "description",
    )
    value_keys = (
        "value", "count", "registrations", "registeredVehicleCount", "registered_vehicle_count",
        "registrationCount", "registration_count", "registrationsCount", "vehicleRegistrationCount",
        "vehicle_registration_count", "totalRegistration", "totalRegistrationCount", "totalRegistrations",
        "total_registrations", "totalCount", "registeredCount", "regCount", "vehicleCount",
        "vehicle_count", "total", "y", "amount", "txnCount", "transactionCount", "doc_count",
        "registration", "totalRegisteredVehicle", "totalVehicleRegistered", "numberOfRegistrations",
    )
    wrapper_keys = (
        "data", "result", "results", "response", "chartData", "chart_data", "rows", "records",
        "content", "items", "list", "dataList", "stateWiseData", "makerWiseData", "stateList",
        "makerList", "top5", "topFive", "chartList", "graphData", "registrationData",
    )

    def zipped(labels: Any, values: Any) -> list[dict[str, Any]]:
        if not isinstance(labels, list) or not isinstance(values, list):
            return []
        if values and isinstance(values[0], dict):
            values = values[0].get("data") or values[0].get("values") or values[0].get("y") or values
        out = []
        for label, value in zip(labels, values):
            numeric = _number(value)
            if label is not None and numeric is not None and numeric >= 0:
                out.append({"label": str(label).strip(), "value": numeric})
        return out

    def walk(node: Any, depth: int = 0) -> list[dict[str, Any]]:
        if depth > 9 or node is None:
            return []
        if isinstance(node, dict):
            labels = next((node.get(key) for key in (
                "labels", "categories", "states", "stateNames", "state_names", "makerNames",
                "maker_names", "names", "xAxisData", "categoryNames", "xaxisCategories",
            ) if isinstance(node.get(key), list)), None)
            values = next((node.get(key) for key in (
                "values", "counts", "registrations", "registrationCounts", "vehicleCounts",
                "registeredVehicleCounts", "registeredVehicleCountList", "registration_count_list",
            ) if isinstance(node.get(key), list)), None)
            if values is None and isinstance(node.get("data"), list):
                values = node.get("data")
            direct = zipped(labels, values)
            if direct:
                return direct

            # Chart.js: {labels: [...], datasets: [{label: 'Registrations', data: [...]}]}.
            datasets = node.get("datasets")
            if isinstance(labels, list) and isinstance(datasets, list):
                combined = []
                for dataset in datasets:
                    if not isinstance(dataset, dict):
                        continue
                    dataset_values = dataset.get("data") or dataset.get("values")
                    combined.extend(zipped(labels, dataset_values))
                if combined:
                    return combined

            # Apex/ECharts: x-axis categories + series data. Some payloads use
            # xAxis as an object and others use options.xaxis.categories.
            xaxis = node.get("xAxis") or node.get("xaxis") or node.get("x_axis")
            if not isinstance(xaxis, dict):
                options = node.get("options") if isinstance(node.get("options"), dict) else {}
                xaxis = options.get("xaxis") or options.get("xAxis") or {}
            if isinstance(xaxis, list):
                xaxis = xaxis[0] if xaxis else {}
            if isinstance(xaxis, dict) and labels is None:
                labels = xaxis.get("categories") or xaxis.get("data")
            for key in ("series", "datasets"):
                nested = node.get(key)
                if isinstance(nested, list):
                    points: list[dict[str, Any]] = []
                    for series in nested:
                        if not isinstance(series, dict):
                            continue
                        # Preserve labels from the parent chart config while
                        # descending into individual series.
                        series_values = series.get("data") or series.get("values") or series.get("y")
                        if isinstance(labels, list) and isinstance(series_values, list):
                            points.extend(zipped(labels, series_values))
                        if not points:
                            points.extend(walk(series, depth + 1))
                    if points:
                        return points

            # Object rows / Spring DTO payloads.
            for key in wrapper_keys:
                nested = node.get(key)
                if isinstance(nested, (dict, list)):
                    points = walk(nested, depth + 1)
                    if points:
                        return points

            label = next((node.get(key) for key in label_keys if node.get(key) not in (None, "")), None)
            value = next((node.get(key) for key in value_keys if node.get(key) not in (None, "")), None)
            numeric = _number(value)
            if label is not None and numeric is not None and numeric >= 0:
                return [{"label": str(label).strip(), "value": numeric}]

            # A few endpoints return {"State A": 123, "State B": 456} instead
            # of row objects. Accept a map only when at least two entries are
            # numeric, which avoids treating metadata like status/code as data.
            mapped = []
            ignored = {"status", "code", "message", "success", "total", "page", "size", "count"}
            for key, raw_value in node.items():
                if str(key).casefold() in ignored or str(key).startswith("_"):
                    continue
                numeric = _number(raw_value)
                if numeric is not None and numeric >= 0 and not isinstance(raw_value, bool):
                    mapped.append({"label": str(key).strip(), "value": numeric})
            if len(mapped) >= 2:
                return mapped
            return []
        if isinstance(node, list):
            out = []
            for item in node:
                if isinstance(item, dict):
                    row = walk(item, depth + 1)
                    if row:
                        out.extend(row)
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    numeric = _number(item[1])
                    if numeric is not None and numeric >= 0:
                        out.append({"label": str(item[0]).strip(), "value": numeric})
            return out
        return []

    parsed = walk(payload)
    unique: dict[str, dict[str, Any]] = {}
    for item in parsed:
        label = re.sub(r"\s+", " ", str(item.get("label") or "")).strip()
        if not label:
            continue
        key = label.casefold()
        if key in unique:
            unique[key]["value"] += float(item["value"])
        else:
            unique[key] = {"label": label, "value": float(item["value"])}
    return list(unique.values())


def _category_bucket_points(raw_points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fallback classifier for source labels when the bucket-specific endpoint is unavailable.

    The preferred live path gets one source aggregate per filter bucket. This
    helper only groups unmistakable labels; ambiguous labels remain excluded.
    """
    groups = {name: 0.0 for name in CATEGORY_OPTIONS}
    for point in raw_points:
        label = str(point.get("label") or "").upper()
        bucket = None
        if "TWO WHEELER" in label or "M-CYCLE" in label or "MOPED" in label:
            bucket = "2W"
        elif "THREE WHEELER" in label or "E-RICKSHAW" in label or "AUTO RICKSHAW" in label:
            bucket = "3W"
        elif "TRACTOR" in label:
            bucket = "Tractor"
        elif "BUS" in label:
            bucket = "Bus"
        elif any(token in label for token in ("GOODS VEHICLE", "GOODS CARRIER", "PASSENGER VEHICLE", "MOTOR VEHICLE")):
            bucket = "Commercial Vehicle"
        elif "FOUR WHEELER" in label or "MOTOR CAR" in label:
            bucket = "4W"
        if bucket:
            groups[bucket] += float(point.get("value") or 0)
    return [{"label": key, "value": value} for key, value in groups.items() if value > 0]


def _parse_month_period(value: Any) -> str | None:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})[-/\s]+([A-Za-z]+)", text)
    if match:
        try:
            month = datetime.strptime(match.group(2)[:3], "%b").month
            return f"{match.group(1)}-{month:02d}"
        except ValueError:
            try:
                month = datetime.strptime(match.group(2), "%B").month
                return f"{match.group(1)}-{month:02d}"
            except ValueError:
                return None
    match = re.search(r"(20\d{2})[-/](0?[1-9]|1[0-2])\b", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    return None


def _monthly_series(payload: Any) -> list[dict[str, Any]]:
    totals: dict[str, float] = {}
    for item in _unwrap_list(payload):
        if not isinstance(item, dict):
            continue
        period = _parse_month_period(item.get("yearAsString") or item.get("year") or item.get("period") or item.get("monthYear"))
        value = _number(item.get("registeredVehicleCount") if item.get("registeredVehicleCount") is not None else item.get("registrations") if item.get("registrations") is not None else item.get("count"))
        if period and value is not None and value >= 0:
            totals[period] = totals.get(period, 0.0) + value
    return [{"period": period, "registrations": value} for period, value in sorted(totals.items())]


def _derive_series(monthly: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    quarter_totals: dict[str, float] = {}
    year_totals: dict[str, float] = {}
    for row in monthly:
        year, month = row["period"].split("-")
        quarter = (int(month) - 1) // 3 + 1
        quarter_key = f"{year}-Q{quarter}"
        quarter_totals[quarter_key] = quarter_totals.get(quarter_key, 0.0) + row["registrations"]
        year_totals[year] = year_totals.get(year, 0.0) + row["registrations"]
    return {
        "monthly": monthly,
        "quarterly": [{"period": key, "registrations": value} for key, value in sorted(quarter_totals.items())],
        "yearly": [{"period": key, "registrations": value} for key, value in sorted(year_totals.items())],
    }


def _same_period_previous_year(period: str, granularity: str) -> str | None:
    try:
        if granularity == "monthly":
            year, month = period.split("-")
            return f"{int(year)-1:04d}-{month}"
        if granularity == "quarterly":
            year, quarter = period.split("-")
            return f"{int(year)-1:04d}-{quarter}"
        return str(int(period) - 1)
    except (ValueError, TypeError):
        return None


def _latest_complete_row(rows: list[dict[str, Any]], granularity: str, today: date | None = None) -> dict[str, Any] | None:
    if not rows:
        return None
    today = today or date.today()
    eligible = []
    for row in rows:
        period = str(row.get("period") or "")
        try:
            if granularity == "monthly":
                year, month = (int(x) for x in period.split("-"))
                complete = (year, month) < (today.year, today.month)
            elif granularity == "quarterly":
                year_text, quarter_text = period.split("-")
                year, quarter = int(year_text), int(quarter_text.lstrip("Q"))
                current_quarter = (today.month - 1) // 3 + 1
                complete = (year, quarter) < (today.year, current_quarter)
            else:
                complete = int(period) < today.year
        except (ValueError, TypeError):
            continue
        if complete:
            eligible.append(row)
    return eligible[-1] if eligible else rows[-1]


def _period_is_complete(period: str, granularity: str, today: date | None = None) -> bool:
    today = today or date.today()
    try:
        if granularity == "monthly":
            year, month = (int(x) for x in period.split("-"))
            return (year, month) < (today.year, today.month)
        if granularity == "quarterly":
            year_text, quarter_text = period.split("-")
            year, quarter = int(year_text), int(quarter_text.lstrip("Q"))
            current_quarter = (today.month - 1) // 3 + 1
            return (year, quarter) < (today.year, current_quarter)
        return int(period) < today.year
    except (ValueError, TypeError):
        return False


def _growth_table(series: dict[str, list[dict[str, Any]]], granularity: str, today: date | None = None) -> list[dict[str, Any]]:
    # The portal may return the current partial month/quarter/year. Exclude it
    # from YoY comparisons so it cannot create a misleading negative growth row.
    rows = [row for row in (series.get(granularity) or []) if _period_is_complete(str(row.get("period") or ""), granularity, today)]
    by_period = {str(row["period"]): float(row["registrations"]) for row in rows}
    result = []
    for row in rows:
        period = str(row["period"])
        previous_key = _same_period_previous_year(period, granularity)
        previous = by_period.get(previous_key) if previous_key else None
        growth = ((float(row["registrations"]) / previous) - 1.0) * 100 if previous and previous > 0 else None
        result.append({
            "period": period,
            "registrations": float(row["registrations"]),
            "growth_rate": growth,
            "comparison_period": previous_key if previous is not None else None,
        })
    return result


def _previous_period(period: str, granularity: str) -> str | None:
    try:
        if granularity == "monthly":
            year, month = (int(value) for value in period.split("-"))
            if month == 1:
                return f"{year - 1:04d}-12"
            return f"{year:04d}-{month - 1:02d}"
        if granularity == "quarterly":
            year_text, quarter_text = period.split("-")
            year, quarter = int(year_text), int(quarter_text.lstrip("Q"))
            return f"{year - 1:04d}-Q4" if quarter == 1 else f"{year:04d}-Q{quarter - 1}"
        return str(int(period) - 1)
    except (ValueError, TypeError):
        return None


def _maker_comparison_payload(
    maker_series: dict[str, dict[str, list[dict[str, Any]]]],
    makers: list[str],
    timeline: str,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build maker trend lines and period-pivoted tables.

    Growth values are grouped into a single cell per maker/period. This keeps
    the growth table structurally aligned with the absolute-registration table:
    one row per maker (one default row for All Vehicle Registrations), and one
    column per chronological period. Monthly cells contain MoM + YoY; quarterly
    cells contain QoQ + YoY; yearly cells contain YoY.
    """
    metric_names = ["MoM (%)", "YoY (%)"] if timeline == "monthly" else (
        ["QoQ (%)", "YoY (%)"] if timeline == "quarterly" else ["YoY (%)"]
    )
    chart_series: list[dict[str, Any]] = []
    period_values_by_maker: dict[str, dict[str, float]] = {}
    growth_values_by_maker: dict[str, dict[str, dict[str, float | None]]] = {}
    all_periods: set[str] = set()

    for maker in makers:
        series = maker_series.get(maker) or {"monthly": [], "quarterly": [], "yearly": []}
        period_rows = series.get(timeline) or []
        values = {str(row["period"]): float(row["registrations"]) for row in period_rows}
        complete_rows = [
            row for row in period_rows
            if _period_is_complete(str(row.get("period") or ""), timeline)
        ]
        chart_series.append({
            "name": maker,
            "data": [
                {"label": str(row["period"]), "value": float(row["registrations"])}
                for row in complete_rows[-48:]
            ],
        })

        visible_rows = complete_rows[-36:]
        maker_period_values: dict[str, float] = {}
        maker_growth_values: dict[str, dict[str, float | None]] = {metric: {} for metric in metric_names}
        for row in visible_rows:
            period = str(row["period"])
            current = float(row["registrations"])
            all_periods.add(period)
            maker_period_values[period] = current
            previous_period = _previous_period(period, timeline)
            previous_year = _same_period_previous_year(period, timeline)
            previous_value = values.get(previous_period) if previous_period else None
            previous_year_value = values.get(previous_year) if previous_year else None
            if timeline == "monthly":
                maker_growth_values["MoM (%)"][period] = (
                    ((current / previous_value) - 1) * 100
                    if previous_value is not None and previous_value > 0 else None
                )
            elif timeline == "quarterly":
                maker_growth_values["QoQ (%)"][period] = (
                    ((current / previous_value) - 1) * 100
                    if previous_value is not None and previous_value > 0 else None
                )
            maker_growth_values["YoY (%)"][period] = (
                ((current / previous_year_value) - 1) * 100
                if previous_year_value is not None and previous_year_value > 0 else None
            )

        period_values_by_maker[maker] = maker_period_values
        growth_values_by_maker[maker] = maker_growth_values

    periods = sorted(all_periods)
    growth_columns = ["Maker", *periods]
    growth_rows: list[dict[str, Any]] = []
    absolute_columns = ["Maker", *periods]
    absolute_rows: list[dict[str, Any]] = []

    for maker in makers:
        # One table row per maker. Each period cell keeps all applicable growth
        # measures, avoiding separate duplicate Maker/Metric rows.
        growth_row: dict[str, Any] = {"Maker": maker}
        maker_metrics = growth_values_by_maker.get(maker, {})
        for period in periods:
            growth_row[period] = {
                metric: maker_metrics.get(metric, {}).get(period)
                for metric in metric_names
            }
        growth_rows.append(growth_row)

        absolute_row: dict[str, Any] = {"Maker": maker}
        maker_values = period_values_by_maker.get(maker, {})
        for period in periods:
            absolute_row[period] = maker_values.get(period)
        absolute_rows.append(absolute_row)

    return {
        "timeline": timeline,
        "makers": makers,
        "chart": {
            "key": "maker-registration-trend",
            "title": "Vehicle Registrations by Timeline",
            "kind": "line",
            "series": chart_series,
        },
        "table": {
            "key": "maker-registration-growth",
            "title": "Registration Growth (%) by Maker",
            "columns": growth_columns,
            "metrics": metric_names,
            "rows": growth_rows,
        },
        "absolute_table": {
            "key": "maker-registration-absolute",
            "title": "Absolute Registrations by Maker",
            "columns": absolute_columns,
            "rows": absolute_rows,
        },
        "source_errors": errors or {},
    }


async def _get_json(client: httpx.AsyncClient, endpoint: str, params: dict[str, str], *, retries: int = 2) -> Any:
    url = _API_ROOT + endpoint.lstrip("/")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:
                preview = response.text[:180].replace("\n", " ")
                raise ValueError(f"Parivahan endpoint returned non-JSON content: {preview}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(0.45 * (attempt + 1))
    raise RuntimeError(f"{endpoint}: {type(last_error).__name__}: {last_error}") from last_error


async def search_makers(search: str, *, limit: int = 30) -> list[str]:
    """Return source-side maker search suggestions (no full 7k-name crawl)."""
    query = re.sub(r"\s+", " ", str(search or "")).strip()
    if len(query) < 2:
        return []
    limit = min(max(int(limit), 1), 50)
    cache_key = query.casefold()
    cached = _MAKER_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < _MAKER_CACHE_TTL:
        return cached[1][:limit]
    params = {"page": "0", "size": str(limit), "search": query}
    async with httpx.AsyncClient(timeout=_API_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        payload = await _get_json(client, "publicdashboard/lazy/vehicle-makers", params)
    makers = []
    for item in _unwrap_list(payload):
        value = item.get("name") or item.get("label") if isinstance(item, dict) else item
        if value is not None and str(value).strip():
            makers.append(str(value).strip())
    unique = list(dict.fromkeys(makers))
    _MAKER_CACHE[cache_key] = (time.monotonic(), unique)
    return unique[:limit]


async def _capture_overview(source_filters: dict[str, str]) -> dict[str, Any]:
    started = time.monotonic()
    timeline = source_filters.get("timeline", "monthly")
    chart_calendar_type = TIMELINE_OPTIONS.get(timeline, "3")
    filter_without_timeline = {key: value for key, value in source_filters.items() if key != "timeline"}

    if _filter_scope_is_empty(source_filters):
        return _empty_dashboard(source_filters, "The selected Category=EV and Fuel combination has no matching source fuel types.")

    # Keep a monthly source series as the canonical observation set; the UI's
    # selected Timeline is also sent to the chart/ranking endpoints via its own
    # calendarType so every visual is refreshed for the same selected grain.
    applied_params_series = _base_params("3", source_filters)
    # Parivahan's category/fuel and Top 5 aggregate endpoints are published in
    # Calendar Year mode. The Growth Rate Timeline buttons (monthly/quarterly/
    # yearly) control the duration history table, not these static aggregate
    # endpoints. Passing calendarType=2/3 here caused both Top 5 endpoints to
    # return empty chart payloads in the monthly default view.
    applied_params_annual_aggregates = _base_params("1", source_filters)
    share_filters = {key: value for key, value in filter_without_timeline.items() if key != "fuel"}
    share_params = _base_params("1", share_filters)

    calls: dict[str, tuple[str, dict[str, str]]] = {
        "registration_history": ("publicdashboard/vahandashboard/durationWiseRegistrationTable", applied_params_series),
        "fuel_mix": ("publicdashboard/vahandashboard/fueltypedonutchart", applied_params_annual_aggregates),
        "category_mix": ("publicdashboard/vahandashboard/categoriesdonutchart", applied_params_annual_aggregates),
        "top_makers": ("publicdashboard/vahandashboard/top5Makerchart", applied_params_annual_aggregates),
    }
    if source_filters.get("fuel") or source_filters.get("category") == "EV":
        calls["fuel_mix_for_ev_share"] = ("publicdashboard/vahandashboard/fueltypedonutchart", share_params)

    # Obtain fixed vehicle-category totals from Parivahan's actual group filters.
    # This is more reliable than guessing from ambiguous class labels such as
    # LIGHT MOTOR VEHICLE, which can contain multiple vehicle groups.
    if source_filters.get("category"):
        category_candidates = [source_filters["category"]]
    else:
        category_candidates = CATEGORY_OPTIONS
    selected_fuel = source_filters.get("fuel")
    for index, bucket in enumerate(category_candidates):
        candidate_filters = {key: value for key, value in filter_without_timeline.items() if key not in ("category",)}
        candidate_filters["category"] = bucket
        if bucket == "EV" and selected_fuel not in (None, "EV"):
            continue
        if bucket != "EV" and selected_fuel == "EV":
            # EV is the requested fuel filter, not an additional category-group
            # predicate; let the selected group be intersected with EV fuel.
            pass
        calls[f"category_bucket_{index}_{bucket}"] = (
            "publicdashboard/vahandashboard/fueltypedonutchart",
            _base_params("1", candidate_filters),
        )

    comparison_makers = [name.strip() for name in source_filters.get("makers", "").split(",") if name.strip()]
    comparison_call_keys: dict[str, str] = {}
    # Use the same official durationWiseRegistrationTable endpoint and monthly
    # source contract for each maker. Apply the shared Category AND Fuel filters
    # on every individual request so each line is scoped identically.
    comparison_base_filters = {
        key: source_filters[key]
        for key in ("category", "fuel")
        if source_filters.get(key)
    }
    for index, maker_name in enumerate(comparison_makers):
        call_key = f"maker_trend_{index}"
        maker_filters = {**comparison_base_filters, "maker": maker_name}
        calls[call_key] = (
            "publicdashboard/vahandashboard/durationWiseRegistrationTable",
            _base_params("3", maker_filters),
        )
        comparison_call_keys[maker_name] = call_key

    async with httpx.AsyncClient(timeout=_API_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        # Establish the same public-dashboard session as a browser visit before
        # calling the chart endpoints. Some Parivahan chart handlers return an
        # empty 200 response when called without the landing-page session.
        try:
            landing = await client.get(URL)
            landing.raise_for_status()
            logger.info("AUTOMOTIVE_PARIVAHAN_SESSION_READY status=%s bytes=%d", landing.status_code, len(landing.content or b""))
        except Exception as exc:
            # Do not block endpoints that do not need cookies; keep a warning
            # for diagnostics and still attempt each public JSON resource.
            logger.warning("AUTOMOTIVE_PARIVAHAN_SESSION_BOOTSTRAP_FAILED error=%s: %s", type(exc).__name__, str(exc)[:220])
        tasks = {key: asyncio.create_task(_get_json(client, endpoint, params)) for key, (endpoint, params) in calls.items()}
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for key, task in tasks.items():
            try:
                results[key] = await task
            except Exception as exc:
                errors[key] = f"{type(exc).__name__}: {exc}"[:400]

    monthly = _monthly_series(results.get("registration_history"))
    series = _derive_series(monthly)
    maker_series: dict[str, dict[str, list[dict[str, Any]]]] = {}
    maker_comparison_errors: dict[str, str] = {}
    # With no maker selected, show the total vehicle-registration history as
    # the default single line. Once makers are selected, each maker receives
    # its own source request and independent line.
    comparison_display_makers = comparison_makers or ["All Vehicle Registrations"]
    if not comparison_makers:
        maker_series["All Vehicle Registrations"] = series
    else:
        for maker_name, call_key in comparison_call_keys.items():
            if call_key in results:
                maker_monthly = _monthly_series(results.get(call_key))
                maker_series[maker_name] = _derive_series(maker_monthly)
                if not maker_monthly:
                    maker_comparison_errors[maker_name] = "Parivahan returned no usable registration history for this maker."
            else:
                maker_comparison_errors[maker_name] = errors.get(call_key, "Unable to retrieve registration history for this maker.")
    maker_comparison = _maker_comparison_payload(
        maker_series, comparison_display_makers, timeline, maker_comparison_errors
    )
    raw_fuel_points = _chart_points(results.get("fuel_mix"), preferred_label_keys=("fuelType", "fuelName"))
    fuel_points = _bucket_fuel_points(raw_fuel_points)
    raw_category_points = _chart_points(results.get("category_mix"), preferred_label_keys=("categoryName", "vehicleCategory"))

    category_points: list[dict[str, Any]] = []
    for index, bucket in enumerate(category_candidates):
        raw_bucket = _chart_points(results.get(f"category_bucket_{index}_{bucket}"), preferred_label_keys=("fuelType", "fuelName"))
        bucket_total = sum(point["value"] for point in raw_bucket)
        if bucket_total > 0:
            category_points.append({"label": bucket, "value": bucket_total})
        elif f"category_bucket_{index}_{bucket}" not in results:
            errors.setdefault(f"category_{bucket}", "Parivahan category aggregate did not return a usable response")
    if not category_points and raw_category_points:
        category_points = _category_bucket_points(raw_category_points)

    state_points: list[dict[str, Any]] = []
    maker_points = sorted(
        _chart_points(results.get("top_makers"), preferred_label_keys=("makerName", "maker_name", "manufacturerName", "manufacturer")),
        key=lambda item: item["value"], reverse=True,
    )[:20]
    if "top_makers" in results and not maker_points:
        payload = results["top_makers"]
        shape = ",".join(list(payload)[:12]) if isinstance(payload, dict) else f"items={len(payload)}" if isinstance(payload, list) else type(payload).__name__
        errors["top_makers"] = f"Parivahan responded but no ranking rows could be parsed (payload type: {type(payload).__name__}; keys/shape: {shape})"

    # Use the unbucketed source fuel aggregate for KPI denominator and the
    # canonical 5-bucket display for the chart. If a Fuel filter is selected,
    # make a second request without that fuel predicate for a true EV share.
    all_fuel_points = raw_fuel_points
    if "fuel_mix_for_ev_share" in results:
        all_fuel_points = _chart_points(results.get("fuel_mix_for_ev_share"), preferred_label_keys=("fuelType", "fuelName"))
    fuel_total = sum(point["value"] for point in all_fuel_points)
    ev_total = sum(point["value"] for point in all_fuel_points if _fuel_bucket(point["label"]) == "EV")
    ev_share = (ev_total / fuel_total * 100.0) if fuel_total > 0 else None

    filtered_total = sum(point["value"] for point in raw_fuel_points)
    if not filtered_total:
        filtered_total = sum(point["value"] for point in raw_category_points)
    if not filtered_total:
        filtered_total = sum(row["registrations"] for row in monthly)

    growth_tables = {granularity: _growth_table(series, granularity) for granularity in ("monthly", "quarterly", "yearly")}
    growth_by_timeline: dict[str, dict[str, Any]] = {}
    for granularity in ("monthly", "quarterly", "yearly"):
        latest = growth_tables[granularity][-1] if growth_tables[granularity] else None
        growth_by_timeline[granularity] = {
            "value": latest.get("growth_rate") if latest else None,
            "period": latest.get("period") if latest else None,
        }
    growth_rows = growth_tables.get(timeline) or []
    growth_latest = growth_rows[-1] if growth_rows else None
    growth_rate = growth_latest.get("growth_rate") if growth_latest else None
    growth_period = growth_latest.get("period") if growth_latest else None

    kpis = [
        {"key": "total-registrations", "label": "Total Registrations", "value": filtered_total,
         "display_value": f"{filtered_total:,.0f}",
         "definition": "Sum of source-returned registration aggregates for the selected source filters."},
        {"key": "registration-growth", "label": "Registration Growth", "value": growth_rate,
         "display_value": f"{growth_rate:+.2f}%" if growth_rate is not None else "Not available",
         "period": growth_period, "growth_by_timeline": growth_by_timeline,
         "definition": "Year-over-year growth for the latest complete period at the selected Timeline grain."},
        {"key": "ev-share", "label": "EV Share", "value": ev_share,
         "display_value": f"{ev_share:.2f}%" if ev_share is not None else "Not available",
         "definition": "EV fuel registrations divided by all fuel registrations for the same category/manufacturer scope."},
    ]

    source_status = {}
    for key in calls:
        rows = {
            "fuel_mix": fuel_points, "category_mix": category_points,
            "top_makers": maker_points,
        }.get(key)
        if key.startswith("category_bucket_"):
            rows = [point for point in category_points if point["label"] in key]
        if key in results:
            source_status[key] = {"status": "success" if rows is None or rows else "empty", "error": errors.get(key)}
        else:
            source_status[key] = {"status": "failed", "error": errors.get(key)}
    data_quality_notes = [
        "Charts and rankings use official Parivahan aggregate endpoint responses.",
        "Fuel chart is grouped into Petrol, CNG, Diesel, Hybrid and EV; unclassified source fuel labels are omitted rather than mislabelled.",
        "Category chart uses the source's 2W, 4W, 3W, Tractor, Commercial Vehicle, EV and Bus filter buckets.",
        "Timeline controls the source monthly history and selected growth-table grain. Parivahan's fuel/category and ranking aggregate endpoints use Calendar Year mode (calendarType=1), as in its public aggregate contract.",
        "The official Parivahan aggregate maker-ranking endpoint is top5Makerchart (the public dashboard labels it Top 5 Makers). The vertical Top 20 chart displays up to 20 source-returned rows; the current endpoint supplies five, and missing ranks are not fabricated.",
        "Growth table excludes the current incomplete month, quarter or year.",
        "Commercial Vehicle combines the source's published Heavy CV and Light CV vehicle sub-category labels.",
        "EV is a propulsion bucket and may overlap vehicle-type categories; it is not a mutually exclusive vehicle body type.",
    ]
    if errors:
        data_quality_notes.append("Some source datasets returned errors or could not be parsed; affected visuals are shown as unavailable, not estimated.")

    logger.info(
        "AUTOMOTIVE_PARIVAHAN_API_CAPTURE duration=%.2fs timeline=%s filters=%s monthly=%d fuel=%d category=%d makers=%d errors=%s",
        time.monotonic() - started, timeline, filter_without_timeline, len(monthly), len(fuel_points), len(category_points), len(maker_points), list(errors),
    )

    return {
        "filters": {
            "selected": {"timeline": timeline, "category": source_filters.get("category") or "All", "fuel": source_filters.get("fuel") or "All", "makers": comparison_makers or ["All Vehicle Registrations"]},
            "category_options": CATEGORY_OPTIONS, "fuel_options": FUEL_OPTIONS,
            "timeline_options": list(TIMELINE_OPTIONS),
        },
        "selected_timeline": timeline,
        "kpis": kpis,
        "series": series,
        "growth_table": growth_rows[-36:],
        "growth_tables": {key: rows[-36:] for key, rows in growth_tables.items()},
        "growth_by_timeline": growth_by_timeline,
        "maker_comparison": maker_comparison,
        "breakdowns": {"fuel": fuel_points, "category": category_points},
        "top_states": state_points,
        "top_makers": maker_points,
        "charts": [
            {"key": "fuel-mix", "title": "Vehicle Registrations by Fuel Type", "kind": "pie", "series": [{"name": "Fuel", "data": fuel_points}]},
            {"key": "category-mix", "title": "Vehicle Registration by Category", "kind": "pie", "series": [{"name": "Category", "data": category_points}]},
            {"key": "top-makers", "title": "Top 20 Makers by Registration Volume", "kind": "bar", "series": [{"name": "Makers", "data": maker_points}]},
        ],
        "tables": [
            {
                "key": "registration-growth", "title": "Vehicle Registration Growth Rate",
                "columns": ["Period", "Vehicle Registrations", "Growth Rate YoY"],
                "rows": [{"Period": row["period"], "Vehicle Registrations": row["registrations"], "Growth Rate YoY": row["growth_rate"]} for row in growth_rows[-36:]],
            },
        ],
        "latest_complete_period": growth_period,
        "data_quality": data_quality_notes,
        "source_url": URL,
        "source_as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_endpoints": source_status,
        "source_errors": errors,
        "request_params": {
            "timeline": timeline,
            "calendar_type": chart_calendar_type,
            "category_group": source_filters.get("category"),
            "fuel_group": source_filters.get("fuel"),
            "makers": comparison_makers,
        },
    }


def _empty_dashboard(source_filters: dict[str, str], note: str) -> dict[str, Any]:
    return {
        "filters": {"selected": {"timeline": source_filters.get("timeline", "monthly"), "category": source_filters.get("category", "All"), "fuel": source_filters.get("fuel", "All"), "makers": [name.strip() for name in source_filters.get("makers", "").split(",") if name.strip()] or ["All Vehicle Registrations"]}, "category_options": CATEGORY_OPTIONS, "fuel_options": FUEL_OPTIONS, "timeline_options": list(TIMELINE_OPTIONS)},
        "selected_timeline": source_filters.get("timeline", "monthly"),
        "kpis": [
            {"key": "total-registrations", "label": "Total Registrations", "value": None, "display_value": "—"},
            {"key": "registration-growth", "label": "Registration Growth", "value": None, "display_value": "—"},
            {"key": "ev-share", "label": "EV Share", "value": None, "display_value": "—"},
        ],
        "series": {"monthly": [], "quarterly": [], "yearly": []}, "growth_table": [],
        "growth_tables": {"monthly": [], "quarterly": [], "yearly": []}, "growth_by_timeline": {},
        "maker_comparison": {"timeline": source_filters.get("timeline", "monthly"), "makers": [name.strip() for name in source_filters.get("makers", "").split(",") if name.strip()], "chart": {"kind": "line", "series": []}, "table": {"columns": ["Maker"], "metrics": [], "rows": []}, "absolute_table": {"columns": ["Maker"], "rows": []}, "source_errors": {}},
        "breakdowns": {"fuel": [], "category": []}, "top_states": [], "top_makers": [],
        "charts": [], "tables": [], "latest_complete_period": None,
        "data_quality": [note], "source_url": URL,
        "source_as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_endpoints": {}, "source_errors": {"filters": note},
        "request_params": {"timeline": source_filters.get("timeline", "monthly")},
    }


_FLASH_TIMEOUT = httpx.Timeout(connect=15.0, read=50.0, write=15.0, pool=15.0)
_FLASH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}


def _flash_clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _flash_numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = _flash_clean(value).replace("₹", "").replace(",", "")
    sign = -1.0 if text.startswith(("▼", "−", "-")) else 1.0
    text = text.replace("▲", "").replace("▼", "").replace("−", "").replace("+", "").replace("%", "")
    multiplier = 1.0
    if re.search(r"\b(?:cr|crore)s?\b", text, re.I):
        multiplier = 10_000_000.0
    elif re.search(r"\b(?:l|lac|lakh)s?\b", text, re.I):
        multiplier = 100_000.0
    text = re.sub(r"\b(?:units?|registrations?|vehicles?|evs?|e-cars?|e-2w|ytd|cr|crore|l|lac|lakhs?)\b", "", text, flags=re.I)
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return sign * float(match.group(0)) * multiplier
    except ValueError:
        return None


def _flash_is_value_line(value: str) -> bool:
    text = _flash_clean(value)
    if not text or len(text) > 45:
        return False
    # The entire line must be a value, not a sentence containing a number.
    return bool(re.fullmatch(r"(?:[▲▼+−-]\s*)?[\d,]+(?:\.\d+)?(?:\s*(?:%|L|lakh|lakhs|Cr|crore))?(?:\s+units?\s+YTD)?", text, re.I))


def _flash_heading_text(node: Any) -> str:
    heading = node.find_previous(["h2", "h3", "h4"]) if hasattr(node, "find_previous") else None
    return _flash_clean(heading.get_text(" ", strip=True)) if heading else "Source table"


def _flash_extract_tables(soup: BeautifulSoup) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for table_index, table in enumerate(soup.find_all("table"), 1):
        raw_rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            if not cells:
                cells = tr.find_all(["th", "td"])
            row = [_flash_clean(cell.get_text(" ", strip=True)) for cell in cells]
            if row and any(row):
                raw_rows.append(row)
        if len(raw_rows) < 2:
            continue
        header_row_index = next((i for i, row in enumerate(raw_rows[:3]) if len(row) >= 2 and all(x for x in row)), 0)
        headers = raw_rows[header_row_index]
        # Avoid treating a rank column without a name as a useful table.
        headers = [header or f"Column {i+1}" for i, header in enumerate(headers)]
        unique_headers: list[str] = []
        for index, header in enumerate(headers):
            name = header[:160]
            if name in unique_headers:
                name = f"{name} {index+1}"
            unique_headers.append(name)
        rows: list[dict[str, Any]] = []
        for raw_row in raw_rows[header_row_index + 1:]:
            if len(raw_row) < 2:
                continue
            aligned = raw_row[:len(unique_headers)] + [""] * max(0, len(unique_headers) - len(raw_row))
            record = {unique_headers[i]: aligned[i] for i in range(len(unique_headers))}
            if any(value for value in record.values()):
                rows.append(record)
        if not rows:
            continue
        title = _flash_heading_text(table)
        tables.append({
            "key": f"source-table-{table_index}", "title": title,
            "columns": unique_headers, "rows": rows,
            "row_count": len(rows), "state": "Flash Finance source",
        })
    return tables


def _flash_extract_kpis(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Read the metric strip between the page title and its editorial/chart body."""
    lines = [_flash_clean(item) for item in soup.stripped_strings]
    title = _flash_clean(soup.find("h1").get_text(" ", strip=True)) if soup.find("h1") else ""
    try:
        start = lines.index(title) + 1 if title in lines else 0
    except ValueError:
        start = 0
    stop_candidates = [i for i in range(start, len(lines)) if re.search(r"Auto Registration Pulse|Monthly .* registrations|November so far|Top \d+ states by", lines[i], re.I)]
    stop = min(stop_candidates) if stop_candidates else min(len(lines), start + 90)
    metric_lines = lines[start:stop]
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    detail_prefixes = ("mom", "vs ", "yoy ▲", "yoy ▼", "nov mtd", "jan–", "jan-", "oct ", "live.", "source:", "data updated")
    for index, line in enumerate(metric_lines[:-1]):
        next_line = metric_lines[index + 1]
        if not next_line or next_line.casefold().startswith(detail_prefixes):
            continue
        is_numeric = _flash_is_value_line(line)
        # A leader / fastest-growing-state KPI can have a text value rather than a number.
        text_value = (not is_numeric and len(line) <= 55 and bool(re.search(r"[A-Za-z]", line))
                      and bool(re.search(r"market leader|leading so far|top state by volume|fastest-growing", next_line, re.I)))
        if not is_numeric and not text_value:
            continue
        if _flash_is_value_line(next_line):
            continue
        if not re.search(r"[A-Za-z]", next_line) or len(next_line) > 95:
            continue
        # Skip commentary descriptions; KPI labels are short and metric-like.
        if re.search(r"registered nationally|vehicles registered|source|updated|registrations reach", next_line, re.I):
            continue
        value_num = _flash_numeric(line) if is_numeric else None
        display = line.replace("−", "-").strip()
        label = next_line.strip()
        identity = (label.casefold(), display.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        output.append({
            "key": f"flash-kpi-{len(output)+1}", "label": label,
            "value": value_num, "display_value": display,
            "state": "Flash Finance · Vahan",
        })
    return output[:16]


def _flash_points_from_values(labels: Any, values: Any) -> list[dict[str, Any]]:
    if not isinstance(labels, list) or not isinstance(values, list):
        return []
    if values and isinstance(values[0], dict):
        values = [point.get("y", point.get("value")) for point in values]
    return [
        {"label": _flash_clean(label), "value": number}
        for label, raw in zip(labels, values)
        if _flash_clean(label) and (number := _flash_numeric(raw)) is not None
    ]


def _flash_find_charts(payload: Any, charts: list[dict[str, Any]], seen: set[str], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(payload, list):
        for item in payload:
            _flash_find_charts(item, charts, seen, depth + 1)
        return
    if not isinstance(payload, dict):
        return
    labels = payload.get("labels") or payload.get("categories")
    datasets = payload.get("datasets") or payload.get("series")
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    xaxis = options.get("xaxis") or options.get("xAxis") or payload.get("xaxis") or payload.get("xAxis") or {}
    if not labels and isinstance(xaxis, dict):
        labels = xaxis.get("categories") or xaxis.get("data")
    series_out: list[dict[str, Any]] = []
    if isinstance(datasets, list):
        for ds_index, dataset in enumerate(datasets):
            if isinstance(dataset, dict):
                values = dataset.get("data") or dataset.get("values")
                name = _flash_clean(dataset.get("label") or dataset.get("name") or f"Series {ds_index+1}")
            elif isinstance(dataset, list):
                values, name = dataset, f"Series {ds_index+1}"
            else:
                values, name = None, ""
            points = _flash_points_from_values(labels, values)
            # For {x,y} points, labels can be embedded in each point.
            if not points and isinstance(values, list):
                points = [{"label": _flash_clean(point.get("x") or point.get("label") or point.get("name")), "value": num}
                          for point in values if isinstance(point, dict) and _flash_clean(point.get("x") or point.get("label") or point.get("name"))
                          and (num := _flash_numeric(point.get("y", point.get("value")))) is not None]
            if points:
                series_out.append({"name": name or f"Series {ds_index+1}", "data": points})
    if not series_out and isinstance(datasets, list) and datasets and all(isinstance(x, (int, float, str)) for x in datasets):
        points = _flash_points_from_values(labels, datasets)
        if points:
            series_out = [{"name": "Registrations", "data": points}]
    if series_out:
        title = _flash_clean(payload.get("title") or payload.get("name") or payload.get("label") or "")
        if isinstance(payload.get("title"), dict):
            title = _flash_clean(payload["title"].get("text"))
        if not title:
            title = _flash_clean(options.get("title", {}).get("text") if isinstance(options.get("title"), dict) else "")
        kind = str(payload.get("type") or options.get("chart", {}).get("type") if isinstance(options.get("chart"), dict) else payload.get("type") or "line").lower()
        if kind not in {"bar", "line", "area", "pie"}:
            kind = "line"
        key = json.dumps([title, series_out], ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            charts.append({"key": f"flash-chart-{len(charts)+1}", "title": title or f"Source chart {len(charts)+1}", "kind": kind, "series": series_out, "state": "Flash Finance source script"})
    inherited_title = _flash_clean(payload.get("title") or payload.get("name") or payload.get("label") or "")
    if not inherited_title:
        plugins = options.get("plugins") if isinstance(options.get("plugins"), dict) else {}
        title_config = plugins.get("title") if isinstance(plugins.get("title"), dict) else {}
        inherited_title = _flash_clean(title_config.get("text"))
    inherited_type = payload.get("type")
    for value in payload.values():
        if isinstance(value, dict):
            child = dict(value)
            if inherited_title and not child.get("title") and not child.get("name"):
                child["title"] = inherited_title
            if inherited_type and not child.get("type"):
                child["type"] = inherited_type
            _flash_find_charts(child, charts, seen, depth + 1)
        elif isinstance(value, list):
            _flash_find_charts(value, charts, seen, depth + 1)


def _flash_parse_js_literal(candidate: str) -> Any:
    """Decode a JSON-compatible JavaScript object literal without executing it."""
    text = candidate.strip()
    # Keep quoted content intact while normalizing common literal syntax.
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    text = re.sub(r"(^|\s)//[^\n\r]*", r"\1", text)
    text = re.sub(r"'((?:\\.|[^'\\])*)'", lambda match: json.dumps(match.group(1).replace("\\'", "'")), text)
    text = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = re.sub(r"\bundefined\b", "null", text)
    try:
        return json.loads(text)
    except Exception:
        return None


def _flash_script_charts(soup: BeautifulSoup) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw or len(raw) > 8_000_000:
            continue
        script_type = (script.get("type") or "").lower()
        if script_type in {"application/ld+json", "application/json"} or script.get("id") in {"__NEXT_DATA__", "__NUXT_DATA__"}:
            try:
                payload = json.loads(raw)
                _flash_find_charts(payload, charts, seen)
                continue
            except Exception:
                pass

        # Discover data literals assigned to variables and chart constructor args.
        patterns = (
            r"(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*([{[])",
            r"new\s+Chart\s*\([^,]+,\s*({)",
            r"Chart\.[A-Za-z_$][\w$]*\s*\([^,]+,\s*({)",
            r"(?:chartData|chartConfig|chartOptions)\s*[:=]\s*({)",
        )
        candidate_starts: set[int] = set()
        for pattern in patterns:
            candidate_starts.update(match.start(1) for match in re.finditer(pattern, raw, flags=re.I))
        for start in sorted(candidate_starts):
            opening = raw[start]
            closing = "}" if opening == "{" else "]"
            depth, quote, escaped = 0, None, False
            end = None
            for pos in range(start, len(raw)):
                char = raw[pos]
                if quote:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == quote:
                        quote = None
                    continue
                if char in "'\"`":
                    quote = char
                elif char == opening:
                    depth += 1
                elif char == closing:
                    depth -= 1
                    if depth == 0:
                        end = pos + 1
                        break
            if end:
                payload = _flash_parse_js_literal(raw[start:end])
                if payload is not None:
                    _flash_find_charts(payload, charts, seen)
    return charts


def _flash_table_charts(report_key: str, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build visual encodings only from exact numeric values in source tables.

    These projections are used where the page publishes a table for a named
    chart/scoreboard. They do not invent or estimate any observations.
    """
    charts: list[dict[str, Any]] = []
    for table in tables:
        title = _flash_clean(table.get("title"))
        columns = table.get("columns") or []
        rows = table.get("rows") or []
        if not rows or len(columns) < 2:
            continue
        label_col = next((c for c in columns if re.search(r"manufacturer|maker|brand|state|fuel|segment|vehicle", c, re.I)), columns[1] if len(columns) > 1 else columns[0])
        metric_candidates = [c for c in columns if c != label_col and re.search(r"2026|registrations?|units?|volume|share|nov|ev", c, re.I)]
        if not metric_candidates:
            metric_candidates = [c for c in columns if c != label_col]
        # Prefer raw volume columns, not percentage/share columns.
        metric_col = next((c for c in metric_candidates if not re.search(r"share|growth|%|vs ", c, re.I)), metric_candidates[0] if metric_candidates else None)
        if not metric_col:
            continue
        points = []
        for row in rows:
            label = _flash_clean(row.get(label_col))
            value = _flash_numeric(row.get(metric_col))
            if label and value is not None and value >= 0:
                points.append({"label": label, "value": value})
        if not points:
            continue
        title_lower = title.casefold()
        if report_key == "ev" and not any(token in title_lower for token in ("manufacturer", "maker", "vehicle segment", "state")):
            continue
        if report_key == "two_wheeler" and not any(token in title_lower for token in ("brand", "maker", "electric two-wheeler")):
            continue
        if report_key == "four_wheeler" and not any(token in title_lower for token in ("manufacturer", "maker", "fuel")):
            continue
        if report_key == "commercial_vehicle" and not any(token in title_lower for token in ("manufacturer", "market share", "fuel", "state")):
            continue
        if report_key == "statewise" and not any(token in title_lower for token in ("state", "vehicle registration")):
            continue
        kind = "pie" if "fuel" in title_lower or "segment" in title_lower and "vehicle" in title_lower else "bar"
        key = f"table-projection:{table['key']}:{metric_col}"
        charts.append({"key": key, "title": title, "kind": kind, "series": [{"name": metric_col, "data": points[:30]}], "state": "Flash Finance source table values"})
    return charts


def _scrape_flashfinance_html(report_key: str, html: str) -> dict[str, Any]:
    title, source_url = FLASH_REPORTS[report_key]
    soup = BeautifulSoup(html, "lxml")
    page_title = _flash_clean(soup.find("h1").get_text(" ", strip=True)) if soup.find("h1") else title
    kpis = _flash_extract_kpis(soup)
    tables = _flash_extract_tables(soup)
    charts = _flash_script_charts(soup)
    table_charts = _flash_table_charts(report_key, tables)
    chart_titles = {chart["title"].casefold() for chart in charts}
    for chart in table_charts:
        if chart["title"].casefold() not in chart_titles:
            charts.append(chart)
            chart_titles.add(chart["title"].casefold())
    headings = [_flash_clean(node.get_text(" ", strip=True)) for node in soup.find_all(["h2", "h3", "h4"]) if _flash_clean(node.get_text(" ", strip=True))]
    page_text = _flash_clean(soup.get_text(" ", strip=True))
    updated = None
    match = re.search(r"Data updated\s+([^·|]+)", page_text, re.I)
    if match:
        updated = _flash_clean(match.group(1))
    if not kpis and not tables and not charts:
        raise RuntimeError(f"Flash Finance {title} page returned no extractable KPI, chart or table data")
    notes = [
        "Source: Flash Finance Vahan analytics page; the page states that its figures are vehicle registrations sourced from Vahan / Ministry of Road Transport & Highways.",
        "KPI values and table cells are read from the returned page HTML. Charts are extracted from embedded chart data where available; table-backed chart series use the exact numeric rows of their corresponding source table.",
        "Current-month registrations may be partial and can be revised as Vahan publishes delayed RTO records.",
    ]
    return {
        "source_name": "FLASH_FINANCE_VAHAN",
        "source_url": source_url,
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "fetch": {"mode": "flashfinance_http_html", "http_status": 200, "updated_label": updated},
        "page_title": page_title,
        "kpi_cards": kpis,
        "charts": charts,
        "tables": tables,
        "sections": headings[:60],
        "capture_completeness": {
            "status": "complete" if kpis and charts and tables else "partial",
            "counts": {"kpis": len(kpis), "charts": len(charts), "tables": len(tables)},
            "warnings": [] if kpis and charts and tables else ["The source page exposed only part of the expected KPI/chart/table content in its HTML response."],
        },
        "data_quality_notes": notes,
    }


async def _scrape_flashfinance_report(report_key: str) -> dict[str, Any]:
    if report_key not in FLASH_REPORTS:
        raise KeyError(f"Unknown Automotive Flash Finance report: {report_key}")
    title, source_url = FLASH_REPORTS[report_key]
    async with httpx.AsyncClient(timeout=_FLASH_TIMEOUT, headers=_FLASH_HEADERS, follow_redirects=True) as client:
        response = await client.get(source_url)
        response.raise_for_status()
        result = _scrape_flashfinance_html(report_key, response.text)
        result["fetch"]["http_status"] = response.status_code
        result["fetch"]["final_url"] = str(response.url)
        result["capture_completeness"]["source_name"] = title
        logger.info("AUTOMOTIVE_FLASH_CAPTURE report=%s status=%s kpis=%d charts=%d tables=%d", report_key, response.status_code, len(result["kpi_cards"]), len(result["charts"]), len(result["tables"]))
        return result


async def scrape(report_key: str = "overview", *, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    if report_key != "overview":
        return await _scrape_flashfinance_report(report_key)
    source_filters = _normalise_filters(filters)
    logger.info("AUTOMOTIVE_SCRAPE_START report=%s source=%s filters=%s", report_key, URL, source_filters)
    dashboard = await _capture_overview(source_filters)

    total_kpi = next((item for item in dashboard["kpis"] if item["key"] == "total-registrations"), None)
    growth_kpi = next((item for item in dashboard["kpis"] if item["key"] == "registration-growth"), None)
    ev_kpi = next((item for item in dashboard["kpis"] if item["key"] == "ev-share"), None)

    kpi_cards = [
        {"key": item["key"], "label": item["label"], "value": item["value"], "display_value": item["display_value"], "state": "Parivahan VAHAN"}
        for item in (total_kpi, growth_kpi, ev_kpi) if item is not None
    ]
    charts = dashboard["charts"]
    tables = [
        {"key": item["key"], "title": item["title"], "state": "Overview", "columns": item["columns"], "rows": item["rows"], "row_count": len(item["rows"]), "truncated": False}
        for item in dashboard["tables"]
    ]
    any_values = bool(
        dashboard["series"]["monthly"] or dashboard["breakdowns"]["fuel"] or dashboard["breakdowns"]["category"]
        or dashboard["top_states"] or dashboard["top_makers"]
        or any(item.get("data") for item in (dashboard.get("maker_comparison", {}).get("chart", {}).get("series", [])))
    )
    if not any_values and not _filter_scope_is_empty(source_filters):
        raise RuntimeError("Parivahan returned no usable Automotive Overview datasets. Source endpoint errors: " + str(dashboard["source_errors"]))

    return {
        "source_name": "PARIVAHAN_VAHAN_ANALYTICS",
        "source_url": URL,
        "captured_at": dashboard["source_as_of"],
        "fetch": {"mode": "official_public_json_endpoints", "source_endpoints": dashboard["source_endpoints"]},
        "kpi_cards": kpi_cards,
        "charts": charts,
        "tables": tables,
        "sections": ["Overview"],
        "filters": dashboard["filters"],
        "automotive_dashboard": dashboard,
        "capture_completeness": {
            "status": "complete" if not dashboard["source_errors"] else "partial",
            "warnings": list(dashboard["source_errors"].values()),
            "counts": {
                "monthly_periods": len(dashboard["series"]["monthly"]),
                "fuel_points": len(dashboard["breakdowns"]["fuel"]),
                "category_points": len(dashboard["breakdowns"]["category"]),
                "top_states": len(dashboard["top_states"]),
                "top_makers": len(dashboard["top_makers"]),
            },
        },
    }
