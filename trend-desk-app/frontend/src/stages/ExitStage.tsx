import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getExitCheck, runExitCheck, type ExitListItem, type ExitOverviewItem } from "../api";
import { CodeLink } from "../components/CodeLink";

// ⑧ 出局检查 — exit_check node.
// Lists exit candidates (per-position trigger / action / reason) and lets the
// user (re)run the exit check via the engine.

const INK = "#1f2937";

function actionStyle(action: string): { bg: string; label: string } {
  const a = (action || "").toLowerCase();
  // 全清 / clear / exit → red; 减半 / trim / half → orange; else idle
  if (a.includes("clear") || a.includes("exit") || action.includes("全清") || action.includes("清仓")) {
    return { bg: "#fca5a5", label: action || "全清" };
  }
  if (a.includes("half") || a.includes("trim") || a.includes("reduce") || action.includes("减半") || action.includes("减")) {
    return { bg: "#fed7aa", label: action || "减半" };
  }
  return { bg: "#e5e7eb", label: action || "—" };
}

function overviewSuggestBg(suggest: string): string {
  if (suggest.includes("清仓")) return "#fca5a5";
  if (suggest.includes("止损")) return "#fca5a5";
  if (suggest.includes("止盈")) return "#fed7aa";
  if (suggest.includes("风险")) return "#fde68a";
  if (suggest.includes("无温度") || suggest.includes("无数据")) return "#e5e7eb";
  return "#d1fae5"; // 继续持有
}

function Cell({ children, mono = false }: { children: React.ReactNode; mono?: boolean }) {
  return (
    <td
      className={mono ? "mono" : undefined}
      style={{
        padding: "8px 10px",
        borderBottom: `2px solid ${INK}`,
        fontSize: 11,
        verticalAlign: "top",
      }}
    >
      {children}
    </td>
  );
}

