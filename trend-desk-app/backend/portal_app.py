"""Serve Cindy's static portal and the private Trend Desk on one Space service."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.app import app as trend_desk_app


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Mounted applications do not own the parent process lifecycle.  Enter the
    # Trend Desk lifespan explicitly so its schema checks and migrations run.
    async with trend_desk_app.router.lifespan_context(trend_desk_app):
        yield


app = FastAPI(title="Cindy's Space", lifespan=lifespan)
app.mount("/trend-desk", trend_desk_app)
app.mount(
    "/",
    StaticFiles(
        directory=os.getenv("TREND_DESK_PORTAL_DIR", "/app/portal"),
        html=True,
    ),
    name="portal",
)
