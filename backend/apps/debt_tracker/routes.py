from __future__ import annotations

import io
import logging
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from functools import partial
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.exc import OperationalError

from apps.auth import get_current_user
from ii_platform.database.connection import get_sessionmaker
from ii_platform.database.models import User
from ii_platform.storage.factory import get_object_storage

from .models import DebtTrackerJob, DebtTrackerMapping, DebtTrackerMappingHistory
from .service import (MAX_BROKER_FILES, MAX_TOTAL_BYTES, run_generation, safe_filename,
                      serialize_job, validate_upload, detect_holdings_report_date, save_edited_base)
from .core.normalization import normalize_text, normalize_fund_name
from .core.errors import DebtTrackerError

LOGGER = logging.getLogger("debt-tracker")
router = APIRouter(prefix="/debt-tracker", tags=["Debt Tracker"])


class MappingApproval(BaseModel):
    mapping_type: str = Field(pattern="^(fund_alias|issuer_group|security_group)$")
    source_key: str = Field(min_length=1, max_length=512)
    target_key: str = Field(min_length=1, max_length=512)


class BaseEditPayload(BaseModel):
    rows: list[dict] = Field(min_length=1, max_length=100000)


def _mapping_normalized(mapping_type: str, value: str) -> str:
    if mapping_type == "fund_alias":
        return normalize_fund_name(value)
    return normalize_text(value)


