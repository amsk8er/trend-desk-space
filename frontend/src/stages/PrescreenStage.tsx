import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getPrescreen, runPrescreen } from "../api";
import { type Board, BOARD_ORDER, boardOf } from "./boards";

// ⑤ 初筛 — renders manifest_json as a stack of B-cards:
//   大盘卡 / 今日温转热候选卡 / 出局警告卡 / 初筛报告卡
// Top button triggers runPrescreen.

interface Candidate {
  name?: string;
  code?: string;
  status?: string | null;
  strength?: number | string | null;
  sector?: string | null;
  sector_status?: string | null;
  daily_change_pct?: number | null;
  right_side_days?: number | null;
  right_side_gain_pct?: number | null;
  jieqi?: string | null;
  market_cap_yi?: number | null;
  turnover_yi?: number | null;
  is_etf?: boolean | null;
  tags?: string[] | null;
  [k: string]: unknown;
}

function asArray(v: unknown): unknown[] {
  return Array.isArray(v) ? v : [];
}

function asString(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

function pick(obj: Record<string, unknown>, keys: string[]): unknown {
  for (const k of keys) {
    if (obj[k] != null) return obj[k];
  }
  return undefined;
}

// Q2：把一条具体拒绝原因（带不同数值）归到一个稳定类别，便于「原因一致」的票聚合折叠。
function categorizeReason(reason: string): string {
  if (reason.includes("板块温度未知") || reason.includes("板块=?")) return "板块温度未知";
  if (reason.includes("温度")) return "板块温度不达标";
  if (reason.includes("规模")) return "ETF规模不足";
  if (reason.includes("市值")) return "市值不足";
  if (reason.includes("成交额")) return "成交额不足";
  if (reason.includes("右侧") && reason.includes("窗口")) return "过入场窗口";
  return reason; // 兜底：未知样式按原文单独成类，不静默吞掉
}

interface RejectedStock {
  name: string;
  code: string;
  reasons: string[];
}
interface ReasonGroup {
  categories: string[];
  signature: string;
  stocks: RejectedStock[];
}

interface ApiComponentWarning {
  combo: string;
  constituent_count: number;
  returned_basic_count: number;
  note: string;
}

// 按「原因类别组合」分组：同一组合（如 温度不达标 + 市值不足）的票聚到一组。
function groupRejected(detail: Record<string, unknown>[]): ReasonGroup[] {
  const groups = new Map<string, ReasonGroup>();
  for (const d of detail) {
    const reasons = asArray(pick(d, ["reasons"])).map(asString);
    const categories = [...new Set(reasons.map(categorizeReason))].sort();
    const signature = categories.join(" + ") || "（无原因）";
    if (!groups.has(signature)) groups.set(signature, { categories, signature, stocks: [] });
    groups.get(signature)!.stocks.push({
      name: asString(pick(d, ["name"]) ?? "—"),
      code: asString(pick(d, ["code"]) ?? ""),
      reasons,
    });
  }
  // 票多的组排前面
  return [...groups.values()].sort((a, b) => b.stocks.length - a.stocks.length);
}

// chunky tokens (stat-card / chip live only in the HTML mockup, so inline them)
const INK = "#1f2937";
const cardBase: React.CSSProperties = {
  background: "#fff",
  border: `2px solid ${INK}`,
  borderRadius: 12,
  padding: 14,
  boxShadow: "3px 3px 0 #1f2937",
};
const chip: React.CSSProperties = {
  display: "inline-block",
  fontSize: 9,
  fontWeight: 800,
  padding: "2px 7px",
  border: `1.5px solid ${INK}`,
  borderRadius: 4,
  background: "#fff",
  textTransform: "uppercase",
  letterSpacing: "0.05em",
};
// 大盘紧凑横条用：label + value 同行，不占大块面积
function MiniStat({ label, value, color }: { label: string; value: React.ReactNode; color?: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "baseline", gap: 4 }}>
      <span style={{ fontSize: 9, color: "#6b7280", fontWeight: 700, letterSpacing: "0.04em" }}>
        {label}
      </span>
      <span className="mono" style={{ fontSize: 12, fontWeight: 800, ...(color ? { color } : {}) }}>
        {value}
      </span>
    </span>
  );
}

