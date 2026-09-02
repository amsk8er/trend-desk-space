# backend/engine.py
# Single source of truth for the SQLite engine, so app / routes / sse all import
# from here instead of from backend.app (which created a fragile import-order
# dependency — see code review C1).
from sqlalchemy.engine import make_url
from sqlmodel import create_engine

from backend import config


def database_url() -> str:
    url = config.DATABASE_URL or f"sqlite:///{config.DB_PATH}"
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgres://")
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def is_postgres() -> bool:
    return make_url(database_url()).get_backend_name() == "postgresql"


_url = database_url()
if is_postgres():
    engine = create_engine(_url, pool_pre_ping=True, pool_recycle=300)
else:
    engine = create_engine(_url, connect_args={"check_same_thread": False})
