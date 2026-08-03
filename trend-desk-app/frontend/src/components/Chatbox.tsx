import { useEffect, useRef, useState } from "react";
import {
  type ChatHistoryMsg,
  type PendingTool,
  getChatHistory,
  sendChatMessage,
  confirmChatTool,
} from "../api";

interface ChatboxProps {
  batchId: string | null;
  selectedNode: string;
}

// Sonnet is the spike-validated default. 'opus' is the bare CLI alias (claude
// resolves it) — exact versioned id待小由真终端 `claude --model` 确认后再钉死.
const MODELS: { id: string; label: string }[] = [
  { id: "claude-sonnet-4-6", label: "Claude Sonnet" },
  { id: "opus", label: "Claude Opus" },
];

function argsPreview(args: Record<string, unknown>): string {
  return Object.entries(args)
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(", ");
}

export function Chatbox({ batchId, selectedNode }: ChatboxProps) {
  const [messages, setMessages] = useState<ChatHistoryMsg[]>([]);
  const [pending, setPending] = useState<PendingTool | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [model, setModel] = useState(MODELS[0].id);
  const msgsRef = useRef<HTMLDivElement>(null);

  // (re)load history whenever the selected batch changes
  useEffect(() => {
    setPending(null);
    setError(null);
    if (!batchId) {
      setMessages([]);
      return;
    }
    let alive = true;
    getChatHistory(batchId)
      .then((h) => alive && setMessages(h))
      .catch((e) => alive && setError(String(e)));
    return () => {
      alive = false;
    };
  }, [batchId]);

  // keep the newest message in view
  useEffect(() => {
    const el = msgsRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, pending, busy]);

  async function refresh() {
    if (batchId) setMessages(await getChatHistory(batchId));
  }

  // applies the turn result: refresh transcript, surface any confirm gate
  function applyTurn(status: string, tool?: PendingTool) {
    setPending(status === "needs_confirm" ? tool ?? null : null);
  }

  async function send() {
    const content = text.trim();
    if (!content || !batchId || busy) return;
    setText("");
    setBusy(true);
    setError(null);
    // optimistic: show the user's bubble immediately, don't wait for the model.
    // negative msg_id = temp; refresh() below replaces the list with canonical history.
    setMessages((m) => [
      ...m,
      { msg_id: -Date.now(), role: "user", content, tool_name: null, tool_args: null },
    ]);
    try {
      const turn = await sendChatMessage(batchId, content, { model, currentNode: selectedNode });
      await refresh();
      applyTurn(turn.status, turn.tool);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function answer(confirmed: boolean) {
    if (!pending || !batchId || busy) return;
    const tool = pending;
    setPending(null);
    setBusy(true);
    setError(null);
    try {
      const turn = await confirmChatTool(batchId, tool.name, tool.args, confirmed, {
        model,
        currentNode: selectedNode,
      });
      await refresh();
      applyTurn(turn.status, turn.tool);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="panel chat-col">
      <div className="chat-head">
        <div className="chat-title-row">
          <span className="dot" /> Chatbox
        </div>
        <select className="model-pill" value={model} onChange={(e) => setModel(e.target.value)}>
          {MODELS.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
      </div>

      <div className="msgs" id="chat-msgs" ref={msgsRef}>
        {messages.length === 0 && !busy && (
          <div className="msg msg-a">
            <div className="msg-tag">系统</div>
            {batchId ? "问我任何关于本批次 pipeline 的问题。" : "先在上方选一个批次。"}
          </div>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.msg_id} m={m} />
        ))}
        {busy && <div className="msg msg-a chat-thinking">… 思考中</div>}
      </div>

      {pending && (
        <div className="tool-confirm">
          <div className="tool-confirm-title">Claude 想执行写操作，需你确认</div>
          <code className="tool-confirm-call">
            {pending.name}({argsPreview(pending.args)})
          </code>
          <div className="tool-confirm-btns">
            <button className="cbtn" disabled={busy} onClick={() => answer(false)}>
              取消
            </button>
            <button className="cbtn cbtn-primary" disabled={busy} onClick={() => answer(true)}>
              确认
            </button>
          </div>
        </div>
      )}

      {error && <div className="chat-error">{error}</div>}

      <div className="chat-input-wrap">
        <input
          className="chat-input"
          placeholder={batchId ? "问问题 / 操作 pipeline ..." : "先选批次"}
          value={text}
          disabled={!batchId || busy}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // !isComposing: don't send while the IME is mid-composition (拼音选词回车)
            if (e.key === "Enter" && !e.nativeEvent.isComposing) send();
          }}
        />
        <button className="cbtn cbtn-primary" disabled={!batchId || busy} onClick={send}>
          ↑
        </button>
      </div>
    </aside>
  );
}

function MessageBubble({ m }: { m: ChatHistoryMsg }) {
  if (m.role === "user") {
    return <div className="msg msg-u">{m.content}</div>;
  }
  if (m.role === "assistant") {
    // strip the ```tool fence — it's machinery, not prose
    const prose = m.content.replace(/```tool[\s\S]*?```/g, "").trim();
    return (
      <div className="msg msg-a">
        <div className="msg-tag">Claude</div>
        {prose || "（调用工具中…）"}
      </div>
    );
  }
  if (m.role === "tool_call") {
    return <div className="tool-note">→ 请求工具 {m.tool_name}（待确认）</div>;
  }
  // tool_result
  return <div className="tool-note">✓ {m.tool_name} 结果已返回</div>;
}
