# backend/analysis/trend_brief.py
"""趋势研判：把事实包喂 LLM 写成 markdown。LLM 调用隔离在此。

facts_hash 用于缓存：事实包不变则复用已存研判，不重复调 LLM（见 report 节点 orchestrator）。
"""
import hashlib
import json
from pathlib import Path

from backend.llm import LLMRequest

_SYSTEM = Path(__file__).resolve().parent.parent.parent / "prompts" / "trend_brief_system.md"


def facts_hash(facts: dict) -> str:
    blob = json.dumps(facts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def render_prompt(facts: dict) -> str:
    return ("以下是今日 A 股盘后「趋势事实包」（JSON）。请据此写「趋势研判」，"
            "严格只用其中的数字。\n\n```json\n"
            + json.dumps(facts, ensure_ascii=False, indent=2)
            + "\n```\n")


async def generate_brief(facts: dict, *, client, model: str) -> str:
    """照 conversation.py 的调用样式：await client.complete(LLMRequest(...))。"""
    resp = await client.complete(LLMRequest(
        prompt=render_prompt(facts),
        system=_SYSTEM.read_text(encoding="utf-8"),
        model=model,
    ))
    return resp.text.strip()
