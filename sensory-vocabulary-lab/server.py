#!/usr/bin/env python3
"""词汇感官实验室：零依赖静态服务器与可切换的 OpenAI 兼容模型代理。"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DEMO_PATH = ROOT / "data" / "demo-wordpack.json"
STYLE_PATH = ROOT / "prompts" / "visual-style.txt"
SCENE_DIRECTOR_PATH = ROOT / "prompts" / "scene-director.txt"
GENERATED_DIR = ROOT / "assets" / "generated"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "词汇感官实验室"

DEFAULT_PROVIDER = "openrouter"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_TEXT_MODEL = "qwen/qwen3.7-plus"
OPENROUTER_IMAGE_MODEL = "openai/gpt-image-2"
AI_BUILDER_API_BASE = "https://space.ai-builders.com/backend/v1"
AI_BUILDER_TEXT_MODEL = "gpt-5"
AI_BUILDER_IMAGE_MODEL = "gpt-image-1.5"
OPENAI_API_BASE = "https://api.openai.com/v1"
STYLE_VERSION = "sensory-ink-v1"
MAX_BODY_BYTES = 64 * 1024
MAX_WORDS = 8
MAX_WORD_LENGTH = 48
_CACHE_LOCK = threading.Lock()


class ServiceError(Exception):
    """可安全返回给前端的服务错误。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                **self.details,
            }
        }


def load_demo() -> dict[str, Any]:
    return json.loads(DEMO_PATH.read_text(encoding="utf-8"))


def load_style_prompt() -> str:
    return STYLE_PATH.read_text(encoding="utf-8").strip()


def load_scene_director_prompt() -> str:
    return SCENE_DIRECTOR_PATH.read_text(encoding="utf-8").strip()


def config_path() -> Path:
    raw = os.environ.get("VOCAB_CONFIG_PATH", "").strip()
    return Path(raw).expanduser() if raw else APP_SUPPORT_DIR / "config.json"


def key_path() -> Path:
    raw = os.environ.get("VOCAB_API_KEY_FILE", "").strip()
    return Path(raw).expanduser() if raw else APP_SUPPORT_DIR / "openrouter.key"


def load_local_config() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def configured_value(env_name: str, config_name: str, default: str) -> str:
    environment = os.environ.get(env_name, "").strip()
    if environment:
        return environment
    configured = load_local_config().get(config_name)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return default


def provider() -> str:
    configured = configured_value("VOCAB_PROVIDER", "provider", "")
    if configured:
        return configured.casefold()
    if os.environ.get("AI_BUILDER_TOKEN", "").strip():
        return "ai_builder"
    return DEFAULT_PROVIDER


def text_model() -> str:
    configured = configured_value("VOCAB_TEXT_MODEL", "text_model", "")
    if configured:
        return configured
    return AI_BUILDER_TEXT_MODEL if provider() == "ai_builder" else OPENROUTER_TEXT_MODEL


def image_model() -> str:
    configured = configured_value("VOCAB_IMAGE_MODEL", "image_model", "")
    if configured:
        return configured
    return AI_BUILDER_IMAGE_MODEL if provider() == "ai_builder" else OPENROUTER_IMAGE_MODEL


def api_key() -> str:
    provider_key_names = {
        "ai_builder": ("AI_BUILDER_TOKEN", "AI_BUILDER_API_KEY"),
        "openrouter": ("OPENROUTER_API_KEY",),
        "openai": ("OPENAI_API_KEY",),
        "openai_compatible": ("OPENAI_COMPATIBLE_API_KEY", "OPENAI_API_KEY"),
    }
    for name in ("VOCAB_API_KEY", *provider_key_names.get(provider(), ("OPENAI_API_KEY",))):
        value = os.environ.get(name, "").strip()
        if value:
            return value

    path = key_path()
    try:
        mode = path.stat().st_mode
        if mode & 0o077:
            return ""
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def credential_source() -> str:
    provider_key_names = {
        "ai_builder": ("AI_BUILDER_TOKEN", "AI_BUILDER_API_KEY"),
        "openrouter": ("OPENROUTER_API_KEY",),
        "openai": ("OPENAI_API_KEY",),
        "openai_compatible": ("OPENAI_COMPATIBLE_API_KEY", "OPENAI_API_KEY"),
    }
    for name in ("VOCAB_API_KEY", *provider_key_names.get(provider(), ("OPENAI_API_KEY",))):
        if os.environ.get(name, "").strip():
            return name
    path = key_path()
    try:
        if path.stat().st_mode & 0o077:
            return "insecure-file"
        return "local-private-file" if path.read_text(encoding="utf-8").strip() else "missing"
    except (OSError, UnicodeDecodeError):
        return "missing"


