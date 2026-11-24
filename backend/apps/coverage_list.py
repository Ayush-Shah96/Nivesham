from __future__ import annotations

import io
import logging
import os
import re
import asyncio
import copy
import unicodedata
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from infrastructure.security.permissions import require_role
from infrastructure.security.auth import get_current_user
from ii_platform.database.connection import get_db, get_sessionmaker
from ii_platform.database.models import (
    ApprovalStatus,
    Company,
    TargetCompany,
    TrackMode,
    User,
    UserRole,
    NewsAlertItem,
)
from ii_platform.entity_matching.coverage_matching import TRACK_PRIORITY

router = APIRouter(prefix="/coverage-list", tags=["Coverage List"])
logger = logging.getLogger(__name__)

ALLOWED_BUCKETS = {"LARGE", "MID", "SMALL"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_ANALYST_USERNAMES = {"ahmed", "jainam", "raj", "romil", "aashil", "ashil"}


def _normalize_analyst_username(value: Any) -> str:
    """Normalize analyst spelling/case; Ashil is an accepted alias for Aashil."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return "aashil" if normalized == "ashil" else normalized
_WORKBOOK_SIGNATURE: tuple[int, int] | None = None
_WORKBOOK_FAILED_SIGNATURE: tuple[int, int] | None = None
_WORKBOOK_FAILED_ERROR: str | None = None
_WORKBOOK_SYNC_LOCK = asyncio.Lock()
_WORKBOOK_SYNC_TASK: asyncio.Task | None = None
_GRID_CACHE: dict[str, Any] | None = None
_GRID_CACHE_LOCK = asyncio.Lock()
_LAST_SYNC_WARNINGS: list[str] = []


def _backend_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def coverage_workbook_path() -> Path:
    configured = os.getenv("COVERAGE_LIST_WORKBOOK_PATH", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else (_backend_dir() / path).resolve()

    # Support both the deployed frontend/ backend/ layout and the uploaded
    # frontend-main/ backend-main/ archive layout. Windows is case-insensitive,
    # but Linux deployments may use the workbook's exact filename casing.
    candidates = [
        _backend_dir().parent / "frontend" / "public" / "Coverage_List.xlsx",
        _backend_dir().parent / "frontend" / "public" / "Coverage_list.xlsx",
        _backend_dir().parent / "frontend" / "public" / "coverage_list.xlsx",
        _backend_dir().parent / "frontend-main" / "public" / "Coverage_List.xlsx",
        _backend_dir().parent / "frontend-main" / "public" / "Coverage_list.xlsx",
        _backend_dir().parent / "frontend-main" / "public" / "coverage_list.xlsx",
        _backend_dir() / "data" / "Coverage_List.xlsx",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Return the canonical deployment path so existing health/error messages
    # still tell the operator where the workbook is expected.
    return candidates[0]


def _workbook_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _norm_header(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_NULL_IDENTIFIER_VALUES = {
    "",
    "0",
    "0.0",
    "nan",
    "none",
    "null",
    "nil",
    "na",
    "n/a",
    "-",
    "—",
    "–",
}


def _normalize_company_text(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    text = unicodedata.normalize("NFKC", text).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _normalize_identifier(value: Any) -> str | None:
    text = _normalize_company_text(value)
    if not text:
        return None
    if text.casefold() in _NULL_IDENTIFIER_VALUES:
        return None
    # Excel frequently turns numeric identifiers into floats such as 543318.0.
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    return text.upper()


def _looks_like_nse(value: Any) -> bool:
    text = _normalize_identifier(value)
    return bool(text and re.fullmatch(r"[A-Z][A-Z0-9._-]{1,19}", text))


def _looks_like_bse(value: Any) -> bool:
    text = _normalize_identifier(value)
    return bool(text and re.fullmatch(r"\d{5,7}", text))


def _normalize_workbook_identifiers(nse_value: Any, bse_value: Any) -> tuple[str | None, str | None]:
    """Normalize the workbook's NSE/BSE columns without using placeholder values as identifiers.

    Some rows in the supplied workbook have the NSE/BSE values under the opposite
    headings. Correct that only when the value shapes make the swap unambiguous.
    """
    nse = _normalize_identifier(nse_value)
    bse = _normalize_identifier(bse_value)

    # BSE code is normally numeric; NSE symbols normally contain a letter.
    # This also handles rows such as (526433, 0) and (0, SOLEX).
    if _looks_like_bse(nse) and (bse is None or _looks_like_nse(bse)):
        return bse, nse
    if bse is None and _looks_like_nse(nse):
        return nse, None
    if nse is None and _looks_like_bse(bse):
        return None, bse
    if _looks_like_nse(bse) and (nse is None or not _looks_like_nse(nse)):
        return bse, nse
    return nse, bse


def _parse_date(value: Any) -> datetime | None:
    """Parse optional coverage dates without making a missing/bad cell fatal.

    The workbook is the presentation/source-of-truth. A malformed non-empty date
    therefore stays visible in Excel/FE, while the runtime DB receives ``None``
    until the workbook value is corrected.
    """
    if value is None or _is_blank_cell(value) or str(value).strip() in {"-", "—", "–"}:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.debug("Coverage List: ignoring unparseable optional date %r", text)
    return None


def _parse_market_cap(value: Any) -> float | None:
    if value is None or _is_blank_cell(value) or str(value).strip() in {"-", "—", "–"}:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("₹", "")
    if not text:
        return None
    multiplier = 1.0
    suffix = text[-1:].lower()
    if suffix == "t":
        multiplier, text = 1e12, text[:-1]
    elif suffix == "b":
        multiplier, text = 1e9, text[:-1]
    elif suffix == "m":
        multiplier, text = 1e6, text[:-1]
    elif text.lower().endswith("cr"):
        multiplier, text = 1e7, text[:-2]
    elif text.lower().endswith("crore"):
        multiplier, text = 1e7, text[:-5]
    try:
        return float(text) * multiplier
    except ValueError as exc:
        raise ValueError(f"Invalid market cap '{value}'.") from exc


def _bucket(value: Any) -> str | None:
    text = _text(value)
    if not text or text in {"-", "—", "–"}:
        return None
    key = text.upper().strip()
    aliases = {"LARGE CAP": "LARGE", "MID CAP": "MID", "SMALL CAP": "SMALL"}
    key = aliases.get(key, key)
    if key not in ALLOWED_BUCKETS:
        raise ValueError("Market Cap Bucket must be Large, Mid, or Small.")
    return key.title()


def _track(value: Any) -> TrackMode:
    raw = _text(value) or ""
    if raw.strip() in {"-", "—", "–"}:
        return TrackMode.NT
    text = raw.upper().replace("_", " ").replace("-", " ").strip()
    aliases = {
        "HT": TrackMode.HT,
        "HIGH TRACK": TrackMode.HT,
        "HIGH": TrackMode.HT,
        "LT": TrackMode.LT,
        "LOW TRACK": TrackMode.LT,
        "LOW": TrackMode.LT,
        "NT": TrackMode.NT,
        "NO TRACK": TrackMode.NT,
        "NONE": TrackMode.NT,
        "PO": TrackMode.PO,
        "PORTFOLIO": TrackMode.PO,
        # Legacy values found in the supplied workbook.
        "P": TrackMode.PO,
        "T": TrackMode.LT,
    }
    if text not in aliases:
        raise ValueError("Track Mode must be one of PO, HT, LT, or NT.")
    return aliases[text]


def _company_payload(
    company: Company,
    target: TargetCompany | None,
    analyst: User | None,
) -> dict[str, Any]:
    mode = target.track_mode.value if target else None
    return {
        "id": target.id if target else None,
        "company_id": company.id,
        "company_name": company.legal_name,
        "legal_name": company.legal_name,
        "nse_symbol": company.nse_symbol,
        "bse_code": company.bse_code,
        "isin": company.isin,
        "aliases": company.aliases or [],
        "market_cap": company.market_cap,
        "market_cap_bucket": company.market_cap_bucket,
        "track_mode": mode,
        "priority": TRACK_PRIORITY.get(mode, 99) if mode else None,
        "analyst_id": analyst.id if analyst else None,
        "analyst": analyst.username if analyst else None,
        "start_date": target.start_date if target else None,
        "end_date": target.end_date if target else None,
        "active": bool(target.active) if target else False,
        "approval_status": target.approval_status.value if target else None,
    }


def _master_query():
    return (
        select(Company, TargetCompany, User)
        .join(TargetCompany, TargetCompany.company_id == Company.id)
        .outerjoin(User, User.id == TargetCompany.analyst_id)
    )


# Workbook is the editable presentation of the master Coverage List. Standard
# fields are still mirrored into Company/TargetCompany so every downstream app
# continues to use the same runtime master, while unknown/future Excel columns
# remain untouched and are rendered dynamically by the frontend.
_CANONICAL_HEADERS = {
    "company": "company name",
    "company name": "company name",
    "legal name": "company name",
    "name": "company name",
    "nse": "nse ticker",
    "nse ticker": "nse ticker",
    "nse symbol": "nse ticker",
    "nse code": "nse ticker",
    "bse": "bse code",
    "bse code": "bse code",
    "bse codes": "bse code",
    "bse scrip code": "bse code",
    "bse scrip codes": "bse code",
    "isin": "isin",
    "market cap": "market cap",
    "market cap inr": "market cap",
    "market capitalization": "market cap",
    "market cap bucket": "market cap bucket",
    "market cap category": "market cap bucket",
    "bucket": "market cap bucket",
    "lt ht": "track mode",
    "ht lt po": "track mode",
    "ht lt po nt": "track mode",
    "track mode": "track mode",
    "tracker mode": "track mode",
    "tracker": "track mode",
    "track": "track mode",
    "priority": "priority",
    "analyst": "analyst",
    "analyst covering": "analyst",
    "covering analyst": "analyst",
    "date of start": "date of start",
    "start date": "date of start",
    "date of end": "date of end",
    "end date": "date of end",
    "#": "row number",
    "no": "row number",
    "number": "row number",
}


def _canonical_header(value: Any) -> str | None:
    raw = str(value or "").strip().casefold()
    if raw in {"#", "№"}:
        return "row number"
    key = _norm_header(value)
    return _CANONICAL_HEADERS.get(key) if key else None


def _header_index(headers: list[str], canonical: str) -> int | None:
    for index, header in enumerate(headers):
        if _canonical_header(header) == canonical:
            return index
    return None


def _display_cell(value: Any) -> Any:
    """Make worksheet cells JSON-safe and present missing cells as '-'."""
    if value is None or value == "":
        return "-"
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value


def _is_blank_cell(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _normalize_workbook_bytes(raw: bytes) -> bytes:
    """Fill missing data cells with '-' without changing the workbook layout/style."""
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(raw), read_only=False, data_only=False)
    sheet = workbook.active
    values = list(sheet.iter_rows(values_only=True))
    candidate = None
    for idx, row in enumerate(values[:100]):
        headers = [_norm_header(v) for v in row]
        if any(v == "company" or v == "company name" for v in headers if v):
            candidate = idx
            break
    if candidate is None:
        return raw

    header_row = candidate + 1
    header_values = [str(sheet.cell(header_row, col).value or f"Column {col}").strip() for col in range(1, sheet.max_column + 1)]
    row_number_col = _header_index(header_values, "row number")
    start = header_row + 1
    sequence = 1
    for row_idx in range(start, sheet.max_row + 1):
        row_values = [sheet.cell(row_idx, col).value for col in range(1, sheet.max_column + 1)]
        if not any(not _is_blank_cell(value) for value in row_values):
            continue
        if row_number_col is not None:
            sheet.cell(row_idx, row_number_col + 1).value = sequence
        sequence += 1
        for col_idx, value in enumerate(row_values, start=1):
            if _is_blank_cell(value):
                sheet.cell(row_idx, col_idx).value = "-"

    sheet.freeze_panes = sheet.freeze_panes or "A2"
    sheet.auto_filter.ref = sheet.dimensions
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def _find_workbook_header(raw: bytes) -> tuple[Any, int, list[str]]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    for sheet in workbook.worksheets:
        for idx, row in enumerate(sheet.iter_rows(values_only=True, max_row=100)):
            normalized = [_norm_header(v) for v in row]
            if any(v in {"company", "company name", "legal name", "name"} for v in normalized if v):
                headers = [str(v).strip() if v is not None and str(v).strip() else f"Column {i + 1}" for i, v in enumerate(row)]
                if headers:
                    return sheet, idx + 1, headers
    raise ValueError("Could not find the Coverage List header row. Expected a Company column.")


def _workbook_grid(raw: bytes) -> tuple[str, int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the workbook exactly as displayed, including unknown future columns."""
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    candidate = None
    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        for idx, row in enumerate(rows[:100]):
            if any(_canonical_header(v) == "company name" for v in row if v is not None):
                candidate = (sheet.title, idx, rows)
                break
        if candidate:
            break
    if not candidate:
        raise ValueError("Could not find the Coverage List header row. Expected a Company column.")

    sheet_name, header_idx, values = candidate
    raw_headers = list(values[header_idx])
    max_cols = max([len(raw_headers), *(len(row) for row in values[header_idx + 1 :])] or [len(raw_headers)])
    headers = []
    for i in range(max_cols):
        value = raw_headers[i] if i < len(raw_headers) else None
        headers.append(str(value).strip() if value is not None and str(value).strip() else f"Column {i + 1}")

    rows: list[dict[str, Any]] = []
    for excel_row, row in enumerate(values[header_idx + 1 :], start=header_idx + 2):
        padded = list(row) + [None] * max(0, max_cols - len(row))
        if not any(not _is_blank_cell(v) for v in padded):
            continue
        rows.append({
            "excel_row": excel_row,
            "cells": [_display_cell(v) for v in padded[:max_cols]],
        })

    column_info = []
    storage_key_counts: dict[str, int] = {}
    for index, header in enumerate(headers):
        canonical = _canonical_header(header)
        nonblank = [r["cells"][index] for r in rows if index < len(r["cells"]) and r["cells"][index] not in (None, "", "-")]
        input_type = "text"
        lowered = _norm_header(header)
        if canonical == "row number":
            input_type = "readonly"
        elif canonical == "track mode" or "track mode" in lowered:
            input_type = "track"
        elif canonical == "analyst" or lowered.endswith(" analyst") or lowered == "analyst":
            input_type = "analyst"
        elif canonical in {"date of start", "date of end"} or "date" in lowered:
            input_type = "date"
        elif nonblank and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in nonblank):
            input_type = "number"
        storage_base = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-") or f"column-{index + 1}"
        storage_key_counts[storage_base] = storage_key_counts.get(storage_base, 0) + 1
        occurrence = storage_key_counts[storage_base]
        storage_key = f"column:{storage_base}" if occurrence == 1 else f"column:{storage_base}:{occurrence}"
        column_info.append({
            "key": f"c{index}",
            "storage_key": storage_key,
            "index": index,
            "label": header,
            "canonical": canonical,
            "input_type": input_type,
            "editable": input_type != "readonly",
        })

    return sheet_name, header_idx + 1, column_info, rows


