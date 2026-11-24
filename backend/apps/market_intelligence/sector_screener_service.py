from __future__ import annotations

"""Cache-first unified Sector Screener capture service.

Each sector/report is cached as an atomic JSON file. Most sectors use a
one-hour freshness window; Solar uses a five-minute window. Last-good content
remains available while a refresh runs or when a source is unavailable.
"""

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .scrapper.sector_screener import SECTORS, scrape
from .scrapper.sector_screener.nise_http_capture import scrape_company_monthly
from .tradestat_watchlist import get_all_watchlists

logger = logging.getLogger(__name__)
TRADESTAT_DIAG_VERSION = "TRADESTAT-INDEPENDENT-SNAPSHOTS-V3"
CACHE_TTL_SECONDS = 60 * 60
SOLAR_CACHE_TTL_SECONDS = 5 * 60
PPAC_CACHE_TTL_SECONDS = 6 * 60 * 60
TRADESTAT_CACHE_TTL_SECONDS = 60 * 60
TRADESTAT_SCRAPE_TIMEOUT_SECONDS = 720
RETRY_COOLDOWN_SECONDS = 5 * 60
SCRAPE_TIMEOUT_SECONDS = 105
# Historical all-year Solar capture happens only in the worker. Dashboard
# requests read the product-level snapshot and never run a year/filter scrape.
SOLAR_SCRAPE_TIMEOUT_SECONDS = 210
_SOLAR_COMPANY_LOCKS: dict[str, asyncio.Lock] = {}


def cache_ttl_seconds(sector_key: str) -> int:
    """Return the per-sector freshness window without changing other sectors."""
    if sector_key == "solar":
        return SOLAR_CACHE_TTL_SECONDS
    if sector_key == "lpg":
        return PPAC_CACHE_TTL_SECONDS
    if sector_key == "tradestat":
        return TRADESTAT_CACHE_TTL_SECONDS
    return CACHE_TTL_SECONDS
MAX_CONCURRENT_REPORTS = 2  # Cell and Module source captures run concurrently in the Solar worker.
_CACHE_DIR = Path(os.getenv("SECTOR_SCREENER_CACHE_DIR") or (Path(__file__).resolve().parents[2] / "data" / "sector_screener_cache"))
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_SIGNATURES: dict[str, tuple[int, int]] = {}
_TASKS: dict[str, asyncio.Task] = {}
_LOCK_TOKENS: dict[str, str] = {}
_ERRORS: dict[str, str] = {}
_RETRY_AFTER: dict[str, float] = {}
_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_REPORTS)
_TRADESTAT_SEMAPHORE = asyncio.Semaphore(1)


def _key(sector_key: str, report_key: str, filters: dict[str, Any] | None = None) -> str:
    # Version source-specific dashboards separately so legacy generic captures
    # cannot be served after a collector or response-contract migration.
    # Automotive filter combinations remain isolated. Solar has one canonical
    # all-years snapshot per product; all dashboard filters are local projections.
    namespace = ("automotive-api-v9" if sector_key == "automotive" else
                 "solar-direct-http-v10" if sector_key == "solar" else
                 "ppac-petroleum-gas-v2" if sector_key == "lpg" else
                 "tradestat-meidb-v17" if sector_key == "tradestat" else "native-v6")
    base = f"{namespace}:{sector_key}:{report_key}"
    if sector_key not in {"automotive", "solar", "tradestat"}:
        return base
    supplied = filters or {}
    if sector_key == "solar":
        raw_product = str(supplied.get("product") or "Cell").strip().casefold()
        normalized = {
            "product": "module" if raw_product in {"module", "solar module", "panel"} else "cell",
        }
        # One persistent source snapshot per product. Period, year and local
        # chart selections are intentionally excluded from the cache identity.
        return f"solar-direct-http-v10:solar:{report_key}:{normalized['product']}"
    elif sector_key == "tradestat":
        def clean_codes(values: Any) -> list[str]:
            return sorted({
                re.sub(r"\D", "", str(code))
                for code in (values or [])
                if re.sub(r"\D", "", str(code))
            })[:30]
        raw_import_codes = supplied.get("import_hs_codes")
        raw_export_codes = supplied.get("export_hs_codes")
        if raw_import_codes is None and raw_export_codes is None:
            # Backward compatibility for callers that still provide one HS list.
            raw_import_codes = raw_export_codes = supplied.get("hs_codes")
        elif not raw_import_codes and not raw_export_codes and supplied.get("hs_codes"):
            raw_import_codes = raw_export_codes = supplied.get("hs_codes")
        normalized = {
            "import_hs_codes": clean_codes(raw_import_codes),
            "export_hs_codes": clean_codes(raw_export_codes),
            "trade_type": str(supplied.get("trade_type") or "all").strip().casefold(),
            "focus_code": re.sub(r"\D", "", str(supplied.get("focus_code") or "")),
            "history": bool(supplied.get("history") or supplied.get("focus_code")),
        }
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]
        return f"tradestat-meidb-v17:{sector_key}:{report_key}:{digest}"
    else:
        normalized = {
            key: str(value).strip()
            for key, value in supplied.items()
            if value is not None and str(value).strip() and str(value).strip().casefold() != "all"
        }
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]
    return f"{base}:{digest}"


