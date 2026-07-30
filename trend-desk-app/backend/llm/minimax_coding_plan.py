"""MiniMax Coding Plan vision backend through the official ``mmx`` CLI.

The Coding Plan text model and its vision tool are separate capabilities.  For
screenshots we call ``mmx vision describe`` so authentication stays in the
user's local MiniMax CLI session and no API key is exposed to the browser.
"""

import asyncio
import json
import os
import shutil
import time

from .base import LLMClient, LLMNonRetryableError, LLMRequest, LLMResponse


def resolve_minimax_bin() -> str:
    return (os.getenv("MINIMAX_CLI_BIN") or "").strip() or shutil.which("mmx") or "mmx"


def minimax_cli_available() -> bool:
    binary = resolve_minimax_bin()
    return bool(os.path.isfile(binary) or shutil.which(binary))


class MiniMaxCodingPlanClient(LLMClient):
    name = "minimax_coding_plan"

    def __init__(self, bin_path: str | None = None):
        self.bin = bin_path or resolve_minimax_bin()

    def _build_cmd(self, req: LLMRequest, image: str) -> list[str]:
        prompt = f"{req.system}\n\n{req.prompt}" if req.system else req.prompt
        # mmx inherits the user's global output setting.  When it is ``json``,
        # vision output is wrapped in an API envelope instead of printing the
        # model content directly.  Keep this backend's contract deterministic.
        return [
            self.bin, "--output", "text", "vision", "describe",
            "--image", image, "--prompt", prompt,
        ]

    async def complete(self, req: LLMRequest) -> LLMResponse:
        images = req.images or []
        if len(images) != 1:
            raise ValueError("MiniMax Coding Plan vision requires exactly one image per request")
        t0 = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_cmd(req, images[0]),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "MiniMax CLI 未安装：请先安装 mmx-cli 并运行 `mmx auth login`"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=req.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            detail = (
                stderr.decode(errors="replace").strip()
                or stdout.decode(errors="replace").strip()
            )[:500]
            if "sensitive" in detail.lower():
                raise LLMNonRetryableError(
                    "MiniMax 内容审核拒绝了本次纯表格转写，已停止重复请求；"
                    "请改用 Codex CLI 或 OpenAI 兼容视觉 API"
                )
            raise RuntimeError(f"MiniMax CLI exit {proc.returncode}: {detail or '未返回错误详情'}")
        text = stdout.decode(errors="replace").strip()
        if not text:
            raise RuntimeError("MiniMax CLI returned empty vision response")
        # Defensive compatibility for older/different mmx versions which may
        # ignore ``--output text`` and still return {"content": "..."}.
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict) and isinstance(envelope.get("content"), str):
            text = envelope["content"].strip()
            if not text:
                raise RuntimeError("MiniMax CLI returned empty vision content")
        return LLMResponse(
            text=text,
            raw={"backend": self.name},
            elapsed_ms=int((time.time() - t0) * 1000),
        )