def _master_lookup(rows: list[tuple[Company, TargetCompany, User]]) -> tuple[dict[str, tuple[Company, TargetCompany, User]], dict[str, tuple[Company, TargetCompany, User]]]:
    by_name: dict[str, tuple[Company, TargetCompany, User]] = {}
    by_identifier: dict[str, tuple[Company, TargetCompany, User]] = {}
    for company, target, analyst in rows:
        item = (company, target, analyst)
        name = _normalize_company_text(company.legal_name)
        if name:
            by_name[name.casefold()] = item
        for value in (company.nse_symbol, company.bse_code, company.isin):
            normalized = _normalize_identifier(value)
            if normalized:
                by_identifier[normalized.casefold()] = item
    return by_name, by_identifier


def _priority_for_mode(value: Any) -> int:
    text = str(value or "").strip().upper()
    return TRACK_PRIORITY.get(text, 99)


def _grid_input_values(headers: list[str], cells: list[Any]) -> dict[str, Any]:
    values = list(cells[:len(headers)]) + ["-"] * max(0, len(headers) - len(cells))
    by_canonical: dict[str, Any] = {}
    for index, header in enumerate(headers):
        canonical = _canonical_header(header)
        if canonical and canonical not in by_canonical:
            value = values[index]
            by_canonical[canonical] = value
    return by_canonical


def _prepare_cells_for_workbook(headers: list[str], cells: list[Any], existing: list[Any] | None = None) -> list[Any]:
    values = list(cells[:len(headers)]) + ["-"] * max(0, len(headers) - len(cells))
    if existing is not None:
        for index, value in enumerate(values):
            if _is_blank_cell(value):
                values[index] = existing[index] if index < len(existing) else "-"
    for index, header in enumerate(headers):
        canonical = _canonical_header(header)
        if canonical == "row number":
            continue
        if _is_blank_cell(values[index]):
            values[index] = "-"
    return values


def _infer_grid_headers_for_row(raw: bytes) -> tuple[Any, int, list[str]]:
    from openpyxl import load_workbook
    workbook = load_workbook(io.BytesIO(raw), read_only=False, data_only=False)
    sheet = workbook.active
    for row_idx in range(1, min(sheet.max_row, 100) + 1):
        headers = [sheet.cell(row_idx, col).value for col in range(1, sheet.max_column + 1)]
        if any(_canonical_header(v) == "company name" for v in headers if v is not None):
            return sheet, row_idx, [str(v).strip() if v is not None and str(v).strip() else f"Column {i + 1}" for i, v in enumerate(headers)]
    raise ValueError("Could not find the Coverage List header row.")


def _save_workbook_with_row_change(raw: bytes, *, mode: str, target_row: int | None = None, cells: list[Any] | None = None) -> bytes:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(raw), read_only=False, data_only=False)
    sheet = workbook.active
    header_row = None
    headers: list[str] = []
    for row_idx in range(1, min(sheet.max_row, 100) + 1):
        values = [sheet.cell(row_idx, col).value for col in range(1, sheet.max_column + 1)]
        if any(_canonical_header(v) == "company name" for v in values if v is not None):
            header_row = row_idx
            headers = [str(v).strip() if v is not None and str(v).strip() else f"Column {i + 1}" for i, v in enumerate(values)]
            break
    if header_row is None:
        raise ValueError("Could not find the Coverage List header row.")

    def set_cells(row_number: int, row_values: list[Any]) -> None:
        existing_style_source = max(header_row + 1, row_number - 1)
        for col_idx, value in enumerate(row_values, start=1):
            if row_number > header_row + 1:
                source = sheet.cell(existing_style_source, col_idx)
                target = sheet.cell(row_number, col_idx)
                if source.has_style:
                    target._style = copy.copy(source._style)
                if source.number_format:
                    target.number_format = source.number_format
                if source.alignment:
                    target.alignment = copy.copy(source.alignment)
                if source.protection:
                    target.protection = copy.copy(source.protection)
            canonical = _canonical_header(headers[col_idx - 1])
            if canonical == "row number":
                # Keep # generated/renumbered below.
                continue
            target = sheet.cell(row_number, col_idx)
            target.value = value if not _is_blank_cell(value) else "-"

    if mode == "append":
        data_rows = [r for r in range(header_row + 1, sheet.max_row + 1) if any(not _is_blank_cell(sheet.cell(r, c).value) for c in range(1, sheet.max_column + 1))]
        target_row = (max(data_rows) + 1) if data_rows else (header_row + 1)
        set_cells(target_row, cells or ["-"] * len(headers))
    elif mode == "update":
        if target_row is None or target_row <= header_row or target_row > sheet.max_row:
            raise ValueError("Coverage List workbook row could not be located for update.")
        set_cells(target_row, cells or ["-"] * len(headers))
    elif mode == "delete":
        if target_row is None or target_row <= header_row or target_row > sheet.max_row:
            raise ValueError("Coverage List workbook row could not be located for deletion.")
        sheet.delete_rows(target_row, 1)

    # Renumber the # column when it exists, without altering the workbook's
    # displayed column order.
    row_number_col = _header_index(headers, "row number")
    if row_number_col is not None:
        sequence = 1
        for row_idx in range(header_row + 1, sheet.max_row + 1):
            if not any(not _is_blank_cell(sheet.cell(row_idx, c).value) for c in range(1, sheet.max_column + 1)):
                continue
            sheet.cell(row_idx, row_number_col + 1).value = sequence
            sequence += 1

    sheet.freeze_panes = sheet.freeze_panes or "A2"
    sheet.auto_filter.ref = sheet.dimensions
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def _find_row_for_master(raw: bytes, company: Company, target: TargetCompany) -> int | None:
    from openpyxl import load_workbook
    workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    sheet = workbook.active
    header_row = None
    headers = []
    for row_idx, values in enumerate(sheet.iter_rows(values_only=True, max_row=100), start=1):
        if any(_canonical_header(v) == "company name" for v in values if v is not None):
            header_row = row_idx
            headers = [str(v).strip() if v is not None and str(v).strip() else f"Column {i + 1}" for i, v in enumerate(values)]
            break
    if header_row is None:
        return None
    company_idx = _header_index(headers, "company name")
    nse_idx = _header_index(headers, "nse ticker")
    bse_idx = _header_index(headers, "bse code")
    isin_idx = _header_index(headers, "isin")
    wanted_name = (_normalize_company_text(company.legal_name) or "").casefold()
    wanted_ids = {str(v).casefold() for v in (_normalize_identifier(company.nse_symbol), _normalize_identifier(company.bse_code), _normalize_identifier(company.isin)) if v}
    for row_idx, values in enumerate(sheet.iter_rows(values_only=True, min_row=header_row + 1), start=header_row + 1):
        row_name = (_normalize_company_text(values[company_idx]) or "").casefold() if company_idx is not None and company_idx < len(values) else ""
        row_ids = set()
        for idx in (nse_idx, bse_idx, isin_idx):
            if idx is not None and idx < len(values):
                value = _normalize_identifier(values[idx])
                if value:
                    row_ids.add(value.casefold())
        if wanted_name and row_name == wanted_name:
            return row_idx
        if wanted_ids and wanted_ids & row_ids:
            return row_idx
    return None