def api_base() -> str:
    configured = configured_value("VOCAB_API_BASE", "api_base", "")
    if not configured:
        configured = os.environ.get("OPENAI_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    if provider() == "ai_builder":
        return AI_BUILDER_API_BASE
    if provider() == "openrouter":
        return OPENROUTER_API_BASE
    return OPENAI_API_BASE


def image_endpoint() -> str:
    configured = configured_value("VOCAB_IMAGE_ENDPOINT", "image_endpoint", "")
    if configured:
        return configured.lstrip("/")
    return "images" if provider() == "openrouter" else "images/generations"


def image_request_payload(prompt: str) -> dict[str, Any]:
    model = image_model()
    if provider() == "openrouter":
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "aspect_ratio": configured_value(
                "VOCAB_IMAGE_ASPECT_RATIO", "image_aspect_ratio", "3:2"
            ),
            "n": 1,
        }
        if "gemini" in model:
            payload["resolution"] = configured_value(
                "VOCAB_IMAGE_RESOLUTION", "image_resolution", "1K"
            )
            banana_provider = configured_value(
                "VOCAB_BANANA_PROVIDER", "banana_provider", "google-vertex/global"
            )
            payload["provider"] = {
                "only": [banana_provider],
                "allow_fallbacks": False,
            }
        if "gpt-image" in model:
            payload["quality"] = configured_value(
                "VOCAB_IMAGE_QUALITY", "image_quality", "medium"
            )
        return payload
    payload = {
        "model": model,
        "prompt": prompt,
        "size": configured_value("VOCAB_IMAGE_SIZE", "image_size", "1536x1024"),
        "quality": configured_value("VOCAB_IMAGE_QUALITY", "image_quality", "medium"),
        "n": 1,
    }
    if provider() == "ai_builder":
        if "gemini" in model.casefold():
            # The Builder proxy accepts Gemini image generation through the
            # OpenAI-compatible endpoint, but forwards GPT-only tuning fields
            # as-is. Keep the Banana request deliberately minimal.
            return {"model": model, "prompt": prompt, "n": 1}
        # GPT image models return base64 data by default and reject the legacy
        # response_format parameter even though the Builder proxy documents it.
        payload["output_format"] = "png"
    return payload


def clean_words(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ServiceError(400, "invalid_words", "words 必须是字符串数组。")

    result: list[str] = []
    seen: set[str] = set()
    allowed = re.compile(r"^[\w'’\- ]+$", re.UNICODE)

    for value in raw:
        if not isinstance(value, str):
            raise ServiceError(400, "invalid_word", "每个词都必须是字符串。")
        word = re.sub(r"\s+", " ", value.strip())
        if not word:
            continue
        if len(word) > MAX_WORD_LENGTH or not allowed.fullmatch(word):
            raise ServiceError(
                400,
                "invalid_word",
                f"“{word[:24]}”包含不支持的字符，或长度超过 {MAX_WORD_LENGTH}。",
            )
        key = word.casefold()
        if key not in seen:
            result.append(word)
            seen.add(key)

    if not result:
        raise ServiceError(400, "empty_words", "请至少输入一个单词。")
    if len(result) > MAX_WORDS:
        raise ServiceError(400, "too_many_words", f"一次最多生成 {MAX_WORDS} 个词。")
    return result


def clean_level(raw: Any) -> str:
    level = str(raw or "PET").strip().upper()
    allowed = {"KET", "PET", "FCE", "IELTS", "TOEFL", "GRE", "自定义"}
    return level if level in allowed else "自定义"


def demo_subset(words: list[str], level: str) -> dict[str, Any] | None:
    demo = load_demo()
    index = {item["word"].casefold(): item for item in demo["words"]}
    if any(word.casefold() not in index for word in words):
        return None

    picked = [copy.deepcopy(index[word.casefold()]) for word in words]
    return {
        "id": "demo-" + "-".join(item["id"] for item in picked),
        "title": ", ".join(item["word"] for item in picked) + ("…" if len(picked) > 1 else ""),
        "level": level,
        "date": datetime.now().strftime("%Y/%m/%d"),
        "source": "demo",
        "words": picked,
    }


def planner_schema() -> dict[str, Any]:
    scene = {
        "type": "object",
        "properties": {
            "captionCn": {"type": "string"},
            "imagePrompt": {"type": "string"},
            "usageHookCn": {"type": "string"},
        },
        "required": ["captionCn", "imagePrompt", "usageHookCn"],
        "additionalProperties": False,
    }
    word = {
        "type": "object",
        "properties": {
            "word": {"type": "string"},
            "pos": {"type": "string"},
            "phonetic": {"type": "string"},
            "coreSenseCn": {"type": "string"},
            "sensoryPromptCn": {"type": "string"},
            "feelingChips": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 3,
            },
            "exampleEn": {"type": "string"},
            "exampleZh": {"type": "string"},
            "scenes": {
                "type": "array",
                "items": scene,
                "minItems": 2,
                "maxItems": 2,
            },
        },
        "required": [
            "word",
            "pos",
            "phonetic",
            "coreSenseCn",
            "sensoryPromptCn",
            "feelingChips",
            "exampleEn",
            "exampleZh",
            "scenes",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "words": {
                "type": "array",
                "items": word,
                "minItems": 1,
                "maxItems": MAX_WORDS,
            },
        },
        "required": ["title", "words"],
        "additionalProperties": False,
    }


