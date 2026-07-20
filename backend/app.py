import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import SQLModel
from backend import backup, config, migrate
from backend.engine import engine
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
    if not os.getenv("TREND_DESK_DB_PATH") and backup.relocate_legacy_db(config.LEGACY_DB_PATH, config.DB_PATH):
        log.info("relocated legacy DB out of iCloud (D7): %s → %s", config.LEGACY_DB_PATH, config.DB_PATH)
    SQLModel.metadata.create_all(engine)
    added = migrate.ensure_columns(engine)
    if added:
        log.info("schema migrated: added columns %s", ", ".join(added))
    if config.DB_PATH.exists() and config.DB_PATH.stat().st_size > 0:
        result = backup.integrity_check(config.DB_PATH)
        if result != "ok":
            log.error("DB integrity_check FAIL: %s — restore manually from %s", result, config.BACKUPS)
            raise RuntimeError(f"DB corrupted: {result}")
    scheduler_task = None
    from backend.discipline.scheduler import scheduler_enabled, scheduler_loop
    if scheduler_enabled():
        scheduler_task = __import__("asyncio").create_task(scheduler_loop())
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except __import__("asyncio").CancelledError:
                pass

app = FastAPI(title="trend-desk", lifespan=lifespan)


@app.middleware("http")
async def protect_private_api(request: Request, call_next):
    """Keep financial APIs private when the app is on a public Space URL."""
    public_paths = {
        "/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout",
    }
    if (
        auth_required()
        and request.url.path.startswith("/api/")
        and request.url.path not in public_paths
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
app.include_router(routes_router)
app.include_router(sse_router)
app.include_router(read_router)
app.include_router(swing_router)
app.include_router(discipline_router)

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
