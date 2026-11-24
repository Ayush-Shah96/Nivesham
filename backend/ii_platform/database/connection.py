from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator
from uuid import uuid4
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql import CompoundSelect, Select
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv(os.path.join(_BACKEND_DIR, ".env"), override=False)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _normalize_database_url(raw_url: str) -> tuple[str, dict]:
    raw_url = raw_url.strip()
    if not raw_url:
        raise RuntimeError(
            "DATABASE_URL is empty. Set DATABASE_URL in backend/.env or the environment."
        )

    parts = urlsplit(raw_url)
    scheme = parts.scheme.lower()

    if scheme in {"sqlite", "sqlite+pysqlite", "sqlite+aiosqlite"}:
        # Preserve SQLite's triple-slash form; urlunsplit would collapse
        # sqlite:///./file.db into sqlite:/./file.db.
        suffix = raw_url.split(":", 1)[1]
        if suffix.startswith("//"):
            return ("sqlite+aiosqlite:" + suffix, {})
        return ("sqlite+aiosqlite:///" + suffix.lstrip("/"), {})

    postgres_schemes = {
        "postgres", "postgresql", "postgresql+psycopg2",
        "postgresql+psycopg", "postgresql+asyncpg",
    }
    if scheme not in postgres_schemes:
        raise ValueError(
            f"Unsupported database URL scheme '{scheme}'. "
            "Use PostgreSQL with asyncpg or SQLite with aiosqlite."
        )

    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    connect_args: dict = {}
    remaining: list[tuple[str, str]] = []
    hostname = (parts.hostname or "").lower()
    is_neon_pooler = "-pooler." in hostname or hostname.endswith("-pooler")

    # libpq parameters that should not be passed verbatim to asyncpg.connect().
    ignored = {
        "channel_binding",
        "target_session_attrs",
        "application_name",
        "gssencmode",
        "krbsrvname",
        "sslcompression",
    }

    for key, value in query_pairs:
        lower = key.lower()
        if lower == "sslmode":
            sslmode = value.strip().lower()
            allowed = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
            if sslmode not in allowed:
                raise ValueError(
                    f"Unsupported PostgreSQL sslmode={value!r}; expected one of {sorted(allowed)}"
                )
            connect_args["ssl"] = sslmode
        elif lower in ignored:
            continue
        else:
            remaining.append((key, value))

    # Neon pooled endpoints use PgBouncer. Disable SQLAlchemy/asyncpg prepared
    # statement caching on the pooler path so a transaction can move between
    # server-side connections without stale prepared-state assumptions.
    # See SQLAlchemy asyncpg guidance for PgBouncer-backed connections.
    if is_neon_pooler and not any(key == "prepared_statement_cache_size" for key, _ in remaining):
        remaining.append(("prepared_statement_cache_size", "0"))

    clean_query = urlencode(remaining, doseq=True)
    return (
        urlunsplit(("postgresql+asyncpg", parts.netloc, parts.path, clean_query, parts.fragment)),
        connect_args,
    )


def get_raw_database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    return value or "sqlite+aiosqlite:///./investment.db"


