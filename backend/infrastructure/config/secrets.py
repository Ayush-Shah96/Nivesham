from .settings import Settings

def validate_production_secrets(settings: Settings) -> None:
    if settings.environment != "production": return
    if settings.jwt_secret_key.startswith("CHANGE_ME") or len(settings.jwt_secret_key) < 48:
        raise RuntimeError("JWT_SECRET_KEY must be a high-entropy secret of at least 48 characters in production")
    if settings.database_url.startswith("sqlite"):
        raise RuntimeError("SQLite is not supported for production deployment; configure PostgreSQL")
    if settings.event_bus_backend == "disabled":
        raise RuntimeError("EVENT_BUS_BACKEND cannot be disabled in production")
    if settings.event_bus_backend == "redis" and not settings.redis_url:
        raise RuntimeError("REDIS_URL is required in production")
    if not settings.bootstrap_admin_password:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD is required on first production deployment")
