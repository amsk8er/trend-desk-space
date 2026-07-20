import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { runReport, getLlmConfig } from "../api";

// 视觉后端选择：与节点②⑦共享 localStorage，跨页面保持一致（OCR/持仓/研判同源）。
const VISION_BACKEND_KEY = "td-vision-backend";
const BACKEND_LABEL: Record<string, string> = {
  claude_cli: "Claude CLI", anthropic_api: "Claude API", codex_cli: "Codex CLI",
};

// runReport() returns the report payload (typed `unknown` in api.ts). The
// daily report markdown lives under `.markdown`; we narrow defensively.
interface ReportResult {
  markdown?: string;
  [k: string]: unknown;
}

function extractMarkdown(data: unknown): string {
  if (data && typeof data === "object" && "markdown" in data) {
    const md = (data as ReportResult).markdown;
    if (typeof md === "string") return md;
  }
  // Fallback: pretty-print whatever the backend returned so nothing is lost.
  try {
    return JSON.stringify(data, null, 2);
  } catch {
    return String(data);
  }
}

export default function ReportStage({ batchId }: { batchId: string | null }) {
  // 初始化读 localStorage（与节点②OCR、节点⑦持仓页共享），保持研判与 OCR 同后端。
  const [backend, _setBackend] = useState<string>(() => {
    try { return localStorage.getItem(VISION_BACKEND_KEY) || ""; } catch { return ""; }
  });
  const setBackend = (v: string) => {
    _setBackend(v);
    try { if (v) localStorage.setItem(VISION_BACKEND_KEY, v); else localStorage.removeItem(VISION_BACKEND_KEY); } catch { /* private mode */ }
  };
  const llmCfg = useQuery({ queryKey: ["llm-config"], queryFn: getLlmConfig });
  const effBackend = backend || llmCfg.data?.backend || undefined;

  const mutation = useMutation({
    mutationFn: () => runReport(batchId as string, effBackend),
  });

  if (!batchId) {
    return (
      <div className="panel" style={{ textAlign: "center", color: "#9ca3af" }}>
        <div style={{ fontSize: 28, marginBottom: 6 }}>📰</div>
        <div style={{ fontSize: 13, fontWeight: 700 }}>选择一个批次</div>
        <div style={{ fontSize: 11, marginTop: 4 }}>
          选中批次后即可生成当日日报
        </div>
      </div>
    );
  }

  const md = mutation.data !== undefined ? extractMarkdown(mutation.data) : null;

  return (
    <div className="panel">
      {/* header / toolbar */}
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <div className="toolbar-l">
          <span className="cnode-num" style={{ fontSize: 14 }}>
            ⑨ 日报
          </span>
          <span className="chip">REPORT</span>
          <span className="pill pill-date mono">{batchId}</span>
        </div>
        <select
          value={backend || llmCfg.data?.backend || ""}
          onChange={(e) => setBackend(e.target.value)}
          className="rounded border border-zinc-600 bg-zinc-800 px-2 py-1 text-sm"
          title="本次研判用哪个 LLM 后端（默认跟随服务端配置；与节点②OCR、节点⑦持仓页选择保持一致）"
        >
          {(llmCfg.data?.choices ?? []).map((c) => (
            <option key={c} value={c}>
              {BACKEND_LABEL[c] ?? c}
            </option>
          ))}
        </select>
        <button
          className="cbtn cbtn-primary"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
        >
          {mutation.isPending ? "生成中…" : md ? "重新生成日报" : "生成日报"}
        </button>
      </div>

      {/* error */}
      {mutation.isError && (
        <div
          className="chunky"
          style={{
            background: "#fee2e2",
            borderColor: "#1f2937",
            padding: "10px 12px",
            marginBottom: 12,
            fontSize: 11,
            lineHeight: 1.5,
          }}
        >
          <div style={{ fontWeight: 800, marginBottom: 2 }}>生成失败</div>
          <div className="mono" style={{ wordBreak: "break-word" }}>
            {(mutation.error as Error)?.message ?? "未知错误"}
          </div>
        </div>
      )}

      {/* loading */}
      {mutation.isPending && (
        <div
          className="section-h"
          style={{ textAlign: "center", padding: "32px 0" }}
        >
          正在生成日报…
        </div>
      )}

      {/* idle (never run yet) */}
      {!mutation.isPending && !mutation.isError && md === null && (
        <div
          style={{
            textAlign: "center",
            color: "#9ca3af",
            padding: "40px 0",
            fontSize: 12,
          }}
        >
          <div style={{ fontSize: 28, marginBottom: 6 }}>📝</div>
          点击「生成日报」预览当日完整日报
        </div>
      )}

      {/* result: scrollable full report preview (card-stack B spirit) */}
      {md !== null && !mutation.isPending && (
        <>
          <div className="section-h">日报预览</div>
          <div
            className="chunky"
            style={{
              background: "#fffdf5",
              padding: 0,
              maxHeight: "62vh",
              overflow: "auto",
            }}
          >
            <pre
              className="mono"
              style={{
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                margin: 0,
                padding: "16px 18px",
                fontSize: 12,
                lineHeight: 1.7,
                color: "#1f2937",
              }}
            >
              {md}
            </pre>
          </div>
        </>
      )}
    </div>
  );
}
