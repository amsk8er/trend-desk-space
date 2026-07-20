import { useMemo, useState, type ReactNode } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getRows, getReview, chatTool, type Row } from "../api";

// ③ 校对 — node review（默认通过 + 异常审查模型）。
// 后端 default-pass：下游消费「非 rejected」行，pending 默认可用 —— 所以校对不再需要
// 逐行盖章。这里只把**异常行**（截断 / 缺关键字段）挑出来重点显示，用户只 reject 读错的。
// - approve 直接生效（不再弹 window.prompt）
// - reject 用 inline 输入填原因（不用浏览器原生弹窗）
// - 顶部「全部通过」批量键 + 「只看异常」筛选

// ── 异常判定：哪些行值得人工看一眼 ──
function anomalyReasons(r: Row): string[] {
  const out: string[] = [];
  const name = (r.name ?? "").trim();
  if (name.includes("截断") || (!r.code && !name)) out.push("截断/无身份");
  if (r.row_type === "instrument") {
    const rf = (r.raw_fields ?? {}) as Record<string, unknown>;
    if (rf.market_cap_yi == null) out.push("缺市值");
    if (rf.turnover_yi == null) out.push("缺成交额");
    if (r.right_side_days == null) out.push("缺右侧天数");
  }
  return out;
}
const isAnomaly = (r: Row) => anomalyReasons(r).length > 0;
const isRejected = (r: Row) => (r.review_status ?? "").toLowerCase().includes("reject");

export default function ReviewStage({ batchId }: { batchId: string | null }) {
  if (!batchId) {
    return (
      <div className="panel" style={{ textAlign: "center", color: "#6b7280" }}>
        <div style={{ fontSize: 28, marginBottom: 6 }}>📋</div>
        <div style={{ fontWeight: 700 }}>选择一个批次</div>
        <div style={{ fontSize: 11, marginTop: 4 }}>选定批次后即可校对识别行。</div>
      </div>
    );
  }
  return <ReviewBody batchId={batchId} />;
}

