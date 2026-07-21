"""Database bootstrap policy.

SQLite remains a local compatibility mode. Postgres is production-only and must
already be at the Alembic head before the web process starts.
"""

from sqlalchemy import inspect, text
from sqlmodel import SQLModel

from backend import migrate
from backend.engine import engine, is_postgres

ALEMBIC_HEAD = "20260719_01"


def postgres_revision() -> str | None:
    if not is_postgres():
        return None
    if "alembic_version" not in inspect(engine).get_table_names():
        return None
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def ensure_database_ready() -> list[str]:
    if is_postgres():
        revision = postgres_revision()
        if revision != ALEMBIC_HEAD:
            raise RuntimeError(
                f"database_schema_not_ready: expected {ALEMBIC_HEAD}, got {revision or 'none'}; "
                "run `alembic upgrade head` with DATABASE_MIGRATION_URL first"
            )
        return []
    SQLModel.metadata.create_all(engine)
    return migrate.ensure_columns(engine)
