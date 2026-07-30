import { useState, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  postImport, getOcr, getConfigPaths, getTrendAnimalsConfig,
  estimateTrendSelection, runTrendSelection,
} from "../api";

function today(): string {
  // local YYYY-MM-DD
  const d = new Date();
  const off = d.getTimezoneOffset();
  return new Date(d.getTime() - off * 60_000).toISOString().slice(0, 10);
}

export default function ImportStage({ batchId }: { batchId: string | null }) {
  const qc = useQueryClient();
  const [source, setSource] = useState("");
  const [date, setDate] = useState(today());
  const [lastBatch, setLastBatch] = useState<string | null>(null);
  const [apiBudget, setApiBudget] = useState("3.00");
  const [stockCap, setStockCap] = useState("300");
  const [stockTurnover, setStockTurnover] = useState("5");
  const [etfAum, setEtfAum] = useState("5");
  const [etfTurnover, setEtfTurnover] = useState("2");

  const pathsQ = useQuery({ queryKey: ["config-paths"], queryFn: getConfigPaths });
  const trendCfg = useQuery({ queryKey: ["trend-animals-config"], queryFn: getTrendAnimalsConfig });
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    if (!touched && pathsQ.data?.import_dir) setSource(pathsQ.data.import_dir);
  }, [pathsQ.data, touched]);
  useEffect(() => {
    if (trendCfg.data?.selection_budget != null) {
      setApiBudget(trendCfg.data.selection_budget.toFixed(2));
    }
  }, [trendCfg.data?.selection_budget]);

  const apiEstimate = useMutation({
    mutationFn: () => estimateTrendSelection(date),
  });
  const apiRun = useMutation({
    mutationFn: () => runTrendSelection({
      date,
      approved_budget: Number(apiBudget),
      min_market_cap_yi: Number(stockCap),
      min_turnover_yi: Number(stockTurnover),
      etf_min_aum_yi: Number(etfAum),
      etf_min_turnover_yi: Number(etfTurnover),
    }),
    onSuccess: (res) => {
      setLastBatch(res.batch_id);
      qc.invalidateQueries();
      window.dispatchEvent(new CustomEvent("trenddesk:select-batch", { detail: res.batch_id }));
      window.dispatchEvent(new CustomEvent("trenddesk:select-node", { detail: "prescreen" }));
    },
  });

  const importMut = useMutation({
    mutationFn: () => postImport(source.trim(), date),
    onSuccess: (res) => {
      setLastBatch(res.batch_id);
      qc.invalidateQueries({ queryKey: ["batches"] });
      qc.invalidateQueries({ queryKey: ["ocr", res.batch_id] });
      // 导入成功：顶部批次选择器切到新批次，并自动跳到下一步 OCR（省一次手点）
      window.dispatchEvent(new CustomEvent("trenddesk:select-batch", { detail: res.batch_id }));
      window.dispatchEvent(new CustomEvent("trenddesk:select-node", { detail: "ocr" }));
    },
  });

  // Whichever batch we want to show shot-count for: the freshly-imported one
  // (preferred) or the currently-selected batch.
  const shownBatch = lastBatch ?? batchId;

  const ocrQ = useQuery({
    queryKey: ["ocr", shownBatch],
    queryFn: () => getOcr(shownBatch as string),
    enabled: !!shownBatch,
  });

  const shotCount =
    ocrQ.data?.stats.total ?? (ocrQ.data ? ocrQ.data.jobs.length : null);

  return (
    <div className="panel" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div>
        <div className="section-h" style={{ marginBottom: 4 }}>① 导入</div>
        <div style={{ fontSize: 12, fontWeight: 800 }}>建立新批次：API 选股或截图导入</div>
        <div className="mono" style={{ fontSize: 10, color: "#6b7280", marginTop: 2 }}>
          API 快车道直接跑到初筛；截图通道继续保留作为降级与人工补充
        </div>
      </div>

      {/* 趋势动物 API 快车道：工业仪表式费用/参数面板，沿用 chunky cockpit 语言。 */}
      <section className="chunky" style={{ background: "#ecfccb", padding: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <div>
            <div className="section-h" style={{ margin: 0 }}>API FAST LANE · 选股 → 初筛</div>
            <div style={{ fontSize: 11, fontWeight: 800, marginTop: 2 }}>
              温转热组合 → 全量成分 → 两阶段快照 → 板块温度 → M1-M4
            </div>
          </div>
          <span className="pill mono" style={{ background: trendCfg.data?.configured ? "#86efac" : "#fecaca" }}>
            {trendCfg.data?.enabled ? (trendCfg.data?.configured ? "API READY" : "KEY MISSING") : "API OFF"}
          </span>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(112px,1fr))", gap: 8, marginTop: 12 }}>
          <MiniParam label="日期" value={date} onChange={setDate} type="date" />
          <MiniParam label="预算上限/元" value={apiBudget} onChange={setApiBudget} />
          <MiniParam label="个股市值门/亿" value={stockCap} onChange={setStockCap} />
          <MiniParam label="个股成交额门/亿" value={stockTurnover} onChange={setStockTurnover} />
          <MiniParam label="ETF规模门/亿" value={etfAum} onChange={setEtfAum} />
          <MiniParam label="ETF成交额门/亿" value={etfTurnover} onChange={setEtfTurnover} />
        </div>

        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 12 }}>
          <button
            className="cbtn"
            disabled={!trendCfg.data?.enabled || !trendCfg.data?.configured || apiEstimate.isPending || !date}
            onClick={() => apiEstimate.mutate()}
            title="估算会调用 searchTicker 和低价 constituentCount 快照，会产生少量费用"
          >
            {apiEstimate.isPending ? "估算中…" : "先估费用"}
          </button>
          <button
            className="cbtn cbtn-primary"
            disabled={!trendCfg.data?.enabled || !trendCfg.data?.configured || apiRun.isPending || !date || !(Number(apiBudget) > 0)}
            onClick={() => apiRun.mutate()}
          >
            {apiRun.isPending ? "流水线运行中…" : "建立 API 批次并跑到初筛"}
          </button>
          {apiEstimate.data && (
            <span className="pill mono" style={{ background: "#fef08a" }}>
              预计 ¥{apiEstimate.data.estimated_cost.toFixed(3)} · {Object.values(apiEstimate.data.counts).reduce((a, b) => a + b, 0)} 成分
            </span>
          )}
          {apiRun.data && (
            <span className="pill mono" style={{ background: "#86efac" }}>
              ✓ {apiRun.data.candidates.length} 入选 / {apiRun.data.rejected.length} 拒绝
            </span>
          )}
        </div>
        {(apiEstimate.isError || apiRun.isError) && (
          <div className="mono" style={{ marginTop: 9, color: "#991b1b", fontSize: 10, fontWeight: 800 }}>
            {(apiRun.error as Error | null)?.message ?? (apiEstimate.error as Error | null)?.message}
          </div>
        )}
        {apiRun.data?.component_count_warnings.map((warning) => (
          <div
            key={warning.combo}
            className="mono"
            style={{ marginTop: 8, padding: "7px 9px", background: "#fef3c7", border: "1.5px solid #92400e", fontSize: 9.5, color: "#78350f" }}
          >
            计数差异 · {warning.combo}：constituentCount={warning.constituent_count}，
            全部子级品种={warning.returned_basic_count}。{warning.note}，本批按成分接口实际返回处理。
          </div>
        ))}
        <div className="mono" style={{ marginTop: 8, fontSize: 9, color: "#4b5563" }}>
          费用达到 1 元时必须由预算字段显式批准；数据日期或全量成分数不一致时付费链会停止。
        </div>
      </section>

      <div className="section-h" style={{ margin: "2px 0 -6px" }}>截图降级通道</div>

      {/* form */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span className="section-h" style={{ margin: 0 }}>截图目录 (source)</span>
          <input
            className="chat-input"
            placeholder="/path/to/screenshots"
            value={source}
            onChange={(e) => { setSource(e.target.value); setTouched(true); }}
            disabled={importMut.isPending}
          />
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span className="section-h" style={{ margin: 0 }}>日期 (date)</span>
          <input
            type="date"
            className="chat-input"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            disabled={importMut.isPending}
            style={{ maxWidth: 200 }}
          />
        </label>

        <div>
          <button
            className="cbtn cbtn-primary"
            onClick={() => importMut.mutate()}
            disabled={importMut.isPending || !source.trim() || !date}
          >
            {importMut.isPending ? "导入中…" : "导入"}
          </button>
        </div>
      </div>

      {/* error */}
      {importMut.isError && (
        <div
          className="chunky"
          style={{
            background: "#fee2e2",
            borderColor: "#1f2937",
            padding: "8px 10px",
            fontSize: 11,
            fontWeight: 700,
          }}
        >
          导入失败：{(importMut.error as Error).message}
        </div>
      )}

      {/* result */}
      {lastBatch && (
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <div
            className="chunky"
            style={{ padding: "8px 12px", background: "#d1fae5", minWidth: 0 }}
          >
            <div className="section-h" style={{ margin: 0 }}>新批次 BATCH_ID</div>
            <div className="mono" style={{ fontSize: 13, fontWeight: 800, wordBreak: "break-all" }}>
              {lastBatch}
            </div>
          </div>
          <div className="chunky" style={{ padding: "8px 12px", textAlign: "center" }}>
            <div className="section-h" style={{ margin: 0 }}>截图数量</div>
            <div className="num" style={{ fontSize: 22, fontWeight: 800, color: "#2563eb" }}>
              {ocrQ.isLoading
                ? "…"
                : ocrQ.isError
                  ? "—"
                  : shotCount ?? "—"}
            </div>
            {ocrQ.isError && (
              <div className="mono" style={{ fontSize: 9, color: "#dc2626" }}>读取失败</div>
            )}
          </div>
        </div>
      )}

      {/* show count for already-selected batch even before a fresh import */}
      {!lastBatch && batchId && !ocrQ.isLoading && shotCount != null && (
        <div className="mono" style={{ fontSize: 10, color: "#6b7280" }}>
          当前批次 <b>{batchId}</b> 已有 {shotCount} 张截图。
        </div>
      )}

      {!lastBatch && !batchId && (
        <div
          className="chunky"
          style={{ padding: "10px 12px", fontSize: 11, color: "#6b7280", background: "#faf7e8" }}
        >
          选择一个批次，或在上方填写目录后点击「导入」新建批次。
        </div>
      )}
    </div>
  );
}

function MiniParam({ label, value, onChange, type = "number" }: {
  label: string; value: string; onChange: (v: string) => void; type?: string;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <span className="mono" style={{ fontSize: 9, fontWeight: 900, color: "#374151" }}>{label}</span>
      <input
        className="chat-input mono"
        type={type}
        value={value}
        min={type === "number" ? "0" : undefined}
        step={type === "number" ? "0.1" : undefined}
        onChange={(e) => onChange(e.target.value)}
        style={{ padding: "6px 7px", fontSize: 11 }}
      />
    </label>
  );
}