def get_database_url() -> str:
    normalized, _ = _normalize_database_url(get_raw_database_url())
    return normalized


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is not None:
        return _engine

    database_url, connect_args = _normalize_database_url(get_raw_database_url())
    kwargs = {
        "echo": os.getenv("DATABASE_ECHO", "false").lower() == "true",
    }

    is_postgres = database_url.startswith("postgresql+asyncpg://")
    is_neon_pooler = False
    if is_postgres:
        host = (urlsplit(database_url).hostname or "").lower()
        is_neon_pooler = (
            ".neon.tech" in host
            or "-pooler." in host
            or host.endswith("-pooler")
        )

    if database_url.startswith("sqlite+aiosqlite://"):
        kwargs["poolclass"] = NullPool
    else:
        disable_pool = os.getenv(
            "DB_DISABLE_POOL",
            "false",
        ).lower() in {"1", "true", "yes", "on"}

        # Keep a bounded application-side pool for Neon/PgBouncer. The API and
        # background worker are separate processes, so an oversized pool is
        # multiplied across processes and can turn connection pressure into
        # request latency. Use conservative Neon defaults with explicit env
        # overrides.
        if is_neon_pooler:
            # The web API and worker are separate processes. An 8+8 pool per
            # process creates up to 32 DB connections, which is excessive for
            # a small/medium Neon project and can turn connection pressure into
            # request latency. Keep a small bounded pool and let SQL do the work.
            kwargs["pool_size"] = int(os.getenv("NEON_DB_POOL_SIZE", "5"))
            kwargs["max_overflow"] = int(os.getenv("NEON_DB_MAX_OVERFLOW", "5"))
            kwargs["pool_timeout"] = int(os.getenv("DB_POOL_TIMEOUT_SECONDS", "8"))
            kwargs["pool_recycle"] = 300
            kwargs["pool_pre_ping"] = True
            kwargs["pool_use_lifo"] = True
        elif disable_pool:
            kwargs["poolclass"] = NullPool
        else:
            kwargs["pool_size"] = int(os.getenv("DB_POOL_SIZE", "5"))
            kwargs["max_overflow"] = int(os.getenv("DB_MAX_OVERFLOW", "5"))
            kwargs["pool_timeout"] = int(os.getenv("DB_POOL_TIMEOUT_SECONDS", "8"))
            kwargs["pool_recycle"] = int(os.getenv("DB_POOL_RECYCLE_SECONDS", "300"))
            kwargs["pool_pre_ping"] = True
            kwargs["pool_use_lifo"] = True

    # asyncpg-level timeouts are deliberately conservative; callers may still
    # set per-request timeouts at the HTTP layer.
    if database_url.startswith("postgresql+asyncpg://"):
        connect_args = {
            **connect_args,
            "timeout": float(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "10")),
            "command_timeout": float(os.getenv("DB_COMMAND_TIMEOUT_SECONDS", "60")),
            # Avoid asyncpg's own prepared-statement cache against Neon/PgBouncer.
            "statement_cache_size": 0,
        }

        # PgBouncer may reuse a backend connection for another logical client.
        # Give every prepared statement a unique name to avoid cross-session
        # prepared-statement name collisions. SQLAlchemy documents this pattern
        # for asyncpg + PgBouncer.
        if is_neon_pooler:
            connect_args["prepared_statement_name_func"] = (
                lambda: f"__asyncpg_{uuid4()}__"
            )

    if connect_args:
        kwargs["connect_args"] = connect_args

    _engine = create_async_engine(database_url, **kwargs)

    if is_postgres:
        pool_name = type(_engine.sync_engine.pool).__name__
        print(
            f"DATABASE: PostgreSQL engine initialized host={urlsplit(database_url).hostname} "
            f"pool={pool_name} neon={is_neon_pooler}"
        )

    return _engine



