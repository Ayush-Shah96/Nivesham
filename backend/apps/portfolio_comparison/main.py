from fastapi import APIRouter

from .routes import router


def get_router() -> APIRouter:
    return router
