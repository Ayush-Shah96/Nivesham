from fastapi import APIRouter
from fastapi.responses import Response
from sqlalchemy import text
from infrastructure.config.settings import get_settings
from ii_platform.database.connection import get_engine

health_router=APIRouter(tags=["health"])

@health_router.get("/health")
async def health():
    s=get_settings()
    return {"status":"ok","service":s.app_name,"version":"1.3.0","event_bus":s.event_bus_backend,"ai_configured":bool(s.gemini_api_key)}

@health_router.get("/health/ready")
async def readiness():
    s=get_settings(); checks={}; overall=True
    try:
        async with get_engine().connect() as conn: await conn.execute(text("SELECT 1"))
        checks["database"]="ok"
    except Exception as exc:
        checks["database"]="error"; overall=False
    if s.event_bus_backend=="disabled": checks["event_bus"]="disabled"
    elif s.event_bus_backend=="redis":
        try:
            from redis.asyncio import Redis
            r=Redis.from_url(s.redis_url,socket_connect_timeout=2); await r.ping(); await r.aclose(); checks["event_bus"]="ok"
        except Exception:
            checks["event_bus"]="error"; overall=False
    checks["ai"]="configured" if s.gemini_api_key else "not_configured"
    return Response(content=__import__('json').dumps({"status":"ready" if overall else "not_ready","checks":checks}),media_type="application/json",status_code=200 if overall else 503)

@health_router.get("/metrics")
async def metrics():
    from prometheus_client import CONTENT_TYPE_LATEST,generate_latest
    return Response(generate_latest(),media_type=CONTENT_TYPE_LATEST)
