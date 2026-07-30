"""Mount the persistent Sensory Vocabulary Lab inside Cindy's Space."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from backend.engine import engine
from backend import sensory_vocab_auth as auth
from backend import sensory_vocab_store as store


DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "sensory-vocabulary-lab"
APP_ROOT = Path(os.getenv("SENSORY_VOCAB_ROOT", DEFAULT_ROOT)).resolve()
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
app.state.store_ready = False
app.state.store_error = None


def initialize_store() -> None:
    store.ensure_schema()
    app.state.store_ready = True
    app.state.store_error = None


def relative_path(request: Request) -> str:
    path = request.url.path
    root_path = str(request.scope.get("root_path") or "").rstrip("/")
    if root_path and path.startswith(root_path):
        return path[len(root_path):] or "/"
    return path


def request_origin(request: Request) -> str:
    """Return the browser origin without the mounted application's root path."""
    return f"{request.url.scheme}://{request.url.netloc}".rstrip("/")


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
        if "retryable" in details:
            payload["error"]["retryable"] = details["retryable"]
    return JSONResponse(payload, status_code=status)


@app.middleware("http")
async def protect_shared_library(request: Request, call_next):
    path = relative_path(request)
    public_api = {"/api/health", "/api/access"}
    is_api = path.startswith("/api/")
    is_private_asset = path.startswith("/assets/generated/")

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != request_origin(request):
            return error_response(403, "origin_mismatch", "请求来源无效。")

    if (is_api and path not in public_api) or is_private_asset:
        if not app.state.store_ready:
            return error_response(
                503,
                "library_not_ready",
                "共享词库正在启动，请稍后刷新。",
                details={"retryable": True},
            )
        if not auth.verify_session(request.cookies.get(auth.COOKIE_NAME)):
            return error_response(401, "access_required", "请先输入访问密钥。")
    return await call_next(request)


@app.exception_handler(vocab.ServiceError)
async def service_error_handler(_request: Request, error: Exception) -> JSONResponse:
    return JSONResponse(error.payload(), status_code=error.status)


async def read_json(request: Request, *, max_bytes: int | None = None) -> dict:
    limit = max_bytes or vocab.MAX_BODY_BYTES
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError as exc:
        raise vocab.ServiceError(400, "invalid_length", "请求长度无效。") from exc
    if length <= 0 or length > limit:
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
        "persistentLibrary": True,
        "libraryReady": bool(app.state.store_ready),
        "accessConfigured": auth.access_configured(),
    }


@app.get("/api/access")
async def access_status(request: Request) -> dict:
    return {
        "configured": auth.access_configured(),
        "authenticated": auth.verify_session(
            request.cookies.get(auth.COOKIE_NAME)
        ),
    }


@app.post("/api/access")
async def access_login(request: Request, response: Response) -> dict:
    if not app.state.store_ready:
        raise vocab.ServiceError(
            503,
            "library_not_ready",
            "共享词库正在启动，请稍后再试。",
            details={"retryable": True},
        )
    if not auth.access_configured():
        raise vocab.ServiceError(
            503,
            "access_not_configured",
            "站点尚未配置访问密钥。",
            details={"retryable": False},
        )
    visitor = auth.visitor_hash(request.client.host if request.client else None)
    retry_after = auth.blocked_seconds(visitor)
    if retry_after:
        return error_response(
            429,
            "access_rate_limited",
            "连续输入错误次数过多，请稍后再试。",
            details={"retryAfterSeconds": retry_after, "retryable": True},
        )
    payload = await read_json(request)
    if not auth.access_key_matches(str(payload.get("key") or "")):
        retry_after = auth.record_failure(visitor)
        details = {"retryable": True}
        if retry_after:
            details["retryAfterSeconds"] = retry_after
        return error_response(
            401,
            "invalid_access_key",
            "访问密钥不正确。",
            details=details,
        )
    auth.clear_failures(visitor)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.create_session(),
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        secure=auth.hosted_runtime(),
        samesite="lax",
        path=str(request.scope.get("root_path") or "/") + (
            "/" if request.scope.get("root_path") else ""
        ),
    )
    return {"authenticated": True}


