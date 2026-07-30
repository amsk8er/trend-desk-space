from .base import LLMClient, LLMRequest, LLMResponse

class ClaudeAgentSdkClient(LLMClient):
    async def complete(self, req: LLMRequest) -> LLMResponse:
        raise NotImplementedError("Claude Agent SDK integration is v2; see spec §17")
