from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sqlalchemy import desc, select, update

from infrastructure.config.settings import get_settings
from ii_platform.database.connection import get_sessionmaker
from ii_platform.storage.factory import get_object_storage

from .comparison import _latest_previous_file, build_comparison
from .models import PMSMonthlyFile, PMSMonthlyHolding, PMSRegistry, PortfolioUploadBatch
from .normalization import is_equity_stock, load_company_maps, load_sector_map, normalized_holding
from .parsers import (
    canonical_pms_name, detect_file_format, filename_key, identify_pms_name, parse_file, pms_identity_key,
)
from .report import build_report_bytes

LOGGER = logging.getLogger("portfolio-comparison")
settings = get_settings()

MAX_FILE_BYTES = int(getattr(settings, "portfolio_max_file_bytes", 25 * 1024 * 1024))
MAX_ARCHIVE_BYTES = int(getattr(settings, "portfolio_max_archive_bytes", 120 * 1024 * 1024))
MAX_ARCHIVE_UNCOMPRESSED_BYTES = int(getattr(settings, "portfolio_max_archive_uncompressed_bytes", 300 * 1024 * 1024))
MAX_ARCHIVE_FILES = int(getattr(settings, "portfolio_max_archive_files", 100))
ALLOWED_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm", ".pdf"}


def safe_filename(filename: str) -> str:
    name = Path(filename or "portfolio.zip").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name[:180] or "portfolio.zip"