def _sync_workbook_raw(raw: bytes) -> bytes:
    """Normalize blanks before DB parsing so '-' is the persisted display value."""
    return _normalize_workbook_bytes(raw)


async def _build_grid_payload(db) -> dict[str, Any]:
    path = coverage_workbook_path()
    if not path.exists():
        raise ValueError(f"Coverage List workbook does not exist: {path}")
    raw = path.read_bytes()
    sheet_name, header_row, columns, raw_rows = _workbook_grid(raw)

    master_rows = (await db.execute(_master_query())).all()
    by_name, by_identifier = _master_lookup(master_rows)
    labels = [c["label"] for c in columns]

    rows: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        values = _grid_input_values(labels, raw_row["cells"])
        name = _normalize_company_text(values.get("company name"))
        master = by_name.get(name.casefold()) if name else None
        if master is None:
            for key in ("nse ticker", "bse code", "isin"):
                value = _normalize_identifier(values.get(key))
                if value and value.casefold() in by_identifier:
                    master = by_identifier[value.casefold()]
                    break

        company = target = analyst = None
        if master:
            company, target, analyst = master

        mode = target.track_mode.value if target else "NT"
        track_raw = values.get("track mode")
        if track_raw and not is_dash_like(track_raw):
            try:
                mode = _track(track_raw).value
            except ValueError:
                pass

        analyst_raw = _text(values.get("analyst"))
        display_analyst = analyst_raw if analyst_raw and not is_dash_like(analyst_raw) else (analyst.username if analyst else None)
        status = target.approval_status.value if target else ApprovalStatus.PENDING_AASHIL_APPROVAL.value
        rows.append({
            "id": target.id if target else None,
            "company_id": company.id if company else None,
            "company_name": name or "-",
            "track_mode": mode,
            "priority": TRACK_PRIORITY.get(mode, 99),
            "analyst": display_analyst,
            "excel_row": raw_row["excel_row"],
            "cells": list(raw_row["cells"]),
            "active": bool(target.active) if target else False,
            "approval_status": status,
        })

    rows.sort(key=lambda row: (row["priority"], row["excel_row"]))
    track_counts = {"PO": 0, "HT": 0, "LT": 0, "NT": 0}
    analyst_counts: dict[str, int] = {}
    approval_counts: dict[str, int] = {}
    for row in rows:
        mode = row["track_mode"]
        if mode in track_counts:
            track_counts[mode] += 1
        # Normalize keys so capitalization/Unicode variants count as one analyst.
        analyst_name = unicodedata.normalize(
            "NFKC", str(row.get("analyst") or "Unassigned")
        ).strip()
        analyst_key = _normalize_analyst_username(analyst_name) or "unassigned"
        analyst_counts[analyst_key] = analyst_counts.get(analyst_key, 0) + 1
        status = row.get("approval_status") or "UNKNOWN"
        approval_counts[status] = approval_counts.get(status, 0) + 1

    return {
        "sheet": sheet_name,
        "header_row": header_row,
        "version": f"{path.stat().st_mtime_ns}:{path.stat().st_size}",
        "columns": columns,
        "rows": rows,
        "counts": {
            "total": len(rows),
            "track": track_counts,
            "analyst": dict(sorted(analyst_counts.items(), key=lambda item: item[0].casefold())),
            "approval": approval_counts,
        },
        "allowed_analysts": ["aashil", "ahmed", "jainam", "raj", "romil"],
    }


async def _grid_payload(db) -> dict[str, Any]:
    """Return a cached workbook grid; never reparses the same XLSX per request."""
    global _GRID_CACHE
    path = coverage_workbook_path()
    if not path.exists():
        raise ValueError(f"Coverage List workbook does not exist: {path}")
    signature = _workbook_signature(path)
    cache_key = f"{signature[0]}:{signature[1]}"
    async with _GRID_CACHE_LOCK:
        if _GRID_CACHE and _GRID_CACHE.get("key") == cache_key:
            return copy.deepcopy(_GRID_CACHE["payload"])
        payload = await _build_grid_payload(db)
        _GRID_CACHE = {"key": cache_key, "payload": payload}
        return copy.deepcopy(payload)


async def get_pulse_coverage_rows(db) -> list[dict[str, Any]]:
    """Return instrument rows from the Coverage List workbook for Pulse.

    The workbook is the source of truth for the Pulse stock universe. Unlike
    the user-facing active/approved endpoint, this returns every valid listed
    instrument row so the heatmap mirrors the workbook itself. No symbols are
    added from a separate hard-coded market universe.
    """
    payload = await _grid_payload(db)
    columns = payload.get("columns") or []
    labels = [str(column.get("label") or "") for column in columns]
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in payload.get("rows") or []:
        cells = list(row.get("cells") or [])
        values = _grid_input_values(labels, cells)
        name = _normalize_company_text(values.get("company name"))
        nse_symbol, bse_code = _normalize_workbook_identifiers(
            values.get("nse ticker"), values.get("bse code")
        )
        if not name or not (nse_symbol or bse_code):
            continue
        if nse_symbol:
            symbol, exchange = nse_symbol, "NSE"
            yahoo_symbol = f"{nse_symbol}.NS"
        else:
            symbol, exchange = str(bse_code), "BSE"
            yahoo_symbol = f"{bse_code}.BO"
        identity = (exchange, str(symbol).upper())
        if identity in seen:
            continue
        seen.add(identity)
        rows.append({
            "symbol": str(symbol).upper(),
            "name": name,
            "exchange": exchange,
            "nse_symbol": nse_symbol,
            "bse_code": bse_code,
            "yahoo": yahoo_symbol,
            "watchlist_source": "coverage_list",
            "coverage_excel_row": row.get("excel_row"),
            "track_mode": values.get("track mode"),
            "analyst": values.get("analyst"),
            "approval_status": row.get("approval_status"),
            "active": bool(row.get("active")),
        })
    return rows


def is_dash_like(value: Any) -> bool:
    text = str(value or "").strip()
    return text in {"", "-", "—", "–"}

async def _resolve_analyst(db, analyst_value: Any) -> User | None:
    name = _text(analyst_value)
    if not name or str(name).strip() in {"-", "—", "–"}:
        return None
    normalized_name = unicodedata.normalize("NFKC", str(name)).strip().casefold()
    candidate_names = [normalized_name]
    # Either spelling is accepted, preferring an exact case-insensitive username
    # match before falling back to the alternate spelling.
    if normalized_name == "aashil":
        candidate_names.append("ashil")
    elif normalized_name == "ashil":
        candidate_names.append("aashil")

    for candidate_name in candidate_names:
        row = await db.execute(
            select(User).where(
                User.is_active.is_(True),
                User.username.ilike(candidate_name),
            )
        )
        analyst = row.scalar_one_or_none()
        if analyst:
            return analyst
    raise ValueError(f"Coverage assignee '{name}' is not an active user.")

class CoverageCompanyCreate(BaseModel):
    # Dynamic callers send the workbook row as `cells`; legacy fixed fields stay
    # accepted so older frontend/API callers continue to work.
    cells: list[Any] | None = None
    company_name: str | None = None
    nse_symbol: str | None = None
    bse_code: str | None = None
    isin: str | None = None
    aliases: list[str] = Field(default_factory=list, max_length=30)
    market_cap: float | None = None
    market_cap_bucket: str | None = None
    track_mode: TrackMode | None = None
    analyst_id: int | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None


class CoverageCompanyUpdate(CoverageCompanyCreate):
    pass

async def _validate_company_identifiers(
    db,
    data: CoverageCompanyCreate | CoverageCompanyUpdate,
    company_id: int | None = None,
):
    query = select(Company)
    if company_id is not None:
        query = query.where(Company.id != company_id)

    if data.nse_symbol:
        duplicate = await db.scalar(query.where(Company.nse_symbol == data.nse_symbol.upper()))
        if duplicate:
            raise HTTPException(409, f"NSE symbol {data.nse_symbol} already belongs to {duplicate.legal_name}.")

    if data.bse_code:
        duplicate = await db.scalar(query.where(Company.bse_code == data.bse_code.strip()))
        if duplicate:
            raise HTTPException(409, f"BSE code {data.bse_code} already belongs to {duplicate.legal_name}.")

    if data.isin:
        duplicate = await db.scalar(query.where(Company.isin == data.isin.upper()))
        if duplicate:
            raise HTTPException(409, f"ISIN {data.isin} already belongs to {duplicate.legal_name}.")

    duplicate = await db.scalar(query.where(Company.legal_name.ilike(data.company_name.strip())))
    if duplicate:
        raise HTTPException(409, f"A company already exists with the name {duplicate.legal_name}.")