def planner_instructions(level: str) -> str:
    return (
        f"目标学习者级别：{level}。\n\n"
        "先为每个词确定该级别最常用的词性、核心释义和具体使用语境；"
        "再严格遵循下面的视觉导演规则。不要罗列全部字典义项。"
        "必须按用户输入的原始顺序返回，每个词给出两个视觉上完全不同、但语义骨架一致的场景。\n\n"
        + load_scene_director_prompt()
    )


def openai_request(
    endpoint: str,
    payload: dict[str, Any],
    key: str,
    *,
    timeout: int,
) -> tuple[dict[str, Any], str | None]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"{api_base()}/{endpoint.lstrip('/')}"

    for attempt in range(2):
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        if provider() == "openrouter":
            headers["X-Title"] = "Sensory Vocabulary Lab"
        request = Request(
            url,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                request_id = response.headers.get("x-request-id")
                return json.loads(raw.decode("utf-8")), request_id
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                error_body = json.loads(raw)
            except json.JSONDecodeError:
                error_body = {}
            remote = error_body.get("error", {}) if isinstance(error_body, dict) else {}
            remote_code = str(remote.get("code") or remote.get("type") or "openai_error")
            remote_message = str(remote.get("message") or "生成服务暂时不可用。")
            if exc.code in {429, 500, 502, 503, 504} and attempt == 0:
                time.sleep(0.6)
                continue
            if exc.code == 403 and "provider terms of service" in remote_message.casefold():
                raise ServiceError(
                    503,
                    "provider_terms_restricted",
                    "当前模型账户没有所选模型的使用资格（常见原因是账户或账单地区受模型条款限制）。"
                    "这不是单词或画面描述触发审核，请站点维护者检查模型提供商设置。",
                    details={"retryable": False, "upstreamStatus": 403},
                ) from exc
            if remote_code == "moderation_blocked":
                raise ServiceError(
                    400,
                    "moderation_blocked",
                    "这个画面描述触发了安全限制，请换一种更中性的表达。",
                ) from exc
            status = 429 if exc.code == 429 else 502
            raise ServiceError(
                status,
                remote_code,
                remote_message,
                details={"requestId": exc.headers.get("x-request-id")},
            ) from exc
        except (URLError, TimeoutError) as exc:
            if attempt == 0:
                time.sleep(0.6)
                continue
            raise ServiceError(504, "upstream_timeout", "生成等待超时，请稍后重试。") from exc

    raise ServiceError(502, "upstream_error", "生成服务暂时不可用。")


def extract_response_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    raise ServiceError(502, "empty_model_output", "语义策划没有返回可读取的结果。")


def extract_chat_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ServiceError(502, "empty_model_output", "语义策划没有返回可读取的结果。")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content
    raise ServiceError(502, "empty_model_output", "语义策划没有返回可读取的结果。")


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return cleaned[:36] or uuid.uuid4().hex[:10]


def normalize_plan(plan: dict[str, Any], requested: list[str], level: str) -> dict[str, Any]:
    words = plan.get("words")
    if not isinstance(words, list) or len(words) != len(requested):
        raise ServiceError(502, "invalid_model_output", "语义策划返回的单词数量不正确。")

    by_word = {
        str(item.get("word", "")).casefold(): item
        for item in words
        if isinstance(item, dict) and item.get("word")
    }

    normalized: list[dict[str, Any]] = []
    for position, requested_word in enumerate(requested):
        item = by_word.get(requested_word.casefold(), words[position])
        if not isinstance(item, dict):
            raise ServiceError(502, "invalid_model_output", "语义策划结果格式不正确。")
        scenes = item.get("scenes")
        if not isinstance(scenes, list) or len(scenes) < 1:
            raise ServiceError(502, "invalid_model_output", "语义策划缺少可绘制场景。")
        item = copy.deepcopy(item)
        item["word"] = requested_word
        item["id"] = slug(requested_word)
        item["scenes"] = [
            {
                **scene,
                "id": f"{slug(requested_word)}-{scene_index + 1}",
                "image": None,
            }
            for scene_index, scene in enumerate(scenes[:2])
            if isinstance(scene, dict)
        ]
        normalized.append(item)

    return {
        "id": "generated-" + uuid.uuid4().hex[:10],
        "title": str(plan.get("title") or ", ".join(requested)),
        "level": level,
        "date": datetime.now().strftime("%Y/%m/%d"),
        "source": "live",
        "words": normalized,
    }


def plan_wordpack(words: list[str], level: str, key: str) -> dict[str, Any]:
    subset = demo_subset(words, level)
    if not key:
        if subset:
            return subset
        raise ServiceError(
            503,
            "api_key_missing",
            "当前是演示模式。可直接体验 pour、individual、transfer；生成新词需要服务端模型凭证。",
            details={"setup": "请按 README 配置所选模型提供商的服务端凭证。"},
        )

    if provider() in {"openrouter", "ai_builder", "openai_compatible"}:
        response_format: dict[str, Any]
        instructions = planner_instructions(level)
        if provider() == "ai_builder":
            response_format = {"type": "json_object"}
            instructions += (
                "\n\n只返回一个 JSON 对象，不要使用 Markdown 代码块。"
                "对象必须严格符合这个 JSON Schema：\n"
                + json.dumps(planner_schema(), ensure_ascii=False)
            )
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "vocabulary_wordpack",
                    "strict": True,
                    "schema": planner_schema(),
                },
            }
        payload = {
            "model": text_model(),
            "messages": [
                {"role": "system", "content": instructions},
                {
                    "role": "user",
                    "content": "Create a word pack for: " + ", ".join(words),
                },
            ],
            "response_format": response_format,
            "max_tokens": 6000,
        }
        response, _ = openai_request("chat/completions", payload, key, timeout=120)
        raw_plan = extract_chat_text(response)
    else:
        payload = {
            "model": text_model(),
            "reasoning": {"effort": "low"},
            "input": [
                {
                    "role": "system",
                    "content": [
                        {"type": "input_text", "text": planner_instructions(level)}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Create a word pack for: " + ", ".join(words),
                        }
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "vocabulary_wordpack",
                    "strict": True,
                    "schema": planner_schema(),
                }
            },
            "max_output_tokens": 6000,
        }
        response, _ = openai_request("responses", payload, key, timeout=90)
        raw_plan = extract_response_text(response)
    try:
        plan = json.loads(raw_plan)
    except json.JSONDecodeError as exc:
        raise ServiceError(502, "invalid_model_json", "语义策划返回了无效 JSON。") from exc
    return normalize_plan(plan, words, level)


