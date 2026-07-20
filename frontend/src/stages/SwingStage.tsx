// frontend/src/stages/SwingStage.tsx
// 重要低点标注页：输代码+区间 → /api/swing → lightweight-charts 交互式 K 线 +
// 重要/次要低点标注 + 追踪止损阶梯 + 右侧信息面板（crosshair 联动）+ 历史记录。
// 批次无关（独立工具，不依赖 batchId）。
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  createChart, ColorType, LineStyle,
  type IChartApi, type SeriesMarker, type Time,
} from "lightweight-charts";
import { getSwing, type SwingData, type SwingBar, type SwingParams } from "../api";

// A 股惯例：红涨绿跌
const UP = "#e23b3b";
const DOWN = "#1f9d55";
const IMPORTANT = "#1f9d55"; // 重要低点 绿 ▲
const MINOR = "#9aa0a6";     // 次要低点 灰点
const STOP = "#e23b3b";      // 追踪止损 红虚线

const HISTORY_KEY = "swing:history";
const HISTORY_MAX = 8;

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

function pushHistory(code: string): string[] {
  const next = [code, ...loadHistory().filter((c) => c !== code)].slice(0, HISTORY_MAX);
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
  } catch {
    /* localStorage 不可用时静默降级 */
  }
  return next;
}

