import base64
import os
import time

from anthropic import AsyncAnthropic

from .base import LLMClient, LLMRequest, LLMResponse


class AnthropicApiClient(LLMClient):
    name = "anthropic_api"

    def __init__(self, api_key: str | None = None):
        self._client = AsyncAnthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    async def complete(self, req: LLMRequest) -> LLMResponse:
        content: list = [{"type": "text", "text": req.prompt}]
        for img in req.images or []:
            with open(img, "rb") as fh:
                data = base64.standard_b64encode(fh.read()).decode()
            media = "image/png" if img.lower().endswith("png") else "image/jpeg"
            content.insert(0, {
                "type": "image",
                "source": {"type": "base64", "media_type": media, "data": data},
            })
        t0 = time.time()
        msg = await self._client.messages.create(
            model=req.model, max_tokens=4096,
            system=req.system or "",
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(b.text for b in msg.content if hasattr(b, "text"))
        return LLMResponse(text=text, raw={}, elapsed_ms=int((time.time() - t0) * 1000))