def _cache_file(key: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", key)
    return _CACHE_DIR / f"{safe}.json"


def _attempt_file(key: str) -> Path:
    """Separate diagnostic record; never overwrite the last-known-good report."""
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", key)
    return _CACHE_DIR / f"{safe}_attempt.json"


def _running_file(key: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", key)
    return _CACHE_DIR / f"{safe}_running.json"


def _read_running_lock(key: str) -> dict[str, Any] | None:
    """Read a process-shared refresh lease so API and worker do not double-scrape."""
    path = _running_file(key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
        try:
            started = datetime.fromisoformat(str(payload.get("started_at") or "").replace("Z", "+00:00"))
            age = max(0.0, (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds())
        except Exception:
            age = time.time() - path.stat().st_mtime
        # Uvicorn --reload can terminate a process while it owns a refresh
        # lease. A lock from a dead process must not make every subsequent
        # request report "scraping" for 15 minutes. API and worker processes
        # share this local cache directory, so verify the owner PID when known.
        owner_pid = payload.get("pid")
        if owner_pid is not None:
            try:
                owner_pid = int(owner_pid)
                if owner_pid > 0 and owner_pid != os.getpid():
                    os.kill(owner_pid, 0)
            except ProcessLookupError:
                path.unlink(missing_ok=True)
                logger.warning(
                    "SECTOR_SCREENER_RUNNING_LOCK_OWNER_GONE key=%s pid=%s age_seconds=%.1f",
                    key, owner_pid, age,
                )
                return None
            except PermissionError:
                # The process exists but belongs to another OS user.
                pass
            except (TypeError, ValueError):
                pass
            except OSError as exc:
                # ESRCH means the PID no longer exists on POSIX. Keep the
                # conservative age fallback for other platform-specific errors.
                if getattr(exc, "errno", None) == 3 or getattr(exc, "winerror", None) in {87, 1168}:
                    path.unlink(missing_ok=True)
                    logger.warning(
                        "SECTOR_SCREENER_RUNNING_LOCK_OWNER_GONE key=%s pid=%s age_seconds=%.1f",
                        key, owner_pid, age,
                    )
                    return None

        # Solar's bounded all-years direct-HTTP scrape has a 210-second
        # service timeout. Its lease should expire shortly after that limit.
        max_age = SOLAR_SCRAPE_TIMEOUT_SECONDS + 15 if payload.get("sector_key") == "solar" else 900
        if age > max_age:
            path.unlink(missing_ok=True)
            logger.warning("SECTOR_SCREENER_RUNNING_LOCK_EXPIRED key=%s age_seconds=%.1f", key, age)
            return None
        payload["age_seconds"] = round(age, 1)
        return payload
    except FileNotFoundError:
        return None
    except Exception as exc:
        # A process can be observed in the tiny interval between O_EXCL file
        # creation and JSON write completion. Treat a fresh malformed marker as
        # an active lease; remove only a genuinely old/corrupt marker so it can
        # never block a refresh forever.
        try:
            age = max(0.0, time.time() - path.stat().st_mtime)
            if age > 30:
                path.unlink(missing_ok=True)
                logger.warning("SECTOR_SCREENER_RUNNING_LOCK_CORRUPT_REMOVED key=%s age_seconds=%.1f", key, age)
                return None
            return {"status": "scraping", "age_seconds": round(age, 1), "lock_unverified": True}
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning("SECTOR_SCREENER_RUNNING_LOCK_READ_FAILED key=%s error=%s", key, type(exc).__name__)
            return None


def _reserve_running_lock(sector_key: str, key: str) -> str | None:
    """Atomically claim one report refresh across separate API/worker processes."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _running_file(key)
    token = uuid.uuid4().hex
    payload = {
        "token": token,
        "pid": os.getpid(),
        "sector_key": sector_key,
        "status": "scraping",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    for attempt in range(2):
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            descriptor = os.open(str(path), flags)
            try:
                encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                # Small lease documents are written in one OS write and fsynced
                # so reloads are less likely to observe an empty/truncated lock.
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return token
        except FileExistsError:
            existing = _read_running_lock(key)
            if existing is not None or attempt:
                return None
        except Exception as exc:
            logger.warning("SECTOR_SCREENER_RUNNING_LOCK_CREATE_FAILED key=%s error=%s", key, type(exc).__name__)
            return None
    return None


def _release_running_lock(key: str, token: str | None) -> None:
    if not token:
        return
    path = _running_file(key)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(current, dict) and current.get("token") == token:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("SECTOR_SCREENER_RUNNING_LOCK_RELEASE_FAILED key=%s error=%s", key, type(exc).__name__)


def _read_attempt(key: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(_attempt_file(key).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("SECTOR_SCREENER_ATTEMPT_READ_FAILED key=%s error=%s", key, type(exc).__name__)
        return None


def _write_attempt(key: str, payload: dict[str, Any]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _attempt_file(key)
    temp = path.with_suffix(f".{os.getpid()}.attempt.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


def _clear_attempt(key: str) -> None:
    try:
        _attempt_file(key).unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("SECTOR_SCREENER_ATTEMPT_CLEAR_FAILED key=%s error=%s", key, type(exc).__name__)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def _read_cached(key: str) -> dict[str, Any] | None:
    """Read the shared disk cache and notice writes made by the worker process.

    The API and worker are separate processes. Returning an in-process value
    forever would hide the worker's newer one-hour snapshots, so the cached
    file signature is checked before reusing the memory copy.
    """
    path = _cache_file(key)
    try:
        stat = path.stat()
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        if key in _CACHE and _CACHE_SIGNATURES.get(key) == signature:
            return _CACHE[key]
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            _CACHE[key] = payload
            _CACHE_SIGNATURES[key] = signature
            return payload
    except FileNotFoundError:
        return _CACHE.get(key)
    except Exception as exc:
        logger.warning("SECTOR_SCREENER_CACHE_READ_FAILED key=%s error=%s", key, type(exc).__name__)
    return _CACHE.get(key)


def _write_cached(key: str, payload: dict[str, Any]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_file(key)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)
    stat = path.stat()
    _CACHE[key] = payload
    _CACHE_SIGNATURES[key] = (int(stat.st_mtime_ns), int(stat.st_size))


def _first_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        value = value[0] if value else {}
    return value if isinstance(value, dict) else {}


def _safe_display(value: Any, depth: int = 0) -> str:
    """Return a compact primitive string for any source-provided display value."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (str, int, float)):
        return re.sub(r"\s+", " ", str(value)).strip()[:2000]
    if depth >= 4:
        return re.sub(r"\s+", " ", str(value)).strip()[:500]
    if isinstance(value, dict):
        parts = []
        for key, child in list(value.items())[:30]:
            rendered = _safe_display(child, depth + 1)
            if rendered:
                label = re.sub(r"[_\s]+", " ", str(key)).strip().title() or "Value"
                parts.append(f"{label}: {rendered}")
        return " · ".join(parts)[:2000]
    if isinstance(value, (list, tuple, set)):
        return ", ".join(part for part in (_safe_display(item, depth + 1) for item in list(value)[:100]) if part)[:2000]
    return re.sub(r"\s+", " ", str(value)).strip()[:500]


def _safe_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _chart_series(chart: dict[str, Any]) -> dict[str, Any] | None:
    model = chart.get("model") if isinstance(chart.get("model"), dict) else None
    if not model:
        return None
    # Libraries commonly wrap the actual option object one level down.
    for key in ("option", "options", "config"):
        nested = model.get(key)
        if isinstance(nested, dict) and (nested.get("series") or nested.get("data")):
            model = nested
            break

    options = _first_dict(model.get("options"))
    data_obj = _first_dict(model.get("data"))
    xaxis = model.get("xAxis") or model.get("xaxis") or options.get("xaxis") or options.get("xAxis")
    xaxis = _first_dict(xaxis)
    labels = (
        model.get("labels") or data_obj.get("labels") or xaxis.get("categories")
        or xaxis.get("data") or options.get("labels") or []
    )
    if not isinstance(labels, list):
        labels = []
    raw_series = model.get("series")
    if not isinstance(raw_series, list):
        raw_series = data_obj.get("datasets") if isinstance(data_obj.get("datasets"), list) else []
    if not isinstance(raw_series, list) or not raw_series:
        return None
    # ApexCharts pie/donut reports commonly expose series as a flat numeric
    # array and keep category names separately in options.labels.
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in raw_series):
        raw_series = [{"name": "Series 1", "data": raw_series}]

    output_series: list[dict[str, Any]] = []
    max_points = 2500
    for series_index, item in enumerate(raw_series[:20]):
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("label") or f"Series {series_index + 1}")
            values = item.get("data")
            if values is None:
                values = item.get("values")
        elif isinstance(item, list):
            name, values = f"Series {series_index + 1}", item
        else:
            continue
        if not isinstance(values, list):
            continue
        points = []
        for point_index, point in enumerate(values[:max_points]):
            if isinstance(point, dict):
                label = point.get("name") or point.get("label") or point.get("x")
                value = point.get("y") if point.get("y") is not None else point.get("value")
            elif isinstance(point, (int, float)):
                label = None
                value = point
            else:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if not (numeric == numeric and abs(numeric) != float("inf")):
                continue
            if label is None:
                label = labels[point_index] if point_index < len(labels) else str(point_index + 1)
            points.append({"label": str(label), "value": numeric})
        if points:
            output_series.append({"name": name, "data": points})
    if not output_series:
        return None
    first_series = raw_series[0] if raw_series and isinstance(raw_series[0], dict) else {}
    chart_meta = _first_dict(model.get("chart"))
    chart_type = str(model.get("type") or chart_meta.get("type") or first_series.get("type") or chart.get("type") or "").lower()
    if not chart_type:
        chart_type = "line" if "line" in str(chart.get("library") or "").lower() else "bar"
    if chart_type not in {"pie", "doughnut", "donut", "bar", "column", "line", "area"}:
        chart_type = "bar"
    if chart_type in {"column"}:
        chart_type = "bar"
    if chart_type in {"doughnut", "donut"}:
        chart_type = "pie"
    return {"kind": chart_type, "series": output_series}


def _compact_capture(raw: dict[str, Any]) -> dict[str, Any]:
    """Project source output into a strict JSON/React-safe dashboard contract."""
    kpis: list[dict[str, Any]] = []
    seen_kpis: set[tuple[str, str]] = set()
    for item in (raw.get("kpi_cards") or [])[:120]:
        if not isinstance(item, dict):
            continue
        label = _safe_display(item.get("label") or item.get("key"))
        display = _safe_display(item.get("display_value") or item.get("text") or item.get("value"))
        state = _safe_display(item.get("state") or "Overview")
        if not label or not display or any(token in (label + display + state).casefold() for token in ("chatbot", "ai engine", "auto-tab")):
            continue
        identity = (label.casefold(), display.casefold())
        if identity in seen_kpis:
            continue
        seen_kpis.add(identity)
        kpis.append({
            "key": _safe_display(item.get("key") or f"kpi-{len(kpis)+1}"),
            "label": label,
            "display_value": display,
            "value": _safe_number(item.get("value")),
            "change": _safe_display(item.get("change")) or None,
            "unit": _safe_display(item.get("unit")) or None,
            "period_basis": _safe_display(item.get("period_basis")) or None,
            "state": state,
            "section": _safe_display(item.get("section")) or None,
        })

    charts: list[dict[str, Any]] = []
    seen_charts: set[str] = set()
    for item in (raw.get("charts") or [])[:160]:
        if not isinstance(item, dict) or not isinstance(item.get("series"), list):
            continue
        series = []
        for series_item in item.get("series")[:16]:
            if not isinstance(series_item, dict):
                continue
            points = []
            for point in (series_item.get("data") or [])[:2500]:
                if not isinstance(point, dict):
                    continue
                value = _safe_number(point.get("value"))
                if value is None:
                    continue
                label = _safe_display(point.get("label"))
                if label:
                    point_out = {"label": label[:160], "value": value}
                    if point.get("financial_year"):
                        point_out["financial_year"] = _safe_display(point.get("financial_year"))[:40]
                    if point.get("agency_id"):
                        point_out["agency_id"] = _safe_display(point.get("agency_id"))[:100]
                    if point.get("source_rank") is not None:
                        point_out["source_rank"] = _safe_display(point.get("source_rank"))[:40]
                    if isinstance(point.get("by_year"), dict):
                        point_out["by_year"] = {
                            str(year): _safe_number(year_value)
                            for year, year_value in point["by_year"].items()
                            if _safe_number(year_value) is not None and re.fullmatch(r"20\d{2}", str(year))
                        }
                    points.append(point_out)
            if points:
                series.append({"name": _safe_display(series_item.get("name") or "Series")[:100], "data": points})
        if not series:
            continue
        kind = _safe_display(item.get("kind") or "bar").lower()
        if kind not in {"bar", "line", "area", "pie", "scatter"}:
            kind = "bar"
        chart = {
            "key": _safe_display(item.get("key") or f"chart-{len(charts)+1}"),
            "title": _safe_display(item.get("title") or "Chart")[:180],
            "kind": kind,
            "state": _safe_display(item.get("state") or "Overview"),
            "unit": _safe_display(item.get("unit")) or None,
            "section": _safe_display(item.get("section")) or None,
            "source_url": _safe_display(item.get("source_url")) or None,
            "series": series,
        }
        signature = json.dumps(chart, ensure_ascii=False, sort_keys=True, default=str)
        if signature not in seen_charts:
            seen_charts.add(signature)
            charts.append(chart)

    tables: list[dict[str, Any]] = []
    seen_tables: set[str] = set()
    for item in (raw.get("tables") or [])[:120]:
        if not isinstance(item, dict):
            continue
        raw_columns = item.get("columns") if isinstance(item.get("columns"), list) else []
        columns = [_safe_display(column)[:160] for column in raw_columns[:100]]
        rows = []
        raw_rows = item.get("rows") if isinstance(item.get("rows"), list) else []
        for row in raw_rows[:10000]:
            if isinstance(row, dict):
                rows.append({_safe_display(key)[:160] or f"Column {index+1}": _safe_display(value)
                             for index, (key, value) in enumerate(list(row.items())[:100])})
            elif isinstance(row, (list, tuple)):
                rows.append([_safe_display(value) for value in list(row)[:100]])
        # NISE may return a plain list of manufacturer names with one visible
        # column. Preserve that useful drilldown instead of silently dropping
        # it under the generic two-column table rule.
        table_context = " ".join((
            _safe_display(item.get("title")), _safe_display(item.get("state")),
            " ".join(columns),
        )).casefold()
        one_column_directory = (
            bool(columns) and len(columns) == 1
            and any(token in table_context for token in ("manufacturer", "maker", "company", "producer"))
        )
        if (len(columns) < 2 and not one_column_directory) or not rows:
            continue
        try:
            row_count = int(item.get("row_count") or len(rows))
        except (TypeError, ValueError):
            row_count = len(rows)
        table = {
            "key": _safe_display(item.get("key") or f"table-{len(tables)+1}"),
            "title": _safe_display(item.get("title") or "Data table")[:180],
            "state": _safe_display(item.get("state") or "Overview"),
            "section": _safe_display(item.get("section")) or None,
            "source_endpoint": _safe_display(item.get("source_endpoint")) or None,
            "source_request_method": _safe_display(item.get("source_request_method")) or None,
            "json_path": _safe_display(item.get("json_path")) or None,
            "columns": columns,
            "display_columns": [_safe_display(column)[:160] for column in (item.get("display_columns") or columns)[:100]],
            "rows": rows,
            "row_count": max(row_count, len(rows)),
            "captured_row_count": int(item.get("captured_row_count") or len(rows)),
            "source_total_count": _safe_number(item.get("source_total_count")),
            "expected_count": _safe_number(item.get("expected_count")),
            "is_partial": bool(item.get("is_partial")),
            "coverage_label": _safe_display(item.get("coverage_label")) or None,
            "capture_method": _safe_display(item.get("capture_method")) or None,
            "capture_methods": [_safe_display(value) for value in (item.get("capture_methods") or [])[:10]],
            "page_index": _safe_number(item.get("page_index")),
            "page_size": _safe_number(item.get("page_size")),
            "full_page_load_attempted": bool(item.get("full_page_load_attempted")),
            "full_page_load_error": _safe_display(item.get("full_page_load_error")) or None,
            "dom_fallback_used": bool(item.get("dom_fallback_used")),
            "truncated": bool(item.get("truncated")),
        }
        signature = json.dumps([table["title"], table["state"], columns, rows[:5]], ensure_ascii=False, sort_keys=True, default=str)
        if signature not in seen_tables:
            seen_tables.add(signature)
            tables.append(table)

    relevance = {
        "commodities": ("commodity", "iron ore", "steel", "crude", "oil", "gas", "copper", "aluminium", "gold", "silver", "wheat", "rice", "price", "spot", "futures"),
        "gpu": ("gpu", "compute", "silicon index", "rental", "cloud", "h100", "h200", "b200", "a100", "token", "benchmark", "price per hour"),
        "lpg": ("lpg", "cng", "png", "natural gas", "gas", "cylinder", "consumption", "production", "import", "export", "sales", "connection", "crude", "petroleum", "refinery", "capacity", "lng", "products"),
        "battery": ("battery", "bess", "storage", "energy storage", "installation", "capacity", "mwh", "gwh", "tender", "project"),
        "automotive": ("vehicle", "registration", "vahan", "maker", "manufacturer", "brand", "fuel", "state", "ev", "electric", "tractor", "bus", "two wheeler", "two-wheeler", "four wheeler", "four-wheeler", "2w", "4w", "3w", "car", "commercial vehicle", "leader", "sales"),
    }
    blocked = ("organization", "website", "imageobject", "webpage", "article", "post", "breadcrumb", "navigation", "social", "facebook", "linkedin", "twitter", "newsletter", "author")
    sector_key = str(raw.get("sector_key") or "")
    terms = relevance.get(sector_key, ())
    if sector_key == "automotive" and (isinstance(raw.get("automotive_dashboard"), dict) or str(raw.get("source_name") or "").upper() == "FLASH_FINANCE_VAHAN"):
        terms = ()
    if terms:
        kpis = [x for x in kpis if not any(b in (x["label"] + " " + x["display_value"]).casefold() for b in blocked)
                and any(t in x["label"].casefold() for t in terms)]
        charts = [x for x in charts if (sector_key == "lpg" and x.get("section") in {"overview", "production", "consumption"})
                  or any(t in (x["title"] + " " + x["state"]).casefold() for t in terms)]
        tables = [x for x in tables if not any(b in (x["title"] + " " + " ".join(x["columns"])).casefold() for b in blocked)
                  and ((sector_key == "lpg" and x.get("section") in {"overview", "production", "consumption"})
                       or any(t in (x["title"] + " " + " ".join(x["columns"])).casefold() for t in terms))]

    observations = []
    for item in (raw.get("manufacturing_observations") or [])[:20_000]:
        if not isinstance(item, dict):
            continue
        row = {
            "product": _safe_display(item.get("product")) or None,
            "observation_type": _safe_display(item.get("observation_type")) or None,
            "manufacturer": _safe_display(item.get("manufacturer")) or None,
            "manufacturer_id": _safe_display(item.get("manufacturer_id")) or None,
            "year": int(item["year"]) if str(item.get("year") or "").isdigit() else None,
            "month": int(item["month"]) if str(item.get("month") or "").isdigit() else None,
            "period": _safe_display(item.get("period")) or None,
            "manufactured_mw": _safe_number(item.get("manufactured_mw")),
            "source_endpoint": _safe_display(item.get("source_endpoint")) or None,
        }
        if row["manufactured_mw"] is not None:
            observations.append(row)

    return {
        "captured_at": raw.get("captured_at"),
        "fetch": raw.get("fetch") or {},
        "kpis": kpis,
        "charts": charts,
        "manufacturing_observations": observations,
        "manufacturers": [
            {
                "agency_id": _safe_display(item.get("agency_id")) or None,
                "name": _safe_display(item.get("name")) or None,
                "is_almm": item.get("is_almm"),
                "yearly_values": {
                    str(year): _safe_number(value)
                    for year, value in (item.get("yearly_values") or {}).items()
                    if re.fullmatch(r"20\d{2}", str(year)) and _safe_number(value) is not None
                },
                "latest_yearly_mw": _safe_number(item.get("latest_yearly_mw")),
            }
            for item in (raw.get("manufacturers") or [])[:1000]
            if isinstance(item, dict)
        ],
        "company_monthly_observations": [
            {
                "product": _safe_display(item.get("product")) or None,
                "manufacturer": _safe_display(item.get("manufacturer")) or None,
                "manufacturer_id": _safe_display(item.get("manufacturer_id")) or None,
                "year": int(item["year"]) if str(item.get("year") or "").isdigit() else None,
                "month": int(item["month"]) if str(item.get("month") or "").isdigit() else None,
                "period": _safe_display(item.get("period")) or None,
                "manufactured_mw": _safe_number(item.get("manufactured_mw")),
                "source_endpoint": _safe_display(item.get("source_endpoint")) or None,
            }
            for item in (raw.get("company_monthly_observations") or [])[:30000]
            if isinstance(item, dict) and _safe_number(item.get("manufactured_mw")) is not None
        ],
        "company_series_coverage": [
            dict(item) for item in (raw.get("company_series_coverage") or [])[:12000]
            if isinstance(item, dict)
        ],
        "company_data_updated_at": raw.get("company_data_updated_at"),
        "capture_audit": raw.get("capture_audit") if isinstance(raw.get("capture_audit"), dict) else {},
        "tables": tables,
        "sections": [_safe_display(x) for x in (raw.get("sections") or [])[:40]],
        "capture_completeness": raw.get("capture_completeness") or {},
        "source_diagnostics": raw.get("source_diagnostics") or [],
        "observation_count": int(raw.get("observation_count") or 0),
        "source_name": raw.get("source_name"),
        "source_url": raw.get("source_url"),
        "source_urls": raw.get("source_urls") or {},
        "source_labels": raw.get("source_labels") or {},
        "source_views": [
            {"label": _safe_display(item.get("label")), "url": _safe_display(item.get("url"))}
            for item in (raw.get("source_views") or [])[:40]
            if isinstance(item, dict) and _safe_display(item.get("label")) and _safe_display(item.get("url"))
        ],
        "page_title": raw.get("page_title"),
        "data_quality_notes": [_safe_display(x) for x in (raw.get("data_quality_notes") or [])[:60]],
        "filters": raw.get("filters") or {},
        "available_years": raw.get("available_years") or raw.get("selected_years") or (raw.get("filters") or {}).get("years") or [],
        "selected_years": raw.get("selected_years") or [],
        "selected_product": raw.get("selected_product"),
        "selected_period": raw.get("selected_period"),
        "source_snapshot_version": raw.get("source_snapshot_version") or "nise-solar-source-mirror-v1",
        "source_filters": raw.get("source_filters") or [],
        "selected_source_filters": raw.get("selected_source_filters") or {},
        "automotive_dashboard": raw.get("automotive_dashboard") if isinstance(raw.get("automotive_dashboard"), dict) else None,
        "sector_payload": raw.get("sector_payload") if isinstance(raw.get("sector_payload"), dict) else {},
    }



async def _run_refresh(
    sector_key: str,
    report_key: str,
    filters: dict[str, Any] | None = None,
    lock_token: str | None = None,
) -> None:
    key = _key(sector_key, report_key, filters)
    semaphore = _TRADESTAT_SEMAPHORE if sector_key == "tradestat" else _SEMAPHORE
    logger.info("TRADESTAT_PIPELINE_REFRESH_ENTER diag=%s pid=%s sector=%s report=%s key=%s filters=%s", TRADESTAT_DIAG_VERSION, os.getpid(), sector_key, report_key, key, filters if sector_key == "tradestat" else None)
    async with semaphore:
        started = time.monotonic()
        logger.info("TRADESTAT_PIPELINE_REFRESH_SEMAPHORE_ACQUIRED diag=%s pid=%s key=%s", TRADESTAT_DIAG_VERSION, os.getpid(), key)
        try:
            # A new attempt supersedes the previous diagnostic failure. Keep
            # the last-good report cache, but don't show stale source errors
            # while this attempt is actively collecting fresh data.
            _ERRORS.pop(key, None)
            await asyncio.to_thread(_clear_attempt, key)
            logger.info("SECTOR_SCREENER_CAPTURE_START sector=%s report=%s key=%s", sector_key, report_key, key)
            if sector_key == "tradestat":
                logger.info("TRADESTAT_PIPELINE_COLLECTOR_CALL pid=%s key=%s filters=%s timeout_seconds=%s", os.getpid(), key, filters, TRADESTAT_SCRAPE_TIMEOUT_SECONDS)
            supports_filters = sector_key in {"automotive", "solar", "tradestat"}
            raw = await asyncio.wait_for(
                scrape(sector_key, report_key, filters=filters) if supports_filters else scrape(sector_key, report_key),
                timeout=(180 if sector_key in {"automotive", "lpg"} else (TRADESTAT_SCRAPE_TIMEOUT_SECONDS if sector_key == "tradestat" else (SOLAR_SCRAPE_TIMEOUT_SECONDS if sector_key == "solar" else SCRAPE_TIMEOUT_SECONDS))),
            )
            if sector_key == "tradestat":
                payload = raw.get("sector_payload") if isinstance(raw, dict) else None
                logger.info("TRADESTAT_PIPELINE_COLLECTOR_RETURN pid=%s key=%s raw_type=%s raw_keys=%s payload_keys=%s duration_seconds=%.2f", os.getpid(), key, type(raw).__name__, sorted(list(raw.keys())) if isinstance(raw, dict) else None, sorted(list(payload.keys())) if isinstance(payload, dict) else None, time.monotonic()-started)
            raw["sector_key"] = sector_key
            report = _compact_capture(raw)
            # For TradeStat, KPI cards can exist even when both source queries
            # failed (for example, "0 observations" plus a placeholder HS row).
            # Only genuine numeric trade observations count as usable content.
            # LPG similarly requires a numeric source KPI, chart point or table
            # cell; an empty/header-only page is not a publishable snapshot.
            sector_payload = report.get("sector_payload") if isinstance(report.get("sector_payload"), dict) else {}
            if sector_key == "tradestat":
                trade_rows = (sector_payload.get("imports") or []) + (sector_payload.get("exports") or [])
                has_content = any(isinstance(item, dict) and _safe_number(item.get("trade_value")) is not None for item in trade_rows)
            elif sector_key == "lpg":
                has_numeric_kpi = any(isinstance(item, dict) and _safe_number(item.get("value")) is not None for item in report["kpis"])
                has_numeric_chart = any(
                    _safe_number(point.get("value")) is not None
                    for chart in report["charts"] if isinstance(chart, dict)
                    for series in chart.get("series") or [] if isinstance(series, dict)
                    for point in series.get("data") or [] if isinstance(point, dict)
                )
                has_numeric_table = any(
                    _safe_number(value) is not None
                    for table in report["tables"] if isinstance(table, dict)
                    for row in table.get("rows") or [] if isinstance(row, dict)
                    for value in row.values()
                )
                has_content = has_numeric_kpi or has_numeric_chart or has_numeric_table
            else:
                has_content = bool(report["kpis"] or report["charts"] or report["tables"] or sector_payload.get("imports") or sector_payload.get("exports") or sector_payload.get("tables"))
            solar_complete = True
            if sector_key == "solar":
                # Only source-backed KPI values and successful endpoint audits
                # count toward a READY Solar snapshot.
                supplied_filters = filters or {}
                product = "module" if str(supplied_filters.get("product") or "Cell").casefold() in {"module", "solar module", "panel"} else "cell"
                period = "yearly"  # Canonical source mirror always contains every captured year.
                kpi_by_key = {str(item.get("key") or ""): item for item in report["kpis"] if isinstance(item, dict)}
                has_manufacturer_count = (kpi_by_key.get(f"solar-{product}-manufacturers") or {}).get("value") is not None
                has_production_total = (kpi_by_key.get(f"solar-{product}-manufactured-mw") or {}).get("value") is not None
                chart_keys = {
                    str(item.get("key") or "")
                    for item in report["charts"]
                    if isinstance(item, dict) and any((series.get("data") or []) for series in (item.get("series") or []) if isinstance(series, dict))
                }
                has_time_chart = f"nise-{product}-monthwise-manufactured" in chart_keys or f"nise-{product}-yearwise-manufactured" in chart_keys
                has_company_chart = f"nise-{product}-manufacturer-manufactured" in chart_keys
                manufacturers = report.get("manufacturers") if isinstance(report.get("manufacturers"), list) else []
                has_stable_manufacturer_ids = any(
                    isinstance(item, dict) and item.get("agency_id")
                    and not str(item.get("agency_id")).startswith("name:")
                    for item in manufacturers
                )
                has_sold_kpi = (kpi_by_key.get(f"solar-{product}-sold-mw") or {}).get("value") is not None
                capture_audit = report.get("capture_audit") if isinstance(report.get("capture_audit"), dict) else {}
                direct_capture_complete = bool(capture_audit.get("complete"))
                required_time_chart = (
                    f"nise-{product}-monthwise-manufactured" in chart_keys
                    or f"nise-{product}-yearwise-manufactured" in chart_keys
                )
                # A complete snapshot requires real source-backed KPIs,
                # aggregate time series, manufacturer observations and a
                # successful direct-HTTP endpoint audit.
                solar_complete = (
                    has_manufacturer_count and has_production_total
                    and required_time_chart and has_company_chart and has_stable_manufacturer_ids
                    and direct_capture_complete
                )
                has_product_chart = any(key.startswith(f"nise-{product}-") for key in chart_keys)
                has_content = has_manufacturer_count or has_production_total or has_sold_kpi or has_product_chart
                if not solar_complete:
                    missing_parts = []
                    if not has_manufacturer_count:
                        missing_parts.append("manufacturer-count KPI")
                    if not has_production_total:
                        missing_parts.append("manufactured-total KPI")
                    if not required_time_chart:
                        missing_parts.append("monthly/yearly manufacturing chart")
                    if not has_company_chart:
                        missing_parts.append("manufacturer-wise manufacturing chart")
                    if not has_stable_manufacturer_ids:
                        missing_parts.append("stable NISE manufacturer IDs for company comparisons")
                    if not direct_capture_complete:
                        missing_parts.append("complete direct-HTTP endpoint capture")
                    report["solar_missing_components"] = missing_parts
                    report["data_quality_notes"] = list(report.get("data_quality_notes") or []) + [
                        "Solar source capture is partial: " + ", ".join(missing_parts) + ". Available source-backed components are published only when no last-good READY snapshot exists."
                    ]
            if sector_key == "solar":
                # The scheduled base refresh must not erase monthly company
                # series previously fetched on demand by the analyst UI.
                existing_snapshot = await asyncio.to_thread(_read_cached, key)
                if isinstance(existing_snapshot, dict):
                    report["company_monthly_observations"] = existing_snapshot.get("company_monthly_observations") or []
                    report["company_series_coverage"] = existing_snapshot.get("company_series_coverage") or []
                    report["company_data_updated_at"] = existing_snapshot.get("company_data_updated_at")
            report["sector_key"] = sector_key
            report["report_key"] = report_key
            report["refreshed_at"] = datetime.now(timezone.utc).isoformat()
            report["scrape_duration_seconds"] = round(time.monotonic() - started, 2)
            if sector_key == "solar" and has_content and not solar_complete:
                report["status"] = "partial"
                report["warning"] = (
                    "NISE returned some usable Solar data, but one or more KPI/chart/AJAX components are missing. "
                    "Available source-backed components are shown; a last-good READY snapshot is never overwritten by a partial capture."
                )
                report["attempted_at"] = datetime.now(timezone.utc).isoformat()
                _ERRORS[key] = report["warning"]
                _RETRY_AFTER[key] = time.monotonic() + RETRY_COOLDOWN_SECONDS
                await asyncio.to_thread(_write_attempt, key, report)
                existing = await asyncio.to_thread(_read_cached, key)
                # Show genuine partial source output on first collection. Keep
                # an existing READY report immutable until a complete refresh.
                if not existing or existing.get("status") != "ready":
                    def quality(payload: dict[str, Any] | None) -> tuple[int, int]:
                        if not isinstance(payload, dict):
                            return (0, 0)
                        valid_kpis = sum(1 for item in payload.get("kpis") or [] if isinstance(item, dict) and item.get("value") is not None)
                        chart_points = sum(
                            len(series.get("data") or [])
                            for chart in payload.get("charts") or [] if isinstance(chart, dict)
                            for series in chart.get("series") or [] if isinstance(series, dict)
                        )
                        return (valid_kpis, chart_points)
                    if quality(report) >= quality(existing):
                        await asyncio.to_thread(_write_cached, key, report)
                        logger.warning(
                            "SOLAR_PARTIAL_SNAPSHOT_PUBLISHED product=%s kpis=%d charts=%d missing=%s",
                            product, sum(1 for item in report["kpis"] if item.get("value") is not None),
                            len(report["charts"]), report.get("solar_missing_components", []),
                        )
                logger.warning(
                    "SECTOR_SCREENER_CAPTURE_PARTIAL sector=%s report=%s duration=%.2fs kpis=%d charts=%d http_expected=%s http_captured=%s http_missing_years=%s http_complete=%s; partial cache published only when no last-good READY cache exists",
                    sector_key, report_key, report["scrape_duration_seconds"],
                    len(report["kpis"]), len(report["charts"]),
                    capture_audit.get("expected_responses"), capture_audit.get("captured_responses"),
                    capture_audit.get("missing_years", []), capture_audit.get("complete", False),
                )
            elif sector_key in {"lpg", "tradestat"} and has_content and not bool((report.get("capture_completeness") or {}).get("complete")):
                report["status"] = "partial"
                report["warning"] = "The source returned some usable information, but the expected LPG/CNG or TradeStat dataset is incomplete. A previous READY snapshot is retained."
                report["attempted_at"] = datetime.now(timezone.utc).isoformat()
                _ERRORS[key] = report["warning"]
                _RETRY_AFTER[key] = time.monotonic() + RETRY_COOLDOWN_SECONDS
                await asyncio.to_thread(_write_attempt, key, report)
                existing = await asyncio.to_thread(_read_cached, key)
                if has_content and (not existing or existing.get("status") != "ready"):
                    await asyncio.to_thread(_write_cached, key, report)
                logger.warning("SECTOR_SCREENER_CAPTURE_PARTIAL sector=%s report=%s kpis=%d charts=%d tables=%d completeness=%s usable_observations=%s", sector_key, report_key, len(report["kpis"]), len(report["charts"]), len(report["tables"]), report.get("capture_completeness"), has_content)
            elif not has_content:
                report["status"] = "partial"
                report["warning"] = "The source responded, but no usable KPI, numeric chart dataset or table was extracted. The previous successful cache, if any, is retained."
                report["attempted_at"] = datetime.now(timezone.utc).isoformat()
                _ERRORS[key] = report["warning"]
                _RETRY_AFTER[key] = time.monotonic() + RETRY_COOLDOWN_SECONDS
                await asyncio.to_thread(_write_attempt, key, report)
                logger.warning(
                    "SECTOR_SCREENER_CAPTURE_EMPTY sector=%s report=%s duration=%.2fs kpis=%d charts=%d tables=%d diagnostics_saved=true; previous cache retained",
                    sector_key, report_key, report["scrape_duration_seconds"],
                    len(report["kpis"]), len(report["charts"]), len(report["tables"]),
                )
            else:
                report["status"] = "ready"
                report["attempted_at"] = datetime.now(timezone.utc).isoformat()
                await asyncio.to_thread(_write_cached, key, report)
                await asyncio.to_thread(_clear_attempt, key)
                _ERRORS.pop(key, None)
                _RETRY_AFTER.pop(key, None)
                logger.info("SECTOR_SCREENER_CAPTURE_COMPLETE sector=%s report=%s duration=%.2fs kpis=%d charts=%d tables=%d", sector_key, report_key, report["scrape_duration_seconds"], len(report["kpis"]), len(report["charts"]), len(report["tables"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:500]
            _ERRORS[key] = message
            _RETRY_AFTER[key] = time.monotonic() + RETRY_COOLDOWN_SECONDS
            attempt = {
                "status": "failed",
                "sector_key": sector_key,
                "report_key": report_key,
                "attempted_at": datetime.now(timezone.utc).isoformat(),
                "error": message,
                "warning": f"Sector capture failed: {message}",
                "fetch": {},
                "capture_completeness": {"status": "failed", "warnings": [message], "counts": {}},
                "kpis": [], "charts": [], "tables": [],
            }
            try:
                await asyncio.to_thread(_write_attempt, key, attempt)
            except Exception:
                logger.exception("SECTOR_SCREENER_ATTEMPT_SAVE_FAILED sector=%s report=%s", sector_key, report_key)
            logger.warning("SECTOR_SCREENER_CAPTURE_FAILED sector=%s report=%s error=%s diagnostics_saved=true", sector_key, report_key, message)
        finally:
            _TASKS.pop(key, None)
            _LOCK_TOKENS.pop(key, None)
            _release_running_lock(key, lock_token)


async def _refresh_tradestat_now(filters: dict[str, Any] | None = None) -> bool:
    """Run one TradeStat collection from the scheduled worker loop.

    User-facing API requests dispatch background tasks and return cache/status
    immediately. This helper remains for the durable hourly worker; the shared
    filesystem lease prevents duplicate worker/API scrapes across processes.
    """
    key = _key("tradestat", "overview", filters)
    logger.info("TRADESTAT_SYNC_REFRESH_REQUEST diag=%s pid=%s key=%s filters=%s", TRADESTAT_DIAG_VERSION, os.getpid(), key, filters)
    lock_token = _reserve_running_lock("tradestat", key)
    if not lock_token:
        logger.warning("TRADESTAT_SYNC_REFRESH_LOCK_REJECTED pid=%s key=%s lock=%s", os.getpid(), key, await asyncio.to_thread(_read_running_lock, key))
        return False
    started = time.monotonic()
    logger.warning("TRADESTAT_SYNC_REFRESH_START key=%s pid=%s", key, os.getpid())
    try:
        await _run_refresh("tradestat", "overview", filters, lock_token)
        logger.info("TRADESTAT_SYNC_REFRESH_DONE pid=%s key=%s duration_seconds=%.2f", os.getpid(), key, time.monotonic() - started)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("TRADESTAT_SYNC_REFRESH_FAILED key=%s", key)
        return False


async def refresh_tradestat_watchlists_once() -> dict[str, Any]:
    """Refresh stale Import and Export snapshots independently.

    A source failure for one trade direction must not block the other direction
    from being refreshed or published. The monitor API uses the same per-side
    cache identities, so scheduled and on-demand refreshes share snapshots.
    """
    watchlists = await asyncio.to_thread(get_all_watchlists)
    results: list[Any] = []
    refreshed = 0
    skipped = 0
    users = 0
    for user_id, lists in watchlists.items():
        import_items = [item for item in (lists.get("imports") or []) if isinstance(item, dict) and item.get("hs_code")]
        export_items = [item for item in (lists.get("exports") or []) if isinstance(item, dict) and item.get("hs_code")]
        import_codes = [str(item.get("hs_code")) for item in import_items]
        export_codes = [str(item.get("hs_code")) for item in export_items]
        if not import_codes and not export_codes:
            continue
        users += 1
        for trade, codes in (("import", import_codes[:30]), ("export", export_codes[:30])):
            if not codes:
                continue
            filters = {
                "import_hs_codes": codes if trade == "import" else [],
                "export_hs_codes": codes if trade == "export" else [],
                "trade_type": trade,
            }
            key = _key("tradestat", "overview", filters)
            cached = await asyncio.to_thread(_read_cached, key)
            fresh = False
            if isinstance(cached, dict) and cached.get("status") == "ready":
                try:
                    stamp = datetime.fromisoformat(str(cached.get("refreshed_at") or "").replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    fresh = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() < TRADESTAT_CACHE_TTL_SECONDS
                except Exception:
                    fresh = False
            if fresh:
                skipped += 1
                continue
            ok = await _refresh_tradestat_now(filters)
            refreshed += int(ok)
            results.append({"user_id": str(user_id), "trade_type": trade, "ok": ok, "watchlist_codes": len(codes)})
    return {"users": users, "refreshed": refreshed, "skipped_fresh": skipped, "results": results[-100:]}


async def run_tradestat_refresh_loop(stop_event: asyncio.Event, interval_seconds: int = TRADESTAT_CACHE_TTL_SECONDS) -> None:
    interval = max(300, int(interval_seconds))
    logger.info("TRADESTAT_SCHEDULER_STARTED interval_seconds=%s cache_ttl_seconds=%s", interval, TRADESTAT_CACHE_TTL_SECONDS)
    next_run = time.monotonic()
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            result = await refresh_tradestat_watchlists_once()
            logger.info("TRADESTAT_SCHEDULED_REFRESH_RESULT duration_seconds=%.2f %s", time.monotonic() - started, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("TRADESTAT_SCHEDULED_REFRESH_FAILED")
        next_run = max(next_run + interval, time.monotonic() + interval)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, next_run - time.monotonic()))
            return
        except asyncio.TimeoutError:
            continue


def _start_refresh(sector_key: str, report_key: str, filters: dict[str, Any] | None = None) -> bool:
    key = _key(sector_key, report_key, filters)
    logger.info("TRADESTAT_REFRESH_DISPATCH_CHECK pid=%s sector=%s report=%s key=%s existing_task=%s", os.getpid(), sector_key, report_key, key, bool(_TASKS.get(key) and not _TASKS[key].done()))
    existing = _TASKS.get(key)
    if existing and not existing.done():
        logger.info(
            "SECTOR_SCREENER_REFRESH_ALREADY_RUNNING sector=%s report=%s key=%s",
            sector_key, report_key, key,
        )
        return False
    lock_token = _reserve_running_lock(sector_key, key)
    if not lock_token:
        lock_state = _read_running_lock(key)
        logger.warning(
            "SECTOR_SCREENER_REFRESH_LOCKED sector=%s report=%s key=%s lock=%s",
            sector_key, report_key, key, lock_state or "unavailable",
        )
        return False
    _LOCK_TOKENS[key] = lock_token
    try:
        task = asyncio.create_task(
            _run_refresh(sector_key, report_key, filters, lock_token),
            name=f"sector-screener-{key}",
        )
        _TASKS[key] = task
        logger.info(
            "SECTOR_SCREENER_REFRESH_TASK_CREATED sector=%s report=%s key=%s task=%s",
            sector_key, report_key, key, task.get_name(),
        )
    except Exception:
        _LOCK_TOKENS.pop(key, None)
        _release_running_lock(key, lock_token)
        raise
    return True


async def get_report(
    sector_key: str, report_key: str = "overview", *, force_refresh: bool = False, filters: dict[str, Any] | None = None
) -> dict[str, Any]:
    key = _key(sector_key, report_key, filters)
    logger.info("TRADESTAT_GET_REPORT_ENTER pid=%s sector=%s report=%s key=%s force_refresh=%s filters=%s", os.getpid(), sector_key, report_key, key, force_refresh, filters if sector_key == "tradestat" else None)

    # An empty TradeStat watchlist is a normal UI state, not a collection error.
    # Avoid launching a source job (or leaving the UI in a queued state) until at
    # least one import/export HS code has been added.
    if sector_key == "tradestat":
        supplied = filters or {}
        import_codes = supplied.get("import_hs_codes")
        export_codes = supplied.get("export_hs_codes")
        if import_codes is None and export_codes is None:
            import_codes = export_codes = supplied.get("hs_codes") or []
        elif not import_codes and not export_codes and supplied.get("hs_codes"):
            import_codes = export_codes = supplied.get("hs_codes")
        import_codes = import_codes or []
        export_codes = export_codes or []
        focus_code = re.sub(r"\D", "", str(supplied.get("focus_code") or ""))
        if not import_codes and not export_codes and not focus_code:
            empty_payload = {
                "sector_key": "tradestat",
                "report_key": report_key,
                "status": "ready",
                "captured_at": None,
                "refreshed_at": None,
                "kpis": [],
                "charts": [],
                "tables": [],
                "sector_payload": {
                    "imports": [],
                    "exports": [],
                    "monitor_rows": [],
                    "monitor_rows_by_trade": {"import": [], "export": []},
                    "watchlist_empty": True,
                    "diagnostics": ["Add HS codes to the separate Import or Export watchlist to start monitoring."],
                },
                "capture_completeness": {
                    "status": "ready", "complete": True, "requested_queries": 0,
                    "successful_queries": 0, "failed_queries": 0,
                },
            }
            sector = SECTORS[sector_key]
            return {
                "sector": {"key": sector_key, "name": sector["name"], "source": sector["source"]},
                "report": {"key": report_key, **sector["reports"][report_key]},
                "selected_filters": filters or {},
                "status": "ready",
                "stale": False,
                "cache_ttl_seconds": cache_ttl_seconds(sector_key),
                "cache_ttl_minutes": cache_ttl_seconds(sector_key) // 60,
                "cache_source": "empty-watchlist",
                "age_seconds": None,
                "refresh_started": False,
                "refresh_in_progress": False,
                "error": None,
                "last_attempt": None,
                "data": empty_payload,
            }

    payload = await asyncio.to_thread(_read_cached, key)
    exact_payload_found = payload is not None
    cache_source = "current" if payload else None
    refreshed_at = None
    age_seconds = None
    if payload:
        refreshed_at = payload.get("refreshed_at")
        try:
            dt = datetime.fromisoformat(str(refreshed_at).replace("Z", "+00:00"))
            age_seconds = max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
        except Exception:
            age_seconds = None
    ttl_seconds = cache_ttl_seconds(sector_key)
    # Solar uses one direct-HTTP product snapshot per Cell/Module. No legacy XHR
    # cache is consulted: old snapshots must not be mistaken for data produced
    # by the replacement collector.
    stale = (not exact_payload_found) or age_seconds is None or age_seconds >= ttl_seconds
    started = False
    last_attempt = await asyncio.to_thread(_read_attempt, key)
    persisted_retry_block = False
    if last_attempt and last_attempt.get("attempted_at"):
        try:
            attempted_at = datetime.fromisoformat(str(last_attempt["attempted_at"]).replace("Z", "+00:00"))
            attempt_age = max(0.0, (datetime.now(timezone.utc) - attempted_at.astimezone(timezone.utc)).total_seconds())
            persisted_retry_block = attempt_age < RETRY_COOLDOWN_SECONDS
        except Exception:
            persisted_retry_block = False
    retry_allowed = time.monotonic() >= _RETRY_AFTER.get(key, 0.0) and not persisted_retry_block
    if sector_key == "tradestat":
        logger.info("TRADESTAT_GET_REPORT_CACHE pid=%s key=%s found=%s status=%s age_seconds=%s ttl_seconds=%s stale=%s last_attempt=%s persisted_retry_block=%s", os.getpid(), key, exact_payload_found, payload.get("status") if isinstance(payload, dict) else None, age_seconds, ttl_seconds, stale, bool(last_attempt), persisted_retry_block)

    # TradeStat refreshes run as background tasks, just like the other sector
    # collectors. The API always returns the last-good cache immediately and
    # exposes refresh_in_progress separately; a 12-month browser scrape must
    # never occupy a user-facing HTTP request.
    if sector_key == "tradestat":
        should_start_refresh = force_refresh or stale
        if should_start_refresh and (force_refresh or retry_allowed):
            try:
                started = _start_refresh(sector_key, report_key, filters)
            except Exception:
                logger.exception("TRADESTAT_ASYNC_REFRESH_START_FAILED key=%s", key)
                started = False
    elif sector_key != "tradestat":
        should_start_refresh = (
            force_refresh
            or (sector_key == "solar" and payload is None and retry_allowed)
            or (sector_key != "solar" and stale and retry_allowed)
        )
        if should_start_refresh:
            try:
                started = _start_refresh(sector_key, report_key, filters)
            except Exception:
                logger.exception(
                    "SECTOR_SCREENER_REFRESH_START_FAILED sector=%s report=%s key=%s",
                    sector_key, report_key, key,
                )
                started = False

            if sector_key == "lpg" and force_refresh and not started:
                existing_task = _TASKS.get(key)
                running_lock_now = await asyncio.to_thread(_read_running_lock, key)
                if (existing_task is None or existing_task.done()) and running_lock_now is None:
                    logger.warning(
                        "PPAC_API_REFRESH_FALLBACK sector=lpg report=%s key=%s reason=background_task_not_started",
                        report_key, key,
                    )
                    try:
                        started = _start_refresh(sector_key, report_key, filters)
                    except Exception:
                        logger.exception(
                            "PPAC_API_REFRESH_FALLBACK_FAILED sector=lpg report=%s key=%s",
                            report_key, key,
                        )

    local_task_running = bool(_TASKS.get(key) and not _TASKS[key].done())
    running_lock = await asyncio.to_thread(_read_running_lock, key)
    task_running = local_task_running or bool(running_lock)
    # Never keep a previous attempt's parser error visible while a new manual
    # refresh is actually running. The active attempt is authoritative.
    last_error = None if task_running else (_ERRORS.get(key) or (last_attempt or {}).get("error") or (last_attempt or {}).get("warning"))
    status = "scraping" if task_running and not payload else (payload.get("status", "ready") if payload else ("failed" if last_error else "queued"))
    if payload and stale and task_running:
        status = "stale"
    if sector_key == "tradestat":
        logger.info("TRADESTAT_GET_REPORT_EXIT pid=%s key=%s status=%s refresh_started=%s refresh_in_progress=%s stale=%s cache_source=%s age_seconds=%s error=%s", os.getpid(), key, status, started, task_running, stale, cache_source, age_seconds, last_error)
    sector = SECTORS[sector_key]
    report_meta = sector["reports"][report_key]
    return {
        "sector": {"key": sector_key, "name": sector["name"], "source": sector["source"]},
        "report": {"key": report_key, **report_meta},
        "selected_filters": filters or {},
        "status": status,
        "stale": bool(stale),
        "cache_ttl_seconds": ttl_seconds,
        "cache_ttl_minutes": ttl_seconds // 60,
        "cache_source": cache_source,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "refresh_started": started,
        "refresh_in_progress": task_running,
        "error": last_error,
        "last_attempt": last_attempt,
        "data": payload,
    }


async def get_tradestat_monitor_report(
    *,
    import_codes: list[str] | None = None,
    export_codes: list[str] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return a combined monitor response backed by independent side caches.

    Import and Export are intentionally separate collection jobs and cache
    snapshots. A partial Import response can therefore never cause an Export
    READY snapshot to be retained (or vice versa).
    """
    sides = {
        "import": list(dict.fromkeys(str(code) for code in (import_codes or []) if str(code).strip()))[:30],
        "export": list(dict.fromkeys(str(code) for code in (export_codes or []) if str(code).strip()))[:30],
    }

    # Migrate the last complete combined TradeStat snapshot written by the
    # previous version into two direction-specific cache files. This keeps
    # last-good Import and Export rows visible immediately after deployment,
    # while the new side-specific collectors run independently.
    legacy_filters = {
        "import_hs_codes": sides["import"],
        "export_hs_codes": sides["export"],
    }
    legacy_key = _key("tradestat", "overview", legacy_filters)
    legacy_snapshot = await asyncio.to_thread(_read_cached, legacy_key)
    legacy_attempt = await asyncio.to_thread(_read_attempt, legacy_key)
    if isinstance(legacy_snapshot, dict) and legacy_snapshot.get("status") == "ready":
        legacy_payload = legacy_snapshot.get("sector_payload")
        if isinstance(legacy_payload, dict):
            for trade in ("import", "export"):
                if not sides[trade]:
                    continue
                side_filters = {
                    "import_hs_codes": sides[trade] if trade == "import" else [],
                    "export_hs_codes": sides[trade] if trade == "export" else [],
                    "trade_type": trade,
                }
                side_key = _key("tradestat", "overview", side_filters)
                side_cached = await asyncio.to_thread(_read_cached, side_key)
                if side_cached is None:
                    side_snapshot = copy.deepcopy(legacy_snapshot)
                    side_payload = copy.deepcopy(legacy_payload)
                    own_rows = side_payload.get("imports" if trade == "import" else "exports") or []
                    old_monitor_map = side_payload.get("monitor_rows_by_trade")
                    old_monitor_map = old_monitor_map if isinstance(old_monitor_map, dict) else {}
                    own_monitor_rows = old_monitor_map.get(trade) or []
                    side_payload.update({
                        "imports": own_rows if trade == "import" else [],
                        "exports": own_rows if trade == "export" else [],
                        "monitor_rows": own_monitor_rows,
                        "monitor_rows_by_trade": {
                            "import": own_monitor_rows if trade == "import" else [],
                            "export": own_monitor_rows if trade == "export" else [],
                        },
                        "trade_type": trade,
                        "migration_source": "previous-combined-ready-snapshot",
                    })
                    side_snapshot["sector_payload"] = side_payload
                    side_snapshot["filters"] = side_filters
                    side_snapshot["migration_source"] = "previous-combined-ready-snapshot"
                    await asyncio.to_thread(_write_cached, side_key, side_snapshot)
                    logger.info(
                        "TRADESTAT_LEGACY_SNAPSHOT_MIGRATED pid=%s trade=%s key=%s observations=%d",
                        os.getpid(), trade, side_key, len(own_rows),
                    )

                # Carry forward a concise diagnostic from the old aggregate
                # attempt. Its aggregate counters are intentionally omitted:
                # they cannot be attributed accurately to just one direction.
                if legacy_attempt and await asyncio.to_thread(_read_attempt, side_key) is None:
                    old_attempt_payload = legacy_attempt.get("sector_payload")
                    old_attempt_payload = old_attempt_payload if isinstance(old_attempt_payload, dict) else {}
                    old_lines = old_attempt_payload.get("diagnostics") or []
                    matching_lines = [
                        str(line) for line in old_lines
                        if trade in str(line).casefold()
                    ]
                    if legacy_attempt.get("error"):
                        matching_lines.insert(0, str(legacy_attempt["error"]))
                    if legacy_attempt.get("warning"):
                        matching_lines.insert(0, str(legacy_attempt["warning"]))
                    if matching_lines:
                        side_attempt = {
                            "status": legacy_attempt.get("status") or "partial",
                            "attempted_at": legacy_attempt.get("attempted_at"),
                            "warning": "Diagnostics migrated from the previous combined Import/Export attempt; this attempt's counters were not direction-specific.",
                            "capture_completeness": {
                                "status": "partial",
                                "complete": False,
                                "legacy_aggregate": True,
                                "diagnostics": matching_lines[:20],
                            },
                            "sector_payload": {"diagnostics": matching_lines[:20]},
                        }
                        await asyncio.to_thread(_write_attempt, side_key, side_attempt)

    async def load_side(trade: str, codes: list[str]) -> dict[str, Any]:
        filters = {
            "import_hs_codes": codes if trade == "import" else [],
            "export_hs_codes": codes if trade == "export" else [],
            "trade_type": trade,
        }
        return await get_report(
            "tradestat", "overview", force_refresh=force_refresh, filters=filters
        )

    import_report, export_report = await asyncio.gather(
        load_side("import", sides["import"]),
        load_side("export", sides["export"]),
    )
    reports = {"import": import_report, "export": export_report}

    def data_for(trade: str) -> dict[str, Any]:
        data = reports[trade].get("data")
        return data if isinstance(data, dict) else {}

    def payload_for(trade: str) -> dict[str, Any]:
        payload = data_for(trade).get("sector_payload")
        return payload if isinstance(payload, dict) else {}

    def side_status(trade: str) -> dict[str, Any]:
        report = reports[trade]
        data = data_for(trade)
        payload = payload_for(trade)
        attempt = report.get("last_attempt") if isinstance(report.get("last_attempt"), dict) else None
        completeness = (data.get("capture_completeness") if isinstance(data.get("capture_completeness"), dict) else None)
        if not sides[trade]:
            status = "not_configured"
        elif report.get("refresh_in_progress"):
            status = "refreshing" if data else "queued"
        elif attempt and str(attempt.get("status") or "").casefold() in {"partial", "failed"}:
            status = str(attempt.get("status")).casefold()
        elif report.get("error"):
            status = "partial" if data else "failed"
        elif data.get("status") == "partial" or (completeness and not completeness.get("complete", False)):
            status = "partial"
        elif data:
            status = "ready"
        else:
            status = report.get("status") or "queued"
        return {
            "trade_type": trade,
            "status": status,
            "watchlist_count": len(sides[trade]),
            "observation_count": len(payload.get("imports" if trade == "import" else "exports") or []),
            "stale": bool(report.get("stale")),
            "cache_source": report.get("cache_source"),
            "age_seconds": report.get("age_seconds"),
            "refreshed_at": data.get("refreshed_at"),
            "refresh_started": bool(report.get("refresh_started")),
            "refresh_in_progress": bool(report.get("refresh_in_progress")),
            "error": report.get("error"),
            "last_attempt": attempt,
            # Prefer the latest attempt's completeness counters. The cached
            # last-good snapshot may be complete even when this attempt failed.
            "capture_completeness": (
                (attempt.get("capture_completeness") if attempt else None)
                or completeness
                or {}
            ),
        }

    direction_status = {trade: side_status(trade) for trade in ("import", "export")}
    import_data = data_for("import")
    export_data = data_for("export")
    import_payload = payload_for("import")
    export_payload = payload_for("export")

    # Use last-good observations for each direction, not partial attempt rows.
    imports = import_payload.get("imports") or []
    exports = export_payload.get("exports") or []
    import_monitor_map = import_payload.get("monitor_rows_by_trade")
    export_monitor_map = export_payload.get("monitor_rows_by_trade")
    import_monitor_map = import_monitor_map if isinstance(import_monitor_map, dict) else {}
    export_monitor_map = export_monitor_map if isinstance(export_monitor_map, dict) else {}
    monitor_by_trade = {
        "import": import_monitor_map.get("import") or [],
        "export": export_monitor_map.get("export") or [],
    }
    diagnostics: list[str] = []
    for trade in ("import", "export"):
        current = import_payload if trade == "import" else export_payload
        for item in current.get("diagnostics") or []:
            text_value = f"{trade.title()}: {item}"
            if text_value not in diagnostics:
                diagnostics.append(text_value)

    periods = [
        value for value in (
            import_payload.get("latest_period"), export_payload.get("latest_period")
        ) if value
    ]
    latest_period = max(periods) if periods else None
    latest_period_label = next((
        label for label in (
            import_payload.get("latest_period_label"),
            export_payload.get("latest_period_label"),
        ) if label
    ), "Not available")

    active_statuses = [
        direction_status[trade]["status"]
        for trade in ("import", "export") if sides[trade]
    ]
    refresh_in_progress = any(item["refresh_in_progress"] for item in direction_status.values())
    all_ready = bool(active_statuses) and all(value == "ready" for value in active_statuses)
    all_failed = bool(active_statuses) and all(value == "failed" for value in active_statuses)
    overall_status = (
        "ready" if not active_statuses or all_ready
        else "failed" if all_failed and not (imports or exports)
        else "partial"
    )
    if refresh_in_progress and not (imports or exports):
        overall_status = "scraping"

    data = {
        "sector_key": "tradestat",
        "report_key": "overview",
        "status": overall_status,
        "captured_at": max(
            [value for value in (import_data.get("captured_at"), export_data.get("captured_at")) if value]
            or [None],
            key=lambda value: value or "",
        ),
        "refreshed_at": max(
            [value for value in (import_data.get("refreshed_at"), export_data.get("refreshed_at")) if value]
            or [None],
            key=lambda value: value or "",
        ),
        "kpis": (import_data.get("kpis") or []) + (export_data.get("kpis") or []),
        "charts": (import_data.get("charts") or []) + (export_data.get("charts") or []),
        "tables": (import_data.get("tables") or []) + (export_data.get("tables") or []),
        "sector_payload": {
            "latest_period": latest_period,
            "latest_period_label": latest_period_label,
            "imports": imports,
            "exports": exports,
            "monitor_rows": monitor_by_trade["import"] + monitor_by_trade["export"],
            "monitor_rows_by_trade": monitor_by_trade,
            "watchlist_empty": not bool(sides["import"] or sides["export"]),
            "diagnostics": diagnostics[:300],
            "direction_status": direction_status,
        },
        "capture_completeness": {
            "status": overall_status,
            "complete": all_ready or not active_statuses,
            "directions": {
                trade: direction_status[trade]["capture_completeness"]
                for trade in ("import", "export")
            },
        },
    }

    age_values = [
        item["age_seconds"] for item in direction_status.values()
        if isinstance(item.get("age_seconds"), (int, float))
    ]
    errors = [
        f"{trade.title()}: {direction_status[trade]['error']}"
        for trade in ("import", "export") if direction_status[trade].get("error")
    ]
    side_attempts = {
        trade: direction_status[trade].get("last_attempt")
        for trade in ("import", "export")
    }
    return {
        "sector": import_report.get("sector") or export_report.get("sector"),
        "report": import_report.get("report") or export_report.get("report"),
        "selected_filters": {"import_hs_codes": sides["import"], "export_hs_codes": sides["export"]},
        "status": "stale" if refresh_in_progress and (imports or exports) else overall_status,
        "stale": any(item["stale"] for item in direction_status.values()),
        "cache_ttl_seconds": TRADESTAT_CACHE_TTL_SECONDS,
        "cache_ttl_minutes": TRADESTAT_CACHE_TTL_SECONDS // 60,
        "cache_source": "independent-direction-snapshots",
        "age_seconds": max(age_values) if age_values else None,
        "refresh_started": any(reports[trade].get("refresh_started") for trade in reports),
        "refresh_in_progress": refresh_in_progress,
        "error": " · ".join(errors) if errors else None,
        "last_attempt": side_attempts,
        "direction_status": direction_status,
        "data": data,
    }


async def _collect_missing_solar_company_series(
    *,
    product: str,
    key: str,
    agency_ids: list[str],
    years: list[int],
) -> None:
    """Populate only selected company/month observations in the product cache."""
    lock = _SOLAR_COMPANY_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        try:
            snapshot = await asyncio.to_thread(_read_cached, key)
            if not isinstance(snapshot, dict):
                return
            directory = {
                str(item.get("agency_id")): item
                for item in (snapshot.get("manufacturers") or [])
                if isinstance(item, dict) and item.get("agency_id")
            }
            valid_ids = [agency_id for agency_id in agency_ids if agency_id in directory]
            coverage = [item for item in (snapshot.get("company_series_coverage") or []) if isinstance(item, dict)]
            covered = {
                (str(item.get("agency_id")), int(item.get("year")))
                for item in coverage
                if item.get("status") == "captured" and str(item.get("year") or "").isdigit()
            }
            recent_failures: set[tuple[str, int]] = set()
            now = datetime.now(timezone.utc)
            for item in coverage:
                if item.get("status") != "failed" or not str(item.get("year") or "").isdigit():
                    continue
                try:
                    failed_at = datetime.fromisoformat(str(item.get("captured_at") or "").replace("Z", "+00:00"))
                    age = (now - failed_at.astimezone(timezone.utc)).total_seconds()
                    if 0 <= age < 300:
                        recent_failures.add((str(item.get("agency_id")), int(item.get("year"))))
                except (TypeError, ValueError):
                    continue
            missing_pairs = [
                (agency_id, year) for agency_id in valid_ids for year in years
                if (agency_id, year) not in covered and (agency_id, year) not in recent_failures
            ]
            if not missing_pairs:
                return
            missing_ids = list(dict.fromkeys(agency_id for agency_id, _ in missing_pairs))
            missing_years = sorted({year for _, year in missing_pairs})
            result = await scrape_company_monthly(
                product, missing_ids, missing_years, pairs=missing_pairs,
            )
            name_by_id = {agency_id: _safe_display(directory[agency_id].get("name")) for agency_id in missing_ids}
            existing_observations = [
                item for item in (snapshot.get("company_monthly_observations") or [])
                if isinstance(item, dict)
            ]
            merged: dict[tuple[str, int, int], dict[str, Any]] = {}
            for item in existing_observations:
                if not (item.get("manufacturer_id") and item.get("year") and item.get("month")):
                    continue
                merged[(str(item["manufacturer_id"]), int(item["year"]), int(item["month"]))] = item
            for item in result.get("observations") or []:
                agency_id = str(item.get("manufacturer_id") or "")
                if not agency_id or not item.get("year") or not item.get("month"):
                    continue
                item["manufacturer"] = name_by_id.get(agency_id) or item.get("manufacturer")
                merged[(agency_id, int(item["year"]), int(item["month"]))] = item
            coverage_by_pair = {
                (str(item.get("agency_id")), int(item.get("year"))): item
                for item in coverage
                if item.get("agency_id") and str(item.get("year") or "").isdigit()
            }
            for item in result.get("coverage") or []:
                if not isinstance(item, dict) or not item.get("agency_id") or not str(item.get("year") or "").isdigit():
                    continue
                pair = (str(item["agency_id"]), int(item["year"]))
                item["manufacturer"] = name_by_id.get(pair[0])
                coverage_by_pair[pair] = item
            snapshot["company_monthly_observations"] = list(merged.values())[-30000:]
            snapshot["company_series_coverage"] = list(coverage_by_pair.values())[-12000:]
            snapshot["company_data_updated_at"] = result.get("captured_at")
            snapshot["company_series_failures"] = result.get("failures") or []
            snapshot["observation_count"] = int(snapshot.get("observation_count") or 0)
            await asyncio.to_thread(_write_cached, key, snapshot)
            logger.info(
                "SOLAR_COMPANY_SERIES_CACHE_UPDATED product=%s requested_ids=%d observations=%d coverage=%d failures=%d",
                product, len(missing_ids), len(result.get("observations") or []),
                len(result.get("coverage") or []), len(result.get("failures") or []),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SOLAR_COMPANY_SERIES_REFRESH_FAILED product=%s ids=%s", product, agency_ids[:12])
        finally:
            _SOLAR_COMPANY_TASKS.pop(key, None)


_SOLAR_COMPANY_TASKS: dict[str, asyncio.Task] = {}


async def get_solar_company_series(
    *,
    product: str,
    agency_ids: list[str],
    period: str = "monthly",
) -> dict[str, Any]:
    """Return selected manufacturer series and warm missing monthly data async."""
    normalized_product = "Module" if str(product).casefold() in {"module", "solar module", "panel"} else "Cell"
    normalized_period = "yearly" if str(period).casefold() in {"year", "yearly", "annual"} else "monthly"
    requested_ids = list(dict.fromkeys(str(value).strip() for value in agency_ids if str(value).strip()))[:12]
    filters = {"product": normalized_product, "period": "yearly", "source_filters": {}}
    key = _key("solar", "overview", filters)
    snapshot = await asyncio.to_thread(_read_cached, key)
    if not isinstance(snapshot, dict):
        return {"product": normalized_product, "period": normalized_period, "series": [], "pending": True, "warning": "The Solar product snapshot is not ready yet."}

    directory = {
        str(item.get("agency_id")): item
        for item in (snapshot.get("manufacturers") or [])
        if isinstance(item, dict) and item.get("agency_id")
    }
    valid_ids = [agency_id for agency_id in requested_ids if agency_id in directory and not agency_id.startswith("name:")]
    years = sorted({int(value) for value in (snapshot.get("available_years") or []) if str(value).isdigit()})
    monthly_observations = [
        item for item in (snapshot.get("company_monthly_observations") or [])
        if isinstance(item, dict) and item.get("manufacturer_id") in valid_ids
    ]
    coverage = [item for item in (snapshot.get("company_series_coverage") or []) if isinstance(item, dict)]
    covered = {
        (str(item.get("agency_id")), int(item.get("year")))
        for item in coverage
        if item.get("status") == "captured" and str(item.get("year") or "").isdigit()
    }
    recent_failures: set[tuple[str, int]] = set()
    now = datetime.now(timezone.utc)
    for item in coverage:
        if item.get("status") != "failed" or not str(item.get("year") or "").isdigit():
            continue
        try:
            failed_at = datetime.fromisoformat(str(item.get("captured_at") or "").replace("Z", "+00:00"))
            age = (now - failed_at.astimezone(timezone.utc)).total_seconds()
            if 0 <= age < 300:
                recent_failures.add((str(item.get("agency_id")), int(item.get("year"))))
        except (TypeError, ValueError):
            continue
    missing_pairs = [
        (agency_id, year) for agency_id in valid_ids for year in years
        if (agency_id, year) not in covered and (agency_id, year) not in recent_failures
    ] if normalized_period == "monthly" else []
    task = _SOLAR_COMPANY_TASKS.get(key)
    pending = bool(missing_pairs or (task is not None and not task.done()))
    if missing_pairs and (task is None or task.done()):
        task = asyncio.create_task(_collect_missing_solar_company_series(
            product=normalized_product,
            key=key,
            agency_ids=valid_ids,
            years=years,
        ))
        _SOLAR_COMPANY_TASKS[key] = task
        pending = True

    series: list[dict[str, Any]] = []
    for agency_id in valid_ids:
        manufacturer = directory[agency_id]
        points: dict[str, float] = {}
        if normalized_period == "yearly":
            for year, value in (manufacturer.get("yearly_values") or {}).items():
                try:
                    points[str(int(year))] = float(value)
                except (TypeError, ValueError):
                    continue
        else:
            for item in monthly_observations:
                if str(item.get("manufacturer_id")) != agency_id or not item.get("period"):
                    continue
                try:
                    points[str(item["period"])] = float(item.get("manufactured_mw"))
                except (TypeError, ValueError):
                    continue
        series.append({
            "agency_id": agency_id,
            "name": _safe_display(manufacturer.get("name")) or agency_id,
            "data": [{"label": label, "value": value} for label, value in sorted(points.items())],
        })

    return {
        "product": normalized_product,
        "period": normalized_period,
        "unit": "MWp" if normalized_product == "Module" else "MW",
        "series": series,
        "pending": pending,
        "requested_ids": requested_ids,
        "unknown_ids": [agency_id for agency_id in requested_ids if agency_id not in directory],
        "missing_company_years": len(missing_pairs),
        "temporarily_unavailable_company_years": len(recent_failures),
        "company_data_updated_at": snapshot.get("company_data_updated_at"),
        "coverage": coverage,
    }


async def refresh_all_reports() -> dict[str, Any]:
    """Refresh registered reports on the existing cadence.

    Solar has its dedicated five-minute loop. TradeStat has its own user-watchlist
    hourly loop. PPAC LPG/CNG is refreshed only when its longer source freshness
    window has elapsed.
    """
    jobs: list[asyncio.Task] = []
    requested = 0
    for sector_key, sector in SECTORS.items():
        # Solar is warmed by its own five-minute scheduler. Keeping it out of
        # this hourly all-sector cycle prevents duplicate Cell scrapes and
        # avoids starving the other publishers during four Solar variants.
        if sector_key in {"solar", "tradestat"}:
            continue
        for report_key in sector["reports"]:
            requested += 1
            key = _key(sector_key, report_key)
            if sector_key == "lpg":
                cached_report = await asyncio.to_thread(_read_cached, key)
                if cached_report and cached_report.get("status") == "ready":
                    try:
                        stamp = datetime.fromisoformat(str(cached_report.get("refreshed_at") or "").replace("Z", "+00:00"))
                        if stamp.tzinfo is None:
                            stamp = stamp.replace(tzinfo=timezone.utc)
                        age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
                        if age < PPAC_CACHE_TTL_SECONDS:
                            continue
                    except Exception:
                        pass
            task = _TASKS.get(key)
            if task is None or task.done():
                _start_refresh(sector_key, report_key)
                task = _TASKS.get(key)
            if task is not None:
                jobs.append(task)

    results = await asyncio.gather(*jobs, return_exceptions=True) if jobs else []
    failures = sum(1 for result in results if isinstance(result, BaseException))
    ready = 0
    cached = 0
    for sector_key, sector in SECTORS.items():
        if sector_key in {"solar", "tradestat"}:
            continue
        for report_key in sector["reports"]:
            payload = await asyncio.to_thread(_read_cached, _key(sector_key, report_key))
            if payload:
                cached += 1
                if payload.get("status") == "ready":
                    ready += 1
    return {
        "requested_reports": requested,
        "completed_jobs": len(results),
        "task_failures": failures,
        "cached_reports": cached,
        "ready_reports": ready,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "cache_ttl_minutes": CACHE_TTL_SECONDS // 60,
    }


async def refresh_solar_reports(*, force: bool = False) -> dict[str, Any]:
    """Warm Cell and Module direct-HTTP snapshots concurrently.

    Product/year/period selection is a local dashboard projection. The worker
    owns exactly two independent source captures (Cell and Module) and starts
    both before awaiting either one. A complete product snapshot is retained
    for five minutes; incomplete attempts never replace a last-good snapshot.
    """
    variants = (
        {"product": "Cell", "period": "yearly", "source_filters": {}},
        {"product": "Module", "period": "yearly", "source_filters": {}},
    )

    async def prepare(filters: dict[str, Any]) -> dict[str, Any]:
        product = str(filters["product"])
        key = _key("solar", "overview", filters)
        cached = await asyncio.to_thread(_read_cached, key)
        fresh = False
        if cached and cached.get("status") == "ready" and not force:
            try:
                stamp = datetime.fromisoformat(str(cached.get("refreshed_at") or "").replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                fresh = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() < SOLAR_CACHE_TTL_SECONDS
            except Exception:
                fresh = False
        if fresh:
            logger.info("SOLAR_PARALLEL_PRODUCT_SKIP product=%s reason=fresh_cache", product)
            return {"product": product, "key": key, "started": False, "task": None}

        started = _start_refresh("solar", "overview", filters)
        task = _TASKS.get(key)
        logger.info(
            "SOLAR_PARALLEL_PRODUCT_%s product=%s key=%s",
            "STARTED" if started else "JOINED_OR_LOCKED", product, key,
        )
        return {"product": product, "key": key, "started": started, "task": task}

    # Both preparations complete and launch their independent asyncio tasks
    # before the gather below. The global semaphore is set to two.
    prepared = await asyncio.gather(*(prepare(filters) for filters in variants))
    tasks = [item["task"] for item in prepared if item.get("task") is not None]
    results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    failures = sum(1 for item in results if isinstance(item, BaseException))

    ready_products: list[str] = []
    partial_products: list[str] = []
    missing_products: list[str] = []
    for filters in variants:
        product = str(filters["product"])
        payload = await asyncio.to_thread(_read_cached, _key("solar", "overview", filters))
        if payload and payload.get("status") == "ready":
            ready_products.append(product)
        elif payload and payload.get("status") == "partial":
            partial_products.append(product)
        else:
            missing_products.append(product)

    started_count = sum(1 for item in prepared if item.get("started"))
    skipped_fresh = sum(1 for item in prepared if not item.get("started") and item.get("task") is None)
    logger.info(
        "SOLAR_SOURCE_MIRROR_WARM_COMPLETE started=%d skipped_fresh=%d completed=%d failures=%d ready_products=%s partial_products=%s missing_products=%s parallel=true max_concurrency=%d ttl_seconds=%d",
        started_count, skipped_fresh, len(results), failures, ready_products,
        partial_products, missing_products, MAX_CONCURRENT_REPORTS, SOLAR_CACHE_TTL_SECONDS,
    )
    return {
        "started": started_count,
        "skipped_fresh": skipped_fresh,
        "completed": len(results),
        "failures": failures,
        "ready_products": ready_products,
        "partial_products": partial_products,
        "missing_products": missing_products,
        "variants": [item["product"] for item in variants],
        "parallel": True,
        "max_concurrency": MAX_CONCURRENT_REPORTS,
        "cache_ttl_seconds": SOLAR_CACHE_TTL_SECONDS,
    }

async def run_solar_refresh_loop(stop_event: asyncio.Event, interval_seconds: int = SOLAR_CACHE_TTL_SECONDS) -> None:
    """Collect immediately, then target a five-minute start-to-start cadence."""
    interval = max(60, int(interval_seconds))
    loop = asyncio.get_running_loop()
    next_run = loop.time()
    logger.info("SOLAR_CACHE_SCHEDULER_STARTED interval_seconds=%d cadence=start_to_start", interval)
    while not stop_event.is_set():
        try:
            result = await refresh_solar_reports()
            logger.info("SOLAR_CACHE_SCHEDULED_REFRESH_RESULT %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SOLAR_CACHE_SCHEDULED_REFRESH_FAILED")
        next_run += interval
        # If a slow NISE cycle overruns its slot, schedule one fresh cycle
        # interval after completion instead of launching overlapping scrapes.
        delay = next_run - loop.time()
        if delay <= 0:
            next_run = loop.time() + interval
            delay = interval
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            continue


async def run_periodic_refresh_loop(stop_event: asyncio.Event, interval_seconds: int = CACHE_TTL_SECONDS) -> None:
    """Warm non-Solar reports immediately, then repeat on their hourly cadence.

    Solar is maintained by ``run_solar_refresh_loop``. This coroutine is
    intended to be started exactly once by the dedicated investment worker.
    """
    interval = max(60, int(interval_seconds))
    next_run = time.monotonic()
    logger.info("SECTOR_SCREENER_SCHEDULER_STARTED interval_seconds=%s reports=%s", interval, sum(len(item["reports"]) for item in SECTORS.values()))
    while not stop_event.is_set():
        cycle_started = time.monotonic()
        try:
            result = await refresh_all_reports()
            logger.info("SECTOR_SCREENER_SCHEDULED_REFRESH_RESULT %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SECTOR_SCREENER_SCHEDULED_REFRESH_FAILED")

        next_run += interval
        now = time.monotonic()
        while next_run <= now:
            next_run += interval
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, next_run - time.monotonic()))
            return
        except asyncio.TimeoutError:
            logger.debug("SECTOR_SCREENER_NEXT_REFRESH_IN interval_seconds=%s cycle_seconds=%.1f", interval, time.monotonic() - cycle_started)
        except asyncio.CancelledError:
            raise


async def shutdown() -> None:
    tasks = list(_TASKS.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _TASKS.clear()
