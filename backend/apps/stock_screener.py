from __future__ import annotations

"""Coverage-aware company research API backed by QuarterMark's public pages."""

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from infrastructure.security.permissions import require_role
from ii_platform.database.models import User, UserRole
from ii_platform.web.collectors.quartermark_collector import quartermark_collector

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/stock-screener", tags=["Stock Screener"])


@router.get("/search")
async def search_companies(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    refresh: bool = Query(default=False),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
) -> dict[str, Any]:
    """Search the QuarterMark company universe by company name or ticker."""
    result = await quartermark_collector.search_companies(q, limit, force_refresh=refresh)
    if result.get("status") == "unavailable":
        # Preserve partial UI functionality with a structured response rather
        # than converting a source outage into an empty successful search.
        return result
    return result


@router.get("/company/{symbol}")
async def get_company_research(
    symbol: str = Path(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9&._-]+$"),
    refresh: bool = Query(default=False),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
) -> dict[str, Any]:
    """Load Overview, Financials, Ownership, Peers and earnings-call documents from QuarterMark."""
    try:
        result = await quartermark_collector.company_detail(symbol, force_refresh=refresh)
        if result.get("status") == "unavailable":
            return result
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Stock Screener company detail failed symbol=%s", symbol)
        raise HTTPException(status_code=502, detail="QuarterMark company details are temporarily unavailable") from exc


def get_router() -> APIRouter:
    return router