async def _validate_track_analyst(db, track_mode: TrackMode | None, analyst_id: int | None) -> User | None:
    # An empty Analyst cell is valid for any track mode. The master workbook uses
    # "-" for missing values and downstream workflows understand an unassigned row.
    if analyst_id is None:
        return None
    analyst = await db.get(User, analyst_id)
    if not analyst or not analyst.is_active or analyst.role != UserRole.ANALYST:
        raise HTTPException(400, "Selected coverage assignee is not an active analyst.")
    if analyst.username.casefold() not in ALLOWED_ANALYST_USERNAMES:
        raise HTTPException(400, "Coverage assignee must be one of: ahmed, jainam, raj, romil, or Aashil (also accepted as Ashil).")
    return analyst

async def _save_row(
    db,
    *,
    company: Company,
    target: TargetCompany,
    data: CoverageCompanyCreate | CoverageCompanyUpdate,
    admin: User,
):
    analyst = await _validate_track_analyst(db, data.track_mode, data.analyst_id)
    await _validate_company_identifiers(db, data, company.id)

    company.legal_name = data.company_name.strip()
    company.nse_symbol = data.nse_symbol.upper().strip() if data.nse_symbol else None
    company.bse_code = data.bse_code.strip() if data.bse_code else None
    company.isin = data.isin.upper().strip() if data.isin else None
    company.aliases = list(dict.fromkeys([str(x).strip() for x in data.aliases if str(x).strip()]))
    company.market_cap = data.market_cap
    company.market_cap_bucket = _bucket(data.market_cap_bucket)

    target.analyst_id = analyst.id if analyst else None
    target.track_mode = data.track_mode
    target.start_date = data.start_date
    target.end_date = data.end_date
    target.active = True
    target.approval_status = ApprovalStatus.APPROVED
    target.approval_by = admin.id
    target.approval_at = datetime.now(timezone.utc)
    return analyst


async def _apply_parsed_rows(
    db,
    parsed_rows: list[dict[str, Any]],
    admin: User | None,
    *,
    approve_new: bool = True,
    force_approve: bool = False,
) -> dict[str, Any]:
    """Reconcile workbook rows into the runtime master.

    New rows may be approved automatically for trusted Excel imports, while an
    analyst-created FE row stays pending until an admin explicitly approves it.
    Existing pending/rejected rows are never silently promoted by a background
    workbook sync.
    """
    users = (await db.execute(select(User).where(User.is_active.is_(True)))).scalars().all()
    users_by_name = {unicodedata.normalize("NFKC", u.username).strip().casefold(): u for u in users}
    companies = (await db.execute(select(Company))).scalars().all()
    targets = (await db.execute(select(TargetCompany))).scalars().all()
    targets_by_company_id = {t.company_id: t for t in targets}

    def key_name(value: Any) -> str | None:
        text = _normalize_company_text(value)
        return text.casefold() if text else None

    def key_id(value: Any) -> str | None:
        return _normalize_identifier(value)

    by_name: dict[str, Company] = {}
    by_nse: dict[str, Company] = {}
    by_bse: dict[str, Company] = {}
    by_isin: dict[str, Company] = {}
    for company in companies:
        if (k := key_name(company.legal_name)):
            by_name[k] = company
        if (k := key_id(company.nse_symbol)):
            by_nse[k] = company
        if (k := key_id(company.bse_code)):
            by_bse[k] = company
        if (k := key_id(company.isin)):
            by_isin[k] = company

    pending_companies: list[Company] = []
    pending_rows: list[tuple[dict[str, Any], Company, User | None]] = []
    pending_identifier_claims: dict[tuple[str, str], Company] = {}
    seen_company_ids: dict[int, str] = {}
    sync_warnings: list[str] = []
    identifier_resolution_conflicts = 0
    explicitly_bse_unlisted_company_ids: set[int] = set()
    explicitly_bse_unlisted_company_names: set[str] = set()

    def register_pending_identifier(kind: str, value: Any, company: Company, company_name: str) -> None:
        normalized = key_id(value)
        if not normalized:
            return
        claim_key = (kind, normalized)
        previous = pending_identifier_claims.get(claim_key)
        if previous is not None and previous is not company:
            raise ValueError(f"Workbook identifier conflict: {kind} '{normalized}' is assigned to multiple companies, including '{company_name}'.")
        pending_identifier_claims[claim_key] = company

    def resolve_existing(row: dict[str, Any]) -> Company | None:
        nonlocal identifier_resolution_conflicts
        matches: dict[int, Company] = {}
        for key, mapping in (("nse_symbol", by_nse), ("bse_code", by_bse), ("isin", by_isin)):
            value = row.get(key)
            if value and key_id(value) in mapping:
                company = mapping[key_id(value)]
                matches[company.id] = company
        name_match = by_name.get(key_name(row["company_name"]))
        if len(matches) > 1:
            details = ", ".join(f"{c.id}:{c.legal_name}" for c in matches.values())
            if name_match is not None:
                identifier_resolution_conflicts += 1
                return name_match
            raise ValueError(f"Coverage List identifier conflict for '{row['company_name']}': identifiers resolve to multiple companies ({details}).")
        matched = next(iter(matches.values()), None)
        if matched is not None and name_match is not None and name_match.id != matched.id:
            identifier_resolution_conflicts += 1
            return name_match
        return matched or name_match

    for row in parsed_rows:
        analyst = None
        analyst_name = _normalize_company_text(row.get("analyst_name"))
        if analyst_name:
            analyst_key = analyst_name.casefold()
            analyst = users_by_name.get(analyst_key)
            if analyst is None and analyst_key in {"aashil", "ashil"}:
                alternate_key = "ashil" if analyst_key == "aashil" else "aashil"
                analyst = users_by_name.get(alternate_key)
            # Excel is an admin-controlled master import. The current workbook
            # intentionally contains legacy rows assigned to the admin (Aashil),
            # while the FE Add Company dropdown is restricted to the four active
            # analysts. Do not reject the canonical workbook just because an
            # existing legacy assignee is not selectable for new FE submissions.
            if analyst is None:
                raise ValueError(f"Coverage assignee '{analyst_name}' is not an active user.")

        company = resolve_existing(row)
        if company is None:
            company = Company(legal_name=_normalize_company_text(row["company_name"]) or row["company_name"], aliases=[])
            db.add(company)
            pending_companies.append(company)
            for kind, key in (("NSE", "nse_symbol"), ("BSE", "bse_code"), ("ISIN", "isin")):
                register_pending_identifier(kind, row.get(key), company, row["company_name"])
        else:
            if company.id is not None and company.id in seen_company_ids:
                raise ValueError(f"Coverage List contains multiple rows for the same company: '{seen_company_ids[company.id]}' and '{row['company_name']}'.")
        if company.id is not None:
            seen_company_ids[company.id] = row["company_name"]
        pending_rows.append((row, company, analyst))

    if pending_companies:
        await db.flush()
        for row, company, _ in pending_rows:
            if company in pending_companies and company.id is not None:
                seen_company_ids[company.id] = row["company_name"]

    # Build the complete workbook identity map before applying identifiers. This is
    # essential when a valid workbook BSE code is still attached to an obsolete /
    # duplicate Company row: we can tell whether that old owner has its own explicit
    # replacement identifier in this workbook before moving the unique code.
    workbook_rows_by_company_id: dict[int, dict[str, Any]] = {
        int(company.id): row for row, company, _analyst in pending_rows if company.id is not None
    }
    workbook_company_ids: set[int] = set(workbook_rows_by_company_id)
    identifier_reassignments = 0
    bse_codes_applied = 0

    created = 0
    updated = 0
    for row, company, analyst in pending_rows:
        if company.id is None:
            raise ValueError(f"Company '{row['company_name']}' has no database id after flush.")
        company.legal_name = _normalize_company_text(row["company_name"]) or row["company_name"]
        present_fields = row.get("present_fields", set())
        async def safe_identifier(field_name: str, value: Any, kind: str) -> None:
            nonlocal identifier_reassignments, bse_codes_applied
            if field_name not in present_fields:
                return
            mapping = {
                "nse_symbol": by_nse,
                "bse_code": by_bse,
                "isin": by_isin,
            }[field_name]
            if field_name == "bse_code" and row.get("bse_code_unlisted"):
                # An explicit N/A / Not Listed marker means this issuer has no
                # BSE listing. Unlike an empty cell, it must clear any stale code
                # previously discovered in the database so NewsAlert uses NSE.
                old_code = key_id(company.bse_code)
                company.bse_code = None
                if company.id is not None:
                    explicitly_bse_unlisted_company_ids.add(int(company.id))
                if company.legal_name:
                    explicitly_bse_unlisted_company_names.add(str(company.legal_name).strip())
                if old_code and mapping.get(old_code) is company:
                    mapping.pop(old_code, None)
                return

            normalized = key_id(value)
            if not normalized:
                # A newly added workbook column may be blank for legacy rows.
                # Do not erase a valid DB identifier simply because that cell is
                # empty; only an explicit non-empty value replaces the master.
                return

            owner = mapping.get(normalized)
            if owner is not None and owner.id != company.id:
                # The workbook validates that each identifier is claimed by only
                # one row. If its current DB owner is outside the workbook, or its
                # own workbook row explicitly claims a different value / N/A, the
                # DB ownership is stale. Release it before assigning the workbook
                # value so the DB unique constraint is respected. Never steal an
                # identifier from another workbook row whose cell is blank: blank
                # cells intentionally preserve legacy identifiers.
                owner_row = workbook_rows_by_company_id.get(int(owner.id)) if owner.id is not None else None
                owner_claim = key_id(owner_row.get(field_name)) if owner_row else None
                owner_explicit_unlisted = bool(
                    field_name == "bse_code" and owner_row and owner_row.get("bse_code_unlisted")
                )
                owner_explicitly_replaced = bool(owner_row and owner_claim and owner_claim != normalized)
                can_reassign = owner_row is None or owner_explicitly_replaced or owner_explicit_unlisted
                if can_reassign:
                    setattr(owner, field_name, None)
                    # If this row explicitly says the issuer is not BSE-listed,
                    # also clear a stale BSE code on the legacy duplicate that
                    # owned its NSE symbol. Keep any code claimed by that owner's
                    # own workbook row.
                    if (field_name == "nse_symbol" and row.get("bse_code_unlisted")
                            and (owner_row is None or owner_explicit_unlisted)):
                        stale_bse_code = key_id(getattr(owner, "bse_code", None))
                        if stale_bse_code:
                            owner.bse_code = None
                            if by_bse.get(stale_bse_code) is owner:
                                by_bse.pop(stale_bse_code, None)
                    await db.flush()
                    if mapping.get(normalized) is owner:
                        mapping.pop(normalized, None)
                    identifier_reassignments += 1
                    logger.debug(
                        "COVERAGE_IDENTIFIER_REASSIGNED kind=%s value=%s previous_owner=%s new_owner=%s previous_owner_in_workbook=%s",
                        kind, normalized, getattr(owner, "legal_name", owner.id),
                        row["company_name"], bool(owner_row),
                    )
                else:
                    sync_warnings.append(
                        f"Row {row.get('excel_row', '?')} '{row['company_name']}': {kind} '{normalized}' is still claimed by '{owner.legal_name}', whose workbook cell is blank; preserved the existing owner."
                    )
                    return

            # Remove this company's previous key from the in-memory index before
            # replacing it, then publish the new canonical workbook owner.
            previous_value = key_id(getattr(company, field_name, None))
            if previous_value and previous_value != normalized and mapping.get(previous_value) is company:
                mapping.pop(previous_value, None)
            setattr(company, field_name, normalized)
            mapping[normalized] = company
            if field_name == "bse_code":
                bse_codes_applied += 1

        await safe_identifier("nse_symbol", row.get("nse_symbol"), "NSE Symbol")
        await safe_identifier("bse_code", row.get("bse_code"), "BSE Code")
        await safe_identifier("isin", row.get("isin"), "ISIN")
        if "market cap" in present_fields:
            company.market_cap = row.get("market_cap")
        if "market cap bucket" in present_fields:
            company.market_cap_bucket = row.get("market_cap_bucket")

        target = targets_by_company_id.get(company.id)
        is_new_target = target is None
        if target is None:
            target = TargetCompany(company_id=company.id)
            db.add(target)
            targets_by_company_id[company.id] = target
            created += 1
        else:
            updated += 1

        if "analyst" in present_fields:
            target.analyst_id = analyst.id if analyst else None
        if "track mode" in present_fields:
            target.track_mode = row["track_mode"]
        if "date of start" in present_fields:
            target.start_date = row["start_date"]
        if "date of end" in present_fields:
            target.end_date = row["end_date"]

        if force_approve:
            target.active = True
            target.approval_status = ApprovalStatus.APPROVED
            target.approval_by = admin.id if admin else None
            target.approval_at = datetime.now(timezone.utc) if admin else None
        elif is_new_target:
            target.active = True
            target.approval_status = ApprovalStatus.APPROVED if approve_new else ApprovalStatus.PENDING_AASHIL_APPROVAL
            target.approval_by = admin.id if (approve_new and admin) else None
            target.approval_at = datetime.now(timezone.utc) if (approve_new and admin) else None
        elif target.approval_status == ApprovalStatus.PENDING_AASHIL_APPROVAL:
            target.active = True
        elif target.approval_status == ApprovalStatus.REJECTED:
            target.active = False
        else:
            target.active = True
        workbook_company_ids.add(company.id)

    # An explicit N/A in BSE Code also invalidates BSE routing metadata already
    # stored on historical NewsAlert rows. Otherwise old alerts could continue
    # opening an obsolete BSE URL even after the master code is cleared.
    if explicitly_bse_unlisted_company_ids or explicitly_bse_unlisted_company_names:
        stale_item_filters = []
        if explicitly_bse_unlisted_company_ids:
            stale_item_filters.append(NewsAlertItem.company_id.in_(explicitly_bse_unlisted_company_ids))
        if explicitly_bse_unlisted_company_names:
            stale_item_filters.append(NewsAlertItem.company_name.in_(explicitly_bse_unlisted_company_names))
        stale_items = (await db.execute(
            select(NewsAlertItem).where(or_(*stale_item_filters))
        )).scalars().all()
        for stale_item in stale_items:
            metadata = stale_item.metadata_json if isinstance(stale_item.metadata_json, dict) else {}
            metadata = dict(metadata)
            for field_name in ("bse_code", "bse_symbol", "bse_stock_url"):
                metadata.pop(field_name, None)
            stale_item.metadata_json = metadata

    # Rows removed from Excel are no longer part of the active master. Rejected
    # rows that are still present remain rejected so approval cannot be bypassed.
    for target in targets:
        if target.company_id not in workbook_company_ids:
            target.active = False
            if target.approval_status != ApprovalStatus.REJECTED:
                target.approval_status = ApprovalStatus.REJECTED

    await db.flush()
    _LAST_SYNC_WARNINGS[:] = sync_warnings[-200:]
    return {
        "rows": len(parsed_rows),
        "created": created,
        "updated": updated,
        "active_coverage_rows": len(workbook_company_ids),
        "bse_codes_applied": bse_codes_applied,
        "bse_companies_unlisted": len(explicitly_bse_unlisted_company_ids),
        "identifier_resolution_conflicts": identifier_resolution_conflicts,
        "identifier_ownership_reassigned": identifier_reassignments,
        "warnings": list(sync_warnings[-200:]),
    }