def build_image_prompt(word: str, scene_prompt: str) -> str:
    if len(scene_prompt) > 1800:
        raise ServiceError(400, "prompt_too_long", "画面描述过长。")
    return (
        load_style_prompt()
        + "\n\nVocabulary concept (context only; never render this as text): "
        + word
        + "\nScene to draw:\n"
        + scene_prompt.strip()
    )


def find_demo_scene(word: str, scene_id: str) -> str | None:
    for item in load_demo()["words"]:
        if item["word"].casefold() != word.casefold():
            continue
        for scene in item["scenes"]:
            if scene["id"] == scene_id:
                return scene.get("image")
    return None


def generate_image(
    *,
    word: str,
    scene_id: str,
    scene_prompt: str,
    key: str,
    regenerate: bool = False,
) -> dict[str, Any]:
    demo_image = find_demo_scene(word, scene_id)
    if not key:
        if demo_image:
            return {"image": demo_image, "source": "demo", "cached": True}
        raise ServiceError(
            503,
            "api_key_missing",
            "实时手绘生成需要服务端模型凭证。",
            details={"setup": "请按 README 配置所选模型提供商的服务端凭证。"},
        )

    final_prompt = build_image_prompt(word, scene_prompt)
    cache_material = f"{provider()}\n{image_model()}\n{STYLE_VERSION}\n{final_prompt}"
    if regenerate:
        cache_material += "\n" + uuid.uuid4().hex
    digest = hashlib.sha256(cache_material.encode("utf-8")).hexdigest()[:24]
    target = GENERATED_DIR / f"{slug(word)}-{digest}.png"
    if target.exists() and not regenerate:
        return {
            "image": f"/assets/generated/{target.name}",
            "source": "live",
            "cached": True,
        }

    response, request_id = openai_request(
        image_endpoint(), image_request_payload(final_prompt), key, timeout=180
    )
    data = response.get("data")
    encoded = data[0].get("b64_json") if isinstance(data, list) and data else None
    if not isinstance(encoded, str) or not encoded:
        raise ServiceError(502, "empty_image_output", "图片服务没有返回可保存的图像。")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ServiceError(502, "invalid_image_output", "图片服务返回了无效图像。") from exc

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    with _CACHE_LOCK:
        if not target.exists():
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=GENERATED_DIR, prefix=".tmp-", delete=False
            ) as temporary:
                temporary.write(image_bytes)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target)

    result = {
        "image": f"/assets/generated/{target.name}",
        "source": "live",
        "cached": False,
        "requestId": request_id,
    }
    usage = response.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("cost"), (int, float)):
        result["costUsd"] = round(float(usage["cost"]), 6)
    return result


