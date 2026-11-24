from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select
from ii_platform.database.connection import get_sessionmaker
from .models import SignalRadarRecord, SignalRadarSourceRun
from .collectors.source_registry import public_sources
from .collectors.public_portal import collect_source

logger = logging.getLogger("signal-radar")
_COLLECTION_LOCK = asyncio.Lock()


async def _run_one_source(source: dict, semaphore: asyncio.Semaphore) -> dict:
    async with get_sessionmaker()() as db:
        run = SignalRadarSourceRun(source_id=source["id"], source_name=source["name"], status="running")
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    try:
        async with semaphore:
            result = await asyncio.wait_for(collect_source(source, timeout_seconds=25.0, max_links=12), timeout=100.0)
        async with get_sessionmaker()() as db:
            new_count = 0
            for item in result["records"]:
                existing = await db.scalar(select(SignalRadarRecord).where(
                    SignalRadarRecord.source_id == source["id"],
                    SignalRadarRecord.source_url == item["source_url"],
                ))
                if existing:
                    existing.last_seen_at = datetime.now(timezone.utc)
                    existing.raw_metadata = item["raw_metadata"]
                    continue
                db.add(SignalRadarRecord(**item))
                new_count += 1
            run = await db.get(SignalRadarSourceRun, run_id)
            if run:
                run.status = result.get("status", "success" if result["records"] else "no_records")
                run.http_status = result.get("http_status")
                run.discovered_count = len(result["records"])
                run.new_count = new_count
                run.diagnostics = result.get("diagnostics", {})
                run.completed_at = datetime.now(timezone.utc)
            await db.commit()
        return {"source_id": source["id"], "source_name": source["name"], "status": result.get("status", "success" if result["records"] else "no_records"), "http_status": result.get("http_status"), "discovered": len(result["records"]), "new": new_count, "diagnostics": result.get("diagnostics", {})}
    except Exception as exc:
        async with get_sessionmaker()() as db:
            run = await db.get(SignalRadarSourceRun, run_id)
            if run:
                run.status = "failed"
                run.error_message = f"{type(exc).__name__}: {str(exc)}"[:2000]
                run.diagnostics = {"error_type": type(exc).__name__, "timeout_seconds": 25, "source_url": source.get("url")}
                run.completed_at = datetime.now(timezone.utc)
                await db.commit()
        logger.exception("SIGNAL_RADAR_SOURCE_FAILED source=%s", source["id"])
        return {"source_id": source["id"], "source_name": source["name"], "status": "failed", "error_type": type(exc).__name__, "error": str(exc)[:300]}


async def run_signal_radar_collection(source_ids: list[str] | None = None) -> dict:
    if _COLLECTION_LOCK.locked():
        return {"status": "skipped", "reason": "collection_already_running"}
    async with _COLLECTION_LOCK:
        selected = [s for s in public_sources() if not source_ids or s["id"] in source_ids]
        semaphore = asyncio.Semaphore(2)
        results = await asyncio.gather(*[_run_one_source(source, semaphore) for source in selected])
        summary = {
            "status": "partial" if any(x["status"] in ("failed", "partial") for x in results) else "completed",
            "attempted": len(results),
            "sources": sum(x["status"] in ("success", "no_records", "partial") for x in results),
            "partial": sum(x["status"] == "partial" for x in results),
            "no_records": sum(x["status"] == "no_records" for x in results),
            "failed": sum(x["status"] == "failed" for x in results),
            "failed_source_ids": [x["source_id"] for x in results if x["status"] == "failed"],
            "discovered": sum(x.get("discovered", 0) for x in results),
            "new": sum(x.get("new", 0) for x in results),
            "source_results": results,
        }
        logger.info("SIGNAL_RADAR_COLLECTION_DONE %s", summary)
        return summary