export default function PrescreenStage({ batchId }: { batchId: string | null }) {
  const qc = useQueryClient();

  // ETF 线全局参数（留空 → 后端默认）。单位亿，正数才生效。
  const [etfAumStr, setEtfAumStr] = useState("");
  const [etfTurnoverStr, setEtfTurnoverStr] = useState("");
  // 个股线参数（市值门 / 成交额门）。
  const [stkCapStr, setStkCapStr] = useState("");
  const [stkTurnoverStr, setStkTurnoverStr] = useState("");
  // 复制候选按钮状态
  const [copiedCandidates, setCopiedCandidates] = useState(false);
  const toYi = (s: string): number | null => {
    const v = parseFloat(s);
    return Number.isFinite(v) && v > 0 ? v : null;
  };

  const query = useQuery({
    queryKey: ["prescreen", batchId],
    queryFn: () => getPrescreen(batchId as string),
    enabled: !!batchId,
  });

  const run = useMutation({
    mutationFn: () =>
      runPrescreen(batchId as string, {
        etfMinAumYi: toYi(etfAumStr),
        etfMinTurnoverYi: toYi(etfTurnoverStr),
        minMarketCapYi: toYi(stkCapStr),
        minTurnoverYi: toYi(stkTurnoverStr),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["prescreen", batchId] });
      qc.invalidateQueries({ queryKey: ["state", batchId] });
    },
  });

  if (!batchId) {
    return (
      <div className="panel" style={{ textAlign: "center", color: "#6b7280" }}>
        <div style={{ fontSize: 13, fontWeight: 800, marginBottom: 4 }}>⑤ 初筛</div>
        <div style={{ fontSize: 11 }}>选择一个批次以查看初筛结果</div>
      </div>
    );
  }

  // ── manifest extraction (free-form, defensive) ──
  const manifestRaw = query.data ?? {};
  const manifest: Record<string, unknown> =
    (manifestRaw.manifest_json as Record<string, unknown>) ?? manifestRaw;

  const market =
    (pick(manifest, ["market", "market_status", "大盘", "index", "regime"]) as
      | Record<string, unknown>
      | string
      | undefined) ?? undefined;

  const candidates = asArray(
    pick(manifest, ["candidates", "warm_to_hot", "温转热", "today_candidates", "hot_candidates"]),
  ) as Candidate[];

  const report =
    pick(manifest, ["report", "summary", "report_text", "初筛报告", "note", "conclusion"]) ??
    undefined;
  const apiComponentWarnings = asArray(
    pick(manifest, ["api_component_count_warnings"]),
  ) as ApiComponentWarning[];

  const hasAny = Object.keys(manifest).length > 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* toolbar */}
      <div className="toolbar">
        <div className="toolbar-l">
          <span style={{ fontSize: 13, fontWeight: 800 }}>⑤ 初筛</span>
          <span style={{ ...chip, background: "#c4b5fd" }}>PRESCREEN</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <label style={{ fontSize: 10.5, fontWeight: 700, color: "#374151", display: "flex", alignItems: "center", gap: 3 }}
            title="个股市值门：市值(亿)低于此值的个股被剔。留空用默认 300 亿。">
            个股市值
            <input type="number" step="10" min="0" placeholder="300"
              value={stkCapStr} onChange={(e) => setStkCapStr(e.target.value)}
              style={{ width: 48, padding: "3px 5px", border: "2px solid #1f2937", borderRadius: 6, fontSize: 11 }} />亿
          </label>
          <label style={{ fontSize: 10.5, fontWeight: 700, color: "#374151", display: "flex", alignItems: "center", gap: 3 }}
            title="个股成交额门：日成交额(亿)低于此值的个股被剔。留空用默认 5 亿。">
            个股成交额
            <input type="number" step="0.5" min="0" placeholder="5"
              value={stkTurnoverStr} onChange={(e) => setStkTurnoverStr(e.target.value)}
              style={{ width: 48, padding: "3px 5px", border: "2px solid #1f2937", borderRadius: 6, fontSize: 11 }} />亿
          </label>
          <label style={{ fontSize: 10.5, fontWeight: 700, color: "#374151", display: "flex", alignItems: "center", gap: 3 }}
            title="ETF 规模门：低于此规模(亿)的 ETF 被剔（流动性/清盘风险）。留空用后端默认。">
            ETF规模
            <input type="number" step="1" min="0" placeholder="5"
              value={etfAumStr} onChange={(e) => setEtfAumStr(e.target.value)}
              style={{ width: 48, padding: "3px 5px", border: "2px solid #1f2937", borderRadius: 6, fontSize: 11 }} />亿
          </label>
          <label style={{ fontSize: 10.5, fontWeight: 700, color: "#374151", display: "flex", alignItems: "center", gap: 3 }}
            title="ETF 成交额门：日成交额(亿)低于此值的 ETF 被剔。留空用后端默认。">
            ETF成交额
            <input type="number" step="0.5" min="0" placeholder="2"
              value={etfTurnoverStr} onChange={(e) => setEtfTurnoverStr(e.target.value)}
              style={{ width: 48, padding: "3px 5px", border: "2px solid #1f2937", borderRadius: 6, fontSize: 11 }} />亿
          </label>
          <button
            className="cbtn cbtn-primary"
            onClick={() => run.mutate()}
            disabled={run.isPending}
          >
            {run.isPending ? "初筛中…" : "触发初筛"}
          </button>
        </div>
      </div>

      {run.isError && (
        <div className="panel" style={{ background: "#fee2e2", borderColor: INK }}>
          <div className="section-h" style={{ color: "#b91c1c" }}>触发失败</div>
          <div style={{ fontSize: 11 }}>{String((run.error as Error)?.message ?? run.error)}</div>
        </div>
      )}

      {apiComponentWarnings.map((warning) => (
        <div
          key={warning.combo}
          className="panel mono"
          style={{ background: "#fef3c7", borderColor: "#92400e", color: "#78350f", fontSize: 10.5, padding: "9px 12px" }}
        >
          <b>API 计数差异 · {warning.combo}</b>：constituentCount={warning.constituent_count}，
          全部子级品种={warning.returned_basic_count}。{warning.note}；本批按成分接口实际返回处理。
        </div>
      ))}

      {query.isLoading && (
        <div className="panel" style={{ color: "#6b7280", fontSize: 11 }}>加载初筛结果中…</div>
      )}

      {query.isError && (
        <div className="panel" style={{ background: "#fee2e2" }}>
          <div className="section-h" style={{ color: "#b91c1c" }}>读取失败</div>
          <div style={{ fontSize: 11 }}>
            {String((query.error as Error)?.message ?? query.error)}
          </div>
        </div>
      )}

      {!query.isLoading && !query.isError && !hasAny && (
        <div className="panel" style={{ color: "#6b7280", fontSize: 11 }}>
          暂无初筛结果。点击右上角「触发初筛」生成大盘判断与今日候选。
        </div>
      )}

      {!query.isLoading && !query.isError && hasAny && (
        <>
          {/* ── 大盘卡（紧凑横条） ── */}
          <div style={{ ...cardBase, padding: "6px 12px" }}>
            {market == null ? (
              <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11 }}>
                <span style={{ fontSize: 11, fontWeight: 800 }}>大盘</span>
                <span style={{ color: "#9ca3af" }}>manifest 未包含大盘字段</span>
              </div>
            ) : typeof market === "string" ? (
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontSize: 11, fontWeight: 800 }}>大盘</span>
                <span style={{ fontSize: 12, fontWeight: 700 }}>{market}</span>
              </div>
            ) : (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
                <span style={{ fontSize: 11, fontWeight: 800 }}>大盘</span>
                <span style={{ fontSize: 12, fontWeight: 800 }}>
                  {asString(pick(market, ["name"]) ?? "A股")}
                </span>
                {pick(market, ["status"]) != null && (
                  <MiniStat label="温度" value={asString(pick(market, ["status"]))} color="#ea580c" />
                )}
                {pick(market, ["strength"]) != null && (
                  <MiniStat label="强度" value={asString(pick(market, ["strength"]))} />
                )}
                {pick(market, ["regime"]) != null && (
                  <MiniStat label="格局" value={asString(pick(market, ["regime"]))} />
                )}
              </div>
            )}
          </div>

          {/* ── 今日温转热候选卡 ── */}
          <div style={cardBase}>
            <div
              className="section-h"
              style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}
            >
              <span>今日温转热候选</span>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                {candidates.length > 0 && (
                  <button
                    className="cbtn"
                    style={{ fontSize: 10, padding: "2px 8px" }}
                    onClick={() => {
                      const text = candidates
                        .map((c) => {
                          const n = asString(pick(c, ["name", "名称", "stock_name"]) ?? "");
                          const cd = asString(pick(c, ["code", "代码", "symbol"]) ?? "");
                          return `${n} ${cd}`.trim();
                        })
                        .filter(Boolean)
                        .join("\n");
                      navigator.clipboard.writeText(text).then(() => {
                        setCopiedCandidates(true);
                        setTimeout(() => setCopiedCandidates(false), 1500);
                      });
                    }}
                  >
                    {copiedCandidates ? "已复制 ✓" : "复制候选"}
                  </button>
                )}
                <span style={{ ...chip, background: "#fcd34d" }}>{candidates.length} 只</span>
              </div>
            </div>
            {candidates.length === 0 ? (
              <div style={{ fontSize: 11, color: "#9ca3af" }}>今日无温转热候选</div>
            ) : (
              <CandidateGroupedList candidates={candidates} />
            )}
          </div>

          {/* 出局警告由节点⑧出局检查负责；初筛 manifest 从无 exits 键，原卡片是 v1 mockup 遗留已删 */}

          {/* ── 初筛报告卡 ── */}
          <div style={cardBase}>
            <div className="section-h">初筛报告</div>
            {report == null ? (
              <details>
                <summary style={{ fontSize: 11, color: "#6b7280", cursor: "pointer" }}>
                  无结构化报告 — 查看原始 manifest
                </summary>
                <pre
                  className="mono"
                  style={{
                    fontSize: 10,
                    background: "#1f2937",
                    color: "#d1d5db",
                    padding: "10px 12px",
                    borderRadius: 8,
                    overflow: "auto",
                    marginTop: 8,
                    maxHeight: 320,
                  }}
                >
                  {JSON.stringify(manifest, null, 2)}
                </pre>
              </details>
            ) : typeof report === "string" ? (
              <div style={{ fontSize: 12, lineHeight: 1.7, whiteSpace: "pre-wrap" }}>{report}</div>
            ) : (
              (() => {
                const rep = report as Record<string, unknown>;
                const summary = asString(pick(rep, ["summary"]));
                const detail = asArray(pick(rep, ["rejected_detail"])) as Record<string, unknown>[];
                return (
                  <div>
                    {summary && (
                      <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8 }}>{summary}</div>
                    )}
                    {detail.length === 0 ? (
                      <div style={{ fontSize: 11, color: "#16a34a", fontWeight: 700 }}>
                        无标的被拒
                      </div>
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                        {groupRejected(detail).map((g, gi) => (
                          <details
                            key={g.signature || gi}
                            style={{
                              border: `2px solid ${INK}`,
                              borderRadius: 8,
                              background: "#fef2f2",
                              boxShadow: "2px 2px 0 #1f2937",
                              padding: "7px 11px",
                            }}
                          >
                            <summary
                              style={{
                                cursor: "pointer",
                                display: "flex",
                                alignItems: "center",
                                gap: 8,
                                flexWrap: "wrap",
                                listStyle: "none",
                              }}
                            >
                              <span
                                className="mono"
                                style={{
                                  fontSize: 13,
                                  fontWeight: 800,
                                  color: "#dc2626",
                                  minWidth: 26,
                                }}
                              >
                                {g.stocks.length}
                              </span>
                              <span style={{ fontSize: 10, color: "#6b7280" }}>只 ·</span>
                              {g.categories.map((c, ci) => (
                                <span
                                  key={ci}
                                  style={{
                                    fontSize: 10.5,
                                    fontWeight: 700,
                                    color: "#7f1d1d",
                                    border: `1.5px solid ${INK}`,
                                    borderRadius: 999,
                                    padding: "1px 8px",
                                    background: "#fecaca",
                                  }}
                                >
                                  {c}
                                </span>
                              ))}
                              <span style={{ fontSize: 9.5, color: "#9ca3af", marginLeft: "auto" }}>
                                展开看明细 ▾
                              </span>
                            </summary>
                            <div style={{ display: "flex", flexDirection: "column", gap: 5, marginTop: 8 }}>
                              {g.stocks.map((s, si) => (
                                <div
                                  key={s.code || s.name || si}
                                  style={{
                                    borderTop: si === 0 ? "none" : "1.5px dashed #d1d5db",
                                    paddingTop: si === 0 ? 0 : 5,
                                  }}
                                >
                                  <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                                    <span style={{ fontSize: 11.5, fontWeight: 800 }}>{s.name}</span>
                                    {s.code && (
                                      <span className="mono" style={{ fontSize: 10, color: "#6b7280" }}>
                                        {s.code}
                                      </span>
                                    )}
                                  </div>
                                  {s.reasons.length > 0 && (
                                    <ul
                                      style={{
                                        margin: "2px 0 0",
                                        paddingLeft: 18,
                                        fontSize: 10.5,
                                        lineHeight: 1.6,
                                        color: "#7f1d1d",
                                      }}
                                    >
                                      {s.reasons.map((r, j) => (
                                        <li key={j}>{r}</li>
                                      ))}
                                    </ul>
                                  )}
                                </div>
                              ))}
                            </div>
                          </details>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })()
            )}
          </div>
        </>
      )}
    </div>
  );
}

