import logging
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from backend import backup, config
from backend.engine import is_postgres
from backend.schema import ensure_database_ready
from backend.secrets import load_secrets_env
from backend.web_auth import (
    COOKIE_NAME, SESSION_DAYS, access_key_matches, auth_required,
    create_session, verify_session,
)

DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

log = logging.getLogger("trend-desk")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # uvicorn doesn't source secrets.env — load it so ClaudeCliClient gets the
    # OAuth token (else the first live chat/OCR call runs credential-less → 500).
    loaded = load_secrets_env()
    if loaded:
        log.info("loaded secrets.env keys: %s", ", ".join(loaded))
    # D7 一次性迁移：旧库（仓库内 data/，iCloud）→ 新库（App Support，非 iCloud）。
    # 必须在 create_all 之前——否则会先在新位置建空库，迁移被幂等守卫跳过 → 丢历史批次。
    # 显式 TREND_DESK_DB_PATH 覆盖（测试/CI）时不迁移，避免误搬真实库到临时路径。
    if (
        not is_postgres()
        and not os.getenv("TREND_DESK_DB_PATH")
        and backup.relocate_legacy_db(config.LEGACY_DB_PATH, config.DB_PATH)
    ):
        log.info("relocated legacy DB out of iCloud (D7): %s → %s", config.LEGACY_DB_PATH, config.DB_PATH)
    from backend.discipline.scheduler import scheduler_enabled, scheduler_loop

    scheduler_task = None
    bootstrap_task = None
    app.state.database_ready = False

    async def bootstrap_database_and_scheduler():
        nonlocal scheduler_task
        try:
            added = await asyncio.to_thread(ensure_database_ready)
            if added:
                log.info("schema migrated: added columns %s", ", ".join(added))
            if not is_postgres() and config.DB_PATH.exists() and config.DB_PATH.stat().st_size > 0:
                result = backup.integrity_check(config.DB_PATH)
                if result != "ok":
                    log.error("DB integrity_check FAIL: %s — restore manually from %s", result, config.BACKUPS)
                    raise RuntimeError(f"DB corrupted: {result}")
            app.state.database_ready = True
            if scheduler_enabled():
                scheduler_task = asyncio.create_task(scheduler_loop())
        except Exception as exc:
            app.state.database_startup_error = str(exc)
            log.exception("database bootstrap failed; private APIs remain gated")

    # A remote Postgres connection can take longer than Koyeb's default 5-second
    # TCP probe.  Let the process bind its port immediately, while keeping every
    # data/automation endpoint gated until the fail-closed bootstrap succeeds.
    if is_postgres():
        bootstrap_task = asyncio.create_task(bootstrap_database_and_scheduler())
    else:
        await bootstrap_database_and_scheduler()
    try:
        yield
    finally:
        if bootstrap_task is not None and not bootstrap_task.done():
            bootstrap_task.cancel()
            try:
                await bootstrap_task
            except asyncio.CancelledError:
                pass
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass

app = FastAPI(title="trend-desk", lifespan=lifespan)


@app.middleware("http")
async def protect_private_api(request: Request, call_next):
    """Keep financial APIs private when the app is on a public Space URL."""
    path = request.url.path
    root_path = str(request.scope.get("root_path") or "").rstrip("/")
    if root_path and path.startswith(root_path):
        path = path[len(root_path):] or "/"
    runtime_public_paths = {
        "/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout",
    }
    if (
        path.startswith("/api/")
        and not getattr(app.state, "database_ready", True)
        and path not in runtime_public_paths
    ):
        return JSONResponse({"detail": "database_not_ready"}, status_code=503)
    auth_bypass_paths = runtime_public_paths | {"/api/automation/tick"}
    if (
        auth_required()
        and path.startswith("/api/")
        and path not in auth_bypass_paths
        and not verify_session(request.cookies.get(COOKIE_NAME))
    ):
        return JSONResponse(
            {"detail": "authentication_required"},
            status_code=401,
        )
    return await call_next(request)


@app.get("/api/health")
def health(): return {"ok": True}


@app.get("/api/auth/status")
def auth_status(request: Request):
    required = auth_required()
    return {
        "required": required,
        "authenticated": not required
        or verify_session(request.cookies.get(COOKIE_NAME)),
    }


@app.post("/api/auth/login")
def auth_login(response: Response, payload: dict = Body(default_factory=dict)):
    if not auth_required():
        return {"ok": True}
    if not access_key_matches(str(payload.get("access_key") or "")):
        raise HTTPException(status_code=401, detail="访问密钥不正确")
    response.set_cookie(
        COOKIE_NAME,
        create_session(),
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


from backend.api.routes import router as routes_router  # noqa: E402
from backend.api.sse import router as sse_router  # noqa: E402
from backend.api.read import router as read_router  # noqa: E402
from backend.api.swing import router as swing_router  # noqa: E402
from backend.api.discipline import router as discipline_router  # noqa: E402
from backend.api.automation import router as automation_router  # noqa: E402
app.include_router(routes_router)
app.include_router(sse_router)
app.include_router(read_router)
app.include_router(swing_router)
app.include_router(discipline_router)
app.include_router(automation_router)

# --- single-port deploy: FastAPI serves the built frontend (spec §12.1) ---
# Registered AFTER the API routers so /api/* always wins; the SPA catch-all only
# handles everything else, falling back to index.html for client-side routes.
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api"):
            raise HTTPException(status_code=404)  # don't mask unknown API routes as html
        candidate = DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")