@router.post("/generate")
async def generate_debt_tracker(
    mapping_file: UploadFile = File(...),
    holdings_file: UploadFile = File(...),
    raw_file: list[UploadFile] = File(...),
    report_date: str = Form(""),
    baseline_date: str = Form(""),
    base_sheet: str = Form(""),
    estimate_bharat_fof: str = Form("false"),
    limit_policy: str = Form("standard"),
    user: User = Depends(get_current_user),
):
    if not raw_file or len(raw_file) > MAX_BROKER_FILES:
        raise HTTPException(status_code=400, detail=f"Upload between 1 and {MAX_BROKER_FILES} broker workbooks.")
    try:
        selected_date = date.fromisoformat(report_date) if report_date else None
        opening_date = date.fromisoformat(baseline_date) if baseline_date else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Dates must use YYYY-MM-DD.") from exc
    if estimate_bharat_fof.strip().lower() not in {"true", "false"}:
        raise HTTPException(status_code=400, detail="estimate_bharat_fof must be true or false.")

    uploads = [(mapping_file, "mapping"), (holdings_file, "holdings"), *[(f, "broker") for f in raw_file]]
    content_by_name = []
    total_size = 0
    seen_names = set()
    try:
        for upload, role in uploads:
            name = safe_filename(upload.filename or f"{role}.xlsx")
            if name.lower() in seen_names:
                name = f"{role}_{len(content_by_name)+1}_{name}"
            seen_names.add(name.lower())
            content = await upload.read()
            validate_upload(name, content)
            total_size += len(content)
            if total_size > MAX_TOTAL_BYTES:
                raise ValueError(f"Combined workbook upload exceeds {MAX_TOTAL_BYTES // (1024 * 1024)} MB.")
            content_by_name.append((role, name, content))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = str(uuid4())
    input_files = {
        "mapping": next(name for role, name, _ in content_by_name if role == "mapping"),
        "holdings": next(name for role, name, _ in content_by_name if role == "holdings"),
        "raw": [name for role, name, _ in content_by_name if role == "broker"],
    }
    try:
        async with get_sessionmaker()() as db:
            job = DebtTrackerJob(id=job_id, requested_by=user.id, status="processing", input_files=input_files)
            db.add(job)
            await db.commit()

            mappings = (await db.scalars(select(DebtTrackerMapping))).all()
            fund_aliases = {item.source_key: item.target_key for item in mappings if item.mapping_type == "fund_alias"}
            group_mappings = [
                {"mapping_type": item.mapping_type, "source_key": item.source_key, "target_key": item.target_key}
                for item in mappings if item.mapping_type in {"issuer_group", "security_group"}
            ]
    except OperationalError as exc:
        LOGGER.exception("DEBT_TRACKER_DATABASE_NOT_READY")
        raise HTTPException(status_code=503, detail=(
            "Debt Tracker database tables are not installed or the database is unavailable. "
            "From the backend folder run: alembic upgrade head, then restart the API."
        )) from exc

    try:
        with tempfile.TemporaryDirectory(prefix="debt-tracker-") as temp:
            root = Path(temp)
            paths = {}
            broker_paths = []
            for role, name, content in content_by_name:
                path = root / name
                path.write_bytes(content)
                if role == "mapping": paths[role] = path
                elif role == "holdings": paths[role] = path
                else: broker_paths.append(path)
            output_path = root / f"debt-exposure-{job_id}.xlsx"
            detected_report_date = detect_holdings_report_date(paths["holdings"])
            effective_report_date = selected_date or detected_report_date
            if effective_report_date is None:
                raise ValueError("Could not read the portfolio report date from the MProfit workbook. Use a dated Portfolio Summary Report.")
            if opening_date and opening_date > effective_report_date:
                raise ValueError("The baseline date cannot be after the report date.")
            # Workbook parsing, reconciliation and Excel rendering are CPU/disk-heavy.
            # Keep this work off the ASGI event loop so unrelated API requests remain responsive.
            report, allocation_exceptions, _digest = await run_in_threadpool(partial(
                run_generation,
                mapping_path=paths["mapping"], holdings_path=paths["holdings"], broker_paths=broker_paths,
                report_date=effective_report_date, baseline_date=opening_date, base_sheet=base_sheet or None,
                estimate_bharat_fof=estimate_bharat_fof.strip().lower() == "true",
                limit_policy=limit_policy, fund_aliases=fund_aliases, group_mappings=group_mappings,
                output_path=output_path,
            ))
            output_bytes = output_path.read_bytes()
            storage_key = f"debt-tracker/jobs/{job_id}/report.xlsx"
            await get_object_storage().put(storage_key, output_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        async with get_sessionmaker()() as db:
            job = await db.get(DebtTrackerJob, job_id)
            job.status = "complete"
            job.report_data = report
            job.output_key = storage_key
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception as exc:
        LOGGER.exception("DEBT_TRACKER_GENERATION_FAILED job=%s", job_id)
        async with get_sessionmaker()() as db:
            job = await db.get(DebtTrackerJob, job_id)
            if job:
                job.status = "failed"
                job.error_message = str(exc)[:4000]
                job.completed_at = datetime.now(timezone.utc)
                await db.commit()
        detail = str(exc) if isinstance(exc, (ValueError, DebtTrackerError)) else "Debt Tracker could not process these workbooks. Check the backend log entry DEBT_TRACKER_GENERATION_FAILED for the underlying cause."
        raise HTTPException(status_code=422, detail=detail) from exc

    return {"job": {"id": job_id, "status": "complete", "input_files": input_files}}


@router.get("/jobs")
async def list_jobs(limit: int = 20, user: User = Depends(get_current_user)):
    """List completed company-wide reports for every authenticated user.

    `requested_by` remains on the database row for audit purposes, but report
    visibility is shared across the company deployment rather than owned by
    the user who uploaded the workbooks.
    """
    limit = min(max(limit, 1), 100)
    async with get_sessionmaker()() as db:
        rows = (await db.scalars(
            select(DebtTrackerJob)
            .where(DebtTrackerJob.status == "complete")
            .order_by(desc(DebtTrackerJob.created_at))
            .limit(limit)
        )).all()
        return {"jobs": [serialize_job(row) for row in rows]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, user: User = Depends(get_current_user)):
    """Read a report job created by any user in this company deployment."""
    async with get_sessionmaker()() as db:
        job = await db.get(DebtTrackerJob, job_id)
        if job is None or job.status != "complete":
            raise HTTPException(status_code=404, detail="Debt Tracker report not found.")
        return serialize_job(job)


@router.get("/jobs/{job_id}/data")
async def get_job_data(job_id: str, user: User = Depends(get_current_user)):
    async with get_sessionmaker()() as db:
        job = await db.get(DebtTrackerJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Debt Tracker report not found.")
        if job.status != "complete" or not job.report_data:
            raise HTTPException(status_code=409, detail="Debt Tracker report is not ready.")
        return {**job.report_data, "job": serialize_job(job)}




@router.put("/jobs/{job_id}/base")
async def update_base_sheet(job_id: str, payload: BaseEditPayload, user: User = Depends(get_current_user)):
    """Persist full-row BASE cell edits and refresh the saved workbook summaries."""
    async with get_sessionmaker()() as db:
        job = await db.get(DebtTrackerJob, job_id)
        if job is None or job.status != "complete" or not job.output_key or not job.report_data:
            raise HTTPException(status_code=404, detail="Debt Tracker report not found or not editable.")
        report = dict(job.report_data)
        storage_key = job.output_key
    workbook_bytes = await get_object_storage().get(storage_key)
    try:
        updated_report, updated_workbook = await run_in_threadpool(
            partial(save_edited_base, report, workbook_bytes, payload.rows)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        LOGGER.exception("DEBT_TRACKER_BASE_EDIT_FAILED job=%s", job_id)
        raise HTTPException(status_code=422, detail="BASE edits could not be saved. Check the backend log for details.") from exc
    await get_object_storage().put(
        storage_key, updated_workbook,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    async with get_sessionmaker()() as db:
        job = await db.get(DebtTrackerJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Debt Tracker report no longer exists.")
        job.report_data = updated_report
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        serialized = serialize_job(job)
    return {**updated_report, "job": serialized}

@router.get("/jobs/{job_id}/download")
async def download_job(job_id: str, user: User = Depends(get_current_user)):
    async with get_sessionmaker()() as db:
        job = await db.get(DebtTrackerJob, job_id)
        if job is None or job.status != "complete" or not job.output_key:
            raise HTTPException(status_code=404, detail="Debt Tracker workbook not found.")
        key = job.output_key
    content = await get_object_storage().get(key)
    return StreamingResponse(io.BytesIO(content), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="debt-exposure-{job_id}.xlsx"'} )


@router.get("/jobs/{job_id}/download-sheet")
async def download_job_sheet(job_id: str, sheet: str, user: User = Depends(get_current_user)):
    if sheet not in {"BASE", "Sheet4", "Sheet8", "Broker Reconciliation"}:
        raise HTTPException(status_code=400, detail="Only BASE, Sheet4, Sheet8 and Broker Reconciliation can be downloaded separately.")
    async with get_sessionmaker()() as db:
        job = await db.get(DebtTrackerJob, job_id)
        if job is None or job.status != "complete" or not job.output_key:
            raise HTTPException(status_code=404, detail="Debt Tracker workbook not found.")
        key = job.output_key
    from openpyxl import load_workbook, Workbook
    source = load_workbook(io.BytesIO(await get_object_storage().get(key)), data_only=True, read_only=True)
    output = Workbook()
    target = output.active
    target.title = sheet
    for row in source[sheet].iter_rows(values_only=True):
        target.append(list(row))
    stream = io.BytesIO()
    output.save(stream)
    output.close(); source.close(); stream.seek(0)
    return StreamingResponse(stream, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{sheet}-{job_id}.xlsx"'})


@router.get("/mappings")
async def list_mappings(user: User = Depends(get_current_user)):
    async with get_sessionmaker()() as db:
        rows = (await db.scalars(select(DebtTrackerMapping).order_by(DebtTrackerMapping.mapping_type, DebtTrackerMapping.source_key))).all()
        return {"mappings": [{"id": x.id, "mapping_type": x.mapping_type, "source_key": x.source_key, "target_key": x.target_key,
                              "approved_by": x.approved_by, "updated_at": x.updated_at.isoformat() if x.updated_at else None} for x in rows]}


@router.post("/mappings")
async def approve_mapping(payload: MappingApproval, user: User = Depends(get_current_user)):
    source = payload.source_key.strip(); target = payload.target_key.strip()
    normalized = _mapping_normalized(payload.mapping_type, source)
    if not normalized or not target or any(ord(c) < 32 for c in source + target):
        raise HTTPException(status_code=400, detail="Mapping source and target must be valid text.")
    if payload.mapping_type == "security_group":
        isin = source.upper().replace(" ", "")
        if not (len(isin) == 12 and isin[:2].isalpha() and isin[2:].isalnum()):
            raise HTTPException(status_code=400, detail="Security group mappings require a 12-character ISIN as the source key.")
        source = isin; normalized = normalize_text(isin)
    async with get_sessionmaker()() as db:
        if payload.mapping_type == "fund_alias":
            aliases = (await db.scalars(select(DebtTrackerMapping).where(DebtTrackerMapping.mapping_type == "fund_alias"))).all()
            conflicting = next((item for item in aliases
                                if item.source_normalized != normalized
                                and normalize_fund_name(item.target_key) == normalize_fund_name(target)), None)
            if conflicting:
                raise HTTPException(status_code=409, detail="That portfolio fund already has an approved broker alias. Review the existing mapping before adding another.")
        existing = await db.scalar(select(DebtTrackerMapping).where(DebtTrackerMapping.mapping_type == payload.mapping_type,
                                                                      DebtTrackerMapping.source_normalized == normalized))
        previous_target = existing.target_key if existing else None
        if existing:
            existing.source_key = source; existing.target_key = target; existing.approved_by = user.id
            existing.updated_at = datetime.now(timezone.utc)
        else:
            db.add(DebtTrackerMapping(mapping_type=payload.mapping_type, source_key=source, source_normalized=normalized,
                                      target_key=target, approved_by=user.id))
        db.add(DebtTrackerMappingHistory(mapping_type=payload.mapping_type, source_key=source,
                                         previous_target=previous_target, new_target=target, approved_by=user.id))
        await db.commit()
    return {"status": "approved", "mapping_type": payload.mapping_type, "source_key": source, "target_key": target}
