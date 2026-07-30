"""Mount the Sensory Vocabulary Lab inside Cindy's composite Space service."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "sensory-vocabulary-lab"
APP_ROOT = Path(os.getenv("SENSORY_VOCAB_ROOT", DEFAULT_ROOT)).resolve()
# Composite deployments must use runtime environment variables. Avoid silently
# inheriting a developer Mac's standalone config when portal tests run locally.
os.environ.setdefault("VOCAB_CONFIG_PATH", str(APP_ROOT / ".runtime-config.json"))
os.environ.setdefault("VOCAB_API_KEY_FILE", str(APP_ROOT / ".runtime-key"))


def _load_server() -> ModuleType:
    module_path = APP_ROOT / "server.py"
    spec = importlib.util.spec_from_file_location("sensory_vocabulary_server", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Sensory Vocabulary Lab from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vocab = _load_server()
app = FastAPI(
    title="Sensory Vocabulary Lab",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(vocab.ServiceError)
async def service_error_handler(_request: Request, error: Exception) -> JSONResponse:
    return JSONResponse(error.payload(), status_code=error.status)


async def read_json(request: Request) -> dict:
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError as exc:
        raise vocab.ServiceError(400, "invalid_length", "请求长度无效。") from exc
    if length <= 0 or length > vocab.MAX_BODY_BYTES:
        raise vocab.ServiceError(413, "invalid_body_size", "请求为空或过大。")
    try:
        payload = await request.json()
    except (UnicodeDecodeError, ValueError) as exc:
        raise vocab.ServiceError(400, "invalid_json", "请求不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise vocab.ServiceError(400, "invalid_json", "请求必须是 JSON 对象。")
    return payload


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "mode": "live" if vocab.api_key() else "demo",
        "provider": vocab.provider(),
        "credentialSource": vocab.credential_source(),
        "textModel": vocab.text_model(),
        "imageModel": vocab.image_model(),
        "styleVersion": vocab.STYLE_VERSION,
    }


@app.get("/api/demo")
async def demo() -> dict:
    return await run_in_threadpool(vocab.load_demo)


@app.post("/api/plan")
async def plan(request: Request) -> dict:
    payload = await read_json(request)
    words = vocab.clean_words(payload.get("words"))
    level = vocab.clean_level(payload.get("level"))
    pack = await run_in_threadpool(vocab.plan_wordpack, words, level, vocab.api_key())
    return {"pack": pack}


@app.post("/api/generate-image")
async def generate_image(request: Request) -> dict:
    payload = await read_json(request)
    word = str(payload.get("word") or "").strip()
    scene_id = str(payload.get("sceneId") or "").strip()
    scene_prompt = str(payload.get("imagePrompt") or "").strip()
    if not word or not scene_id or not scene_prompt:
        raise vocab.ServiceError(400, "missing_image_fields", "缺少单词或画面描述。")
    if len(word) > vocab.MAX_WORD_LENGTH or len(scene_id) > 96:
        raise vocab.ServiceError(400, "invalid_image_fields", "单词或场景标识过长。")
    return await run_in_threadpool(
        vocab.generate_image,
        word=word,
        scene_id=scene_id,
        scene_prompt=scene_prompt,
        key=vocab.api_key(),
        regenerate=bool(payload.get("regenerate")),
    )


@app.get("/")
@app.get("/index.html")
async def index() -> FileResponse:
    return FileResponse(APP_ROOT / "index.html")


@app.get("/app.js")
async def javascript() -> FileResponse:
    return FileResponse(APP_ROOT / "app.js", media_type="text/javascript")


@app.get("/styles.css")
async def stylesheet() -> FileResponse:
    return FileResponse(APP_ROOT / "styles.css", media_type="text/css")


app.mount("/assets", StaticFiles(directory=APP_ROOT / "assets"), name="assets")
