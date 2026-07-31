"""Serve Cindy's static portal and the private mounted applications."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.app import app as trend_desk_app
from backend.sensory_vocab_app import app as sensory_vocab_app
from backend.sensory_vocab_app import initialize_store as initialize_vocab_store


log = logging.getLogger("cindy-portal")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Mounted applications do not own the parent process lifecycle.  Enter the
    # Trend Desk lifespan explicitly so its schema checks and migrations run.
    async with trend_desk_app.router.lifespan_context(trend_desk_app):
        sensory_vocab_app.state.store_ready = False
        sensory_vocab_app.state.store_error = None

        async def bootstrap_vocab_store():
            try:
                await asyncio.to_thread(initialize_vocab_store)
            except Exception as exc:
                sensory_vocab_app.state.store_error = str(exc)
                log.exception("sensory vocabulary store bootstrap failed")

        task = asyncio.create_task(bootstrap_vocab_store())
        if not os.getenv("DATABASE_URL"):
            await task
        try:
            yield
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


app = FastAPI(title="Cindy's Space", lifespan=lifespan)
app.mount("/trend-desk", trend_desk_app)
app.mount("/sensory-vocabulary-lab", sensory_vocab_app)
app.mount(
    "/",
    StaticFiles(
        directory=os.getenv("TREND_DESK_PORTAL_DIR", "/app/portal"),
        html=True,
    ),
    name="portal",
)
