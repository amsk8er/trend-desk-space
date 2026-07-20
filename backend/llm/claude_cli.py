import asyncio
import base64
import json
import os
import time

from .base import LLMClient, LLMRequest, LLMResponse


def parse_stream_json(stdout: str) -> tuple[str, list[dict]]:
    """Parse `claude --output-format stream-json` output (D11 spike schema).

    No text_delta frames: assistant text is in `assistant` frames'
    message.content[] text blocks; the final full text is also in the
    `result` frame's `.result`. Tolerate non-JSON / malformed lines.
    """
    frames: list[dict] = []
    text_parts: list[str] = []
    result_text: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            f = json.loads(line)
        except json.JSONDecodeError:
            continue
        frames.append(f)
        ftype = f.get("type")
        if ftype == "assistant":
            content = (f.get("message") or {}).get("content") or []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
        elif ftype == "result" and isinstance(f.get("result"), str):
            result_text = f["result"]
    text = "".join(text_parts) or (result_text or "")
    return text, frames


def result_error(frames: list[dict]) -> str | None:
    """If the result frame signals failure, return an error string; else None."""
    for f in frames:
        if f.get("type") == "result" and f.get("is_error"):
            return (
                f.get("result")
                or f.get("error")
                or f"api_error_status={f.get('api_error_status')}"
            )
    return None


class ClaudeCliClient(LLMClient):
    BIN = "claude"
    name = "claude_cli"

    def __init__(self, oauth_token: str | None = None):
        self._token = oauth_token or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")

    def _build_env(self) -> dict:
        # D11: headless --print 会优先取用串台的 API key/base_url 而 401，必须剥离，
        # 让它回退到 CLAUDE_CODE_OAUTH_TOKEN 骑 Max 订阅。
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        if self._token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self._token
        return env

    def _build_stdin(self, req: LLMRequest) -> tuple[str, list[str]]:
        """Return (stdin_payload, extra_cli_args). Images → stream-json input."""
        if not req.images:
            return req.prompt, []
        content: list[dict] = []
        for img in req.images:
            with open(img, "rb") as fh:
                data = base64.standard_b64encode(fh.read()).decode()
            media = "image/png" if img.lower().endswith("png") else "image/jpeg"
            content.append(
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
            )
        content.append({"type": "text", "text": req.prompt})
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        return json.dumps(msg), ["--input-format", "stream-json"]

    async def complete(self, req: LLMRequest) -> LLMResponse:
        stdin_payload, extra = self._build_stdin(req)
        cmd = [
            self.BIN, "--print", "--output-format", "stream-json", "--verbose",
            "--model", req.model, *extra,
        ]
        if req.system:
            cmd += ["--system-prompt", req.system]
        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=self._build_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_payload.encode()), timeout=req.timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise
        out_text = stdout.decode()
        err_text = stderr.decode()
        text, frames = parse_stream_json(out_text)
        # Non-zero exit often still carries the real reason in stream-json
        # result frames (stderr may be empty). Prefer that over a bare exit code.
        frame_err = result_error(frames)
        if proc.returncode != 0:
            detail = frame_err or err_text.strip() or out_text.strip()[-500:] or "(no stderr)"
            raise RuntimeError(f"claude CLI exit {proc.returncode}: {detail[:500]}")
        if frame_err:
            raise RuntimeError(f"claude CLI result error: {frame_err}")
        return LLMResponse(text=text, raw={"frames": frames}, elapsed_ms=int((time.time() - t0) * 1000))