function Dash() {
  return <span style={{ color: "#9ca3af" }}>—</span>;
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th
      style={{
        padding: "6px 10px",
        textAlign: "left",
        fontSize: 9,
        fontWeight: 800,
        textTransform: "uppercase",
        letterSpacing: "0.05em",
        color: "#6b7280",
        borderBottom: `2px solid ${INK}`,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </th>
  );
}

function Chip({ bg, children }: { bg: string; children: React.ReactNode }) {
  return (
    <span
      style={{
        display: "inline-block",
        fontSize: 9,
        fontWeight: 800,
        padding: "2px 8px",
        border: `1.5px solid ${INK}`,
        borderRadius: 4,
        background: bg,
        textTransform: "uppercase",
        letterSpacing: "0.05em",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

export default function ExitStage({ batchId }: { batchId: string | null }) {
  const qc = useQueryClient();

  const query = useQuery({
    queryKey: ["exit_check", batchId],
    queryFn: () => getExitCheck(batchId as string),
    enabled: !!batchId,
  });

  const mutation = useMutation({
    mutationFn: () => runExitCheck(batchId as string),
    onSuccess: () => {
      // GET 端点现在也返回 {items, overview}；只需 invalidate 即可刷新两者。
      qc.invalidateQueries({ queryKey: ["exit_check", batchId] });
    },
  });

  if (!batchId) {
    return (
      <div className="panel" style={{ textAlign: "center", color: "#6b7280" }}>
        <div style={{ fontSize: 28, marginBottom: 6 }}>⑧</div>
        <div style={{ fontWeight: 700 }}>选择一个批次</div>
        <div style={{ fontSize: 11, marginTop: 4 }}>选中批次后可运行出局检查</div>
      </div>
    );
  }

  const items: ExitListItem[] = query.data?.items ?? [];
  const overview: ExitOverviewItem[] = query.data?.overview ?? [];

  return (
    <div className="panel">
      <div className="toolbar">
        <div className="toolbar-l">
          <span className="section-h" style={{ marginBottom: 0 }}>
            ⑧ 出局检查
          </span>
          <Chip bg="#fca5a5">EXIT</Chip>
        </div>
        <button
          className="cbtn cbtn-primary"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
        >
          {mutation.isPending ? "检查中…" : "触发出局检查"}
        </button>
      </div>

      {mutation.isError && (
        <div
          style={{
            border: `2px solid ${INK}`,
            background: "#fee2e2",
            borderRadius: 8,
            padding: "8px 10px",
            fontSize: 11,
            marginBottom: 12,
          }}
        >
          运行失败：{(mutation.error as Error).message}
        </div>
      )}

      {query.isLoading && (
        <div style={{ fontSize: 11, color: "#6b7280", padding: "10px 0" }}>加载中…</div>
      )}

      {query.isError && (
        <div
          style={{
            border: `2px solid ${INK}`,
            background: "#fee2e2",
            borderRadius: 8,
            padding: "8px 10px",
            fontSize: 11,
          }}
        >
          加载失败：{(query.error as Error).message}
        </div>
      )}

      {/* ── 持仓状态总览：每只持仓都在（含热而正常持有的），区分「判它持有」与「没数据」 ── */}
      {overview.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <span className="section-h" style={{ marginBottom: 6, display: "block" }}>
            持仓状态总览（{overview.length}）
          </span>
          <div className="chunky" style={{ overflow: "hidden", padding: 0 }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <Th>标的</Th>
                  <Th>温度</Th>
                  <Th>右侧天数</Th>
                  <Th>右侧涨幅</Th>
                  <Th>节气</Th>
                  <Th>标签</Th>
                  <Th>盈亏</Th>
                  <Th>建议</Th>
                </tr>
              </thead>
              <tbody>
                {overview.map((o) => (
                  <tr key={o.position_id}>
                    <Cell mono>
                      {o.name}
                      <CodeLink code={o.code} tags={o.tags} className="" fallback="待回填"
                        style={{ color: "#9ca3af", marginLeft: 4 }} />
                    </Cell>
                    <Cell>
                      {o.temperature_status ?? (
                        <span style={{ color: "#b45309", fontWeight: 700 }}>无数据</span>
                      )}
                      {o.temp_source === "holding_temp" && (
                        <span style={{ fontSize: 8.5, color: "#16a34a", marginLeft: 4 }}>持仓页</span>
                      )}
                    </Cell>
                    <Cell mono>
                      {o.right_side_days != null ? `第${o.right_side_days}天` : <Dash />}
                    </Cell>
                    <Cell mono>
                      {o.right_side_gain_pct != null ? (
                        <span style={{ color: o.right_side_gain_pct >= 0 ? "#dc2626" : "#16a34a", fontWeight: 700 }}>
                          {o.right_side_gain_pct >= 0 ? "+" : ""}
                          {o.right_side_gain_pct.toFixed(1)}%
                        </span>
                      ) : <Dash />}
                    </Cell>
                    <Cell>{o.jieqi ?? <Dash />}</Cell>
                    <Cell>
                      {o.tags && o.tags.length ? (
                        <span style={{ color: "#7c3aed" }}>{o.tags.join(" · ")}</span>
                      ) : <Dash />}
                    </Cell>
                    <Cell>
                      <span
                        className="num"
                        style={{
                          color: o.pnl_pct > 0 ? "#dc2626" : o.pnl_pct < 0 ? "#16a34a" : "#6b7280",
                          fontWeight: 800,
                        }}
                      >
                        {o.pnl_pct > 0 ? "+" : ""}
                        {(o.pnl_pct * 100).toFixed(1)}%
                      </span>
                    </Cell>
                    <Cell>
                      <Chip bg={overviewSuggestBg(o.suggest)}>{o.suggest}</Chip>
                    </Cell>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!query.isLoading && !query.isError && items.length === 0 && (
        <div
          style={{
            border: `2px dashed #d4cfb8`,
            borderRadius: 8,
            padding: "20px 10px",
            textAlign: "center",
            color: "#6b7280",
            fontSize: 12,
            fontWeight: 700,
            background: "#d1fae5",
          }}
        >
          {overview.length > 0 ? "无需动作的出局提醒（详见上方总览）" : "无出局信号"}
        </div>
      )}

      {!query.isLoading && !query.isError && items.length > 0 && (
        <div
          className="chunky"
          style={{ overflow: "hidden", padding: 0 }}
        >
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <Th>标的</Th>
                <Th>触发</Th>
                <Th>动作</Th>
                <Th>原因</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => {
                const act = actionStyle(it.action);
                const d = it.detail ?? {};
                return (
                  <tr key={it.exit_id}>
                    <Cell mono>{typeof d.name === "string" && d.name ? d.name : `#${it.position_id}`}</Cell>
                    <Cell mono>{it.trigger || "—"}</Cell>
                    <Cell>
                      <Chip bg={act.bg}>{act.label}</Chip>
                    </Cell>
                    <Cell>
                      {it.reason || "—"}
                      {(d.temperature_status != null || d.sector_status != null || d.pnl_pct != null) && (
                        <div className="num" style={{ fontSize: 9.5, color: "#6b7280", marginTop: 4 }}>
                          {d.temperature_status != null ? `温度 ${d.temperature_status} · ` : ""}
                          {d.sector_status != null ? `板块 ${d.sector_status} · ` : ""}
                          {d.right_side_days != null ? `右侧第${d.right_side_days}天 · ` : ""}
                          {d.avg_cost != null ? `成本 ${d.avg_cost} · ` : ""}
                          {d.pnl_pct != null ? `盈亏 ${(Number(d.pnl_pct) * 100).toFixed(1)}%` : ""}
                        </div>
                      )}
                    </Cell>
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