function CandidateCard({ c, i }: { c: Candidate; i: number }) {
  const name = asString(pick(c, ["name", "名称", "stock_name"]) ?? "—");
  const code = asString(pick(c, ["code", "代码", "symbol"]) ?? "");
  const status = pick(c, ["status", "温度状态"]);
  const strength = pick(c, ["strength", "强度"]);
  const sector = pick(c, ["sector"]);
  const sectorStatus = pick(c, ["sector_status"]);
  const dailyChange = c.daily_change_pct ?? null;
  const jieqi = pick(c, ["jieqi"]);
  const rightSideDays = c.right_side_days ?? null;
  const rightSideGain = c.right_side_gain_pct ?? null;
  const marketCap = c.market_cap_yi ?? null;
  const turnover = c.turnover_yi ?? null;
  const isEtf = c.is_etf === true;

  const numOrDash = (v: unknown, decimals = 1) =>
    v != null ? Number(v).toFixed(decimals) : "—";
  // 日涨幅：A股红涨绿跌；带符号
  const fmtPct = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;

  return (
    <div
      key={code || name || i}
      style={{
        border: `2px solid ${INK}`,
        borderRadius: 6,
        padding: "4px 8px",
        background: "#fff",
        boxShadow: "2px 2px 0 #1f2937",
      }}
    >
      {/* 首行：名称 / 代码 / 温度 / 强度 / ETF 标签 */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 4 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 6, flexWrap: "wrap" }}>
          <span style={{ fontSize: 11.5, fontWeight: 800 }}>{name}</span>
          {code && (
            <span className="mono" style={{ fontSize: 9.5, color: "#6b7280" }}>
              {code}
            </span>
          )}
          {isEtf && (
            <span style={{ ...chip, background: "#a5f3fc", fontSize: 8, color: "#0369a1", padding: "1px 5px" }}>
              ETF
            </span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 4, flexShrink: 0 }}>
          {status != null && (
            <span
              style={{
                fontSize: 11,
                fontWeight: 800,
                color: "#ea580c",
                border: `1.5px solid ${INK}`,
                borderRadius: 999,
                padding: "1px 7px",
                background: "#fed7aa",
              }}
            >
              {asString(status)}
            </span>
          )}
          {strength != null && (
            <span
              className="mono"
              style={{ fontSize: 10, fontWeight: 700, color: "#6b7280" }}
            >
              页面{asString(strength)}
            </span>
          )}
        </div>
      </div>
      {/* 第二行：详细字段 chips */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 3, marginTop: 3 }}>
        {sector != null && (
          <span style={{ fontSize: 9.5, color: "#374151", background: "#f3f4f6",
            border: "1px solid #d1d5db", borderRadius: 3, padding: "0px 5px" }}>
            {asString(sector)}
          </span>
        )}
        <span style={{ fontSize: 9.5, color: "#374151", background: "#f3f4f6",
          border: "1px solid #d1d5db", borderRadius: 3, padding: "0px 5px" }}>
          板块 {sectorStatus != null
            ? <span style={{ color: "#ea580c", fontWeight: 700 }}>{asString(sectorStatus)}</span>
            : "—"}
        </span>
        <span style={{ fontSize: 9.5, color: "#374151", background: "#f3f4f6",
          border: "1px solid #d1d5db", borderRadius: 3, padding: "0px 5px" }}>
          日涨幅 {dailyChange != null
            ? <span style={{ color: dailyChange >= 0 ? "#dc2626" : "#16a34a", fontWeight: 700 }}>{fmtPct(dailyChange)}</span>
            : "—"}
        </span>
        {rightSideDays != null && (
          <span style={{ fontSize: 9.5, color: "#374151", background: "#f3f4f6",
            border: "1px solid #d1d5db", borderRadius: 3, padding: "0px 5px" }}>
            第{String(rightSideDays)}天
            {rightSideGain != null && (
              <span style={{ color: "#16a34a", fontWeight: 700 }}> +{numOrDash(rightSideGain)}%</span>
            )}
          </span>
        )}
        {jieqi != null && (
          <span style={{ ...chip, background: "#bae6fd", fontSize: 8, padding: "0px 5px" }}>
            {asString(jieqi)}
          </span>
        )}
        <span style={{ fontSize: 9.5, color: "#374151", background: "#f3f4f6",
          border: "1px solid #d1d5db", borderRadius: 3, padding: "0px 5px" }}>
          市值 {marketCap != null ? `${numOrDash(marketCap, 0)}亿` : "—"}
        </span>
        <span style={{ fontSize: 9.5, color: "#374151", background: "#f3f4f6",
          border: "1px solid #d1d5db", borderRadius: 3, padding: "0px 5px" }}>
          成交额 {turnover != null ? `${numOrDash(turnover)}亿` : "—"}
        </span>
        {Array.isArray(c.tags) && c.tags.length > 0 && (
          <span style={{ fontSize: 9.5, color: "#7c3aed", background: "#ede9fe",
            border: "1px solid #c4b5fd", borderRadius: 3, padding: "0px 5px" }}>
            {c.tags.join(" · ")}
          </span>
        )}
      </div>
    </div>
  );
}