def _is_transient_connection_error(exc: BaseException) -> bool:
    """Return True for transient remote DB/connectivity failures.

    We only replay READ operations.  Writes are deliberately never replayed
    automatically because a connection can die after PostgreSQL has accepted a
    write, making replay potentially duplicate it.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    needles = (
        "connectiondoesnotexisterror",
        "connection reset",
        "connection was forcibly closed",
        "connection was closed in the middle of operation",
        "server closed the connection",
        "connection is closed",
        "connection closed",
        "winerror 10054",
        "connectionreseterror",
        "connectiontimedouterror",
        "connect call failed",
        "connection timeout",
        "could not connect",
        "timeout during connection",
        "timed out",
    )
    # Never retry a QueuePool checkout timeout. The pool is saturated; retrying
    # only creates another wait and turns one request into a timeout cascade.
    if "queuepool limit" in text:
        return False
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return True
    return any(needle in text for needle in needles)


def _is_read_statement(statement: Any) -> bool:
    """Identify SQLAlchemy statements that are safe to replay."""
    if isinstance(statement, (Select, CompoundSelect)):
        return True
    if isinstance(statement, TextClause):
        text = str(statement).lstrip().lower()
        return text.startswith(("select ", "with ", "show ", "values ", "explain "))
    return False


async def safe_rollback(session: AsyncSession) -> None:
    """Rollback without masking the original error when the socket is dead."""
    try:
        if session.in_transaction():
            await session.rollback()
    except Exception:
        # Do not invalidate/expunge the session during request cleanup.
        # FastAPI will close the request-scoped session immediately afterwards.
        pass


async def _prepare_read_retry(session: AsyncSession) -> bool:
    """Return True only when the current ORM session is still safe to retry.

    Never call Session.invalidate() here. SQLAlchemy invalidate() expunges ORM
    instances, which caused DetachedInstanceError in Notifications after a
    transient Neon connection failure. A normal rollback keeps the instances
    associated with the session (their state may be expired, which SQLAlchemy
    can refresh on demand). If rollback itself fails because the socket is dead,
    the safest action is to stop retrying rather than detach live ORM objects.
    """
    try:
        if session.in_transaction():
            await session.rollback()
        return True
    except Exception:
        return False


class ResilientAsyncSession(AsyncSession):
    """Retry transient PostgreSQL read/connect failures on a fresh connection."""

    _READ_RETRIES = 2
    _RETRY_DELAYS = (0.20, 0.60)

    async def _retry_read(self, operation, description: str):
        last_exc: Exception | None = None
        for attempt in range(self._READ_RETRIES + 1):
            try:
                return await operation()
            except Exception as exc:
                if not _is_transient_connection_error(exc) or attempt >= self._READ_RETRIES:
                    raise
                last_exc = exc
                logger = logging.getLogger(__name__)
                logger.warning(
                    "Transient PostgreSQL %s failure; retrying %s/%s: %s",
                    description,
                    attempt + 1,
                    self._READ_RETRIES,
                    exc,
                )
                if not await _prepare_read_retry(self):
                    logger.warning(
                        "PostgreSQL session rollback failed after %s error; "
                        "stopping retry to preserve ORM session state",
                        description,
                    )
                    raise exc
                await asyncio.sleep(self._RETRY_DELAYS[attempt])
        assert last_exc is not None
        raise last_exc

    async def execute(self, statement, *args, **kwargs):  # type: ignore[override]
        if not _is_read_statement(statement):
            return await super().execute(statement, *args, **kwargs)
        async def operation():
            return await super(ResilientAsyncSession, self).execute(statement, *args, **kwargs)

        return await self._retry_read(operation, "read")

    async def get(self, entity, ident, *args, **kwargs):  # type: ignore[override]
        async def operation():
            return await super(ResilientAsyncSession, self).get(entity, ident, *args, **kwargs)

        return await self._retry_read(operation, "get")

    async def scalar(self, statement, *args, **kwargs):  # type: ignore[override]
        if not _is_read_statement(statement):
            return await super().scalar(statement, *args, **kwargs)
        async def operation():
            return await super(ResilientAsyncSession, self).scalar(statement, *args, **kwargs)
        return await self._retry_read(operation, "scalar read")

    async def scalars(self, statement, *args, **kwargs):  # type: ignore[override]
        if not _is_read_statement(statement):
            return await super().scalars(statement, *args, **kwargs)
        async def operation():
            return await super(ResilientAsyncSession, self).scalars(statement, *args, **kwargs)
        return await self._retry_read(operation, "scalars read")

def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=ResilientAsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await safe_rollback(session)
            raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a request-scoped DB session without an implicit commit on reads."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await safe_rollback(session)
            raise
        finally:
            # Explicitly written endpoints call commit(). For read-only
            # endpoints there is no reason to commit. If a DB connection died,
            # safe_rollback() prevents cleanup from replacing the real error.
            try:
                if session.in_transaction():
                    await safe_rollback(session)
            except Exception:
                try:
                    await session.invalidate()
                except Exception:
                    pass


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


# Backward-compatible name used by older modules.
close_database = dispose_engine