function fmtVolume(v: number): string {
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)} 亿`;
  if (v >= 1e4) return `${(v / 1e4).toFixed(2)} 万`;
  return `${v}`;
}

// 右侧信息面板：名称 + 开/高/低/收 + 涨跌幅% + 成交量（随 crosshair 联动）
function InfoPanel({ data, hover }: { data: SwingData; hover: { bar: SwingBar; chgPct: number | null } | null }) {
  const cur = hover;
  const cell = (label: string, value: string, color?: string) => (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "3px 0", fontSize: 12 }}>
      <span style={{ color: "#888" }}>{label}</span>
      <span className="mono" style={{ color: color ?? "#222", fontWeight: 500 }}>{value}</span>
    </div>
  );
  return (
    <div
      style={{
        width: 220, flexShrink: 0, border: "1px solid #eee", borderRadius: 10,
        padding: "12px 14px", background: "#fafafa", alignSelf: "flex-start",
      }}
    >
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 2 }}>
        {data.name || "—"}
      </div>
      <div className="mono" style={{ fontSize: 11, color: "#999", marginBottom: 10 }}>{data.code}</div>
      {cur ? (
        <>
          <div className="mono" style={{ fontSize: 11, color: "#666", marginBottom: 6 }}>{cur.bar.time}</div>
          {cell("开", cur.bar.open.toFixed(2))}
          {cell("高", cur.bar.high.toFixed(2))}
          {cell("低", cur.bar.low.toFixed(2))}
          {cell("收", cur.bar.close.toFixed(2))}
          {cell(
            "涨跌幅",
            cur.chgPct === null ? "—" : `${cur.chgPct >= 0 ? "+" : ""}${cur.chgPct.toFixed(2)}%`,
            cur.chgPct === null ? "#222" : cur.chgPct >= 0 ? UP : DOWN,
          )}
          {cell("成交量", fmtVolume(cur.bar.volume))}
        </>
      ) : (
        <div style={{ color: "#bbb", fontSize: 12, padding: "8px 0" }}>移到 K 线上看每日数据</div>
      )}
    </div>
  );
}

function SwingChart({
  data,
  onHover,
}: {
  data: SwingData;
  onHover: (time: string | null) => void;
}) {
  const elRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const onHoverRef = useRef(onHover);
  onHoverRef.current = onHover;

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;

    const chart = createChart(el, {
      width: el.clientWidth,
      height: 460,
      layout: { background: { type: ColorType.Solid, color: "#fff" }, textColor: "#333" },
      grid: { vertLines: { color: "#f0f0f0" }, horzLines: { color: "#f0f0f0" } },
      timeScale: { borderColor: "#ddd", timeVisible: false },
      rightPriceScale: { borderColor: "#ddd" },
      crosshair: { mode: 0 }, // 自由十字光标，悬停看每根 K
    });
    chartRef.current = chart;

    const candle = chart.addCandlestickSeries({
      upColor: UP, downColor: DOWN,
      borderUpColor: UP, borderDownColor: DOWN,
      wickUpColor: UP, wickDownColor: DOWN,
    });
    candle.setData(
      data.ohlc.map((b) => ({
        time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close,
      })),
    );

    // 重要低点 绿▲(belowBar) + 次要低点 灰●(belowBar)；markers 必须按时间升序
    const markers: SeriesMarker<Time>[] = [
      ...data.important_lows.map((p) => ({
        time: p.time as Time, position: "belowBar" as const,
        color: IMPORTANT, shape: "arrowUp" as const, text: "重要",
      })),
      ...data.minor_lows.map((p) => ({
        time: p.time as Time, position: "belowBar" as const,
        color: MINOR, shape: "circle" as const,
      })),
    ].sort((a, b) => String(a.time).localeCompare(String(b.time)));
    candle.setMarkers(markers);

    // 追踪止损阶梯：红虚线，按 stop_ladder 转折点连成阶梯
    if (data.stop_ladder.length > 0) {
      const stopLine = chart.addLineSeries({
        color: STOP, lineStyle: LineStyle.Dashed, lineWidth: 1,
        lastValueVisible: false, priceLineVisible: false,
      });
      stopLine.setData(data.stop_ladder.map((s) => ({ time: s.time as Time, value: s.stop })));
    }

    // crosshair 联动：悬停哪根就把它的日期回传父组件
    chart.subscribeCrosshairMove((param) => {
      const t = param.time ? String(param.time) : null;
      onHoverRef.current(t);
    });

    chart.timeScale().fitContent();

    const onResize = () => chart.applyOptions({ width: el.clientWidth });
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.remove();
      chartRef.current = null;
    };
  }, [data]);

  return <div ref={elRef} style={{ width: "100%", minWidth: 0 }} />;
}

export default function SwingStage({ initialCode = null }: { initialCode?: { code: string; n: number; tags?: string[] } | null } = {}) {
  const [code, setCode] = useState("");
  const [start, setStart] = useState(isoDaysAgo(365));
  const [end, setEnd] = useState(isoDaysAgo(0));
  const [k, setK] = useState(2);
  const [breakoutPct, setBreakoutPct] = useState(0);
  const [showAdvanced, setShowAdvanced] = useState(false);
  // 已提交查询的参数（点查询才置位 → useQuery 才 enabled）
  const [submitted, setSubmitted] = useState<SwingParams | null>(null);
  const [hoverTime, setHoverTime] = useState<string | null>(null);
  const [history, setHistory] = useState<string[]>(loadHistory);
  // 从列表跳入时携带的 OCR 标签；手动查询/点历史时清空（无 OCR 上下文）。
  const [swingTags, setSwingTags] = useState<string[]>([]);

  const query = useQuery({
    queryKey: ["swing", submitted],
    queryFn: () => getSwing(submitted as SwingParams),
    enabled: submitted !== null,
  });

  const canSubmit = code.trim().length > 0;
  const runQuery = (raw: string) => {
    const c = raw.trim().toUpperCase();
    if (!c) return;
    setCode(c);
    setHoverTime(null);
    setSubmitted({ code: c, start, end, k, breakout_pct: breakoutPct });
  };
  const onSubmit = () => { setSwingTags([]); runQuery(code); };

  // 从列表页跳入：填入代码并自动查询（沿用当前 start/end，默认近一年），并带入该票 OCR 标签。
  // 依赖 nonce(n)，同代码重复点也能重新触发；与用户手动输入互不干扰。
  useEffect(() => {
    if (initialCode?.code) { setSwingTags(initialCode.tags ?? []); runQuery(initialCode.code); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialCode?.n]);

  const data = query.data;

  // 查询成功 → 记入历史（用后端回显的规范化 code，带后缀）
  useEffect(() => {
    if (data?.code) setHistory(pushHistory(data.code));
  }, [data?.code, data?.start, data?.end]);

  const summary = useMemo(() => {
    if (!data) return null;
    return `${data.ohlc.length} 根 · 重要 ${data.important_lows.length} · 次要 ${data.minor_lows.length}`;
  }, [data]);

  // 查询周期内涨幅（首末收盘）；空/单根防御。
  const rangeChg = useMemo(() => {
    if (!data || data.ohlc.length < 2) return null;
    const first = data.ohlc[0].close, last = data.ohlc[data.ohlc.length - 1].close;
    return first ? ((last - first) / first) * 100 : null;
  }, [data]);

  // 悬停 bar + 涨跌幅（用前一根 close 算；默认落在最后一根）
  const hover = useMemo(() => {
    if (!data || data.ohlc.length === 0) return null;
    let idx = data.ohlc.length - 1;
    if (hoverTime) {
      const found = data.ohlc.findIndex((b) => b.time === hoverTime);
      if (found >= 0) idx = found;
    }
    const bar = data.ohlc[idx];
    const prev = idx > 0 ? data.ohlc[idx - 1] : null;
    const chgPct = prev && prev.close !== 0 ? ((bar.close - prev.close) / prev.close) * 100 : null;
    return { bar, chgPct };
  }, [data, hoverTime]);

  return (
    <div className="panel">
      <div className="section-h" style={{ marginBottom: 10 }}>重要低点标注</div>

      {/* 表单 */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <input
          className="pill mono"
          style={{ width: 150 }}
          placeholder="代码 如 600519 / 159325"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onSubmit()}
        />
        <input className="pill mono" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        <span style={{ color: "#999" }}>→</span>
        <input className="pill mono" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        <button className="cbtn" onClick={() => setShowAdvanced((v) => !v)}>
          高级 {showAdvanced ? "▲" : "▼"}
        </button>
        <button className="cbtn cbtn-primary" disabled={!canSubmit || query.isFetching} onClick={onSubmit}>
          {query.isFetching ? "查询中…" : "查询"}
        </button>
        {summary && <span className="pill mono" style={{ fontSize: 10 }}>{summary}</span>}
        {rangeChg !== null && (
          <span className="pill mono" style={{ fontSize: 10, fontWeight: 700, color: rangeChg >= 0 ? UP : DOWN }}>
            区间 {rangeChg >= 0 ? "+" : ""}{rangeChg.toFixed(1)}%
          </span>
        )}
        {swingTags.length > 0 && (
          <span className="pill mono" style={{ fontSize: 10, color: "#7c3aed", background: "#ede9fe" }}>
            {swingTags.join(" · ")}
          </span>
        )}
      </div>

      {/* 历史记录：可点击快速重查 */}
      {history.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", marginTop: 8 }}>
          <span style={{ fontSize: 11, color: "#999" }}>最近：</span>
          {history.map((c) => (
            <button
              key={c}
              className="pill mono"
              style={{ fontSize: 11, cursor: "pointer", padding: "2px 8px" }}
              onClick={() => { setSwingTags([]); runQuery(c); }}
              title="点击重查"
            >
              {c}
            </button>
          ))}
        </div>
      )}

      {showAdvanced && (
        <div style={{ display: "flex", gap: 16, alignItems: "center", marginTop: 8, fontSize: 12 }}>
          <label>
            k（fractal 半径）：
            <input
              type="number" min={1} value={k} style={{ width: 50, marginLeft: 4 }}
              onChange={(e) => setK(Math.max(1, Number(e.target.value) || 1))}
            />
          </label>
          <label>
            breakout_pct（强势阈值）：
            <input
              type="number" min={0} step={0.001} value={breakoutPct} style={{ width: 70, marginLeft: 4 }}
              onChange={(e) => setBreakoutPct(Math.max(0, Number(e.target.value) || 0))}
            />
          </label>
        </div>
      )}

      {/* 三态 + 图表 */}
      <div style={{ marginTop: 14 }}>
        {submitted === null && (
          <div style={{ color: "#999", padding: "30px 0", textAlign: "center" }}>
            输入代码与区间后点「查询」，标注重要低点。
          </div>
        )}
        {query.isError && (
          <div className="pill mono" style={{ background: "#fee2e2", display: "inline-block" }}>
            查询失败：{(query.error as Error).message}
          </div>
        )}
        {data && data.ohlc.length === 0 && (
          <div style={{ color: "#999", padding: "30px 0", textAlign: "center" }}>
            该区间无数据（非交易区间或代码无行情）。
          </div>
        )}
        {data && data.ohlc.length > 0 && (
          <>
            <div style={{ display: "flex", gap: 14, alignItems: "stretch" }}>
              <SwingChart data={data} onHover={setHoverTime} />
              <InfoPanel data={data} hover={hover} />
            </div>
            <div style={{ display: "flex", gap: 16, marginTop: 8, fontSize: 11, color: "#666" }}>
              <span>▲ 绿 = 重要低点</span>
              <span>● 灰 = 次要低点</span>
              <span style={{ color: STOP }}>— — 追踪止损阶梯</span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
