# backend/engine.py
# Single source of truth for the SQLite engine, so app / routes / sse all import
# from here instead of from backend.app (which created a fragile import-order
# dependency — see code review C1).
from sqlmodel import create_engine

from backend import config

engine = create_engine(f"sqlite:///{config.DB_PATH}", connect_args={"check_same_thread": False})