def validate_month(value: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise ValueError("comparison_month must be YYYY-MM")
    return value


def _archive_member_name(raw_name: str) -> str:
    raw = str(raw_name or "").replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe ZIP path rejected: {raw_name}")
    name = path.name
    if not name:
        raise ValueError(f"Invalid ZIP member: {raw_name}")
    return safe_filename(name)


def _extract_archive(content: bytes, temp_dir: Path) -> list[dict]:
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"ZIP exceeds the {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB archive limit.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("The uploaded file is not a valid ZIP archive.") from exc

    extracted: list[dict] = []
    seen_paths: set[str] = set()
    total_uncompressed = 0
    with archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        if len(members) > MAX_ARCHIVE_FILES:
            raise ValueError(f"ZIP contains too many files. Maximum supported is {MAX_ARCHIVE_FILES}.")
        for member in members:
            raw_name = member.filename
            normalized_archive_path = str(raw_name).replace("\\", "/")
            if normalized_archive_path.startswith("__MACOSX/") or PurePosixPath(normalized_archive_path).name.startswith("."):
                continue
            suffix = Path(normalized_archive_path).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                continue
            if member.flag_bits & 0x1:
                raise ValueError(f"Encrypted ZIP member is not supported: {raw_name}")
            if member.file_size > MAX_FILE_BYTES:
                raise ValueError(f"{Path(raw_name).name}: file exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB file limit.")
            total_uncompressed += member.file_size
            if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError("ZIP uncompressed size exceeds the configured safety limit.")

            clean_name = _archive_member_name(normalized_archive_path)
            token = normalized_archive_path.casefold()
            if token in seen_paths:
                raise ValueError(f"Duplicate file path in ZIP: {normalized_archive_path}")
            seen_paths.add(token)

            # Files are extracted to unique temporary names, while the original
            # relative ZIP path is retained as an optional PMS identity hint.
            destination = temp_dir / f"{len(extracted):03d}_{clean_name}"
            with archive.open(member, "r") as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            extracted.append(
                {
                    "filename": clean_name,
                    "archive_path": normalized_archive_path,
                    "path": destination,
                    "format": detect_file_format(clean_name),
                    "filename_key": filename_key(clean_name),
                }
            )

    if not extracted:
        raise ValueError("ZIP contains no supported portfolio files (.pdf or Excel workbook).")
    return extracted


async def create_upload_batch(
    db,
    *,
    user_id: int | None,
    comparison_month: str,
    archive_filename: str,
    archive_bytes: bytes,
    upload_type: str = "monthly",
    excluded_pms: list[str] | None = None,
) -> str:
    comparison_month = validate_month(comparison_month)
    archive_name = safe_filename(archive_filename)
    if not archive_name.lower().endswith(".zip"):
        raise ValueError("Portfolio Manager accepts one monthly ZIP file.")
    if not archive_bytes:
        raise ValueError("The uploaded ZIP is empty.")
    if upload_type not in {"monthly", "baseline"}:
        raise ValueError("upload_type must be monthly or baseline")

    batch_id = str(uuid4())
    digest = hashlib.sha256(archive_bytes).hexdigest()
    storage_key = f"portfolio-comparison/archives/{batch_id}/{digest[:12]}-{archive_name}"
    await get_object_storage().put(storage_key, archive_bytes, "application/zip")

    batch = PortfolioUploadBatch(
        id=batch_id,
        requested_by=user_id,
        comparison_month=comparison_month,
        status="queued",
        source_count=0,
        archive_filename=archive_name,
        archive_object_key=storage_key,
        upload_type=upload_type,
        excluded_pms=[str(name).strip() for name in (excluded_pms or []) if str(name).strip()],
    )
    db.add(batch)
    await db.commit()
    return batch_id


async def _claim_batch(db) -> PortfolioUploadBatch | None:
    batch = await db.scalar(
        select(PortfolioUploadBatch)
        .where(PortfolioUploadBatch.status == "queued")
        .order_by(PortfolioUploadBatch.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if batch is None:
        return None
    now = datetime.now(timezone.utc)
    batch.status = "processing"
    batch.started_at = now
    batch.completed_at = None
    await db.commit()
    LOGGER.info(
        "PORTFOLIO_BATCH_CLAIMED batch=%s month=%s queue_wait_seconds=%.1f",
        batch.id,
        batch.comparison_month,
        _seconds_between(batch.created_at, now) or 0.0,
    )
    return batch


async def process_one_queued_batch() -> bool:
    async with get_sessionmaker()() as db:
        batch = await _claim_batch(db)
        if batch is None:
            return False
        batch_id = batch.id
    await process_batch(batch_id)
    return True


async def portfolio_queue_loop(stop_event: asyncio.Event, *, owner: str = "worker") -> None:
    LOGGER.info("PORTFOLIO_QUEUE_WORKER_STARTED owner=%s poll_seconds=%s", owner, settings.worker_queue_poll_seconds)
    while not stop_event.is_set():
        try:
            did_work = await process_one_queued_batch()
            if did_work:
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("PORTFOLIO_QUEUE_LOOP_ERROR owner=%s", owner)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(2.0, float(settings.worker_queue_poll_seconds)))
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
    LOGGER.info("PORTFOLIO_QUEUE_WORKER_STOPPED owner=%s", owner)


async def _load_reference_maps() -> tuple[dict, dict, dict]:
    async with get_sessionmaker()() as db:
        company_by_isin, company_by_name = await load_company_maps(db)
    return company_by_isin, company_by_name, load_sector_map()


def _month_is_at_least(current: str, other: str | None) -> bool:
    return not other or current >= other


def _pms_name_key(name: str) -> str:
    # Use the same alias-aware key as filename/content identification.  This is
    # what makes "91 Cap", "Ninetyone Capital" and "91 Capital" one PMS.
    return pms_identity_key(name)


async def _canonical_registry_lookup(db, registry_rows: list[PMSRegistry]) -> dict[str, PMSRegistry]:
    """Canonicalize legacy PMS labels and merge registry aliases without losing history.

    Older uploads may have created separate registry rows for aliases such as
    "Ninetyone Capital" and "91 Capital".  Their monthly files are moved to one
    canonical registry row; file IDs and their holdings remain unchanged.
    """
    groups: dict[str, list[PMSRegistry]] = {}
    for row in registry_rows:
        key = _pms_name_key(row.pms_name or row.normalized_name or "")
        if key:
            groups.setdefault(key, []).append(row)

    result: dict[str, PMSRegistry] = {}
    for key, rows in groups.items():
        canonical_name = canonical_pms_name(rows[0].pms_name or rows[0].normalized_name or key)
        # Prefer the row already carrying the canonical display name.  Otherwise
        # retain the earliest registry record so first-seen history is stable.
        primary = min(
            rows,
            key=lambda row: (
                (row.pms_name or "").casefold() != canonical_name.casefold(),
                row.first_seen_month or "9999-99",
                row.id,
            ),
        )

        if len(rows) > 1:
            registry_ids = [row.id for row in rows]
            files = list((await db.scalars(
                select(PMSMonthlyFile)
                .where(PMSMonthlyFile.pms_id.in_(registry_ids))
                .order_by(PMSMonthlyFile.comparison_month, PMSMonthlyFile.created_at, PMSMonthlyFile.id)
            )).all())

            # Move all files to a temporary unique version range first. This avoids
            # the (pms_id, month, version) unique constraint while alias rows merge.
            for monthly_file in files:
                monthly_file.pms_id = primary.id
                monthly_file.version = 20_000_000 + monthly_file.id
            await db.flush()

            by_month: dict[str, list[PMSMonthlyFile]] = {}
            for monthly_file in files:
                by_month.setdefault(monthly_file.comparison_month, []).append(monthly_file)
            for month_files in by_month.values():
                processed = [item for item in month_files if item.status == "processed"]
                current = max(
                    processed,
                    key=lambda item: (bool(item.is_current), item.created_at, item.id),
                    default=None,
                )
                for version, monthly_file in enumerate(
                    sorted(month_files, key=lambda item: (item.created_at, item.id)), 1
                ):
                    monthly_file.version = version
                    monthly_file.is_current = monthly_file is current
            await db.flush()

            first_seen = min((row.first_seen_month for row in rows if row.first_seen_month), default=None)
            last_seen = max((row.last_seen_month for row in rows if row.last_seen_month), default=None)
            last_processed = max((row.last_processed_month for row in rows if row.last_processed_month), default=None)
            if first_seen:
                primary.first_seen_month = first_seen
            if last_seen:
                primary.last_seen_month = last_seen
            if last_processed:
                primary.last_processed_month = last_processed
            if files:
                latest_processed = max(
                    (item for item in files if item.status == "processed"),
                    key=lambda item: (item.comparison_month, item.created_at, item.id),
                    default=None,
                )
                if latest_processed:
                    primary.format = latest_processed.format
                    primary.last_filename = latest_processed.filename
                    primary.parser_key = latest_processed.parser_key
                    primary.status = "ACTIVE"
                    primary.error_message = None

            for alias_row in rows:
                if alias_row.id != primary.id:
                    await db.delete(alias_row)
            await db.flush()

        primary.pms_name = canonical_name
        primary.normalized_name = key
        result[key] = primary

    await db.flush()
    return result


async def _store_monthly_file(
    db,
    *,
    batch: PortfolioUploadBatch,
    pms: PMSRegistry,
    comparison_month: str,
    filename: str,
    filename_key_value: str,
    format_value: str,
    parser_key: str | None,
    content_hash: str,
    rows: list[dict] | None,
    status: str,
    previous_file_id: int | None = None,
    error_message: str | None = None,
    make_current: bool = True,
) -> PMSMonthlyFile:
    if make_current:
        await db.execute(
            update(PMSMonthlyFile)
            .where(
                PMSMonthlyFile.pms_id == pms.id,
                PMSMonthlyFile.comparison_month == comparison_month,
                PMSMonthlyFile.is_current.is_(True),
            )
            .values(is_current=False)
        )
    last_version = await db.scalar(
        select(PMSMonthlyFile.version)
        .where(PMSMonthlyFile.pms_id == pms.id, PMSMonthlyFile.comparison_month == comparison_month)
        .order_by(desc(PMSMonthlyFile.version))
        .limit(1)
    )
    version = int(last_version or 0) + 1
    monthly_file = PMSMonthlyFile(
        pms_id=pms.id,
        batch_id=batch.id,
        comparison_month=comparison_month,
        filename=filename,
        filename_key=filename_key_value,
        format=format_value,
        object_key=batch.archive_object_key or "",
        content_hash=content_hash,
        version=version,
        is_current=make_current,
        status=status,
        parser_key=parser_key,
        parsed_rows=len(rows or []),
        previous_file_id=previous_file_id,
        error_message=error_message,
    )
    db.add(monthly_file)
    await db.flush()

    for row in rows or []:
        db.add(
            PMSMonthlyHolding(
                monthly_file_id=monthly_file.id,
                isin=row.get("isin"),
                security_name=(row.get("security_name") or "Unknown Security")[:255],
                normalized_security_name=(row.get("normalized_security_name") or "UNKNOWN SECURITY")[:255],
                quantity=float(row.get("quantity") or 0.0),
                measurement_type=row.get("measurement_type") or "QUANTITY",
                average_cost=row.get("average_cost"),
                current_price=row.get("current_price"),
                investment_value=row.get("investment_value"),
                current_value=row.get("current_value"),
                pnl=row.get("pnl"),
                pnl_pct=row.get("pnl_pct"),
                sector=(row.get("sector") or "Unmapped")[:160],
                company_id=row.get("company_id"),
                identity_confidence=float(row.get("identity_confidence") or 0.0),
                source_reference=(row.get("source_reference") or "")[:500],
                raw_row=row.get("raw_row") or {},
            )
        )
    return monthly_file


async def process_batch(batch_id: str) -> dict:
    storage = get_object_storage()
    started_mono = asyncio.get_running_loop().time()
    parse_errors: list[dict] = []
    try:
        LOGGER.info("PORTFOLIO_BATCH_START batch=%s", batch_id)
        async with get_sessionmaker()() as db:
            batch = await db.get(PortfolioUploadBatch, batch_id)
            if batch is None:
                return {"status": "not_found", "batch_id": batch_id}
            if not batch.archive_object_key:
                raise ValueError("Batch does not contain a monthly ZIP archive.")
            archive_key = batch.archive_object_key
            month = validate_month(batch.comparison_month)
            upload_type = batch.upload_type or "monthly"
            excluded_pms = [str(name).strip() for name in (batch.excluded_pms or []) if str(name).strip()]
            excluded_pms_keys = {_pms_name_key(name) for name in excluded_pms}

        archive_bytes = await storage.get(archive_key)
        company_by_isin, company_by_name, sector_map = await _load_reference_maps()

        with tempfile.TemporaryDirectory(prefix="portfolio-pms-") as temp_root:
            extracted = _extract_archive(archive_bytes, Path(temp_root))
            parsed_items: list[dict] = []
            seen_pms_keys: dict[str, str] = {}

            for item in extracted:
                pms_name = identify_pms_name(
                    item["filename"], item["path"], archive_path=item.get("archive_path")
                )
                pms_key = _pms_name_key(pms_name)
                item["pms_name"] = pms_name
                item["pms_key"] = pms_key
                if pms_key in excluded_pms_keys:
                    seen_pms_keys.setdefault(pms_key, item["filename"])
                    LOGGER.info("PORTFOLIO_PMS_EXCLUDED batch=%s pms=%s filename=%s", batch_id, pms_name, item["filename"])
                    continue
                if pms_key in seen_pms_keys:
                    parse_errors.append({
                        "pms_name": pms_name,
                        "pms_key": pms_key,
                        "filename": item["filename"],
                        "format": item["format"],
                        "error": f"Duplicate PMS identity in ZIP. Conflicts with {seen_pms_keys[pms_key]}.",
                    })
                    continue
                seen_pms_keys[pms_key] = item["filename"]
                try:
                    parser_key, parsed = parse_file(str(item["path"]), pms_name=pms_name)
                    if not parsed:
                        raise ValueError("No security-level rows were extracted.")
                    stock_rows = [row for row in parsed if is_equity_stock(row.security_name, row.raw_row)]
                    normalized_rows = [
                        normalized_holding(
                            row,
                            company_by_isin=company_by_isin,
                            company_by_name=company_by_name,
                            sector_map=sector_map,
                        )
                        for row in stock_rows
                    ]
                    parsed_items.append({
                        **item,
                        "pms_name": pms_name,
                        "pms_key": pms_key,
                        "parser_key": parser_key,
                        "rows": normalized_rows,
                    })
                    LOGGER.info(
                        "PORTFOLIO_COLUMNS_DETECTED batch=%s pms=%s format=%s parser=%s rows=%s",
                        batch_id, pms_name, item["format"], parser_key, len(normalized_rows),
                    )
                except Exception as exc:
                    LOGGER.exception("Portfolio parse failed batch=%s file=%s", batch_id, item["filename"])
                    parse_errors.append({
                        "pms_name": pms_name,
                        "pms_key": pms_key,
                        "filename": item["filename"],
                        "format": item["format"],
                        "error": str(exc)[:4000],
                    })

            async with get_sessionmaker()() as db:
                batch = await db.get(PortfolioUploadBatch, batch_id)
                if batch is None:
                    return {"status": "not_found", "batch_id": batch_id}

                registry_rows = (await db.scalars(select(PMSRegistry))).all()
                registry_by_key = await _canonical_registry_lookup(db, registry_rows)
                successful_pms: set[str] = set()
                error_pms: set[str] = set()
                row_count = 0

                # Learn new PMS identities first so the database IDs exist before
                # monthly-file/holding rows are written.
                processable_extracted = [item for item in extracted if item["pms_key"] not in excluded_pms_keys]
                for item in processable_extracted:
                    pms_name = item["pms_name"]
                    pms_key = item["pms_key"]
                    pms = registry_by_key.get(pms_key)
                    if pms is None:
                        pms = PMSRegistry(
                            pms_name=pms_name,
                            normalized_name=pms_key,
                            format=item["format"],
                            status="ACTIVE",
                            first_seen_month=month,
                            last_seen_month=month,
                            last_filename=item["filename"],
                        )
                        db.add(pms)
                        registry_by_key[pms_key] = pms
                    elif _month_is_at_least(month, pms.last_seen_month):
                        pms.pms_name = canonical_pms_name(pms_name)
                        pms.normalized_name = pms_key
                        pms.format = item["format"]
                        pms.last_seen_month = month
                        pms.last_filename = item["filename"]
                await db.flush()

                for item in parsed_items:
                    pms = registry_by_key[item["pms_key"]]
                    previous_file = await _latest_previous_file(db, pms.id, month)
                    content_hash = hashlib.sha256(item["path"].read_bytes()).hexdigest()
                    await _store_monthly_file(
                        db,
                        batch=batch,
                        pms=pms,
                        comparison_month=month,
                        filename=item["filename"],
                        filename_key_value=item["filename_key"],
                        format_value=item["format"],
                        parser_key=item["parser_key"],
                        content_hash=content_hash,
                        rows=item["rows"],
                        status="processed",
                        previous_file_id=previous_file.id if previous_file else None,
                        make_current=True,
                    )
                    if _month_is_at_least(month, pms.last_seen_month):
                        pms.status = "ACTIVE"
                        pms.error_message = None
                    if _month_is_at_least(month, pms.last_processed_month):
                        pms.last_processed_month = month
                        pms.parser_key = item["parser_key"]
                    successful_pms.add(item["pms_key"])
                    row_count += len(item["rows"])

                for error in parse_errors:
                    pms_key = error.get("pms_key") or _pms_name_key(error["pms_name"])
                    pms = registry_by_key[pms_key]
                    if _month_is_at_least(month, pms.last_seen_month):
                        pms.status = "ERROR"
                        pms.error_message = error["error"]
                        pms.last_seen_month = month
                        pms.last_filename = error["filename"]
                    error_pms.add(pms_key)
                    extracted_item = next((item for item in extracted if item["filename"] == error["filename"]), None)
                    if extracted_item:
                        # Failed replacements stay non-current so a valid historical
                        # version remains the baseline until the new file is fixed.
                        await _store_monthly_file(
                            db,
                            batch=batch,
                            pms=pms,
                            comparison_month=month,
                            filename=error["filename"],
                            filename_key_value=extracted_item["filename_key"],
                            format_value=extracted_item["format"],
                            parser_key=None,
                            content_hash=hashlib.sha256(extracted_item["path"].read_bytes()).hexdigest(),
                            rows=None,
                            status="failed",
                            error_message=error["error"],
                            make_current=False,
                        )

                detected_pms_keys = set(seen_pms_keys)
                observed_pms_keys = detected_pms_keys | excluded_pms_keys
                for pms in registry_by_key.values():
                    if pms.normalized_name not in observed_pms_keys and _month_is_at_least(month, pms.last_seen_month):
                        pms.status = "INACTIVE"

                batch.source_count = len(extracted)
                batch.parsed_rows = row_count
                batch.pms_count = len(detected_pms_keys)
                batch.active_pms_count = len(successful_pms)
                batch.inactive_pms_count = sum(1 for pms in registry_by_key.values() if pms.status == "INACTIVE")
                batch.error_count = len(error_pms)
                batch.snapshot_count = len(successful_pms)
                batch.error_message = None if not parse_errors else "; ".join(
                    f"{item['pms_name']}: {item['error']}" for item in parse_errors[:20]
                )
                batch.status = "processing"
                batch.completed_at = None
                await db.commit()

        if upload_type == "baseline":
            async with get_sessionmaker()() as db:
                batch = await db.get(PortfolioUploadBatch, batch_id)
                if batch is not None:
                    batch.status = "completed_with_errors" if batch.error_count else "completed"
                    batch.completed_at = datetime.now(timezone.utc)
                    await db.commit()
                    result = {
                        "status": batch.status,
                        "batch_id": batch.id,
                        "comparison_month": batch.comparison_month,
                        "pms_count": batch.pms_count,
                        "active_pms_count": batch.active_pms_count,
                        "inactive_pms_count": batch.inactive_pms_count,
                        "error_count": batch.error_count,
                    }
            LOGGER.info(
                "PORTFOLIO_BATCH_COMPLETE batch=%s status=%s elapsed_seconds=%.1f",
                batch_id,
                result["status"],
                asyncio.get_running_loop().time() - started_mono,
            )
            return result

        async with get_sessionmaker()() as db:
            report_model = await build_comparison(db, month, excluded_pms=excluded_pms)
        report_bytes = build_report_bytes(report_model)
        report_hash = hashlib.sha256(report_bytes).hexdigest()
        report_key = f"portfolio-comparison/reports/{batch_id}/{report_hash[:12]}-portfolio-{month}.xlsx"
        json_key = report_key.rsplit(".", 1)[0] + ".json"
        await storage.put(
            report_key,
            report_bytes,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        await storage.put(
            json_key,
            json.dumps(report_model, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )

        async with get_sessionmaker()() as db:
            batch = await db.get(PortfolioUploadBatch, batch_id)
            if batch is None:
                return {"status": "not_found", "batch_id": batch_id}
            batch.report_key = report_key
            batch.status = "completed_with_errors" if batch.error_count else "completed"
            batch.completed_at = datetime.now(timezone.utc)
            await db.commit()
            elapsed = asyncio.get_running_loop().time() - started_mono
            LOGGER.info(
                "PORTFOLIO_BATCH_COMPLETE batch=%s status=%s pms=%s rows=%s elapsed_seconds=%.1f",
                batch.id,
                batch.status,
                batch.pms_count,
                batch.parsed_rows,
                elapsed,
            )
            return {
                "status": batch.status,
                "batch_id": batch.id,
                "comparison_month": batch.comparison_month,
                "pms_count": batch.pms_count,
                "active_pms_count": batch.active_pms_count,
                "inactive_pms_count": batch.inactive_pms_count,
                "error_count": batch.error_count,
                "report_available": True,
            }

    except Exception as exc:
        LOGGER.exception("Portfolio batch failed batch=%s", batch_id)
        async with get_sessionmaker()() as db:
            batch = await db.get(PortfolioUploadBatch, batch_id)
            if batch is not None:
                batch.status = "failed"
                batch.error_message = str(exc)[:4000]
                batch.completed_at = datetime.now(timezone.utc)
                await db.commit()
        LOGGER.error(
            "PORTFOLIO_BATCH_FAILED batch=%s elapsed_seconds=%.1f error=%s",
            batch_id,
            asyncio.get_running_loop().time() - started_mono,
            str(exc)[:1000],
        )
        return {"status": "failed", "batch_id": batch_id, "error": str(exc)[:4000]}


def _seconds_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end is None:
        end = datetime.now(timezone.utc)
    elif end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - start).total_seconds())


def job_payload(batch: PortfolioUploadBatch) -> dict:
    queue_wait = _seconds_between(batch.created_at, batch.started_at)
    processing = _seconds_between(batch.started_at, batch.completed_at) if batch.started_at else None
    total = _seconds_between(batch.created_at, batch.completed_at)
    if total is None:
        total = _seconds_between(batch.created_at, None)
    return {
        "id": batch.id,
        "comparison_month": batch.comparison_month,
        "status": batch.status,
        "source_count": batch.source_count,
        "parsed_rows": batch.parsed_rows,
        "snapshot_count": batch.snapshot_count,
        "report_available": bool(batch.report_key),
        "error": batch.error_message,
        "archive_filename": batch.archive_filename,
        "upload_type": batch.upload_type,
        "excluded_pms": list(batch.excluded_pms or []),
        "pms_count": batch.pms_count,
        "active_pms_count": batch.active_pms_count,
        "inactive_pms_count": batch.inactive_pms_count,
        "error_count": batch.error_count,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "started_at": batch.started_at.isoformat() if batch.started_at else None,
        "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
        "queue_wait_seconds": round(queue_wait, 1) if queue_wait is not None else None,
        "processing_elapsed_seconds": round(processing, 1) if processing is not None else None,
        "total_elapsed_seconds": round(total, 1) if total is not None else None,
    }