async def _build_workbook_bytes(db) -> bytes:
    path = coverage_workbook_path()
    if not path.exists():
        raise ValueError(f"Coverage List workbook does not exist: {path}")
    return _normalize_workbook_bytes(path.read_bytes())


async def _write_workbook_file(db) -> Path:
    # Backward-compatible helper: the workbook itself is canonical now, so this
    # simply normalizes/persists it rather than rebuilding a fixed 8-column sheet.
    path = coverage_workbook_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        normalized = _normalize_workbook_bytes(path.read_bytes())
        if normalized != path.read_bytes():
            temp = path.with_suffix(".xlsx.tmp")
            temp.write_bytes(normalized)
            temp.replace(path)
    global _WORKBOOK_SIGNATURE
    _WORKBOOK_SIGNATURE = _workbook_signature(path) if path.exists() else None
    _GRID_CACHE = None
    return path

async def _background_coverage_sync() -> None:
    try:
        async with get_sessionmaker()() as db:
            await sync_coverage_workbook_if_changed(db)
    except Exception:
        logger.exception("Background Coverage List synchronization failed")


def schedule_coverage_workbook_sync() -> None:
    global _WORKBOOK_SYNC_TASK
    path = coverage_workbook_path()
    if not path.exists():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _WORKBOOK_SYNC_TASK is not None and not _WORKBOOK_SYNC_TASK.done():
        return
    _WORKBOOK_SYNC_TASK = loop.create_task(_background_coverage_sync(), name="coverage-workbook-sync")


async def sync_coverage_workbook_if_changed(db) -> bool:
    """Synchronize the editable Coverage List workbook into Company/TargetCompany.

    The workbook remains the canonical editable layout. Standard master fields are
    mirrored into the DB so all downstream applications can query a consistent
    runtime master without losing future/unmodeled Excel columns.
    """
    global _WORKBOOK_SIGNATURE, _WORKBOOK_FAILED_SIGNATURE, _WORKBOOK_FAILED_ERROR, _LAST_SYNC_WARNINGS
    path = coverage_workbook_path()

    if not path.exists():
        logger.warning("Coverage List sync: workbook does not exist; skipping: %s", path)
        return False

    async with _WORKBOOK_SYNC_LOCK:
        signature = _workbook_signature(path)
        if _WORKBOOK_SIGNATURE == signature:
            return False
        if _WORKBOOK_FAILED_SIGNATURE == signature:
            return False

        logger.info("Coverage List sync: importing workbook path=%s signature=%s", path, signature)
        raw = path.read_bytes()
        normalized = _sync_workbook_raw(raw)
        _sheet, _header, parsed_rows = parse_coverage_workbook(normalized)

        try:
            result = await _apply_parsed_rows(db, parsed_rows, None, approve_new=True, force_approve=False)
            await db.commit()

            if normalized != raw:
                temp = path.with_suffix(".xlsx.tmp")
                temp.write_bytes(normalized)
                temp.replace(path)
                signature = _workbook_signature(path)
        except Exception as exc:
            await db.rollback()
            _WORKBOOK_FAILED_SIGNATURE = signature
            _WORKBOOK_FAILED_ERROR = str(exc)
            logger.exception("Coverage List sync failed; workbook version remains pending correction")
            raise

        _WORKBOOK_SIGNATURE = signature
        _WORKBOOK_FAILED_SIGNATURE = None
        global _GRID_CACHE
        _GRID_CACHE = None
        _WORKBOOK_FAILED_ERROR = None
        logger.info("Coverage List sync completed: %s", result)
        return bool(result["rows"])

@router.get("")
async def list_coverage_list(
    db=Depends(get_db),
    _: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            _master_query()
            .where(
                TargetCompany.active.is_(True),
                TargetCompany.approval_status == ApprovalStatus.APPROVED,
            )
        )
    ).all()
    payload = [_company_payload(c, t, u) for c, t, u in rows]
    payload.sort(key=lambda row: (row["priority"] or 99, str(row["company_name"]).casefold()))
    return payload


def _legacy_or_dynamic_values(data: CoverageCompanyCreate, headers: list[str]) -> list[Any]:
    if data.cells is not None:
        return list(data.cells)
    mapping = {
        "company name": data.company_name,
        "nse ticker": data.nse_symbol,
        "bse code": data.bse_code,
        "isin": data.isin,
        "market cap": data.market_cap,
        "market cap bucket": data.market_cap_bucket,
        "track mode": data.track_mode.value if data.track_mode else None,
        "analyst": None,
        "date of start": data.start_date,
        "date of end": data.end_date,
    }
    return [mapping.get(_canonical_header(header), "-") for header in headers]


