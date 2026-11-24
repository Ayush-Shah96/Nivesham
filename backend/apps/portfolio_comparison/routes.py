from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from io import BytesIO

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select

from apps.auth import get_current_user
from ii_platform.database.connection import get_sessionmaker
from ii_platform.database.models import User
from ii_platform.storage.factory import get_object_storage

from .comparison import build_comparison
from .models import PMSMonthlyFile, PMSRegistry, PortfolioUploadBatch
from .service import MAX_ARCHIVE_BYTES, create_upload_batch, job_payload, validate_month

router = APIRouter(prefix="/portfolio-comparison", tags=["Portfolio Comparison"])


@router.post("/upload")
async def upload_portfolio_zip(
    comparison_month: str = Form(...),
    archive: UploadFile = File(...),
    excluded_pms: list[str] = Form(default=[]),
    user: User = Depends(get_current_user),
):
    try:
        month = validate_month(comparison_month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename = archive.filename or "portfolio.zip"
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Portfolio Manager accepts one monthly ZIP file.")

    content = await archive.read()
    if len(content) > MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail=f"ZIP exceeds the {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB upload limit.")
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded ZIP is empty.")

    async with get_sessionmaker()() as db:
        batch_id = await create_upload_batch(
            db,
            user_id=user.id,
            comparison_month=month,
            archive_filename=filename,
            archive_bytes=content,
            upload_type="monthly",
            excluded_pms=excluded_pms,
        )
    return {"batch_id": batch_id, "status": "queued", "comparison_month": month}


@router.get("/jobs")
async def list_jobs(limit: int = 20, _: User = Depends(get_current_user)):
    limit = min(max(limit, 1), 100)
    async with get_sessionmaker()() as db:
        rows = (await db.scalars(select(PortfolioUploadBatch).order_by(desc(PortfolioUploadBatch.created_at)).limit(limit))).all()
        return {"jobs": [job_payload(row) for row in rows]}


@router.get("/jobs/{batch_id}")
async def get_job(batch_id: str, _: User = Depends(get_current_user)):
    async with get_sessionmaker()() as db:
        row = await db.get(PortfolioUploadBatch, batch_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Portfolio upload job not found.")
        files = (
            await db.scalars(
                select(PMSMonthlyFile)
                .where(PMSMonthlyFile.batch_id == batch_id)
                .order_by(PMSMonthlyFile.id)
            )
        ).all()
        payload = job_payload(row)
        payload["files"] = [
            {
                "id": item.id,
                "filename": item.filename,
                "month": item.comparison_month,
                "format": item.format,
                "status": item.status,
                "parsed_rows": item.parsed_rows,
                "parser": item.parser_key,
                "error": item.error_message,
                "version": item.version,
                "is_current": item.is_current,
            }
            for item in files
        ]
        return payload


@router.get("/registry")
async def registry(_: User = Depends(get_current_user)):
    async with get_sessionmaker()() as db:
        rows = (await db.scalars(select(PMSRegistry).order_by(PMSRegistry.pms_name))).all()
        return {
            "registry": [
                {
                    "id": row.id,
                    "pms_name": row.pms_name,
                    "format": row.format,
                    "status": row.status,
                    "first_seen_month": row.first_seen_month,
                    "last_seen_month": row.last_seen_month,
                    "last_processed_month": row.last_processed_month,
                    "last_filename": row.last_filename,
                    "error": row.error_message,
                }
                for row in rows
            ]
        }


@router.get("/months")
async def months(_: User = Depends(get_current_user)):
    async with get_sessionmaker()() as db:
        rows = (
            await db.scalars(
                select(PortfolioUploadBatch.comparison_month)
                .where(PortfolioUploadBatch.status.in_(["completed", "completed_with_errors"]))
                .distinct()
                .order_by(desc(PortfolioUploadBatch.comparison_month))
            )
        ).all()
        return {"months": rows}


@router.get("/history")
async def history(limit: int = 24, _: User = Depends(get_current_user)):
    limit = min(max(limit, 1), 60)
    async with get_sessionmaker()() as db:
        batches = (
            await db.scalars(
                select(PortfolioUploadBatch)
                .where(PortfolioUploadBatch.status.in_(["completed", "completed_with_errors"]))
                .order_by(desc(PortfolioUploadBatch.comparison_month), desc(PortfolioUploadBatch.created_at))
                .limit(limit * 3)
            )
        ).all()
        seen_months: set[str] = set()
        latest = []
        for batch in batches:
            if batch.comparison_month in seen_months:
                continue
            seen_months.add(batch.comparison_month)
            latest.append(batch)
            if len(latest) >= limit:
                break

        # Bulk-load files and registry once. The previous loop issued up to two
        # extra DB queries per month and became slower as report history grew.
        batch_ids = [batch.id for batch in latest]
        files_by_batch = defaultdict(list)
        if batch_ids:
            file_rows = (
                await db.scalars(
                    select(PMSMonthlyFile).where(
                        PMSMonthlyFile.batch_id.in_(batch_ids),
                        PMSMonthlyFile.status == "processed",
                        PMSMonthlyFile.is_current.is_(True),
                    )
                )
            ).all()
            for file_row in file_rows:
                files_by_batch[file_row.batch_id].append(file_row)
        registry_rows = (await db.scalars(select(PMSRegistry))).all()

        result = []
        for batch in latest:
            files = files_by_batch.get(batch.id, [])
            pms_ids = {file_row.pms_id for file_row in files}
            inactive = sum(
                1 for row in registry_rows
                if row.last_seen_month
                and row.last_seen_month < batch.comparison_month
                and row.id not in pms_ids
            )
            result.append({
                **job_payload(batch),
                "active_pms": len(files),
                "inactive_pms": inactive,
            })
        return {"history": result}


@router.get("/comparison")
async def comparison(
    month: str,
    excluded_pms: list[str] = Query(default=[]),
    _: User = Depends(get_current_user),
):
    try:
        month = validate_month(month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with get_sessionmaker()() as db:
        return await build_comparison(db, month, excluded_pms=excluded_pms)


@router.get("/reports/{batch_id}/json")
async def report_json(batch_id: str, _: User = Depends(get_current_user)):
    async with get_sessionmaker()() as db:
        row = await db.get(PortfolioUploadBatch, batch_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Portfolio upload job not found.")
        if not row.report_key:
            raise HTTPException(status_code=409, detail="The report is not ready yet.")
        key = row.report_key.rsplit(".", 1)[0] + ".json"
    try:
        raw = await get_object_storage().get(key)
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Report data is not available in object storage.") from exc


@router.get("/reports/{batch_id}/download")
async def download_report(batch_id: str, _: User = Depends(get_current_user)):
    async with get_sessionmaker()() as db:
        row = await db.get(PortfolioUploadBatch, batch_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Portfolio upload job not found.")
        if not row.report_key:
            raise HTTPException(status_code=409, detail="The report is not ready yet.")
        key = row.report_key
        month = row.comparison_month
    try:
        data = await get_object_storage().get(key)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Report file is no longer available in object storage.") from exc
    filename = f"PMS_Report_{month}.xlsx"
    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
