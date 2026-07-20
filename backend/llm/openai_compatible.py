"""Generic OpenAI-compatible vision client.

This adapter intentionally depends only on httpx.  A provider can be switched
by changing three server-side environment variables; the frontend never sees
the key.
"""

import base64
import mimetypes
import os
import time
from pathlib import Path

import httpx

from .base import LLMClient, LLMRequest, LLMResponse

AI_BUILDER_BASE_URL = "https://space.ai-builders.com/backend/v1"
AI_BUILDER_VISION_MODEL = "kimi-k2.5"


def openai_compatible_configured() -> bool:
    explicit = all((os.getenv(name) or "").strip() for name in (
        "OPENAI_COMPATIBLE_BASE_URL",
        "OPENAI_COMPATIBLE_API_KEY",
        "OPENAI_COMPATIBLE_VISION_MODEL",
    ))
    return explicit or bool((os.getenv("AI_BUILDER_TOKEN") or "").strip())


def ai_builder_space_configured() -> bool:
    return bool((os.getenv("AI_BUILDER_TOKEN") or "").strip())


class OpenAICompatibleClient(LLMClient):
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        space_token = (os.getenv("AI_BUILDER_TOKEN") or "").strip()
        self.base_url = (
            base_url
            or os.getenv("OPENAI_COMPATIBLE_BASE_URL")
            or (AI_BUILDER_BASE_URL if space_token else "")
        ).rstrip("/")
        self.api_key = (
            api_key or os.getenv("OPENAI_COMPATIBLE_API_KEY") or space_token
        )
        self.model = (
            model
            or os.getenv("OPENAI_COMPATIBLE_VISION_MODEL")
            or (AI_BUILDER_VISION_MODEL if space_token else "")
        )
        self._client = client

    @staticmethod
    def _image_part(path: str) -> dict:
        media_type = mimetypes.guess_type(path)[0] or "image/png"
        data = base64.b64encode(Path(path).read_bytes()).decode()
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{data}"},
        }

    def _payload(self, req: LLMRequest) -> dict:
        content = [self._image_part(path) for path in req.images or []]
        content.append({"type": "text", "text": req.prompt})
        messages = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages.append({"role": "user", "content": content})
        return {"model": self.model, "messages": messages, "max_tokens": 4096}

    async def complete(self, req: LLMRequest) -> LLMResponse:
        missing = [
            name for name, value in (
                ("OPENAI_COMPATIBLE_BASE_URL", self.base_url),
                ("OPENAI_COMPATIBLE_API_KEY", self.api_key),
                ("OPENAI_COMPATIBLE_VISION_MODEL", self.model),
            ) if not value
        ]
        if missing:
            raise RuntimeError(f"OpenAI 兼容视觉接口未配置：缺少 {', '.join(missing)}")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        t0 = time.time()
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=self._payload(req),
                timeout=req.timeout_s,
            )
            response.raise_for_status()
            raw = response.json()
        finally:
            if owns_client:
                await client.aclose()
        try:
            content = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenAI 兼容视觉接口返回结构不含 choices[0].message.content") from exc
        if isinstance(content, list):
            text = "".join(
                str(part.get("text", "")) for part in content
                if isinstance(part, dict) and part.get("type") in {None, "text"}
            )
        else:
            text = str(content or "")
        if not text.strip():
            raise RuntimeError("OpenAI 兼容视觉接口返回空内容")
        return LLMResponse(
            text=text,
            raw={"id": raw.get("id"), "model": raw.get("model")},
            elapsed_ms=int((time.time() - t0) * 1000),
        )
