import { useState, type ReactNode } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getBFilter, runBFilter, type BFilterData } from "../api";
import { type Board, BOARD_ORDER, boardOf } from "./boards";
import { CodeLink } from "../components/CodeLink";

// ── item shapes produced by backend/pipeline/nodes/b_filter.py ──
// white_list: { code, name, status, strength, sizing }
// watch_list: { code, name, status, strength, watch_reason, sizing? }
// rejected:   { code, name, status, strength, rejected_by: string[], reasons: {check_id, reason}[] }

interface Sizing {
  verdict?: string;
  position_amount?: number | null;
  position_ratio?: number | null;
  stop_price?: number | null;
  stop_label?: string | null;
  distance_pct?: number | null;
  max_loss?: number | null;
}
interface WhiteItem {
  code?: string;
  name?: string;
  status?: string | null;
  strength?: number | null;
  sector?: string | null;
  sector_status?: string | null;
  daily_change_pct?: number | null;
  market_cap_yi?: number | null;
  turnover_yi?: number | null;
  right_side_gain_pct?: number | null;
  jieqi?: string | null;
  is_etf?: boolean | null;
  tags?: string[] | null;
  sizing?: Sizing;
  [k: string]: unknown;
}
interface WatchItem extends WhiteItem {
  watch_reason?: string;
}
interface RejectReason {
  check_id?: string;
  reason?: string;
}
interface RejectedItem extends WhiteItem {
  rejected_by?: string[];
  reasons?: RejectReason[];
}

function asWhite(x: unknown): WhiteItem {
  return (x ?? {}) as WhiteItem;
}
function asWatch(x: unknown): WatchItem {
  return (x ?? {}) as WatchItem;
}
function asRejected(x: unknown): RejectedItem {
  return (x ?? {}) as RejectedItem;
}

