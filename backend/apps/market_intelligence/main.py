"""Unified Sector Screener API router."""
from fastapi import APIRouter
from .sector_screener_routes import router as sector_screener_router

router = APIRouter(prefix="/sector-screener", tags=["Sector Screener"])
router.include_router(sector_screener_router, prefix="")

def get_router() -> APIRouter:
    return router

__all__ = ["router", "get_router"]
