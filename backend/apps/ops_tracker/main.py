from fastapi import APIRouter

from .links import router as links_router
from .routes import router as core_router


def get_router() -> APIRouter:
    combined = APIRouter()
    combined.include_router(core_router)
    combined.include_router(links_router)
    return combined