@app.delete("/api/access")
async def access_logout(request: Request, response: Response) -> dict:
    path = str(request.scope.get("root_path") or "/") + (
        "/" if request.scope.get("root_path") else ""
    )
    response.delete_cookie(auth.COOKIE_NAME, path=path)
    return {"authenticated": False}


@app.get("/api/demo")
async def demo() -> dict:
    return await run_in_threadpool(vocab.load_demo)


def wait_for_entries(words: list[str], level: str, timeout: float = 125.0):
    deadline = time.monotonic() + timeout
    remaining = list(words)
    found: dict[str, store.VocabEntry] = {}
    while remaining and time.monotonic() < deadline:
        with Session(engine) as session:
            current = store.find_entries(session, remaining, level)
            for word in list(remaining):
                entry = current.get(store.normalize_word(word))
                if entry is not None:
                    session.expunge(entry)
                    found[store.normalize_word(word)] = entry
                    remaining.remove(word)
        if remaining:
            time.sleep(0.25)
    if remaining:
        raise vocab.ServiceError(
            409,
            "generation_in_progress",
            "相同词条正在另一处生成，请稍后重试。",
            details={"retryable": True},
        )
    return found


def create_shared_pack(words: list[str], level: str) -> dict[str, Any]:
    with Session(engine) as session:
        existing = store.find_entries(session, words, level)
        initial_hits = [
            existing[store.normalize_word(word)].display_word
            for word in words
            if store.normalize_word(word) in existing
        ]
    misses = [word for word in words if store.normalize_word(word) not in existing]
    owner = uuid.uuid4().hex
    owned: list[str] = []
    waiting: list[str] = []
    generated: list[str] = []
    try:
        if misses:
            owned, waiting = store.claim_generation_leases(misses, level, owner)
        if owned:
            planned = vocab.plan_wordpack(owned, level, vocab.api_key())
            with Session(engine) as session:
                for word_payload in planned.get("words") or []:
                    current = store.find_entry(
                        session,
                        str(word_payload.get("word") or ""),
                        level,
                    )
                    if current is None:
                        store.create_entry(
                            session,
                            word_payload,
                            level,
                            planner_model=vocab.text_model(),
                            prompt_version="scene-director-v1",
                        )
                        generated.append(str(word_payload.get("word") or ""))
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
        if waiting:
            wait_for_entries(waiting, level)
    finally:
        if owned:
            store.release_generation_leases(owner)

    with Session(engine) as session:
        final_entries = store.find_entries(session, words, level)
        ordered = [
            final_entries.get(store.normalize_word(word))
            for word in words
        ]
        if any(entry is None for entry in ordered):
            raise vocab.ServiceError(
                502,
                "cache_write_failed",
                "词条生成成功，但未能完整保存，请重试。",
            )
        entries = [entry for entry in ordered if entry is not None]
        title = ", ".join(entry.display_word for entry in entries)
        pack = store.save_pack(
            session,
            title=title,
            level=level,
            entries=entries,
        )
        session.commit()
        session.refresh(pack)
        result = store.serialize_pack(session, pack)
    return {
        "pack": result,
        "cacheHits": initial_hits + [store.normalize_word(word) for word in waiting],
        "generatedWords": generated,
    }


@app.get("/api/packs")
async def packs(limit: int = 24, offset: int = 0) -> dict:
    rows, has_more = await run_in_threadpool(store.list_packs, limit, offset)
    return {
        "packs": rows,
        "hasMore": has_more,
        "nextOffset": max(offset, 0) + len(rows),
    }


@app.get("/api/packs/{pack_id}")
async def pack_detail(pack_id: str) -> dict:
    pack = await run_in_threadpool(store.get_pack, pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="词包不存在")
    return {"pack": pack}


@app.post("/api/packs")
async def create_pack(request: Request) -> dict:
    payload = await read_json(request)
    words = vocab.clean_words(payload.get("words"))
    level = vocab.clean_level(payload.get("level"))
    return await run_in_threadpool(create_shared_pack, words, level)


