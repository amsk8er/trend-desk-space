import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getOcr,
  runOcr,
  rerunOcr,
  cancelOcr,
  getLlmConfig,
  openPipelineSse,
  type OcrData,
  type OcrJob,
  type NodeStatus,
} from "../api";
import OcrJobDetail from "./OcrJobDetail";
// OcrAggregate moved to its own node (registry: aggregate)

// ── status → thumbnail color class (mirrors mockup .ct.ok/run/todo/fail/skip) ──
function thumbClass(status: NodeStatus): string {
  switch (status) {
    case "done":
      return "ok";
    case "running":
      return "run";
    case "failed":
      return "fail";
    case "skipped":
      return "skip";
    default:
      return "todo";
  }
}

const THUMB_BG: Record<string, { bg: string; fg: string; pulse?: boolean }> = {
  ok: { bg: "#86efac", fg: "#1f2937" },
  run: { bg: "#93c5fd", fg: "#1f2937", pulse: true },
  todo: { bg: "#f3f4f6", fg: "#9ca3af" },
  fail: { bg: "#fca5a5", fg: "#1f2937" },
  skip: { bg: "#fed7aa", fg: "#1f2937" },
};

interface LogLine {
  t: string;
  kind: "OK" | "START" | "FAIL" | "SKIP" | "INFO";
  text: string;
}

const LOG_KIND_COLOR: Record<LogLine["kind"], string> = {
  OK: "#86efac",
  START: "#93c5fd",
  FAIL: "#fca5a5",
  SKIP: "#fbbf24",
  INFO: "#94a3b8",
};

