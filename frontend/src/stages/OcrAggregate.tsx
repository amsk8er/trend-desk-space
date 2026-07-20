import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getAggregate, runAggregate, type AggregateCategory } from "../api";

const INK = "#1f2937";

function download(name: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

// ④ 聚合 — node aggregate. 按 category 去重成 by_market 唯一清单；持久化为 Manifest，
// 下游初筛消费。每个 category 一块,显示去重/截断统计 + 可导出 unique JSON。
export default function OcrAggregate({ batchId }: { batchId: string | null }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["aggregate", batchId],
    queryFn: () => getAggregate(batchId as string),
    enabled: !!batchId,
  });
  const run = useMutation({
    mutationFn: () => runAggregate(batchId as string),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["aggregate", batchId] }),
  });

  if (!batchId) {
    return (
      <div className="panel" style={{ textAlign: "center", color: "#6b7280" }}>
        <div style={{ fontSize: 28, marginBottom: 6 }}>🧮</div>
        <div style={{ fontWeight: 700 }}>选择一个批次</div>
        <div style={{ fontSize: 11, marginTop: 4 }}>选定批次后可聚合去重</div>
      </div>
    );
  }

  const data = q.data;
  const totalAfter = data?.categories.reduce((n, c) => n + c.row_count_after_dedup, 0) ?? 0;

  return (
    <div className="panel">
      <div className="section-h" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>④ 聚合 · 按类别去重</span>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {data && data.categories.length > 0 && (
            <>
              <span className="mono" style={{ fontSize: 10 }}>{totalAfter} 唯一行</span>
              <button
                className="cbtn"
                style={{ fontSize: 11 }}
                onClick={() => download(`${batchId}_aggregate.json`, data)}
              >
                导出全部
              </button>
            </>
          )}
          <button
            className="cbtn cbtn-primary"
            style={{ fontSize: 11, ...(run.isPending ? { opacity: 0.6, cursor: "not-allowed" } : {}) }}
            disabled={run.isPending}
            onClick={() => run.mutate()}
          >
            {run.isPending ? "聚合中…" : "触发聚合"}
          </button>
        </div>
      </div>

      {run.isError && (
        <div style={{ fontSize: 11, color: "#dc2626", marginBottom: 8 }}>
          聚合失败：{(run.error as Error).message}
        </div>
      )}

      {q.isLoading && <div style={{ fontSize: 11, color: "#6b7280" }}>聚合中…</div>}
      {q.isError && (
        <div style={{ fontSize: 11, color: "#dc2626" }}>聚合失败：{(q.error as Error).message}</div>
      )}
      {data && data.categories.length === 0 && (
        <div style={{ fontSize: 11, color: "#6b7280" }}>暂无可聚合的识别行（先跑 OCR）。</div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {data?.categories.map((c) => (
          <CategoryBlock key={c.category} batchId={batchId} cat={c} />
        ))}
      </div>
    </div>
  );
}

function CategoryBlock({ batchId, cat }: { batchId: string; cat: AggregateCategory }) {
  const [open, setOpen] = useState(true);
  const deduped = cat.row_count_before_dedup - cat.row_count_after_dedup;

  return (
    <div className="chunky" style={{ overflow: "hidden", padding: 0 }}>
      <div
        style={{
          display: "flex", alignItems: "center", gap: 8, padding: "8px 10px",
          background: "#f3f4f6", borderBottom: `2px solid ${INK}`, cursor: "pointer",
        }}
        onClick={() => setOpen((o) => !o)}
      >
        <span style={{ fontSize: 12, fontWeight: 800 }}>{open ? "▾" : "▸"} {cat.category}</span>
        <span className="chip" style={{ marginLeft: 0 }}>{cat.row_count_after_dedup} 唯一</span>
        <span className="mono" style={{ fontSize: 10, color: "#6b7280" }}>
          去重 {cat.row_count_after_dedup}/{cat.row_count_before_dedup}
          {deduped > 0 ? `（去掉 ${deduped} 重复）` : ""}
          {cat.dropped_truncated > 0 ? `（去掉 ${cat.dropped_truncated} 截断）` : ""}
        </span>
        <button
          className="cbtn"
          style={{ marginLeft: "auto", fontSize: 10, padding: "2px 8px" }}
          onClick={(e) => {
            e.stopPropagation();
            const payload = {
              batch_id: batchId, category: cat.category,
              row_count_before_dedup: cat.row_count_before_dedup,
              row_count_after_dedup: cat.row_count_after_dedup, rows: cat.rows,
            };
            download(`${batchId}_${cat.category}_unique.json`, payload);
          }}
        >
          导出 JSON
        </button>
      </div>

      {open && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
            <thead>
              <tr style={{ background: "#fff", textAlign: "left" }}>
                <Th>代码</Th><Th>名称</Th><Th>板块</Th><Th>温度</Th>
                <Th>强度·A股内</Th><Th>强度·页内</Th><Th>右侧天数</Th>
                <Th>右侧涨幅%</Th><Th>节气</Th>
                <Th>价格</Th><Th>市值(亿)</Th><Th>成交额(亿)</Th><Th>tags</Th>
              </tr>
            </thead>
            <tbody>
              {cat.rows.map((r) => (
                <tr key={r.row_id} style={{ borderBottom: "1px solid #e5e7eb" }}>
                  <Td mono>{r.code ?? "—"}</Td>
                  <Td>{r.name ?? "—"}</Td>
                  <Td>{r.sector ?? "—"}</Td>
                  <Td>{r.temperature_status ?? "—"}</Td>
                  <Td mono>{r.strength_a_share ?? "—"}</Td>
                  <Td mono>{r.strength_intraday ?? "—"}</Td>
                  <Td mono>{r.right_side_days ?? "—"}</Td>
                  <Td mono>{r.right_side_gain_pct ?? "—"}</Td>
                  <Td>{r.jieqi ?? "—"}</Td>
                  <Td mono>{r.price ?? "—"}</Td>
                  <Td mono>{r.market_cap_yi ?? "—"}</Td>
                  <Td mono>{r.turnover_yi ?? "—"}</Td>
                  <Td>{r.tags && r.tags.length ? r.tags.join(" · ") : "—"}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th style={{ padding: "5px 8px", fontSize: 9, fontWeight: 800, textTransform: "uppercase",
      letterSpacing: "0.05em", color: "#6b7280", borderBottom: `2px solid ${INK}`, whiteSpace: "nowrap" }}>
      {children}
    </th>
  );
}

function Td({ children, mono }: { children: React.ReactNode; mono?: boolean }) {
  return (
    <td className={mono ? "mono" : undefined} style={{ padding: "5px 8px", whiteSpace: "nowrap" }}>
      {children}
    </td>
  );
}