@app.post("/api/plan")
async def legacy_plan(request: Request) -> dict:
    """Compatibility alias for old clients during the rollout."""
    return await create_pack(request)


def optimized_webp(raw: bytes) -> tuple[bytes, int, int]:
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise vocab.ServiceError(502, "invalid_image_output", "图片文件无效或过大。")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            if source.width < 64 or source.height < 64:
                raise vocab.ServiceError(
                    502,
                    "invalid_image_output",
                    "图片尺寸异常。",
                )
            if source.mode in {"RGBA", "LA"}:
                rgba = source.convert("RGBA")
                canvas = Image.new("RGBA", rgba.size, "white")
                canvas.alpha_composite(rgba)
                rendered = canvas.convert("RGB")
            else:
                rendered = source.convert("RGB")
            rendered.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            rendered.save(
                output,
                format="WEBP",
                quality=88,
                method=6,
                exif=b"",
            )
            return output.getvalue(), rendered.width, rendered.height
    except (UnidentifiedImageError, OSError) as exc:
        raise vocab.ServiceError(
            502,
            "invalid_image_output",
            "图片服务返回了无法读取的图像。",
        ) from exc


def generated_image_bytes(
    *,
    word: str,
    scene_id: str,
    scene_prompt: str,
    regenerate: bool,
) -> tuple[bytes, dict[str, Any]]:
    result = vocab.generate_image(
        word=word,
        scene_id=scene_id,
        scene_prompt=scene_prompt,
        key=vocab.api_key(),
        regenerate=regenerate,
    )
    image_path = str(result.get("image") or "")
    if not image_path.startswith("/assets/") or ".." in image_path:
        raise vocab.ServiceError(502, "invalid_image_output", "图片保存路径无效。")
    target = (APP_ROOT / image_path.lstrip("/")).resolve()
    if not target.is_relative_to(APP_ROOT) or not target.is_file():
        raise vocab.ServiceError(502, "invalid_image_output", "生成图片没有保存成功。")
    return target.read_bytes(), result


def generate_scene_image(scene_id: str, regenerate: bool) -> dict[str, Any]:
    with Session(engine) as session:
        scene = session.get(store.VocabScene, scene_id)
        if scene is None:
            raise vocab.ServiceError(404, "scene_not_found", "场景不存在。")
        entry = session.get(store.VocabEntry, scene.entry_id)
        if entry is None:
            raise vocab.ServiceError(404, "entry_not_found", "词条不存在。")
        if scene.image_id and not regenerate:
            return {
                "image": f"/api/images/{scene.image_id}",
                "source": "library",
                "cached": True,
                "affectedPacks": store.count_entry_packs(session, entry.entry_id),
            }
        word = entry.display_word
        prompt = str(scene.content_json.get("imagePrompt") or "")
        old_image_id = scene.image_id

    raw, upstream = generated_image_bytes(
        word=word,
        scene_id=scene_id,
        scene_prompt=prompt,
        regenerate=regenerate,
    )
    image_data, width, height = optimized_webp(raw)
    with Session(engine) as session:
        scene = session.get(store.VocabScene, scene_id)
        if scene is None:
            raise vocab.ServiceError(409, "scene_changed", "场景已更新，请刷新。")
        image = store.upsert_image(
            session,
            data=image_data,
            width=width,
            height=height,
            image_model=vocab.image_model(),
            style_version=vocab.STYLE_VERSION,
        )
        if scene.image_id and not regenerate:
            store.delete_image_if_unreferenced(session, image.image_id)
            existing_id = scene.image_id
            session.commit()
            return {
                "image": f"/api/images/{existing_id}",
                "source": "library",
                "cached": True,
            }
        scene.image_id = image.image_id
        scene.image_revision += 1
        scene.updated_at = store.utcnow()
        session.add(scene)
        session.flush()
        new_image_id = image.image_id
        store.delete_image_if_unreferenced(session, old_image_id)
        entry = session.get(store.VocabEntry, scene.entry_id)
        affected = (
            store.count_entry_packs(session, entry.entry_id)
            if entry else 0
        )
        session.commit()
    return {
        "image": f"/api/images/{new_image_id}",
        "source": "live",
        "cached": bool(upstream.get("cached")) and not regenerate,
        "affectedPacks": affected,
    }