def _row_cells_from_inputs(headers: list[str], cells: list[Any], existing: list[Any] | None = None) -> list[Any]:
    raw = list(cells)
    values: list[Any] = []
    for index in range(len(headers)):
        if index < len(raw):
            value = raw[index]
            # Preserve future columns if an older browser sends fewer values.
            if existing is not None and _is_blank_cell(value) and index < len(existing):
                value = existing[index]
            values.append(value)
        elif existing is not None and index < len(existing):
            values.append(existing[index])
        else:
            values.append("-")
    return _prepare_cells_for_workbook(headers, values, existing=existing)


def _write_workbook_bytes_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".xlsx.tmp")
    temp.write_bytes(_normalize_workbook_bytes(raw))
    temp.replace(path)


async def _grid_row_existing_values(raw: bytes, row_number: int) -> list[Any] | None:
    from openpyxl import load_workbook
    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        sheet = workbook.active
        return list(next(sheet.iter_rows(min_row=row_number, max_row=row_number, values_only=True)))
    except Exception:
        return None


@router.post("")
async def create_coverage_company(
    data: CoverageCompanyCreate,
    db=Depends(get_db),
    actor: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST)),
):
    path = coverage_workbook_path()
    async with _WORKBOOK_SYNC_LOCK:
        if not path.exists():
            raise HTTPException(500, "Coverage List workbook does not exist.")
        old_raw = _normalize_workbook_bytes(path.read_bytes())
        _sheet, _header, columns, _rows = _workbook_grid(old_raw)
        headers = [column["label"] for column in columns]
        cells = _row_cells_from_inputs(headers, _legacy_or_dynamic_values(data, headers))
        values = _grid_input_values(headers, cells)
        company_name = _normalize_company_text(values.get("company name"))
        if not company_name or company_name == "-":
            raise HTTPException(400, "Company is required.")

        # Convert the dynamic Excel-style row into the standard runtime fields.
        data.company_name = company_name
        data.nse_symbol = _text(values.get("nse ticker"))
        data.bse_code = _text(values.get("bse code"))
        data.isin = _text(values.get("isin"))
        if values.get("track mode") and not is_dash_like(values.get("track mode")):
            data.track_mode = _track(values.get("track mode"))
        else:
            data.track_mode = TrackMode.NT
        analyst = await _resolve_analyst(db, values.get("analyst")) if values.get("analyst") is not None else None
        if actor.role == UserRole.ANALYST and analyst is None:
            analyst = actor
        if analyst and analyst.username.casefold() not in ALLOWED_ANALYST_USERNAMES:
            raise HTTPException(400, "Coverage assignee must be one of: ahmed, jainam, raj, romil, or Aashil (also accepted as Ashil).")
        data.analyst_id = analyst.id if analyst else None
        data.start_date = _parse_date(values.get("date of start"))
        data.end_date = _parse_date(values.get("date of end"))

        await _validate_company_identifiers(db, data)
        new_raw = _save_workbook_with_row_change(old_raw, mode="append", cells=cells)
        try:
            _sheet, _header, parsed_rows = parse_coverage_workbook(new_raw)
            await _apply_parsed_rows(
                db, parsed_rows, actor if actor.role == UserRole.ADMIN else None,
                approve_new=(actor.role == UserRole.ADMIN),
                force_approve=(actor.role == UserRole.ADMIN),
            )
            target = await db.scalar(
                select(TargetCompany)
                .join(Company, TargetCompany.company_id == Company.id)
                .where(Company.legal_name.ilike(company_name))
            )
            if not target:
                raise ValueError("Coverage target was not created.")
            target.analyst_id = analyst.id if analyst else None
            target.active = True
            target.approval_status = (
                ApprovalStatus.APPROVED
                if actor.role == UserRole.ADMIN
                else ApprovalStatus.PENDING_AASHIL_APPROVAL
            )
            target.approval_by = actor.id if actor.role == UserRole.ADMIN else None
            target.approval_at = datetime.now(timezone.utc) if actor.role == UserRole.ADMIN else None
            await db.commit()
            _write_workbook_bytes_atomic(path, new_raw)
        except HTTPException:
            await db.rollback()
            path.write_bytes(old_raw)
            raise
        except Exception as exc:
            await db.rollback()
            path.write_bytes(old_raw)
            raise HTTPException(400, str(exc)) from exc

        global _WORKBOOK_SIGNATURE, _WORKBOOK_FAILED_SIGNATURE, _WORKBOOK_FAILED_ERROR
        _WORKBOOK_SIGNATURE = _workbook_signature(path)
        _WORKBOOK_FAILED_SIGNATURE = None
        _WORKBOOK_FAILED_ERROR = None

    row = (await db.execute(_master_query().where(TargetCompany.id == target.id))).first()
    return _company_payload(*row) if row else {"status": "created", "approval_status": target.approval_status.value}



class CoverageColumnCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    after: str | None = Field(default=None, max_length=120)


_PROTECTED_COVERAGE_COLUMNS = {"company name", "track mode", "analyst"}


def _header_row_and_headers(sheet) -> tuple[int, list[str]]:
    for row_idx in range(1, min(sheet.max_row, 100) + 1):
        values = [sheet.cell(row_idx, col).value for col in range(1, sheet.max_column + 1)]
        if any(_canonical_header(v) == "company name" for v in values if v is not None):
            headers = [
                str(v).strip() if v is not None and str(v).strip() else f"Column {i + 1}"
                for i, v in enumerate(values)
            ]
            return row_idx, headers
    raise ValueError("Could not find the Coverage List header row.")


def _copy_column_style(sheet, source_col: int, target_col: int, start_row: int, end_row: int) -> None:
    if source_col < 1 or source_col > sheet.max_column or target_col < 1:
        return
    from copy import copy as _copy

    source_letter = sheet.cell(start_row, source_col).column_letter
    target_letter = sheet.cell(start_row, target_col).column_letter
    source_dim = sheet.column_dimensions[source_letter]
    target_dim = sheet.column_dimensions[target_letter]
    for attr in ("width", "hidden", "bestFit", "outlineLevel", "collapsed"):
        try:
            setattr(target_dim, attr, getattr(source_dim, attr))
        except Exception:
            pass

    for row_idx in range(start_row, end_row + 1):
        source = sheet.cell(row_idx, source_col)
        target = sheet.cell(row_idx, target_col)
        if source.has_style:
            target._style = _copy(source._style)
        if source.number_format:
            target.number_format = source.number_format
        if source.font:
            target.font = _copy(source.font)
        if source.fill:
            target.fill = _copy(source.fill)
        if source.border:
            target.border = _copy(source.border)
        if source.alignment:
            target.alignment = _copy(source.alignment)
        if source.protection:
            target.protection = _copy(source.protection)


def _normalize_unique_column_name(sheet, header_row: int, requested: str) -> str:
    name = re.sub(r"\s+", " ", requested.replace("\n", " ").strip())
    if not name:
        raise HTTPException(400, "Column name is required.")
    if _norm_header(name) in {"row number", "#", "no", "number"}:
        raise HTTPException(400, "The row-number column is generated automatically.")
    existing = {
        _norm_header(sheet.cell(header_row, col).value)
        for col in range(1, sheet.max_column + 1)
        if sheet.cell(header_row, col).value not in (None, "")
    }
    if _norm_header(name) in existing:
        raise HTTPException(409, f"A Coverage List column named '{name}' already exists.")
    return name


@router.post("/columns")
async def add_coverage_column(
    data: CoverageColumnCreate,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    """Insert a real Excel column into the canonical Coverage List workbook."""
    del db  # Workbook schema mutation is persisted in Excel; no DB row mutation is needed.
    async with _WORKBOOK_SYNC_LOCK:
        path = coverage_workbook_path()
        if not path.exists():
            raise HTTPException(500, "Coverage List workbook does not exist.")
        old_raw = path.read_bytes()
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(old_raw), read_only=False, data_only=False)
        sheet = workbook.active
        header_row, headers = _header_row_and_headers(sheet)
        name = _normalize_unique_column_name(sheet, header_row, data.name)

        after_name = str(data.after or "").strip()
        target_col = sheet.max_column + 1
        if after_name:
            match_col = next(
                (col for col in range(1, sheet.max_column + 1)
                 if str(sheet.cell(header_row, col).value or "").strip().casefold() == after_name.casefold()),
                None,
            )
            if match_col is None:
                raise HTTPException(404, f"Column '{after_name}' was not found.")
            target_col = match_col + 1

        sheet.insert_cols(target_col, 1)
        # Copy the visual format from the neighboring column so the new column
        # behaves like an Excel-inserted column in the existing workbook.
        source_col = target_col - 1 if target_col > 1 else min(2, sheet.max_column)
        _copy_column_style(sheet, source_col, target_col, 1, sheet.max_row)
        sheet.cell(header_row, target_col).value = name
        for row_idx in range(header_row + 1, sheet.max_row + 1):
            if any(not _is_blank_cell(sheet.cell(row_idx, col).value) for col in range(1, sheet.max_column + 1) if col != target_col):
                sheet.cell(row_idx, target_col).value = "-"

        sheet.auto_filter.ref = sheet.dimensions
        out = io.BytesIO()
        workbook.save(out)
        _write_workbook_bytes_atomic(path, out.getvalue())

        global _WORKBOOK_SIGNATURE, _WORKBOOK_FAILED_SIGNATURE, _WORKBOOK_FAILED_ERROR
        _WORKBOOK_SIGNATURE = _workbook_signature(path)
        _WORKBOOK_FAILED_SIGNATURE = None
        _WORKBOOK_FAILED_ERROR = None
        return {"status": "column_added", "name": name, "version": f"{_WORKBOOK_SIGNATURE[0]}:{_WORKBOOK_SIGNATURE[1]}"}


