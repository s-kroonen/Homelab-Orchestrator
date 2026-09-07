"""Alembic environment.

Uses SQLModel.metadata as the target so autogeneration reflects the ORM.
The URL comes from the app's ``Settings`` (env vars / .env) so ``alembic``
and the running service agree on the database.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

# Import for side effects — populates SQLModel.metadata.
from orchestrator.db import models as _models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Prefer the URL the app was configured with so we never migrate the wrong DB.
try:
    from orchestrator.config import get_settings

    app_url = get_settings().database_url
    if app_url:
        config.set_main_option("sqlalchemy.url", app_url)
except Exception:
    # Fall back to alembic.ini value if settings can't load (bare `alembic` runs).
    pass

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite") if url else False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