@app.post("/api/generate-image")
async def generate_image(request: Request) -> dict:
    payload = await read_json(request)
    scene_id = str(payload.get("sceneId") or "").strip()
    if not scene_id or len(scene_id) > 96:
        raise vocab.ServiceError(400, "invalid_image_fields", "场景标识无效。")
    return await run_in_threadpool(
        generate_scene_image,
        scene_id,
        bool(payload.get("regenerate")),
    )


@app.post("/api/scenes/{scene_id}/regenerate")
async def regenerate_scene(scene_id: str) -> dict:
    return await run_in_threadpool(generate_scene_image, scene_id, True)


def regenerate_entry(entry_id: str) -> dict[str, Any]:
    with Session(engine) as session:
        entry = session.get(store.VocabEntry, entry_id)
        if entry is None:
            raise vocab.ServiceError(404, "entry_not_found", "词条不存在。")
        word = entry.display_word
        level = entry.level
        affected = store.count_entry_packs(session, entry_id)

    planned = vocab.plan_wordpack([word], level, vocab.api_key())
    words = planned.get("words") or []
    if len(words) != 1:
        raise vocab.ServiceError(502, "invalid_model_output", "词条重做结果不完整。")
    next_word = dict(words[0])
    staged: list[tuple[dict[str, Any], bytes, int, int]] = []
    for scene_payload in next_word.get("scenes") or []:
        scene_data = dict(scene_payload)
        raw, _ = generated_image_bytes(
            word=word,
            scene_id=f"regenerate-{uuid.uuid4().hex}",
            scene_prompt=str(scene_data.get("imagePrompt") or ""),
            regenerate=True,
        )
        image_data, width, height = optimized_webp(raw)
        staged.append((scene_data, image_data, width, height))
    if not staged:
        raise vocab.ServiceError(502, "invalid_model_output", "词条重做缺少场景。")

    with Session(engine) as session:
        entry = session.get(store.VocabEntry, entry_id)
        if entry is None:
            raise vocab.ServiceError(409, "entry_changed", "词条已被更新，请刷新。")
        old_scenes = session.exec(
            select(store.VocabScene).where(
                store.VocabScene.entry_id == entry_id
            )
        ).all()
        old_image_ids = [scene.image_id for scene in old_scenes]
        session.exec(
            delete(store.VocabScene).where(
                store.VocabScene.entry_id == entry_id
            )
        )
        session.flush()
        entry.content_json = {
            key: value
            for key, value in next_word.items()
            if key not in {"id", "scenes"}
        }
        entry.display_word = str(next_word.get("word") or word)
        entry.planner_model = vocab.text_model()
        entry.prompt_version = "scene-director-v1"
        entry.revision += 1
        entry.updated_at = store.utcnow()
        session.add(entry)
        for position, (scene_payload, image_data, width, height) in enumerate(staged):
            image = store.upsert_image(
                session,
                data=image_data,
                width=width,
                height=height,
                image_model=vocab.image_model(),
                style_version=vocab.STYLE_VERSION,
            )
            session.add(
                store.VocabScene(
                    scene_id=store.new_id("scene"),
                    entry_id=entry_id,
                    position=position,
                    content_json={
                        key: value
                        for key, value in scene_payload.items()
                        if key not in {"id", "image", "imageSource"}
                    },
                    image_id=image.image_id,
                    image_revision=1,
                )
            )
        session.flush()
        for image_id in old_image_ids:
            store.delete_image_if_unreferenced(session, image_id)
        session.commit()
        session.refresh(entry)
        serialized = store.serialize_entry(session, entry)
    return {"entry": serialized, "affectedPacks": affected}


