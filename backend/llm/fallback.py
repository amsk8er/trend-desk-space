"""Fallback decorator: try primary, retry once on secondary on transport errors.

Transport-level = whatever complete() raises (timeout / nonzero exit / API
error). JSON-parse failures never reach here by construction: parsing happens
ABOVE the client (backend/ocr/parser.py), so a wrong-but-well-formed answer is
not silently re-asked to another vendor. Off by default — runner only wraps
when OCR_FALLBACK_BACKEND is set.
"""
import logging

from .base import LLMClient, LLMRequest, LLMResponse

log = logging.getLogger(__name__)


class FallbackClient(LLMClient):
    name = "fallback"

    def __init__(self, primary: LLMClient, secondary: LLMClient):
        self._primary = primary
        self._secondary = secondary

    async def complete(self, req: LLMRequest) -> LLMResponse:
        try:
            resp = await self._primary.complete(req)
        except Exception as e:  # noqa: BLE001 — any transport failure triggers the one fallback
            log.warning("primary %s failed (%s: %s) — falling back to %s",
                        getattr(self._primary, "name", "?"), type(e).__name__, e,
                        getattr(self._secondary, "name", "?"))
            resp = await self._secondary.complete(req)
            resp.raw["served_by"] = getattr(self._secondary, "name", None)
            return resp
        resp.raw["served_by"] = getattr(self._primary, "name", None)
        return resp
