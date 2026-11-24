from logging.config import fileConfig
import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from infrastructure.config.settings import get_settings
from ii_platform.database.models import Base
from apps.signal_radar import models as signal_radar_models  # noqa: F401
from apps.debt_tracker import models as debt_tracker_models  # noqa: F401
from apps.portfolio_comparison import models as portfolio_comparison_models  # noqa: F401
from migrations.legacy_schema import attach_legacy_tables
from ii_platform.database.connection import _normalize_database_url


config = context.config

if config.config_file_name:
    try:
        fileConfig(config.config_file_name)
    except (KeyError, ValueError):
        pass


# Keep historical migrations replayable without exposing legacy tables to app code.
attach_legacy_tables(Base.metadata)
target_metadata = Base.metadata


_LEGACY_TABLES = {"emails", "financial_results", "benpos_records"}


def include_object(obj, name, type_, reflected, compare_to):
    """Hide migration-only legacy schema objects from future autogenerate diffs."""
    if type_ == "table" and name in _LEGACY_TABLES:
        return False
    if type_ == "column":
        table = getattr(obj, "table", None)
        if table is not None and table.name == "users" and name == "telegram_user_id":
            return False
    table = getattr(obj, "table", None)
    if table is not None and getattr(table, "name", None) in _LEGACY_TABLES:
        return False
    return True


def run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline():
    settings = get_settings()

    database_url, _ = _normalize_database_url(
        settings.database_url
    )

    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    settings = get_settings()

    database_url, connect_args = _normalize_database_url(
        settings.database_url
    )

    # Disable asyncpg prepared statement caching for migrations.
    # This prevents stale prepared-statement plans when the schema
    # changes during a migration on Neon/PostgreSQL.
    migration_connect_args = {
        **connect_args,
        "statement_cache_size": 0,
    }

    async def run_async():
        connectable = async_engine_from_config(
            {
                "sqlalchemy.url": database_url,
            },
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
            connect_args=migration_connect_args,
        )

        async with connectable.connect() as connection:
            await connection.run_sync(run_migrations)

        await connectable.dispose()

    asyncio.run(run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