function nowStamp(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// Diff two job snapshots → log lines for any status that changed.
function diffJobs(prev: OcrJob[], next: OcrJob[]): LogLine[] {
  const prevById = new Map(prev.map((j) => [j.job_id, j.status]));
  const lines: LogLine[] = [];
  for (const j of next) {
    const before = prevById.get(j.job_id);
    if (before === undefined || before === j.status) continue;
    const img = j.image ?? `img_${String(j.image_index ?? j.job_id).padStart(3, "0")}`;
    if (j.status === "done") {
      lines.push({ t: nowStamp(), kind: "OK", text: `${img} → ${j.rows} rows${j.backend ? ` (${j.backend})` : ""}` });
    } else if (j.status === "running") {
      lines.push({ t: nowStamp(), kind: "START", text: `${img} 开始解析` });
    } else if (j.status === "failed") {
      lines.push({ t: nowStamp(), kind: "FAIL", text: `${img} → ${j.partial_reason ?? "解析失败"}` });
    } else if (j.status === "skipped") {
      lines.push({ t: nowStamp(), kind: "SKIP", text: `${img} → 跳过${j.partial_reason ? ` (${j.partial_reason})` : ""}` });
    }
  }
  return lines;
}

// 视觉后端选择 — 跟节点⑦持仓页共享 localStorage，跨页面保持一致。
const VISION_BACKEND_KEY = "td-vision-backend";
const BACKEND_LABEL: Record<string, string> = {
  claude_cli: "Claude CLI", anthropic_api: "Claude API", codex_cli: "Codex CLI",
};

export default function OcrStage({ batchId }: { batchId: string | null }) {
  const qc = useQueryClient();
  const [log, setLog] = useState<LogLine[]>([]);
  const [live, setLive] = useState(false);
  const [selectedJobId, setSelectedJobId] = useState<number | null>(null);
  // 初始化读 localStorage（与节点⑦持仓页共享），实现"在 OCR 页选过 → 持仓页默认同款"。
  const [backend, _setBackend] = useState<string>(() => {
    try { return localStorage.getItem(VISION_BACKEND_KEY) || ""; } catch { return ""; }
  });
  const setBackend = (v: string) => {
    _setBackend(v);
    try { if (v) localStorage.setItem(VISION_BACKEND_KEY, v); else localStorage.removeItem(VISION_BACKEND_KEY); } catch { /* private mode */ }
  };
  const llmCfg = useQuery({ queryKey: ["llm-config"], queryFn: getLlmConfig });
  const effBackend = backend || llmCfg.data?.backend || undefined;
  const prevJobsRef = useRef<OcrJob[]>([]);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  const query = useQuery<OcrData>({
    queryKey: ["ocr", batchId],
    queryFn: () => getOcr(batchId as string),
    enabled: !!batchId,
    // poll while anything is running; harmless steady-state refresh otherwise
    refetchInterval: (q) => {
      const d = q.state.data;
      if (!d) return false;
      return d.stats.running > 0 || d.stats.todo > 0 ? 2000 : false;
    },
  });

  const trigger = useMutation({
    mutationFn: () => runOcr(batchId as string, effBackend),
    onSuccess: (r) => {
      const msg = r.queued > 0
        ? `已触发 OCR（${r.queued} 张）— 后台处理中…`
        : "无待跑截图（全部已完成）。要重新识别整批请点「重跑全部」，或点某张「重跑这张」。";
      setLog((l) => [...l, { t: nowStamp(), kind: r.queued > 0 ? "INFO" : "SKIP", text: msg }]);
      qc.invalidateQueries({ queryKey: ["ocr", batchId] });
    },
    onError: (e: unknown) => {
      setLog((l) => [...l, { t: nowStamp(), kind: "FAIL", text: `触发失败: ${(e as Error).message}` }]);
    },
  });

  const rerunFailed = useMutation({
    mutationFn: () => rerunOcr(batchId as string, undefined, effBackend),
    onSuccess: (r) => {
      setLog((l) => [...l, { t: nowStamp(), kind: "INFO", text: `已重跑未成功截图（${r.queued} 张）— 后台处理中…` }]);
      qc.invalidateQueries({ queryKey: ["ocr", batchId] });
    },
    onError: (e: unknown) => {
      setLog((l) => [...l, { t: nowStamp(), kind: "FAIL", text: `重跑失败: ${(e as Error).message}` }]);
    },
  });

  // 重跑全部（含已完成）：用新 prompt/schema 重识别整批。传所有 image_index。
  const rerunAll = useMutation({
    mutationFn: () => {
      const idx = (query.data?.jobs ?? [])
        .map((j) => j.image_index)
        .filter((i): i is number => i != null);
      return rerunOcr(batchId as string, idx, effBackend);
    },
    onSuccess: (r) => {
      setLog((l) => [...l, { t: nowStamp(), kind: "INFO", text: `已重跑全部（${r.queued} 张）— 后台处理中…` }]);
      qc.invalidateQueries({ queryKey: ["ocr", batchId] });
    },
    onError: (e: unknown) => {
      setLog((l) => [...l, { t: nowStamp(), kind: "FAIL", text: `重跑失败: ${(e as Error).message}` }]);
    },
  });

  const cancel = useMutation({
    mutationFn: () => cancelOcr(batchId as string),
    onSuccess: (r) => {
      setLog((l) => [...l, { t: nowStamp(), kind: "FAIL", text: `已强制终止 — 重置 ${r.reset} 张卡住的截图为待办，可重跑` }]);
      qc.invalidateQueries({ queryKey: ["ocr", batchId] });
    },
    onError: (e: unknown) => {
      setLog((l) => [...l, { t: nowStamp(), kind: "FAIL", text: `终止失败: ${(e as Error).message}` }]);
    },
  });

  // SSE: refetch on any pipeline state push so polling + push both feed the table.
  useEffect(() => {
    if (!batchId) return;
    let es: EventSource | null = null;
    try {
      es = openPipelineSse(batchId);
      es.onopen = () => setLive(true);
      const onState = () => qc.invalidateQueries({ queryKey: ["ocr", batchId] });
      es.addEventListener("state", onState);
      es.onerror = () => setLive(false);
    } catch {
      setLive(false);
    }
    return () => {
      setLive(false);
      es?.close();
    };
  }, [batchId, qc]);

  // Derive log lines from job-status diffs whenever data changes.
  useEffect(() => {
    const jobs = query.data?.jobs;
    if (!jobs) return;
    const newLines = diffJobs(prevJobsRef.current, jobs);
    prevJobsRef.current = jobs;
    if (newLines.length) setLog((l) => [...l, ...newLines].slice(-200));
  }, [query.data]);

  // Reset per-batch derived state.
  useEffect(() => {
    prevJobsRef.current = [];
    setLog([]);
    setSelectedJobId(null);
  }, [batchId]);

  // Autoscroll log.
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ block: "end" });
  }, [log]);

  if (!batchId) {
    return (
      <div className="panel" style={{ textAlign: "center", color: "#6b7280" }}>
        <div style={{ fontSize: 28, marginBottom: 6 }}>🗂️</div>
        <div style={{ fontWeight: 800, fontSize: 13 }}>选择一个批次</div>
        <div style={{ fontSize: 11, marginTop: 4 }}>从顶部批次列表选一个，查看 OCR 进度</div>
      </div>
    );
  }

  if (query.isLoading) {
    return <div className="panel" style={{ color: "#6b7280", fontSize: 12 }}>加载 OCR 数据中…</div>;
  }

  if (query.isError) {
    return (
      <div className="panel" style={{ borderColor: "#dc2626" }}>
        <div style={{ fontWeight: 800, color: "#dc2626", fontSize: 13, marginBottom: 4 }}>加载失败</div>
        <div className="mono" style={{ fontSize: 11, color: "#6b7280" }}>{(query.error as Error).message}</div>
        <button className="cbtn" style={{ marginTop: 10 }} onClick={() => query.refetch()}>
          重试
        </button>
      </div>
    );
  }

  const data = query.data!;
  const { stats, jobs } = data;
  const done = stats.done + stats.failed + stats.skipped;
  const pct = stats.total > 0 ? Math.round((done / stats.total) * 100) : 0;

  const statCards: Array<{ label: string; value: number; cls?: string }> = [
    { label: "总数", value: stats.total },
    { label: "完成", value: stats.done, cls: "green" },
    { label: "进行", value: stats.running, cls: "blue" },
    { label: "失败", value: stats.failed, cls: "red" },
    { label: "跳过", value: stats.skipped, cls: "orange" },
    { label: "待办", value: stats.todo },
  ];

  const statColor: Record<string, string> = {
    green: "#16a34a",
    blue: "#2563eb",
    red: "#dc2626",
    orange: "#ea580c",
  };

  const busy = stats.running > 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
    <div className="panel">
      {/* stage head */}
      <div className="stage-head" style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 12 }}>
        <div>
          <span className="stage-title" style={{ fontSize: 16, fontWeight: 800 }}>
            ② OCR + 解析
          </span>
          <span
            className="chip"
            style={{
              display: "inline-block",
              fontSize: 9,
              fontWeight: 800,
              padding: "2px 7px",
              border: "1.5px solid #1f2937",
              borderRadius: 4,
              background: "#c4b5fd",
              textTransform: "uppercase",
              letterSpacing: "0.05em",
              marginLeft: 6,
              verticalAlign: "middle",
            }}
          >
            VISION
          </span>
          <div className="stage-sub mono" style={{ fontSize: 10.5, color: "#6b7280", fontWeight: 600, marginTop: 2 }}>
            {busy ? "解析进行中…" : "Vision OCR · 截图 → 结构化行"}
            {live ? " · ● LIVE" : ""}
          </div>
        </div>
        <div className="stage-actions" style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <select
            value={backend || llmCfg.data?.backend || ""}
            onChange={(e) => setBackend(e.target.value)}
            className="rounded border border-zinc-600 bg-zinc-800 text-zinc-100 px-2 py-1 text-sm"
            title="本次 OCR 用哪个模型后端（默认跟随服务端配置；与节点⑦持仓页、节点⑨研判选择保持一致）"
          >
            {(llmCfg.data?.choices ?? []).map((c) => (
              <option key={c} value={c}>
                {BACKEND_LABEL[c] ?? c}
              </option>
            ))}
          </select>
          {busy && (
            <button
              className="cbtn"
              disabled={cancel.isPending}
              onClick={() => {
                if (window.confirm("强制终止当前 OCR 运行？正在跑的截图会被重置为待办，之后可重跑。")) {
                  cancel.mutate();
                }
              }}
              style={{ borderColor: "#dc2626", color: "#dc2626", ...(cancel.isPending ? { opacity: 0.6, cursor: "not-allowed" } : {}) }}
            >
              {cancel.isPending ? "终止中…" : "强制终止"}
            </button>
          )}
          {stats.failed + stats.skipped > 0 && (
            <button
              className="cbtn"
              disabled={rerunFailed.isPending || busy}
              onClick={() => rerunFailed.mutate()}
              style={rerunFailed.isPending || busy ? { opacity: 0.6, cursor: "not-allowed" } : undefined}
            >
              {rerunFailed.isPending ? "重跑中…" : `重跑未成功 (${stats.failed + stats.skipped})`}
            </button>
          )}
          {stats.done > 0 && (
            <button
              className="cbtn"
              disabled={rerunAll.isPending || busy}
              onClick={() => {
                if (window.confirm(`重跑全部 ${stats.total} 张（含已完成）？会用新规则重新识别整批，耗时较长。`)) {
                  rerunAll.mutate();
                }
              }}
              style={rerunAll.isPending || busy ? { opacity: 0.6, cursor: "not-allowed" } : undefined}
            >
              {rerunAll.isPending ? "重跑中…" : `重跑全部 (${stats.total})`}
            </button>
          )}
          <button
            className="cbtn cbtn-primary"
            disabled={trigger.isPending || busy}
            onClick={() => trigger.mutate()}
            style={trigger.isPending || busy ? { opacity: 0.6, cursor: "not-allowed" } : undefined}
          >
            {trigger.isPending ? "触发中…" : busy ? "运行中…" : "触发 OCR"}
          </button>
        </div>
      </div>

      {/* stats grid */}
      <div
        className="stats"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(90px, 1fr))",
          gap: 8,
          marginBottom: 14,
        }}
      >
        {statCards.map((c) => (
          <div
            key={c.label}
            className="stat-card chunky"
            style={{
              border: "2px solid #1f2937",
              borderRadius: 8,
              padding: "8px 10px",
              boxShadow: "2px 2px 0 #1f2937",
              background: "#fff",
            }}
          >
            <div className="stat-l" style={{ fontSize: 8.5, color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.05em", fontWeight: 700 }}>
              {c.label}
            </div>
            <div
              className="stat-v mono"
              style={{ fontSize: 18, fontWeight: 800, marginTop: 3, color: c.cls ? statColor[c.cls] : "#1f2937" }}
            >
              {c.value}
            </div>
          </div>
        ))}
      </div>

      {/* progress */}
      <div
        className="prog-wrap"
        style={{
          border: "2px solid #1f2937",
          borderRadius: 8,
          overflow: "hidden",
          height: 18,
          background: "#fff",
          boxShadow: "2px 2px 0 #1f2937",
          marginBottom: 14,
          position: "relative",
        }}
      >
        <div
          className="prog-bar"
          style={{
            background: "#fcd34d",
            height: "100%",
            width: `${pct}%`,
            borderRight: pct > 0 && pct < 100 ? "2px solid #1f2937" : "none",
            transition: "width .3s",
          }}
        />
        <div
          className="prog-label mono"
          style={{ position: "absolute", top: "50%", left: "50%", transform: "translate(-50%, -50%)", fontSize: 10, fontWeight: 800 }}
        >
          {pct}% · {done}/{stats.total}
        </div>
      </div>

      {/* thumbnails */}
      <div className="section-h">截图清单</div>
      {jobs.length === 0 ? (
        <div style={{ fontSize: 11, color: "#9ca3af", marginBottom: 14 }}>暂无截图任务</div>
      ) : (
        <div
          className="thumbs"
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(22px, 1fr))",
            gap: 3,
            marginBottom: 14,
          }}
        >
          {jobs.map((j) => {
            const k = thumbClass(j.status);
            const s = THUMB_BG[k];
            const idx = j.image_index ?? j.job_id;
            const selected = j.job_id === selectedJobId;
            return (
              <div
                key={j.job_id}
                className={`ct ${k}`}
                role="button"
                onClick={() => setSelectedJobId(selected ? null : j.job_id)}
                title={`${j.image ?? `#${idx}`} · ${j.status}${j.rows ? ` · ${j.rows} rows` : ""}${j.reason_friendly ? ` · ${j.reason_friendly}` : ""} — 点击看原图`}
                style={{
                  aspectRatio: "1",
                  border: "1px solid #1f2937",
                  borderRadius: 3,
                  fontSize: 7,
                  fontWeight: 700,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  background: s.bg,
                  color: s.fg,
                  cursor: "pointer",
                  // selected → yellow ring; running → blue ring (the mockup's
                  // @keyframes cpulse isn't in chunky.css)
                  boxShadow: selected
                    ? "0 0 0 2px #fcd34d"
                    : s.pulse
                    ? "0 0 0 1.5px #2563eb"
                    : undefined,
                  opacity: s.pulse ? 0.85 : 1,
                }}
              >
                {idx}
              </div>
            );
          })}
        </div>
      )}

      {/* per-screenshot detail: original image + skip/fail reason */}
      {selectedJobId != null &&
        (() => {
          const sel = jobs.find((j) => j.job_id === selectedJobId);
          return sel ? <OcrJobDetail job={sel} batchId={batchId} /> : null;
        })()}

      {/* live log (dark panel) */}
      <div className="section-h">实时日志{live ? " · LIVE" : ""}</div>
      <div
        className="log mono"
        style={{
          background: "#1f2937",
          color: "#d1d5db",
          padding: "10px 12px",
          borderRadius: 8,
          fontSize: 10,
          lineHeight: 1.7,
          border: "2px solid #1f2937",
          boxShadow: "2px 2px 0 #1f2937",
          maxHeight: 220,
          overflowY: "auto",
        }}
      >
        {log.length === 0 ? (
          <div style={{ color: "#94a3b8" }}>等待状态变化… 点击「触发 OCR」开始。</div>
        ) : (
          log.map((line, i) => (
            <div key={i}>
              <span style={{ color: "#94a3b8" }}>[{line.t}]</span>{" "}
              <span style={{ color: LOG_KIND_COLOR[line.kind], fontWeight: 700 }}>{line.kind}</span>{" "}
              {line.text}
            </div>
          ))
        )}
        <div ref={logEndRef} />
      </div>
    </div>
    </div>
  );
}
