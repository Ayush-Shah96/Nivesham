from fastapi import APIRouter

from apps.regulatory_activity.routes import router as activity_router
from apps.regulatory_activity.legacy_routes import router as signal_router


def get_router() -> APIRouter:
    """Return all regulatory endpoints owned by the single regulatory_activity package.

    The /regulatory-signals paths remain as API compatibility aliases for the
    existing frontend and older clients; their implementation lives here too.
    New normalized activity endpoints live under /regulatory-activity.
    """
    router = APIRouter()
    router.include_router(signal_router)
    router.include_router(activity_router)
    return router
