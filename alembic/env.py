"""Alembic migration environment for baas-crawlpilot's `api_keys` table
(`baas.auth.store.PostgresApiKeyStore`). Reads `BAAS_DATABASE_URL` -- the
same env var `Wiring` uses at runtime -- never a hardcoded URL, so `alembic
upgrade head` always targets whatever Postgres the caller actually intends
(fail loudly if unset, matching this repo's fail-closed philosophy for
other required-but-unset config, e.g. `BAAS_ADMIN_TOKEN`).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_raw_url = os.environ.get("BAAS_DATABASE_URL")
if not _raw_url:
    raise RuntimeError(
        "BAAS_DATABASE_URL must be set to run migrations, e.g. "
        "postgresql://baas:baas@localhost:5432/baas -- refusing to guess."
    )
# psycopg (v3) is the driver everywhere in this repo (runtime store and
# migrations alike) -- SQLAlchemy just needs the dialect prefix to pick it.
_sqlalchemy_url = _raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
config.set_main_option("sqlalchemy.url", _sqlalchemy_url)

target_metadata = None  # no ORM/declarative models in this repo -- migrations are hand-written SQL


def run_migrations_offline() -> None:
    context.configure(
        url=_sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