function ReviewBody({ batchId }: { batchId: string }) {
  const qc = useQueryClient();

  const reviewQ = useQuery({
    queryKey: ["review", batchId],
    queryFn: () => getReview(batchId),
  });

  const rowsQ = useQuery({
    queryKey: ["rows", batchId],
    queryFn: () => getRows(batchId),
  });

  const decide = useMutation({
    mutationFn: (v: { action: "approve_rows" | "reject_rows"; rowIds: number[]; reason: string }) =>
      chatTool(v.action, { row_ids: v.rowIds, reason: v.reason }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rows", batchId] });
      qc.invalidateQueries({ queryKey: ["review", batchId] });
    },
  });

  const [sectorFilter, setSectorFilter] = useState<string>("");
  const [onlyAnomaly, setOnlyAnomaly] = useState(false);

  const allRows = rowsQ.data ?? [];
  const sectors = useMemo(
    () => Array.from(new Set(allRows.map((r) => r.sector).filter(Boolean))) as string[],
    [allRows],
  );
  const anomalies = useMemo(() => allRows.filter(isAnomaly), [allRows]);
  const pendingIds = useMemo(
    () => allRows.filter((r) => (r.review_status ?? "").toLowerCase().includes("pending")).map((r) => r.row_id),
    [allRows],
  );

  const filtered = useMemo(
    () =>
      allRows.filter(
        (r) => (!sectorFilter || r.sector === sectorFilter) && (!onlyAnomaly || isAnomaly(r)),
      ),
    [allRows, sectorFilter, onlyAnomaly],
  );
  const groups = useMemo(() => groupRows(filtered), [filtered]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* ── top: review summary + can_proceed banner ── */}
      <div className="panel">
        <div className="section-h">校对汇总</div>
        {reviewQ.isLoading && <div style={{ fontSize: 11, color: "#6b7280" }}>加载汇总中…</div>}
        {reviewQ.isError && (
          <div style={{ fontSize: 11, color: "#dc2626" }}>
            汇总加载失败：{(reviewQ.error as Error).message}
          </div>
        )}
        {reviewQ.data && (
          <>
            <SummaryStats summary={reviewQ.data.summary} />
            <ProceedBanner canProceed={reviewQ.data.can_proceed} message={reviewQ.data.message} />
          </>
        )}
        <div
          className="chunky"
          style={{ marginTop: 10, padding: "8px 12px", background: "#eef2ff", fontSize: 11, lineHeight: 1.6 }}
        >
          默认通过：识别行默认进入筛选，不用逐行盖章。
          下面只需检查 <b style={{ color: "#b45309" }}>{anomalies.length}</b> 个异常行（截断 / 缺关键字段），把读错的驳回即可。
        </div>
      </div>

      {/* ── rows table ── */}
      <div className="panel">
        <div
          className="section-h"
          style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}
        >
          <span>识别行</span>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, fontWeight: 700 }}>
              <input type="checkbox" checked={onlyAnomaly} onChange={(e) => setOnlyAnomaly(e.target.checked)} />
              只看异常({anomalies.length})
            </label>
            {sectors.length > 0 && (
              <select
                className="model-pill"
                value={sectorFilter}
                onChange={(e) => setSectorFilter(e.target.value)}
                title="按板块筛选"
              >
                <option value="">全部板块</option>
                {sectors.map((sec) => (
                  <option key={sec} value={sec}>{sec}</option>
                ))}
              </select>
            )}
            <button
              className="cbtn cbtn-primary"
              style={{ padding: "3px 10px", fontSize: 11 }}
              disabled={decide.isPending || allRows.length === 0}
              title="把所有 pending 行标为通过（reject 的不动），随后自动跳到聚合节点 —— 校对压成 1 次点击"
              onClick={() => {
                // Q1：默认通过模型下校对只是「确认 + 跳下一步」。一键全通过后自动推进到聚合，
                // pending=0（已全通过/无待审）时也直接跳，省去手点节点条。
                const jump = () =>
                  window.dispatchEvent(new CustomEvent("trenddesk:select-node", { detail: "aggregate" }));
                if (pendingIds.length === 0) {
                  jump();
                } else {
                  decide.mutate(
                    { action: "approve_rows", rowIds: pendingIds, reason: "批量通过" },
                    { onSuccess: jump },
                  );
                }
              }}
            >
              全部通过 → 聚合({pendingIds.length})
            </button>
            {rowsQ.data && <span className="mono">{filtered.length}/{allRows.length} 行</span>}
          </div>
        </div>

        {rowsQ.isLoading && <div style={{ fontSize: 11, color: "#6b7280" }}>加载行数据中…</div>}
        {rowsQ.isError && (
          <div style={{ fontSize: 11, color: "#dc2626" }}>
            行数据加载失败：{(rowsQ.error as Error).message}
          </div>
        )}
        {rowsQ.data && allRows.length === 0 && (
          <div style={{ fontSize: 11, color: "#6b7280" }}>该批次暂无识别行。</div>
        )}

        {decide.isError && (
          <div
            className="chunky"
            style={{ fontSize: 11, color: "#dc2626", padding: "6px 10px", marginBottom: 10, background: "#fee2e2" }}
          >
            操作失败：{(decide.error as Error).message}
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {groups.map((g) => (
            <div key={g.rowType}>
              <div
                style={{ fontSize: 12, fontWeight: 800, marginBottom: 6, display: "flex", alignItems: "center", gap: 6 }}
              >
                {g.rowType}
                <span className="chip">{g.count}</span>
              </div>
              {g.markets.map((m) => (
                <MarketBlock
                  key={`${g.rowType}::${m.market}`}
                  market={m.market}
                  rows={m.rows}
                  pending={decide.isPending}
                  onDecide={(action, rowId, reason) => decide.mutate({ action, rowIds: [rowId], reason })}
                />
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── summary stats (summary is free-form; render number-ish entries as cards) ──
function SummaryStats({ summary }: { summary: unknown }) {
  if (!summary || typeof summary !== "object") return null;
  const entries = Object.entries(summary as Record<string, unknown>).filter(
    ([, v]) => typeof v === "number" || typeof v === "string",
  );
  if (entries.length === 0) return null;
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(90px, 1fr))",
        gap: 8,
        marginBottom: 12,
      }}
    >
      {entries.map(([k, v]) => (
        <div key={k} className="stat-card">
          <div className="stat-l">{k}</div>
          <div className="stat-v mono">{String(v)}</div>
        </div>
      ))}
    </div>
  );
}

function ProceedBanner({ canProceed, message }: { canProceed: boolean; message: string }) {
  return (
    <div
      className="chunky"
      style={{
        padding: "8px 12px",
        background: canProceed ? "#d1fae5" : "#fed7aa",
        display: "flex",
        alignItems: "center",
        gap: 8,
        fontSize: 12,
        fontWeight: 700,
      }}
    >
      <span style={{ fontSize: 14 }}>{canProceed ? "✅" : "⚠️"}</span>
      <span>{message || (canProceed ? "可以进入下一步" : "尚不能进入下一步")}</span>
    </div>
  );
}

// ── market block: a small table of rows under one market ──
function MarketBlock({
  market,
  rows,
  pending,
  onDecide,
}: {
  market: string;
  rows: Row[];
  pending: boolean;
  onDecide: (action: "approve_rows" | "reject_rows", rowId: number, reason: string) => void;
}) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div className="pill pill-date" style={{ marginBottom: 6, display: "inline-block" }}>
        {market}
      </div>
      <div className="chunky" style={{ overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
          <thead>
            <tr style={{ background: "#f3f4f6", textAlign: "left" }}>
              <Th />
              <Th>代码</Th>
              <Th>名称</Th>
              <Th>板块</Th>
              <Th>温度</Th>
              <Th>强度</Th>
              <Th>右侧天数</Th>
              <Th>右侧涨幅%</Th>
              <Th>节气</Th>
              <Th>校对状态</Th>
              <Th>操作</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <RowItem key={r.row_id} row={r} pending={pending} onDecide={onDecide} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Th({ children }: { children?: ReactNode }) {
  return (
    <th
      style={{
        padding: "6px 8px",
        fontSize: 9,
        fontWeight: 800,
        textTransform: "uppercase",
        letterSpacing: "0.05em",
        color: "#6b7280",
        borderBottom: "2px solid #1f2937",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </th>
  );
}

function RowItem({
  row,
  pending,
  onDecide,
}: {
  row: Row;
  pending: boolean;
  onDecide: (action: "approve_rows" | "reject_rows", rowId: number, reason: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const flags = anomalyReasons(row);
  const anomaly = flags.length > 0;
  const rejected = isRejected(row);

  return (
    <>
      <tr style={{ borderBottom: "1px solid #e5e7eb", background: anomaly && !rejected ? "#fff7ed" : undefined }}>
        <Td>
          <button
            className="cbtn"
            style={{ padding: "2px 7px", fontSize: 11 }}
            onClick={() => setOpen((o) => !o)}
            title="查看原始字段"
          >
            {open ? "▾" : "▸"}
          </button>
        </Td>
        <Td>
          <span className="mono" style={{ fontWeight: 700 }}>{row.code ?? "—"}</span>
        </Td>
        <Td>
          {row.name ?? "—"}
          {anomaly && (
            <span
              className="chip"
              style={{ marginLeft: 6, background: "var(--warn)", fontSize: 9 }}
              title={flags.join(" / ")}
            >
              ⚠ {flags.join("·")}
            </span>
          )}
        </Td>
        <Td>{row.sector ? <span className="chip" style={{ marginLeft: 0 }}>{row.sector}</span> : <span style={{ color: "#9ca3af" }}>—</span>}</Td>
        <Td><TempStatusChip status={row.temperature_status} /></Td>
        <Td><span className="mono">{row.strength ?? "—"}</span></Td>
        <Td><span className="mono">{row.right_side_days ?? "—"}</span></Td>
        <Td><span className="mono">{row.right_side_gain_pct ?? "—"}</span></Td>
        <Td>{row.jieqi ? <span className="chip" style={{ marginLeft: 0 }}>{row.jieqi}</span> : <span style={{ color: "#9ca3af" }}>—</span>}</Td>
        <Td><ReviewStatusChip status={row.review_status} reason={row.review_reason} /></Td>
        <Td>
          {rejecting ? (
            <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
              <input
                className="chat-input"
                style={{ padding: "2px 6px", fontSize: 10, width: 120 }}
                placeholder="驳回原因"
                value={reason}
                autoFocus
                onChange={(e) => setReason(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") { onDecide("reject_rows", row.row_id, reason); setRejecting(false); setReason(""); }
                  if (e.key === "Escape") { setRejecting(false); setReason(""); }
                }}
              />
              <button
                className="cbtn cbtn-warn"
                style={{ padding: "3px 8px", fontSize: 10 }}
                disabled={pending}
                onClick={() => { onDecide("reject_rows", row.row_id, reason); setRejecting(false); setReason(""); }}
              >
                确认
              </button>
              <button
                className="cbtn"
                style={{ padding: "3px 6px", fontSize: 10 }}
                onClick={() => { setRejecting(false); setReason(""); }}
              >
                取消
              </button>
            </div>
          ) : (
            <div style={{ display: "flex", gap: 4 }}>
              <button
                className="cbtn cbtn-primary"
                style={{ padding: "3px 8px", fontSize: 10 }}
                disabled={pending}
                title="直接通过（默认动作，无需原因）"
                onClick={() => onDecide("approve_rows", row.row_id, "")}
              >
                通过
              </button>
              <button
                className="cbtn cbtn-warn"
                style={{ padding: "3px 8px", fontSize: 10 }}
                disabled={pending}
                onClick={() => setRejecting(true)}
              >
                驳回
              </button>
            </div>
          )}
        </Td>
      </tr>
      {open && (
        <tr style={{ borderBottom: "1px solid #e5e7eb" }}>
          <td colSpan={11} style={{ padding: "8px 10px", background: "#faf7e8" }}>
            <div className="section-h">原始字段 raw_fields</div>
            <pre
              className="mono"
              style={{
                margin: 0,
                fontSize: 10,
                lineHeight: 1.5,
                whiteSpace: "pre-wrap",
                wordBreak: "break-all",
                maxHeight: 220,
                overflow: "auto",
              }}
            >
              {row.raw_fields ? JSON.stringify(row.raw_fields, null, 2) : "（无原始字段）"}
            </pre>
          </td>
        </tr>
      )}
    </>
  );
}

function Td({ children }: { children?: ReactNode }) {
  return (
    <td style={{ padding: "6px 8px", verticalAlign: "middle", whiteSpace: "nowrap" }}>
      {children}
    </td>
  );
}

function TempStatusChip({ status }: { status: string | null }) {
  if (!status) return <span style={{ color: "#9ca3af" }}>—</span>;
  const s = status.toLowerCase();
  let bg = "#fff";
  if (s.includes("hot") || s.includes("热")) bg = "var(--err)";
  else if (s.includes("warm") || s.includes("温")) bg = "var(--warn)";
  else if (s.includes("cool") || s.includes("冷")) bg = "var(--info)";
  return (
    <span className="chip" style={{ background: bg, marginLeft: 0 }}>
      {status}
    </span>
  );
}

function ReviewStatusChip({ status, reason }: { status: string | null; reason: string | null }) {
  if (!status) return <span style={{ color: "#9ca3af" }}>待校对</span>;
  const s = status.toLowerCase();
  let bg = "var(--idle)";
  if (s.includes("approve") || s.includes("通过") || s.includes("ok")) bg = "var(--ok)";
  else if (s.includes("reject") || s.includes("驳回") || s.includes("fail")) bg = "var(--err)";
  else if (s.includes("pending") || s.includes("待")) bg = "var(--warn)";
  return (
    <span className="chip" style={{ background: bg, marginLeft: 0 }} title={reason ?? undefined}>
      {status}
    </span>
  );
}

// ── grouping helper: row_type -> market -> rows ──
interface MarketGroup {
  market: string;
  rows: Row[];
}
interface TypeGroup {
  rowType: string;
  count: number;
  markets: MarketGroup[];
}

function groupRows(rows: Row[]): TypeGroup[] {
  const byType = new Map<string, Map<string, Row[]>>();
  for (const r of rows) {
    const t = r.row_type ?? "（未分类）";
    const m = r.market ?? "（无市场）";
    if (!byType.has(t)) byType.set(t, new Map());
    const markets = byType.get(t)!;
    if (!markets.has(m)) markets.set(m, []);
    markets.get(m)!.push(r);
  }
  const out: TypeGroup[] = [];
  for (const [rowType, markets] of byType) {
    const marketGroups: MarketGroup[] = [];
    let count = 0;
    for (const [market, mrows] of markets) {
      marketGroups.push({ market, rows: mrows });
      count += mrows.length;
    }
    out.push({ rowType, count, markets: marketGroups });
  }
  return out;
}
