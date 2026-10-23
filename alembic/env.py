"""Alembic environment.

The connection URL comes from backend.config, not from alembic.ini, so a migration is
always applied to the same database the application reads — and so a production URL never
has to live in a file that is committed.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import config as app_config  # noqa: E402

alembic_config = context.config
if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

# mysql:// is PyMySQL here; SQLAlchemy needs the driver named explicitly.
url = app_config.DATABASE_URL.replace("mysql://", "mysql+pymysql://", 1)
alembic_config.set_main_option("sqlalchemy.url", url)

# The schema is defined by backend/database.py's SCHEMA statements rather than SQLAlchemy
# models, so there is no metadata to autogenerate against; revisions are written by hand.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(alembic_config.get_section(alembic_config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
