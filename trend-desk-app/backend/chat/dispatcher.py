import json
import re
from dataclasses import dataclass

_TOOL_FENCE = re.compile(r"```tool\s*(.*?)\s*```", re.S)


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict


def extract_tool_calls(text: str) -> list[ToolCall]:
    calls = []
    for m in _TOOL_FENCE.finditer(text):
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if "name" in payload:
            calls.append(ToolCall(name=payload["name"], args=payload.get("args") or {}))
    return calls
