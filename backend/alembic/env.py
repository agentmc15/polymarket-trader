"""Alembic environment configuration for async SQLAlchemy."""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import your models here
from app.config import settings
from app.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Override sqlalchemy.url with the value from settings.
#
# This MUST keep the `+asyncpg` driver. `run_migrations_online` (the
# default path, and what `alembic upgrade head` uses) hands this string
# to `async_engine_from_config`, and SQLAlchemy's asyncio extension
# refuses any sync driver: stripping the driver here produced a bare
# `postgresql://`, which resolves to psycopg2 and fails with either
# `ModuleNotFoundError: psycopg2` (it is not in requirements.txt --
# only asyncpg is) or, once installed, `InvalidRequestError: The
# asyncio extension requires an async driver`. Either way ONLINE
# MIGRATIONS COULD NOT RUN AT ALL against Postgres.
#
# This survived because the only sanctioned way to check migrations
# here was `alembic upgrade head --sql`, and offline mode never builds
# an engine -- so the one command that was safe to run was also the one
# command that could not see this. `run_migrations_offline` strips the
# driver itself, below, where doing so is correct.
config.set_main_option("sqlalchemy.url", settings.async_database_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    # Offline mode emits SQL and never connects, so it needs no DBAPI --
    # and the async driver would only make the rendered URL misleading.
    # Stripping belongs HERE, not at module scope where it also broke
    # the online path.
    url = (config.get_main_option("sqlalchemy.url") or "").replace("+asyncpg", "")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations with the given connection."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
