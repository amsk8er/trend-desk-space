import os

from backend import config
from .base import LLMClient, LLMRequest, LLMResponse
from .claude_cli import ClaudeCliClient
from .anthropic_api import AnthropicApiClient
from .codex_cli import CodexCliClient
from .minimax_coding_plan import MiniMaxCodingPlanClient
from .openai_compatible import OpenAICompatibleClient

__all__ = ["LLMClient", "LLMRequest", "LLMResponse", "ClaudeCliClient",
           "AnthropicApiClient", "CodexCliClient", "MiniMaxCodingPlanClient",
           "OpenAICompatibleClient", "get_client"]

BACKEND_CHOICES = (
    "minimax_coding_plan",
    "codex_cli",
    "anthropic_api",
    "openai_compatible",
    "claude_cli",
)


def default_backend() -> str:
    explicit = (os.getenv("LLM_BACKEND") or "").strip()
    if explicit:
        return explicit
    if (os.getenv("AI_BUILDER_TOKEN") or "").strip():
        return "openai_compatible"
    return config.LLM_BACKEND


def get_client(backend: str | None = None) -> LLMClient:
    # Read env at call time: config.LLM_BACKEND freezes before lifespan loads secrets.env.
    backend = backend or default_backend()
    if backend == "claude_cli":
        return ClaudeCliClient()
    if backend == "anthropic_api":
        return AnthropicApiClient()
    if backend == "codex_cli":
        return CodexCliClient()
    if backend == "minimax_coding_plan":
        return MiniMaxCodingPlanClient()
    if backend == "openai_compatible":
        return OpenAICompatibleClient()
    raise ValueError(f"unsupported LLM backend={backend}")
