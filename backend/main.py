from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import select

from infrastructure.config.settings import get_settings
from infrastructure.config.secrets import validate_production_secrets
from infrastructure.security.auth import hash_password
from ii_platform.ai.gateway import get_ai_gateway, close_ai_gateway
from ii_platform.database.connection import dispose_engine, get_engine, get_sessionmaker
from ii_platform.database.models import Base, User, UserRole
from ii_platform.cache.auth_cache import warm_auth_users
from apps.auth import router as auth_router
from apps.admin import router as admin_router
from apps.dashboard import router as dashboard_router
from apps.pulse import collector as pulse_market_collector, close_pulse_data, get_router as pulse_router
from apps.notifications import router as notifications_router
from apps.coverage_list import router as coverage_list_router
from apps.newsalert.main import get_router as news_router
from apps.regulatory_activity.main import get_router as regulatory_activity_router
from apps.market_intelligence.main import get_router as sector_screener_router
from apps.crisp_investor_notes.main import get_router as crisp_router
from apps.debt_tracker.main import get_router as debt_tracker_router
from apps.portfolio_comparison.main import get_router as portfolio_comparison_router
from apps.portfolio_comparison.service import portfolio_queue_loop
from apps.earnings_calendar.main import get_router as earnings_calendar_router
from apps.youtube_intelligence.main import get_router as youtube_intelligence_router
from apps.opstracker import router as ops_tracker_router
from apps.debt_tracker import models as debt_tracker_models  # register Debt Tracker tables in Base.metadata
from apps.portfolio_comparison import models as portfolio_comparison_models  # register Portfolio Comparison tables in Base.metadata
from ii_platform.monitoring.health import health_router

# Playwright launches child processes. Use the Proactor loop on Windows so browser
# collectors can create subprocesses in the dedicated worker process.
if os.name == "nt" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

settings = get_settings()
validate_production_secrets(settings)
logger = logging.getLogger(__name__)


async def _warm_database_and_bootstrap() -> None:
    """Warm one pooled DB connection and optionally create the first admin user.

    This is intentionally tiny. The web process never starts a scraper, Redis
    consumer, workbook sync, browser collector or scheduler.
    """
    try:
        async with get_sessionmaker()() as db:
            await db.scalar(select(1))
            if settings.bootstrap_admin_password:
                existing = await db.scalar(select(User.id).order_by(User.id).limit(1))
                if existing is None:
                    db.add(
                        User(
                            username=settings.bootstrap_admin_username,
                            password_hash=hash_password(settings.bootstrap_admin_password),
                            role=UserRole.ADMIN,
                        )
                    )
                    await db.commit()
            if settings.auth_cache_enabled:
                cached = await warm_auth_users(db)
                logger.info("Web-process auth cache warm-up count=%s", cached)
        logger.info("Web-process database/auth warm-up complete")
    except asyncio.CancelledError:
        raise
    except Exception:
        # Requests can still use the resilient DB session if the first warm-up hit a
        # cold/unavailable remote connection.
        logger.exception("Web-process database warm-up failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    started = perf_counter()
    portfolio_stop_event = asyncio.Event()
    portfolio_worker_task = None

    # PostgreSQL is migrated explicitly with Alembic. SQLite development databases
    # may still opt into automatic table creation.
    auto_create_env = os.getenv("DB_AUTO_CREATE_SCHEMA")
    if auto_create_env is None:
        auto_create_schema = (
            settings.environment == "development"
            and settings.database_url.lower().startswith("sqlite")
        )
    else:
        auto_create_schema = auto_create_env.strip().lower() in {"1", "true", "yes", "on"}

    if auto_create_schema:
        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    warm_task = asyncio.create_task(
        _warm_database_and_bootstrap(),
        name="web-database-warmup",
    )

    # Portfolio Manager is a durable DB queue. In local development we keep the
    # portfolio queue alive in the API process so a separate worker process is not
    # required just to process an uploaded ZIP. Production can keep this disabled
    # and run the dedicated worker.py process instead. Row locking makes running
    # both owners safe during transitions/restarts.
    if settings.environment == "development" or settings.background_workers_enabled:
        portfolio_worker_task = asyncio.create_task(
            portfolio_queue_loop(portfolio_stop_event, owner="api-background"),
            name="portfolio-comparison-api-background-worker",
        )
        logger.info(
            "PORTFOLIO_API_BACKGROUND_WORKER_ENABLED environment=%s flag=%s",
            settings.environment,
            settings.background_workers_enabled,
        )
    else:
        logger.info(
            "PORTFOLIO_API_BACKGROUND_WORKER_DISABLED; dedicated worker.py is expected"
        )

    logger.info(
        "API startup complete in %.3fs; scheduled collectors remain worker-owned",
        perf_counter() - started,
    )
    try:
        yield
    finally:
        portfolio_stop_event.set()
        if portfolio_worker_task is not None:
            portfolio_worker_task.cancel()
            await asyncio.gather(portfolio_worker_task, return_exceptions=True)
        warm_task.cancel()
        await asyncio.gather(warm_task, return_exceptions=True)
        await pulse_market_collector.aclose()
        await close_pulse_data()
        await close_ai_gateway()
        await dispose_engine()


app = FastAPI(
    title="Unified Investment Intelligence Backend",
    version="1.5.2",
    lifespan=lifespan,
)


def _allowed_frontend_origins() -> list[str]:
    configured = (settings.frontend_origin or "").strip()
    origins = [item.strip().rstrip("/") for item in configured.split(",") if item.strip()]
    for origin in (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ):
        if origin not in origins:
            origins.append(origin)
    return origins


@app.middleware("http")
async def request_timing_middleware(request, call_next):
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (perf_counter() - started) * 1000.0
        logger.exception(
            "HTTP FAIL %s %s duration_ms=%.1f",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise

    elapsed_ms = (perf_counter() - started) * 1000.0
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.1f}"
    response.headers["X-App-Code-Version"] = "1.5.2-PULSE-EVENTS"
    if 300 <= response.status_code < 400:
        logger.warning(
            "HTTP REDIRECT_OR_3XX %s %s status=%s location=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            response.headers.get("Location") or response.headers.get("location") or "",
            elapsed_ms,
        )
    elif elapsed_ms >= 750 or response.status_code >= 400:
        logger.warning(
            "HTTP SLOW %s %s status=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
    return response


app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_frontend_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

prefix = settings.api_prefix
app.include_router(auth_router, prefix=prefix)
app.include_router(admin_router, prefix=prefix)
app.include_router(dashboard_router, prefix=prefix)
app.include_router(pulse_router(), prefix=prefix)
app.include_router(notifications_router, prefix=prefix)
app.include_router(coverage_list_router, prefix=prefix)
app.include_router(news_router(), prefix=prefix)
app.include_router(regulatory_activity_router(), prefix=prefix)
app.include_router(sector_screener_router(), prefix=prefix)
app.include_router(crisp_router(), prefix=prefix)
app.include_router(debt_tracker_router(), prefix=prefix)
app.include_router(portfolio_comparison_router(), prefix=prefix)
app.include_router(earnings_calendar_router(), prefix=prefix)
app.include_router(youtube_intelligence_router(), prefix=prefix)
app.include_router(ops_tracker_router, prefix=prefix)
app.include_router(health_router, prefix=prefix)


@app.get("/")
async def root():
    return {"service": settings.app_name, "version": app.version, "docs": "/docs"}
