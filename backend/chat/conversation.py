"""Chatbox conversation orchestrator (Task 9.12).

The `claude` CLI subprocess is stateless — each call forgets the last. So we
keep the whole conversation in the `chat_messages` table and *replay* it as the
prompt every round (decision: 小由 2026-06-01). One user turn drives a loop:

    call CLI → parse tool calls (fenced ```tool blocks, see dispatcher) →
      read tool  → run now, append result, loop again so claude can use it
      write tool → STOP, persist a pending tool_call, return needs_confirm
      no tool    → done

Write tools never fire here; they wait for explicit user confirm via
`confirm_and_continue`. A max-rounds guard stops runaway tool loops.

Everything is driven through an injected LLM client, so the whole loop is
testable with a FakeClient — no live `claude` needed (tests/test_chat_conversation.py).
"""
import json

from sqlmodel import Session, select

from backend.db import ChatMessage
from backend.chat.tools import REGISTRY, run_tool
from backend.chat.dispatcher import extract_tool_calls
from backend.chat.system import build_system_prompt, build_user_prefix
from backend.llm.base import LLMRequest

MAX_ROUNDS = 4


def persist(s: Session, batch_id: str, role: str, content: str = "",
            tool_name: str | None = None, tool_args: dict | None = None) -> ChatMessage:
    m = ChatMessage(batch_id=batch_id, role=role, content=content,
                    tool_name=tool_name, tool_args=tool_args or {})
    s.add(m)
    s.commit()
    s.refresh(m)
    return m


def persist_user(s: Session, batch_id: str, content: str) -> ChatMessage:
    return persist(s, batch_id, "user", content)


def load_messages(s: Session, batch_id: str) -> list[ChatMessage]:
    return list(s.exec(
        select(ChatMessage).where(ChatMessage.batch_id == batch_id).order_by(ChatMessage.msg_id)
    ))


def render_transcript(messages: list[ChatMessage]) -> str:
    """Flatten history into one prompt string for replay.

    tool_call rows are bookkeeping only — the assistant message already carries
    the ```tool fence, so rendering them again would duplicate the request.
    """
    lines: list[str] = []
    for m in messages:
        if m.role == "user":
            lines.append(f"用户: {m.content}")
        elif m.role == "assistant":
            lines.append(f"助手: {m.content}")
        elif m.role == "tool_result":
            lines.append(f"工具结果({m.tool_name}): {m.content}")
    return "\n\n".join(lines)


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


async def _one_cli_round(s, batch_id, client, model, current_node, recent_logs) -> str:
    """Replay the transcript, call the CLI once, persist + return its reply text."""
    prompt = build_user_prefix(
        batch_id=batch_id, current_node=current_node, recent_logs=recent_logs or [],
    ) + render_transcript(load_messages(s, batch_id))
    resp = await client.complete(LLMRequest(prompt=prompt, system=build_system_prompt(), model=model))
    persist(s, batch_id, "assistant", content=resp.text)
    return resp.text


async def run_turn(s: Session, *, batch_id: str, client, model: str,
                   current_node: str = "", recent_logs: list[str] | None = None,
                   max_rounds: int = MAX_ROUNDS) -> dict:
    """Drive one user turn to completion (or to a confirm gate)."""
    for _ in range(max_rounds):
        text = await _one_cli_round(s, batch_id, client, model, current_node, recent_logs)
        calls = extract_tool_calls(text)
        if not calls:
            return {"status": "done", "assistant": text}

        call = calls[0]  # one tool per round keeps the loop and replay simple in v1
        spec = REGISTRY.get(call.name)
        if spec is None:
            # not whitelisted — feed the error back so claude can self-correct
            persist(s, batch_id, "tool_result",
                    content=f"error: tool not whitelisted: {call.name}", tool_name=call.name)
            continue
        # batch_id 由系统从会话上下文注入，覆盖 LLM 填的值——它常漏填/填错（实盘
        # batch_20260612_2222 的 updated_rows=0 根因）。LLM 只需给业务参数（code/
        # stop_refs/row_ids/node_id…）。
        call_args = {**call.args, "batch_id": batch_id}
        if spec.needs_confirm:
            persist(s, batch_id, "tool_call", tool_name=call.name, tool_args=call_args)
            return {"status": "needs_confirm", "assistant": text,
                    "tool": {"name": call.name, "args": call_args}}

        # read tool: run now, replay the result, loop so claude can use it
        result = _safe_run(s, call.name, call_args)
        persist(s, batch_id, "tool_result", content=_dumps(result), tool_name=call.name)

    return {"status": "max_rounds", "assistant": "(已达到最大工具调用轮次，已停止)"}


async def confirm_and_continue(s: Session, *, batch_id: str, name: str, args: dict,
                               confirmed: bool, client, model: str,
                               current_node: str = "", recent_logs: list[str] | None = None) -> dict:
    """User answered a confirm card: run (or skip) the write tool, then resume."""
    if confirmed:
        # 同样注入 batch_id：前端回传的 confirm args 也以会话 batch_id 为准。
        result = _safe_run(s, name, {**args, "batch_id": batch_id})
    else:
        result = {"cancelled": True, "note": "用户取消了该操作"}
    persist(s, batch_id, "tool_result", content=_dumps(result), tool_name=name)
    return await run_turn(s, batch_id=batch_id, client=client, model=model,
                          current_node=current_node, recent_logs=recent_logs)


def _safe_run(s, name, args):
    """Run a whitelisted tool; surface any failure as data instead of crashing
    the chat loop, so claude/the user sees a readable error."""
    try:
        return run_tool(s, name, args)
    except Exception as e:  # noqa: BLE001 — intentional: a tool error must not kill the turn
        return {"error": str(e)}