@app.post("/api/entries/{entry_id}/regenerate")
async def regenerate_whole_entry(entry_id: str) -> dict:
    return await run_in_threadpool(regenerate_entry, entry_id)


@app.get("/api/images/{image_id}")
async def image_file(image_id: str) -> Response:
    image = await run_in_threadpool(store.get_image, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    return Response(
        content=image.data,
        media_type=image.mime_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "ETag": f'"{image.sha256}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


def parse_import_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError:
        return None


def legacy_image_bytes(value: Any) -> bytes | None:
    path = str(value or "")
    if not path.startswith("/assets/") or ".." in path:
        return None
    target = (APP_ROOT / path.lstrip("/")).resolve()
    if not target.is_relative_to(APP_ROOT) or not target.is_file():
        return None
    return target.read_bytes()


def import_legacy_packs(raw_packs: list[Any]) -> dict[str, Any]:
    imported = 0
    skipped = 0
    missing_images = 0
    for raw_pack in reversed(raw_packs[:8]):
        if not isinstance(raw_pack, dict):
            skipped += 1
            continue
        level = vocab.clean_level(raw_pack.get("level"))
        words_payload = raw_pack.get("words")
        if not isinstance(words_payload, list) or not words_payload:
            skipped += 1
            continue
        fingerprint_source = {
            "id": raw_pack.get("id"),
            "level": level,
            "words": [
                store.normalize_word(str(item.get("word") or ""))
                for item in words_payload
                if isinstance(item, dict)
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_source,
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with Session(engine) as session:
            duplicate = session.exec(
                select(store.VocabPack).where(
                    store.VocabPack.import_fingerprint == fingerprint
                )
            ).first()
            if duplicate:
                skipped += 1
                continue
            entries: list[store.VocabEntry] = []
            for word_payload in words_payload:
                if not isinstance(word_payload, dict):
                    continue
                display_word = str(word_payload.get("word") or "").strip()
                try:
                    vocab.clean_words([display_word])
                except vocab.ServiceError:
                    continue
                entry = store.find_entry(session, display_word, level)
                created = False
                if entry is None:
                    entry = store.create_entry(
                        session,
                        word_payload,
                        level,
                        planner_model="legacy-import",
                        prompt_version="legacy-import",
                    )
                    created = True
                entries.append(entry)
                if not created:
                    continue
                scenes = session.exec(
                    select(store.VocabScene)
                    .where(store.VocabScene.entry_id == entry.entry_id)
                    .order_by(store.VocabScene.position)
                ).all()
                for scene, scene_payload in zip(
                    scenes,
                    word_payload.get("scenes") or [],
                ):
                    raw = legacy_image_bytes(
                        scene_payload.get("image")
                        if isinstance(scene_payload, dict)
                        else None
                    )
                    if raw is None:
                        missing_images += 1
                        continue
                    try:
                        data, width, height = optimized_webp(raw)
                    except vocab.ServiceError:
                        missing_images += 1
                        continue
                    image = store.upsert_image(
                        session,
                        data=data,
                        width=width,
                        height=height,
                        image_model="legacy-import",
                        style_version="legacy-import",
                    )
                    scene.image_id = image.image_id
                    scene.image_revision = 1
                    session.add(scene)
            if not entries:
                skipped += 1
                session.rollback()
                continue
            store.save_pack(
                session,
                title=str(raw_pack.get("title") or ""),
                level=level,
                entries=entries,
                source="live",
                display_date=str(raw_pack.get("date") or ""),
                import_fingerprint=fingerprint,
                created_at=parse_import_datetime(raw_pack.get("createdAt")),
            )
            session.commit()
            imported += 1
    return {
        "importedPacks": imported,
        "skippedPacks": skipped,
        "missingImages": missing_images,
    }


@app.post("/api/import/legacy")
async def import_legacy(request: Request) -> dict:
    payload = await read_json(request, max_bytes=512 * 1024)
    packs = payload.get("packs")
    if not isinstance(packs, list):
        raise vocab.ServiceError(400, "invalid_import", "没有可导入的旧词包。")
    return await run_in_threadpool(import_legacy_packs, packs)


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