@router.delete("/columns")
async def delete_coverage_column(
    name: str,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    """Delete a real Excel column from the canonical Coverage List workbook."""
    del db
    del admin
    async with _WORKBOOK_SYNC_LOCK:
        path = coverage_workbook_path()
        if not path.exists():
            raise HTTPException(500, "Coverage List workbook does not exist.")
        requested = re.sub(r"\s+", " ", str(name or "").replace("\n", " ").strip())
        if not requested:
            raise HTTPException(400, "Column name is required.")

        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(path.read_bytes()), read_only=False, data_only=False)
        sheet = workbook.active
        header_row, headers = _header_row_and_headers(sheet)
        match_col = next(
            (col for col in range(1, sheet.max_column + 1)
             if str(sheet.cell(header_row, col).value or "").strip().casefold() == requested.casefold()),
            None,
        )
        if match_col is None:
            raise HTTPException(404, f"Column '{requested}' was not found.")
        canonical = _canonical_header(sheet.cell(header_row, match_col).value)
        if canonical in _PROTECTED_COVERAGE_COLUMNS:
            raise HTTPException(400, f"'{sheet.cell(header_row, match_col).value}' is a required master Coverage List column and cannot be deleted.")

        sheet.delete_cols(match_col, 1)
        sheet.auto_filter.ref = sheet.dimensions
        out = io.BytesIO()
        workbook.save(out)
        _write_workbook_bytes_atomic(path, out.getvalue())

        global _WORKBOOK_SIGNATURE, _WORKBOOK_FAILED_SIGNATURE, _WORKBOOK_FAILED_ERROR
        _WORKBOOK_SIGNATURE = _workbook_signature(path)
        _WORKBOOK_FAILED_SIGNATURE = None
        _WORKBOOK_FAILED_ERROR = None
        return {"status": "column_deleted", "name": requested, "version": f"{_WORKBOOK_SIGNATURE[0]}:{_WORKBOOK_SIGNATURE[1]}"}


class CoverageAssignmentUpdate(BaseModel):
    track_mode: TrackMode
    analyst: str | None = None


@router.patch("/{target_id}/assignment")
async def update_coverage_assignment(
    target_id: int,
    data: CoverageAssignmentUpdate,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    """Update Tracker Mode (master priority) and Analyst for one row only."""
    async with _WORKBOOK_SYNC_LOCK:
        target = await db.get(TargetCompany, target_id)
        if not target:
            raise HTTPException(404, "Coverage List row not found.")
        company = await db.get(Company, target.company_id)
        if not company:
            raise HTTPException(404, "Company not found.")

        current_analyst = await db.get(User, target.analyst_id) if target.analyst_id else None
        analyst = None
        requested = _text(data.analyst)
        if requested and not is_dash_like(requested):
            candidate = await _resolve_analyst(db, requested)
            requested_key = candidate.username.casefold()
            # Only approved analyst usernames are selectable/new-assignment values.
            # A legacy admin-assigned user may remain untouched on an existing row.
            if requested_key not in ALLOWED_ANALYST_USERNAMES:
                if not current_analyst or current_analyst.username.casefold() != requested_key:
                    raise HTTPException(400, "Analyst must be ahmed, jainam, raj, romil, or Aashil (also accepted as Ashil).")
            analyst = candidate
        elif current_analyst and current_analyst.username.casefold() not in ALLOWED_ANALYST_USERNAMES:
            # Empty selection means keep the current legacy assignment rather than
            # silently converting it to unassigned.
            analyst = current_analyst

        path = coverage_workbook_path()
        if not path.exists():
            raise HTTPException(500, "Coverage List workbook does not exist.")
        old_raw = _normalize_workbook_bytes(path.read_bytes())
        row_number = _find_row_for_master(old_raw, company, target)
        if row_number is None:
            raise HTTPException(404, "Coverage List workbook row could not be located.")

        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(old_raw), read_only=False, data_only=False)
        sheet = workbook.active
        header_row = None
        headers: list[str] = []
        for row_idx in range(1, min(sheet.max_row, 100) + 1):
            values = [sheet.cell(row_idx, col).value for col in range(1, sheet.max_column + 1)]
            if any(_canonical_header(v) == "company name" for v in values if v is not None):
                header_row = row_idx
                headers = [str(v).strip() if v is not None and str(v).strip() else f"Column {i + 1}" for i, v in enumerate(values)]
                break
        if header_row is None:
            raise HTTPException(400, "Could not find the Coverage List header row.")

        track_idx = _header_index(headers, "track mode")
        analyst_idx = _header_index(headers, "analyst")
        if track_idx is None:
            raise HTTPException(400, "Coverage List workbook is missing the Tracker Mode column.")

        sheet.cell(row_number, track_idx + 1).value = data.track_mode.value
        if analyst_idx is not None:
            sheet.cell(row_number, analyst_idx + 1).value = analyst.username if analyst else "-"

        out = io.BytesIO()
        workbook.save(out)
        new_raw = out.getvalue()

        target.track_mode = data.track_mode
        target.analyst_id = analyst.id if analyst else None
        target.active = True
        target.approval_status = ApprovalStatus.APPROVED
        target.approval_by = admin.id
        target.approval_at = datetime.now(timezone.utc)
        try:
            await db.commit()
            _write_workbook_bytes_atomic(path, new_raw)
        except Exception:
            await db.rollback()
            raise

        global _WORKBOOK_SIGNATURE, _WORKBOOK_FAILED_SIGNATURE, _WORKBOOK_FAILED_ERROR
        _WORKBOOK_SIGNATURE = _workbook_signature(path)
        _WORKBOOK_FAILED_SIGNATURE = None
        _WORKBOOK_FAILED_ERROR = None

        return _company_payload(company, target, analyst)