export default function BFilterStage({ batchId }: { batchId: string | null }) {
  const qc = useQueryClient();

  // Q4 全局参数：单笔风险% + 固定止损距离%（风险预填 0.5%=后端默认；固定止损留空 → 用 MA21 等结构止损参考）。
  const [riskPctStr, setRiskPctStr] = useState("0.5");
  const [stopPctStr, setStopPctStr] = useState("");
  const pctToFrac = (s: string): number | null => {
    const v = parseFloat(s);
    return Number.isFinite(v) && v > 0 ? v / 100 : null;
  };

  const query = useQuery<BFilterData>({
    queryKey: ["b_filter", batchId],
    queryFn: () => getBFilter(batchId as string),
    enabled: !!batchId,
  });

  const mutation = useMutation({
    mutationFn: () =>
      runBFilter(batchId as string, {
        riskPct: pctToFrac(riskPctStr),
        fixedStopPct: pctToFrac(stopPctStr),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["b_filter", batchId] });
      qc.invalidateQueries({ queryKey: ["state", batchId] });
    },
  });

  if (!batchId) {
    return (
      <div className="panel" style={{ textAlign: "center", color: "#6b7280" }}>
        <div style={{ fontSize: 28, marginBottom: 6 }}>⑥</div>
        <div style={{ fontWeight: 700 }}>选择一个批次</div>
        <div style={{ fontSize: 11, marginTop: 4 }}>先在顶部选定批次，再查看 B 阶段过滤结果。</div>
      </div>
    );
  }

  const data = query.data;
  const white = (data?.white_list ?? []).map(asWhite);
  const watch = (data?.watch_list ?? []).map(asWatch);
  const rejected = (data?.rejected ?? []).map(asRejected);

  // 白名单/观察池按页面强度降序。
  const byStrength = (a: WhiteItem, b: WhiteItem) =>
    (b.strength ?? -1) - (a.strength ?? -1);
  // 白名单按板块分组（A股·主板/创业板/科创板 + ETF，口径同初筛），组内按强度降序。
  const whiteByBoard = (board: Board) =>
    white.filter((w) => boardOf(w) === board).sort(byStrength);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* toolbar */}
      <div className="toolbar">
        <div className="toolbar-l">
          <span style={{ fontSize: 13, fontWeight: 800 }}>⑥ B 阶段过滤</span>
          <span className="pill pill-date" title="批次">{batchId}</span>
          {query.isFetching && !query.isLoading && (
            <span className="pill" style={{ background: "var(--info)" }}>刷新中…</span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <label style={{ fontSize: 10.5, fontWeight: 700, color: "#374151", display: "flex", alignItems: "center", gap: 3 }}>
            单笔风险
            <input
              type="number" step="0.1" min="0" placeholder="0.5"
              value={riskPctStr}
              onChange={(e) => setRiskPctStr(e.target.value)}
              style={{ width: 48, padding: "3px 5px", border: "2px solid #1f2937", borderRadius: 6, fontSize: 11 }}
            />%
          </label>
          <label style={{ fontSize: 10.5, fontWeight: 700, color: "#374151", display: "flex", alignItems: "center", gap: 3 }}
            title="不接行情源：止损放现价下方此百分比，整批反推仓位（留空则用 MA21 等结构止损参考）">
            固定止损距离
            <input
              type="number" step="0.5" min="0" placeholder="—"
              value={stopPctStr}
              onChange={(e) => setStopPctStr(e.target.value)}
              style={{ width: 48, padding: "3px 5px", border: "2px solid #1f2937", borderRadius: 6, fontSize: 11 }}
            />%
          </label>
          <button
            className="cbtn cbtn-primary"
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
          >
            {mutation.isPending ? "运行中…" : "▶ 触发 B 过滤"}
          </button>
        </div>
      </div>

      {mutation.isError && (
        <div className="panel" style={{ background: "#fee2e2", borderColor: "#1f2937" }}>
          <div className="section-h" style={{ color: "#991b1b" }}>触发失败</div>
          <div style={{ fontSize: 11 }}>{(mutation.error as Error).message}</div>
        </div>
      )}

      {query.isLoading && (
        <div className="panel" style={{ textAlign: "center", color: "#6b7280" }}>加载中…</div>
      )}

      {query.isError && (
        <div className="panel" style={{ background: "#fee2e2" }}>
          <div className="section-h" style={{ color: "#991b1b" }}>读取失败</div>
          <div style={{ fontSize: 11 }}>{(query.error as Error).message}</div>
        </div>
      )}

      {!query.isLoading && !query.isError && (
        <>
          {/* summary stats */}
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <StatCard label="白名单" value={white.length} bg="var(--ok)" />
            <StatCard label="观察池" value={watch.length} bg="#fef08a" />
            <StatCard label="被拒" value={rejected.length} bg="var(--err)" />
            <StatCard label="合计" value={white.length + watch.length + rejected.length} bg="#fff" />
          </div>

          {/* white list — 表格，A股 / ETF 分栏，各按页面强度降序 */}
          <div className="panel" style={{ borderColor: "#1f2937", background: "#f0fdf4" }}>
            <div className="section-h" style={{ color: "#166534" }}>
              ✓ 白名单 · WHITE LIST ({white.length})
            </div>
            {white.length === 0 ? (
              <div style={{ fontSize: 11, color: "#6b7280" }}>暂无通过项。</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                {BOARD_ORDER.map((b) => {
                  const items = whiteByBoard(b);
                  return items.length ? <WhiteTable key={b} title={b} items={items} /> : null;
                })}
              </div>
            )}
          </div>

          {/* watch pool */}
          <div className="panel" style={{ borderColor: "#1f2937", background: "#fefce8" }}>
            <div className="section-h" style={{ color: "#854d0e" }}>
              ⏸ 观察池 · WATCH ({watch.length})
            </div>
            {watch.length === 0 ? (
              <div style={{ fontSize: 11, color: "#6b7280" }}>无观察标的。</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                {BOARD_ORDER.map((board) => {
                  const items = watch.filter((w) => boardOf(w) === board).sort(byStrength);
                  if (!items.length) return null;
                  return (
                    <div key={board}>
                      <div style={{ fontSize: 11, fontWeight: 800, color: "#854d0e", marginBottom: 6, paddingBottom: 4, borderBottom: "2px solid #1f2937" }}>
                        {board} · {items.length} 只
                      </div>
                      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                        {items.map((w, i) => (
                          <WatchCard key={`${w.code ?? i}`} w={w} />
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* rejected detail */}
          <div className="panel" style={{ borderColor: "#1f2937", background: "#fef2f2" }}>
            <div className="section-h" style={{ color: "#991b1b" }}>
              ✗ 拒绝详情 · REJECTED ({rejected.length})
            </div>
            {rejected.length === 0 ? (
              <div style={{ fontSize: 11, color: "#6b7280" }}>无被拒标的。</div>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
                  <thead>
                    <tr style={{ textAlign: "left" }}>
                      <Th>标的</Th>
                      <Th>触发的 check</Th>
                      <Th>拒绝原因</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {rejected.map((r, i) => {
                      const reasons =
                        r.reasons && r.reasons.length
                          ? r.reasons
                          : (r.rejected_by ?? []).map((c) => ({ check_id: c, reason: "" }));
                      return (
                        <tr key={`${r.code ?? i}`} style={{ borderTop: "2px solid #1f2937" }}>
                          <Td>
                            <div className="mono" style={{ fontWeight: 800 }}>{r.code ?? "—"}</div>
                            <div style={{ fontWeight: 600 }}>{r.name ?? ""}</div>
                            {(r.status != null || r.strength != null) && (
                              <div className="num" style={{ fontSize: 9.5, color: "#6b7280" }}>
                                {r.status != null ? `温度 ${r.status}` : ""}
                                {r.status != null && r.strength != null ? " · " : ""}
                                {r.strength != null ? `强度 ${r.strength}` : ""}
                              </div>
                            )}
                          </Td>
                          <Td>
                            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                              {reasons.length === 0 ? (
                                <span style={{ color: "#6b7280" }}>—</span>
                              ) : (
                                reasons.map((rs, j) => (
                                  <span
                                    key={j}
                                    className="mono"
                                    style={{
                                      fontSize: 9.5,
                                      fontWeight: 700,
                                      padding: "2px 7px",
                                      border: "2px solid #1f2937",
                                      borderRadius: 999,
                                      background: "var(--err)",
                                      boxShadow: "1.5px 1.5px 0 #1f2937",
                                    }}
                                  >
                                    {rs.check_id ?? "?"}
                                  </span>
                                ))
                              )}
                            </div>
                          </Td>
                          <Td>
                            {reasons.filter((rs) => rs.reason).length === 0 ? (
                              <span style={{ color: "#6b7280" }}>—</span>
                            ) : (
                              <ul style={{ margin: 0, paddingLeft: 16 }}>
                                {reasons
                                  .filter((rs) => rs.reason)
                                  .map((rs, j) => (
                                    <li key={j} style={{ lineHeight: 1.5 }}>
                                      {rs.reason}
                                    </li>
                                  ))}
                              </ul>
                            )}
                          </Td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function StatCard({ label, value, bg }: { label: string; value: number; bg: string }) {
  return (
    <div
      className="chunky"
      style={{ background: bg, padding: "8px 16px", minWidth: 80, textAlign: "center" }}
    >
      <div className="num" style={{ fontSize: 20, fontWeight: 800 }}>{value}</div>
      <div style={{ fontSize: 9.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em" }}>
        {label}
      </div>
    </div>
  );
}

// 观察池卡片：代码/名称/温度 + 观察原因。
function WatchCard({ w }: { w: WatchItem }) {
  return (
    <div className="chunky" style={{ background: "#fef08a", padding: "8px 12px" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
        <CodeLink code={w.code} tags={w.tags} className="mono" style={{ fontWeight: 800, fontSize: 13 }} />
        <span style={{ fontWeight: 700, fontSize: 11 }}>{w.name ?? ""}</span>
        {w.status != null && (
          <span className="num" style={{ fontSize: 10, color: "#854d0e" }}>温度 {w.status}</span>
        )}
      </div>
      {w.watch_reason && (
        <div style={{ fontSize: 10.5, color: "#854d0e", marginTop: 3, lineHeight: 1.5 }}>
          {w.watch_reason}
        </div>
      )}
      {Array.isArray(w.tags) && w.tags.length > 0 && (
        <div style={{ fontSize: 10, color: "#7c3aed", marginTop: 2 }}>{w.tags.join(" · ")}</div>
      )}
    </div>
  );
}

// 白名单分栏表格：按板块（A股·主板/创业板/科创板 + ETF）各一张，末尾仓位/止损/最大亏（白名单专属）。
function WhiteTable({ title, items }: { title: string; items: WhiteItem[] }) {
  const fmtPct = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
  const num = (v: number | null | undefined, d = 1) =>
    v != null ? Number(v).toFixed(d) : "—";
  return (
    <div>
      <div
        style={{
          fontSize: 11,
          fontWeight: 800,
          color: "#166534",
          marginBottom: 6,
          paddingBottom: 4,
          borderBottom: "2px solid #1f2937",
        }}
      >
        {title} · {items.length} 只
      </div>
      {items.length === 0 ? (
        <div style={{ fontSize: 10.5, color: "#9ca3af" }}>无{title}通过项</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
            <thead>
              <tr style={{ background: "#dcfce7", textAlign: "left" }}>
                <Th>代码</Th>
                <Th>名称</Th>
                <Th>板块</Th>
                <Th>温度</Th>
                <Th>页面强度</Th>
                <Th>日涨幅</Th>
                <Th>市值</Th>
                <Th>成交额</Th>
                <Th>右侧涨幅</Th>
                <Th>节气</Th>
                <Th>标签</Th>
                <Th>仓位</Th>
                <Th>止损</Th>
                <Th>最大亏</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((w, i) => {
                const sz = w.sizing ?? {};
                const dc = w.daily_change_pct;
                return (
                  <tr key={`${w.code ?? i}`} style={{ borderTop: "1.5px solid #1f2937" }}>
                    <Td>
                      <CodeLink code={w.code} tags={w.tags} className="mono" style={{ fontWeight: 800, fontSize: 10 }} />
                    </Td>
                    <Td>
                      <span style={{ fontWeight: 700, whiteSpace: "nowrap" }}>{w.name ?? "—"}</span>
                    </Td>
                    <Td>{w.sector ?? "—"}</Td>
                    <Td>
                      <span style={{ color: "#ea580c", fontWeight: 700 }}>{w.status ?? "—"}</span>
                    </Td>
                    <Td>
                      <span className="mono">{w.strength ?? "—"}</span>
                    </Td>
                    <Td>
                      {dc != null ? (
                        <span style={{ color: dc >= 0 ? "#dc2626" : "#16a34a", fontWeight: 700 }}>
                          {fmtPct(dc)}
                        </span>
                      ) : (
                        "—"
                      )}
                    </Td>
                    <Td>
                      <span className="mono">
                        {w.market_cap_yi != null ? `${num(w.market_cap_yi, 0)}亿` : "—"}
                      </span>
                    </Td>
                    <Td>
                      <span className="mono">
                        {w.turnover_yi != null ? `${num(w.turnover_yi)}亿` : "—"}
                      </span>
                    </Td>
                    <Td>
                      {w.right_side_gain_pct != null ? (
                        <span style={{ color: "#16a34a", fontWeight: 700 }}>
                          {num(w.right_side_gain_pct)}%
                        </span>
                      ) : (
                        "—"
                      )}
                    </Td>
                    <Td>{w.jieqi ?? "—"}</Td>
                    <Td>
                      <span style={{ color: "#7c3aed", whiteSpace: "nowrap" }}>
                        {Array.isArray(w.tags) && w.tags.length ? w.tags.join(" · ") : "—"}
                      </span>
                    </Td>
                    <Td>
                      <span className="mono" style={{ whiteSpace: "nowrap" }}>
                        {sz.position_amount != null
                          ? `${((sz.position_ratio ?? 0) * 100).toFixed(0)}% · ${(sz.position_amount / 10000).toFixed(1)}万`
                          : "—"}
                      </span>
                    </Td>
                    <Td>
                      <span className="mono" title={sz.stop_label ?? ""}>
                        {sz.stop_price ?? "—"}
                      </span>
                    </Td>
                    <Td>
                      <span className="mono">
                        {sz.max_loss != null ? Math.round(sz.max_loss).toLocaleString() : "—"}
                      </span>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Th({ children }: { children: ReactNode }) {
  return (
    <th
      style={{
        fontSize: 9.5,
        fontWeight: 800,
        textTransform: "uppercase",
        letterSpacing: "0.05em",
        color: "#6b7280",
        padding: "4px 8px",
      }}
    >
      {children}
    </th>
  );
}

function Td({ children }: { children: ReactNode }) {
  return <td style={{ padding: "8px", verticalAlign: "top" }}>{children}</td>;
}