class VocabRequestHandler(SimpleHTTPRequestHandler):
    server_version = "SensoryVocabularyLab/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        if self.path in {"/", "/index.html", "/app.js", "/styles.css"}:
            self.send_header("Cache-Control", "no-store")
        elif self.path.startswith("/assets/"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ServiceError(400, "invalid_length", "请求长度无效。") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ServiceError(413, "invalid_body_size", "请求为空或过大。")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError(400, "invalid_json", "请求不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise ServiceError(400, "invalid_json", "请求必须是 JSON 对象。")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/health":
            self.send_json(
                {
                    "status": "ok",
                    "mode": "live" if api_key() else "demo",
                    "provider": provider(),
                    "credentialSource": credential_source(),
                    "textModel": text_model(),
                    "imageModel": image_model(),
                    "styleVersion": STYLE_VERSION,
                }
            )
            return
        if self.path == "/api/demo":
            self.send_json(load_demo())
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self.read_json()
            if self.path == "/api/plan":
                words = clean_words(payload.get("words"))
                level = clean_level(payload.get("level"))
                self.send_json({"pack": plan_wordpack(words, level, api_key())})
                return
            if self.path == "/api/generate-image":
                word = str(payload.get("word") or "").strip()
                scene_id = str(payload.get("sceneId") or "").strip()
                scene_prompt = str(payload.get("imagePrompt") or "").strip()
                if not word or not scene_id or not scene_prompt:
                    raise ServiceError(400, "missing_image_fields", "缺少单词或画面描述。")
                if len(word) > MAX_WORD_LENGTH or len(scene_id) > 96:
                    raise ServiceError(400, "invalid_image_fields", "单词或场景标识过长。")
                result = generate_image(
                    word=word,
                    scene_id=scene_id,
                    scene_prompt=scene_prompt,
                    key=api_key(),
                    regenerate=bool(payload.get("regenerate")),
                )
                self.send_json(result)
                return
            raise ServiceError(404, "not_found", "没有这个 API。")
        except ServiceError as exc:
            self.send_json(exc.payload(), exc.status)
        except Exception:
            self.send_json(
                {"error": {"code": "internal_error", "message": "服务器遇到意外错误。"}},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="运行词汇感官实验室")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8779)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), VocabRequestHandler)
    mode = "实时生成" if api_key() else "演示"
    print(f"词汇感官实验室已启动：http://{args.host}:{args.port}（{mode}模式）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
