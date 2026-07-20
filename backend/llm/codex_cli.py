"""Codex CLI plug: rides the ChatGPT Plus subscription via headless `codex exec`.

Invocation shape is pinned by scripts/spike_codex_ocr.py + docs/architecture.md
D20 (live-verified 2026-07-03, codex-cli 0.133.0) — rerun the spike after any
codex CLI upgrade. All codex-specific flags live in _build_cmd so a CLI change
breaks exactly one place. `LLMRequest.model` is deliberately ignored: anthropic
model ids mean nothing to codex; CODEX_MODEL env picks the model (unset →
codex's own default).

D20 corrections vs the brief's first-draft flags:
  * image flag is `-i <FILE>...` (a VARARG), not `--image` — so the image must
    not be followed by another positional arg;
  * `-o <file>` writes the last agent message, not `--output-last-message`;
  * PROMPT goes over stdin (`-` positional + subprocess input=), because `-i`
    would otherwise swallow a trailing prompt positional;
  * `--dangerously-bypass-approvals-and-sandbox` + `--color never` are required
    to read local PNGs headless and keep the -o file clean.
"""
import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

from .base import LLMClient, LLMRequest, LLMResponse

# ChatGPT desktop ships a newer codex than nvm/global npm. Prefer it when PATH
# still points at an older CLI that rejects the account's default model.
_CHATGPT_CODEX = Path(
    "/Applications/ChatGPT.app/Contents/Resources/codex"
)


def resolve_codex_bin() -> str:
    """Pick a usable codex binary: CODEX_BIN env → ChatGPT.app → PATH."""
    env_bin = (os.getenv("CODEX_BIN") or "").strip()
    if env_bin:
        return env_bin
    if _CHATGPT_CODEX.is_file() and os.access(_CHATGPT_CODEX, os.X_OK):
        return str(_CHATGPT_CODEX)
    return shutil.which("codex") or "codex"


class CodexCliClient(LLMClient):
    name = "codex_cli"

    def __init__(self, bin_path: str | None = None):
        self.bin = bin_path or resolve_codex_bin()

    def _build_cmd(self, req: LLMRequest, out_file: str) -> list[str]:
        cmd = [
            self.bin, "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--color", "never",
            "-o", out_file,
        ]
        model = os.getenv("CODEX_MODEL", "")
        if model:
            cmd += ["--model", model]
        for img in req.images or []:
            cmd += ["-i", img]
        # `-i` is a vararg and codex exec has no system channel; the prompt
        # (system prepended) goes over stdin via the `-` positional.
        cmd.append("-")
        return cmd

    def _build_stdin(self, req: LLMRequest) -> bytes:
        prompt = f"{req.system}\n\n{req.prompt}" if req.system else req.prompt
        return prompt.encode()

    async def complete(self, req: LLMRequest) -> LLMResponse:
        t0 = time.time()
        fd, out_file = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_cmd(req, out_file),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _out, stderr = await asyncio.wait_for(
                    proc.communicate(input=self._build_stdin(req)), timeout=req.timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                raise
            if proc.returncode != 0:
                raise RuntimeError(f"codex CLI exit {proc.returncode}: {stderr.decode()[:500]}")
            text = Path(out_file).read_text(encoding="utf-8")
            if not text.strip():
                raise RuntimeError("codex CLI returned empty last message")
            return LLMResponse(text=text, raw={}, elapsed_ms=int((time.time() - t0) * 1000))
        finally:
            Path(out_file).unlink(missing_ok=True)