// 单个板块小组：原有的「板块名 · N 只」子标题 + 候选卡列表
function BoardSubGroup({ board, items }: { board: Board; items: Candidate[] }) {
  return (
    <div>
      <div style={{ fontSize: 10.5, fontWeight: 800, marginBottom: 4, marginTop: 2, color: "#374151", letterSpacing: "0.03em" }}>
        {board} · {items.length} 只
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {items.map((c, i) => (
          <CandidateCard key={asString(pick(c, ["code", "代码", "symbol"]) ?? "") || asString(pick(c, ["name", "名称"]) ?? "") || i} c={c} i={i} />
        ))}
      </div>
    </div>
  );
}

function CandidateGroupedList({ candidates }: { candidates: Candidate[] }) {
  // 按板块分组
  const groups = new Map<Board, Candidate[]>();
  for (const board of BOARD_ORDER) groups.set(board, []);
  for (const c of candidates) {
    groups.get(boardOf(c))!.push(c);
  }

  const A_BOARDS: Board[] = ["A股·主板", "A股·创业板", "A股·科创板"];
  const aTotal = A_BOARDS.reduce((n, b) => n + groups.get(b)!.length, 0);
  const etfItems = groups.get("ETF")!;

  const colHeading: React.CSSProperties = {
    fontSize: 11,
    fontWeight: 800,
    color: "#111827",
    letterSpacing: "0.04em",
    marginBottom: 6,
    paddingBottom: 4,
    borderBottom: `2px solid ${INK}`,
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
      {/* 左列：A股 个股 */}
      <div>
        <div style={colHeading}>A股个股 · {aTotal} 只</div>
        {aTotal === 0 ? (
          <div style={{ fontSize: 10.5, color: "#9ca3af" }}>无个股候选</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {A_BOARDS.filter((b) => groups.get(b)!.length > 0).map((b) => (
              <BoardSubGroup key={b} board={b} items={groups.get(b)!} />
            ))}
          </div>
        )}
      </div>
      {/* 右列：ETF */}
      <div>
        <div style={colHeading}>ETF · {etfItems.length} 只</div>
        {etfItems.length === 0 ? (
          <div style={{ fontSize: 10.5, color: "#9ca3af" }}>无 ETF 候选</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {etfItems.map((c, i) => (
              <CandidateCard key={asString(pick(c, ["code", "代码", "symbol"]) ?? "") || asString(pick(c, ["name", "名称"]) ?? "") || i} c={c} i={i} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