@router.patch("/{target_id}")
async def update_coverage_company(
    target_id: int,
    data: CoverageCompanyUpdate,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    async with _WORKBOOK_SYNC_LOCK:
        target = await db.get(TargetCompany, target_id)
        if not target:
            raise HTTPException(404, "Coverage List row not found.")
        company = await db.get(Company, target.company_id)
        if not company:
            raise HTTPException(404, "Company not found.")
        path = coverage_workbook_path()
        if not path.exists():
            raise HTTPException(500, "Coverage List workbook does not exist.")
        old_raw = _normalize_workbook_bytes(path.read_bytes())
        _sheet, _header, columns, _rows = _workbook_grid(old_raw)
        headers = [column["label"] for column in columns]
        row_number = _find_row_for_master(old_raw, company, target)
        if row_number is None:
            raise HTTPException(404, "Coverage List workbook row could not be located.")
        current_values = await _grid_row_existing_values(old_raw, row_number)
        cells = _row_cells_from_inputs(headers, _legacy_or_dynamic_values(data, headers), existing=current_values)
        new_raw = _save_workbook_with_row_change(old_raw, mode="update", target_row=row_number, cells=cells)
        try:
            _sheet, _header, parsed_rows = parse_coverage_workbook(new_raw)
            result = await _apply_parsed_rows(db, parsed_rows, admin, approve_new=True, force_approve=True)
            await db.commit()
            _write_workbook_bytes_atomic(path, new_raw)
        except HTTPException:
            await db.rollback()
            path.write_bytes(old_raw)
            raise
        except Exception as exc:
            await db.rollback()
            path.write_bytes(old_raw)
            raise HTTPException(400, str(exc)) from exc

        global _WORKBOOK_SIGNATURE, _WORKBOOK_FAILED_SIGNATURE, _WORKBOOK_FAILED_ERROR
        _WORKBOOK_SIGNATURE = _workbook_signature(path)
        _WORKBOOK_FAILED_SIGNATURE = None
        _WORKBOOK_FAILED_ERROR = None
        return {"status": "updated", "target_id": target_id, **result}


@router.delete("/{target_id}")
async def delete_coverage_company(
    target_id: int,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    async with _WORKBOOK_SYNC_LOCK:
        target = await db.get(TargetCompany, target_id)
        if not target:
            raise HTTPException(404, "Coverage List row not found.")
        company = await db.get(Company, target.company_id)
        if not company:
            raise HTTPException(404, "Company not found.")
        path = coverage_workbook_path()
        if not path.exists():
            raise HTTPException(500, "Coverage List workbook does not exist.")
        old_raw = _normalize_workbook_bytes(path.read_bytes())
        row_number = _find_row_for_master(old_raw, company, target)
        if row_number is None:
            raise HTTPException(404, "Coverage List workbook row could not be located.")
        new_raw = _save_workbook_with_row_change(old_raw, mode="delete", target_row=row_number)
        try:
            _sheet, _header, parsed_rows = parse_coverage_workbook(new_raw)
            await db.delete(target)
            await db.flush()
            if parsed_rows:
                await _apply_parsed_rows(db, parsed_rows, admin)
            await db.commit()
            _write_workbook_bytes_atomic(path, new_raw)
        except HTTPException:
            await db.rollback()
            path.write_bytes(old_raw)
            raise
        except Exception as exc:
            await db.rollback()
            path.write_bytes(old_raw)
            raise HTTPException(400, str(exc)) from exc

        global _WORKBOOK_SIGNATURE, _WORKBOOK_FAILED_SIGNATURE, _WORKBOOK_FAILED_ERROR
        _WORKBOOK_SIGNATURE = _workbook_signature(path)
        _WORKBOOK_FAILED_SIGNATURE = None
        _WORKBOOK_FAILED_ERROR = None
        return {"status": "removed_from_coverage_list", "target_id": target_id, "company_id": company.id}

def _resolve_headers(row: tuple[Any, ...]) -> dict[str, int]:
    aliases = {
        "company": "company name",
        "company name": "company name",
        "legal name": "company name",
        "name": "company name",
        "nse": "nse ticker",
        "nse ticker": "nse ticker",
        "nse symbol": "nse ticker",
        "nse code": "nse ticker",
        "bse": "bse code",
        "bse code": "bse code",
        "bse codes": "bse code",
        "bse scrip code": "bse code",
        "bse scrip codes": "bse code",
        "isin": "isin",
        "market cap": "market cap",
        "market cap inr": "market cap",
        "market capitalization": "market cap",
        "market cap bucket": "market cap bucket",
        "market cap category": "market cap bucket",
        "bucket": "market cap bucket",
        "lt ht": "track mode",
        "ht lt po": "track mode",
        "ht lt po nt": "track mode",
        "track mode": "track mode",
        "tracker mode": "track mode",
        "tracker": "track mode",
        "track": "track mode",
        "analyst": "analyst",
        "analyst covering": "analyst",
        "covering analyst": "analyst",
        "date of start": "date of start",
        "start date": "date of start",
        "date of end": "date of end",
        "end date": "date of end",
    }
    normalized = {_norm_header(v): i for i, v in enumerate(row) if _norm_header(v)}
    return {aliases[key]: idx for key, idx in normalized.items() if key in aliases}


def parse_coverage_workbook(raw: bytes) -> tuple[str, int, list[dict[str, Any]]]:
    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError(f"Could not read Excel file: {exc}") from exc

    candidate: tuple[str, int, dict[str, int]] | None = None
    workbook_values: dict[str, list[tuple[Any, ...]]] = {}
    for sheet in workbook.worksheets:
        values = list(sheet.iter_rows(values_only=True))
        workbook_values[sheet.title] = values
        for idx, row in enumerate(values[:100]):
            resolved = _resolve_headers(row)
            if "company name" in resolved:
                candidate = (sheet.title, idx, resolved)
                break
        if candidate:
            break

    if not candidate:
        available = []
        for title, values in workbook_values.items():
            for idx, row in enumerate(values[:25]):
                normalized = [_norm_header(v) for v in row if _norm_header(v)]
                if normalized:
                    available.append(f"{title}!row {idx + 1}: {', '.join(normalized[:12])}")
        raise ValueError(
            "Could not find Coverage List headers. Expected a Company column. "
            f"Found: {' | '.join(available[:6]) or 'no usable headers'}"
        )

    sheet_name, header_row_index, resolved = candidate
    values = workbook_values[sheet_name]

    parsed: list[dict[str, Any]] = []
    errors: list[str] = []
    for excel_row, row in enumerate(values[header_row_index + 1 :], start=header_row_index + 2):
        if not any(not _is_blank_cell(v) for v in row):
            continue
        try:
            company_name = _normalize_company_text(row[resolved["company name"]])
            if not company_name or company_name in {"-", "—", "–"}:
                raise ValueError("Company is required")

            raw_track_value = (_text(row[resolved["track mode"]]) if "track mode" in resolved else None) or "NT"
            track_mode = _track(raw_track_value)

            analyst_name = None
            if "analyst" in resolved:
                candidate_analyst = _text(row[resolved["analyst"]])
                if candidate_analyst and candidate_analyst not in {"-", "—", "–"}:
                    analyst_name = candidate_analyst

            nse_value = row[resolved["nse ticker"]] if "nse ticker" in resolved else None
            bse_value = row[resolved["bse code"]] if "bse code" in resolved else None
            bse_marker = str(bse_value or "").strip().casefold()
            bse_code_unlisted = bse_marker in {
                "n/a", "na", "not listed", "not bse listed", "not listed on bse",
                "no bse", "no bse listing", "unlisted on bse",
            }
            nse_value, bse_value = _normalize_workbook_identifiers(nse_value, bse_value)

            parsed.append({
                "excel_row": excel_row,
                "company_name": company_name,
                "nse_symbol": _text(nse_value),
                "bse_code": _text(bse_value),
                "bse_code_unlisted": bse_code_unlisted,
                "isin": _text(row[resolved["isin"]]) if "isin" in resolved and not _is_blank_cell(row[resolved["isin"]]) else None,
                "market_cap": _parse_market_cap(row[resolved["market cap"]]) if "market cap" in resolved else None,
                "market_cap_bucket": _bucket(row[resolved["market cap bucket"]]) if "market cap bucket" in resolved else None,
                "track_mode": track_mode,
                "analyst_name": analyst_name,
                "start_date": _parse_date(row[resolved["date of start"]]) if "date of start" in resolved else None,
                "end_date": _parse_date(row[resolved["date of end"]]) if "date of end" in resolved else None,
                # Keep workbook header names for legacy date/track fields, and also
                # expose the internal model attribute names used by safe_identifier.
                # Without these aliases, a perfectly valid "BSE Code" header is
                # stored as "bse code" here while safe_identifier checks for
                # "bse_code"; the importer then silently skips every BSE code.
                "present_fields": (
                    set(resolved.keys())
                    | ({"nse_symbol"} if "nse ticker" in resolved else set())
                    | ({"bse_code"} if "bse code" in resolved else set())
                ),
            })
        except Exception as exc:
            errors.append(f"Row {excel_row}: {exc}")

    if errors:
        raise ValueError("Coverage List validation failed: " + " | ".join(errors[:50]))
    if not parsed:
        raise ValueError("No company rows found in the Excel file.")

    seen_names: dict[str, str] = {}
    seen_identifiers: dict[tuple[str, str], str] = {}
    for row in parsed:
        normalized_name = _normalize_company_text(row["company_name"])
        name_key = normalized_name.casefold() if normalized_name else ""
        previous_name = seen_names.get(name_key)
        if previous_name:
            raise ValueError(f"Duplicate company in workbook: {previous_name} / {row['company_name']}")
        seen_names[name_key] = row["company_name"]
        for key_name, value in (("NSE", row.get("nse_symbol")), ("BSE", row.get("bse_code")), ("ISIN", row.get("isin"))):
            if not value:
                continue
            key = (key_name, str(value).casefold())
            previous = seen_identifiers.get(key)
            if previous and _normalize_company_text(previous).casefold() != name_key:
                raise ValueError(
                    f"Workbook identifier conflict: {key_name} '{value}' is assigned to both '{previous}' and '{row['company_name']}'."
                )
            seen_identifiers[key] = row["company_name"]

    return sheet_name, header_row_index + 1, parsed

@router.post("/{target_id}/approve")
async def approve_coverage_company(
    target_id: int,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    target = await db.get(TargetCompany, target_id)
    if not target:
        raise HTTPException(404, "Coverage List row not found.")
    target.approval_status = ApprovalStatus.APPROVED
    target.active = True
    target.approval_by = admin.id
    target.approval_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": target.id, "approval_status": target.approval_status.value}


@router.post("/{target_id}/reject")
async def reject_coverage_company(
    target_id: int,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    target = await db.get(TargetCompany, target_id)
    if not target:
        raise HTTPException(404, "Coverage List row not found.")
    target.approval_status = ApprovalStatus.REJECTED
    target.active = False
    target.approval_by = admin.id
    target.approval_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": target.id, "approval_status": target.approval_status.value}


@router.get("/health")
async def coverage_list_health(
    db=Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Report Coverage List synchronization and NSE-symbol readiness without mutating the workbook."""
    path = coverage_workbook_path()
    signature = _workbook_signature(path) if path.exists() else None
    rows = (
        await db.execute(
            _master_query().where(
                TargetCompany.active.is_(True),
                TargetCompany.approval_status == ApprovalStatus.APPROVED,
            )
        )
    ).all()
    missing_symbols = sorted(
        [company.legal_name for company, _target, _analyst in rows if not _normalize_identifier(company.nse_symbol)],
        key=str.casefold,
    )
    return {
        "workbook_exists": path.exists(),
        "workbook_path": str(path),
        "workbook_signature": f"{signature[0]}:{signature[1]}" if signature else None,
        "last_sync_failed": _WORKBOOK_FAILED_SIGNATURE is not None,
        "last_failed_signature": f"{_WORKBOOK_FAILED_SIGNATURE[0]}:{_WORKBOOK_FAILED_SIGNATURE[1]}" if _WORKBOOK_FAILED_SIGNATURE else None,
        "last_error": _WORKBOOK_FAILED_ERROR,
        "warnings": list(_LAST_SYNC_WARNINGS[-100:]),
        "active_approved_companies": len(rows),
        "missing_nse_symbol_count": len(missing_symbols),
        "missing_nse_symbols": missing_symbols[:200],
        "nse_ready": bool(rows) and not missing_symbols and _WORKBOOK_FAILED_SIGNATURE is None,
    }


@router.get("/version")
async def coverage_list_version(_: User = Depends(get_current_user)):
    path = coverage_workbook_path()
    if not path.exists():
        return {"exists": False, "version": None}
    signature = _workbook_signature(path)
    return {"exists": True, "version": f"{signature[0]}:{signature[1]}"}


@router.get("/grid")
async def coverage_list_grid(
    db=Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await _grid_payload(db)
    except (ValueError, HTTPException) as exc:
        detail = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        raise HTTPException(400, f"Coverage List workbook is invalid: {detail}") from exc

@router.post("/upload")
async def upload_coverage_list_excel(
    file: UploadFile = File(...),
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    """Replace the canonical workbook while preserving its columns and formatting."""
    name = (file.filename or "").lower()
    if not name.endswith(".xlsx"):
        raise HTTPException(400, "Please upload an .xlsx Excel file.")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Excel file is too large. Maximum size is 5 MB.")

    path = coverage_workbook_path()
    async with _WORKBOOK_SYNC_LOCK:
        old_raw = path.read_bytes() if path.exists() else None
        try:
            normalized_raw = _sync_workbook_raw(raw)
            sheet_name, header_row, parsed_rows = parse_coverage_workbook(normalized_raw)
            result = await _apply_parsed_rows(db, parsed_rows, admin, approve_new=True, force_approve=True)
            await db.commit()
            _write_workbook_bytes_atomic(path, normalized_raw)
        except HTTPException:
            await db.rollback()
            if old_raw is not None:
                path.write_bytes(old_raw)
            raise
        except Exception as exc:
            await db.rollback()
            if old_raw is not None:
                path.write_bytes(old_raw)
            raise HTTPException(400, str(exc)) from exc

        global _WORKBOOK_SIGNATURE, _WORKBOOK_FAILED_SIGNATURE, _WORKBOOK_FAILED_ERROR
        _WORKBOOK_SIGNATURE = _workbook_signature(path)
        _WORKBOOK_FAILED_SIGNATURE = None
        _WORKBOOK_FAILED_ERROR = None
        return {
            "status": "imported",
            "filename": file.filename,
            "sheet": sheet_name,
            "header_row": header_row,
            **result,
            "workbook_path": str(path),
        }


@router.get("/export")
async def export_coverage_list_excel(
    db=Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Return the canonical workbook, including every current/future column."""
    try:
        await sync_coverage_workbook_if_changed(db)
        path = coverage_workbook_path()
        if not path.exists():
            raise HTTPException(404, "Coverage List workbook does not exist.")
        raw = _normalize_workbook_bytes(path.read_bytes())
        return StreamingResponse(
            io.BytesIO(raw),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="Coverage_List.xlsx"'},
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400, f"Coverage List workbook is invalid: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"Could not export Coverage List Excel: {exc}") from exc
