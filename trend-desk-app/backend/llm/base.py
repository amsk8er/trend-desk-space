from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class LLMRequest:
    prompt: str
    model: str = "claude-sonnet-4-6"
    images: list[str] | None = None  # local file paths
    system: str | None = None
    timeout_s: int = 120

@dataclass
class LLMResponse:
    text: str
    raw: dict
    elapsed_ms: int


class LLMNonRetryableError(RuntimeError):
    """A deterministic provider rejection that must not be retried unchanged."""


class LLMClient(ABC):
    @abstractmethod
    async def complete(self, req: LLMRequest) -> LLMResponse: ...
