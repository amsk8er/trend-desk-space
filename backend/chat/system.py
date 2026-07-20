# backend/chat/system.py
from pathlib import Path
from backend.chat.tools import REGISTRY

_PROMPT = Path(__file__).parent.parent.parent / "prompts" / "chat_system.md"


def build_system_prompt() -> str:
    base = _PROMPT.read_text(encoding="utf-8")
    tools = "\n".join(f"- {name}（{'写' if t.needs_confirm else '读'}）" for name, t in REGISTRY.items())
    # frame as the allowed `name` values for the tool block — NOT a tool registry
    return base + "\n\n# 允许写进 tool 块 name 字段的取值（只能用这些）\n" + tools


def build_user_prefix(*, batch_id: str, current_node: str, recent_logs: list[str]) -> str:
    log_block = "\n".join(f"- {line}" for line in recent_logs[-10:])
    return f"[batch={batch_id} node={current_node}]\n最近日志:\n{log_block}\n\n"
