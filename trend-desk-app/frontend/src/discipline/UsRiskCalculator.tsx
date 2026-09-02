import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ColorType, LineStyle, createChart, type Time } from "lightweight-charts";

import { getUsCalculatorMarket, type UsCalculatorMarket } from "../api";
import "./us-risk-calculator.css";


const ACCOUNT_KEY = "trend-desk:us-calculator:account";
const ALLOCATION_PRESETS = [5, 10, 15, 25, 50, 100];

export interface RiskInputs {
  account: number;
  allocationPct: number;
  entry: number;
  stop: number;
  riskBudgetPct: number;
}

export interface RiskResult {
  positionBudget: number;
  sharesByCapital: number;
  actualPosition: number;
  perShareRisk: number;
  riskAmount: number;
  riskPctOfAccount: number;
  riskBudgetAmount: number;
  maxRiskShares: number;
  recommendedShares: number;
  impliedStop: number | null;
  validStop: boolean;
  withinBudget: boolean;
}

export function calculateRisk(input: RiskInputs): RiskResult {
  const account = Number.isFinite(input.account) && input.account > 0 ? input.account : 0;
  const allocationPct = Number.isFinite(input.allocationPct) ? Math.max(0, input.allocationPct) : 0;
  const entry = Number.isFinite(input.entry) && input.entry > 0 ? input.entry : 0;
  const stop = Number.isFinite(input.stop) && input.stop > 0 ? input.stop : 0;
  const riskBudgetPct = Number.isFinite(input.riskBudgetPct) ? Math.max(0, input.riskBudgetPct) : 0;
  const positionBudget = account * allocationPct / 100;
  const sharesByCapital = entry ? Math.floor(positionBudget / entry) : 0;
  const actualPosition = sharesByCapital * entry;
  const validStop = entry > 0 && stop > 0 && stop < entry;
  const perShareRisk = validStop ? entry - stop : 0;
  const riskAmount = perShareRisk * sharesByCapital;
  const riskPctOfAccount = account ? riskAmount / account * 100 : 0;
  const riskBudgetAmount = account * riskBudgetPct / 100;
  const maxRiskShares = perShareRisk ? Math.floor(riskBudgetAmount / perShareRisk) : 0;
  const recommendedShares = validStop ? Math.min(sharesByCapital, maxRiskShares) : 0;
  const impliedStop = entry && sharesByCapital
    ? Math.max(0, entry - riskBudgetAmount / sharesByCapital)
    : null;
  return {
    positionBudget, sharesByCapital, actualPosition, perShareRisk,
    riskAmount, riskPctOfAccount, riskBudgetAmount, maxRiskShares,
    recommendedShares, impliedStop, validStop,
    withinBudget: validStop && riskAmount <= riskBudgetAmount + 1e-8,
  };
}

function savedAccount(): string {
  try { return localStorage.getItem(ACCOUNT_KEY) ?? "100000"; } catch { return "100000"; }
}

function number(value: string): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function usd(value: number, digits = 2): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(value);
}

function pct(value: number, digits = 2): string {
  return `${value.toFixed(digits)}%`;
}

function asOf(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function PriceChart({ data, entry, stop }: { data: UsCalculatorMarket; entry: number; stop: number }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const chart = createChart(element, {
      width: element.clientWidth,
      height: 430,
      layout: { background: { type: ColorType.Solid, color: "#fbfaf7" }, textColor: "#27323b" },
      grid: { vertLines: { color: "#e0ded7" }, horzLines: { color: "#e0ded7" } },
      rightPriceScale: { borderColor: "#27323b" },
      timeScale: { borderColor: "#27323b", timeVisible: false },
      crosshair: { vertLine: { color: "#27323b" }, horzLine: { color: "#27323b" } },
    });
    const candles = chart.addCandlestickSeries({
      upColor: "#33734a", downColor: "#b44b45", borderVisible: false,
      wickUpColor: "#33734a", wickDownColor: "#b44b45",
    });
    candles.setData(data.bars.map(bar => ({
      time: bar.time as Time, open: bar.open, high: bar.high, low: bar.low, close: bar.close,
    })));
    const addEma = (key: "ema10" | "ema20" | "ema50", color: string, width: 1 | 2) => {
      const line = chart.addLineSeries({ color, lineWidth: width, priceLineVisible: false, lastValueVisible: false });
      line.setData(data.bars.filter(bar => bar[key] != null).map(bar => ({
        time: bar.time as Time, value: bar[key] as number,
      })));
    };
    addEma("ema10", "#33734a", 2);
    addEma("ema20", "#b17b31", 1);
    addEma("ema50", "#3f6f9d", 1);
    if (entry > 0) candles.createPriceLine({
      price: entry, color: "#33734a", lineWidth: 2, lineStyle: LineStyle.Dashed,
      axisLabelVisible: true, title: "入场",
    });
    if (stop > 0) candles.createPriceLine({
      price: stop, color: "#b44b45", lineWidth: 2, lineStyle: LineStyle.Dashed,
      axisLabelVisible: true, title: "止损",
    });
    chart.timeScale().fitContent();
    const resize = () => chart.applyOptions({ width: element.clientWidth });
    window.addEventListener("resize", resize);
    return () => { window.removeEventListener("resize", resize); chart.remove(); };
  }, [data, entry, stop]);
  return <div className="risk-chart-canvas" ref={ref} aria-label={`${data.ticker} 日线图`} />;
}

function Field({ label, value, onChange, suffix, step = "any", hint }: {
  label: string; value: string; onChange: (value: string) => void; suffix?: string; step?: string; hint?: string;
}) {
  return <label className="risk-field">
    <span>{label}</span>
    <div><input type="number" min="0" step={step} value={value} onChange={event => onChange(event.target.value)} />{suffix && <i>{suffix}</i>}</div>
    {hint && <small>{hint}</small>}
  </label>;
}

export default function UsRiskCalculator() {
  const [ticker, setTicker] = useState("NVDA");
  const [submittedTicker, setSubmittedTicker] = useState<string | null>(null);
  const [account, setAccount] = useState(savedAccount);
  const [allocationPct, setAllocationPct] = useState("10");
  const [entry, setEntry] = useState("");
  const [stop, setStop] = useState("");
  const [riskBudgetPct, setRiskBudgetPct] = useState("1");
  const [activePreset, setActivePreset] = useState("10EMA");
  const query = useQuery({
    queryKey: ["us-calculator-market", submittedTicker],
    queryFn: () => getUsCalculatorMarket(submittedTicker as string),
    enabled: !!submittedTicker,
    retry: false,
  });
  const data = query.data;

  useEffect(() => {
    if (!data) return;
    setEntry(data.price.toFixed(2));
    const initialStop = data.indicators.ema10 ?? data.indicators.atr_short_stop;
    setStop(initialStop?.toFixed(2) ?? "");
    setActivePreset(data.indicators.ema10 != null ? "10EMA" : "ATR 短线");
  }, [data]);
  useEffect(() => {
    try { localStorage.setItem(ACCOUNT_KEY, account); } catch { /* storage unavailable */ }
  }, [account]);

  const result = useMemo(() => calculateRisk({
    account: number(account), allocationPct: number(allocationPct),
    entry: number(entry), stop: number(stop), riskBudgetPct: number(riskBudgetPct),
  }), [account, allocationPct, entry, stop, riskBudgetPct]);
  const allocationAmount = number(account) * number(allocationPct) / 100;
  const submit = () => {
    const next = ticker.trim().toUpperCase();
    if (!next) return;
    setTicker(next);
    setSubmittedTicker(next);
  };
  const setAllocationAmount = (raw: string) => {
    const total = number(account);
    setAllocationPct(total ? String(Math.max(0, number(raw) / total * 100)) : "0");
  };
  const chooseStop = (label: string, value: number | null) => {
    if (value == null) return;
    setStop(value.toFixed(2));
    setActivePreset(label);
  };

  return <section className="discipline-page risk-calculator" data-testid="page-calculator">
    <header className="risk-hero">
      <div><small>US POSITION LAB · READ ONLY</small><h1>先算最坏情况，再决定买多少。</h1>
        <p>仓位上限与风险上限双重约束；行情只负责给参考点位，纪律由你设定。</p></div>
      <div className="risk-rule-stamp"><b>1R</b><span>风险先行</span><small>NO ORDER ROUTING</small></div>
    </header>

    <div className="risk-layout">
      <div className="risk-left">
        <section className="risk-panel risk-form-panel">
          <div className="risk-panel-head"><div><small>STEP 01</small><h2>标的与资金</h2></div><span>USD</span></div>
          <label className="risk-ticker-field"><span>美股代码</span><div>
            <input aria-label="美股代码" value={ticker} onChange={event => setTicker(event.target.value.toUpperCase())}
              onKeyDown={event => event.key === "Enter" && submit()} placeholder="NVDA" />
            <button className="desk-button cyan" onClick={submit} disabled={query.isFetching}>{query.isFetching ? "加载中" : "加载行情"}</button>
          </div><small>支持美股及美股 ETF 常用代码；例如 NVDA、AAPL、SPY、BRK-B。</small></label>
          {query.isError && <p className="risk-error">{(query.error as Error).message}</p>}
          {data && <div className="risk-quote-strip"><div><b>{data.ticker}</b><span>{data.name}</span></div>
            <strong>{usd(data.price)}</strong><small>{data.exchange} · {asOf(data.as_of)}</small></div>}
          <div className="risk-form-grid">
            <Field label="账户总额" value={account} onChange={setAccount} suffix="$" hint="只保存在当前浏览器" />
            <Field label="风险预算" value={riskBudgetPct} onChange={setRiskBudgetPct} suffix="%" hint="默认每笔最多亏 1%" />
          </div>
          <div className="allocation-block"><span>本笔仓位</span><div className="allocation-presets" role="group" aria-label="仓位比例">
            {ALLOCATION_PRESETS.map(value => <button key={value} className={Math.abs(number(allocationPct) - value) < 1e-7 ? "active" : ""}
              onClick={() => setAllocationPct(String(value))}>{value}%</button>)}
          </div><div className="risk-form-grid">
            <Field label="占账户" value={allocationPct} onChange={setAllocationPct} suffix="%" />
            <Field label="仓位金额" value={allocationAmount ? String(Math.round(allocationAmount * 100) / 100) : ""} onChange={setAllocationAmount} suffix="$" />
          </div></div>
          <div className="risk-form-grid">
            <Field label="入场价" value={entry} onChange={setEntry} suffix="$" />
            <Field label="止损价" value={stop} onChange={value => { setStop(value); setActivePreset("手动"); }} suffix="$" />
          </div>
          {data && <div className="stop-presets" role="group" aria-label="止损预设">
            <button className={activePreset === "10EMA" ? "active" : ""} disabled={data.indicators.ema10 == null}
              onClick={() => chooseStop("10EMA", data.indicators.ema10)}>10EMA <small>{data.indicators.ema10 ? usd(data.indicators.ema10) : "—"}</small></button>
            <button className={activePreset === "ATR 短线" ? "active" : ""} disabled={data.indicators.atr_short_stop == null}
              onClick={() => chooseStop("ATR 短线", data.indicators.atr_short_stop)}>ATR 短线 <small>1.0× ATR14</small></button>
            <button className={activePreset === "ATR 中线" ? "active" : ""} disabled={data.indicators.atr_medium_stop == null}
              onClick={() => chooseStop("ATR 中线", data.indicators.atr_medium_stop)}>ATR 中线 <small>1.6× ATR14</small></button>
          </div>}
          {entry && stop && !result.validStop && <p className="risk-error">止损价必须大于 0 且低于入场价。</p>}
        </section>

        <section className={`risk-result-card ${result.validStop && !result.withinBudget ? "over" : "safe"}`}>
          <div className="risk-result-title"><div><small>POSITION ANSWER</small><h2>{submittedTicker ?? "—"} 建议股数</h2></div>
            <span>{result.validStop ? (result.withinBudget ? "纪律内" : "超预算") : "待输入"}</span></div>
          <div className="risk-share-answer"><strong>{result.recommendedShares}</strong><em>股</em>
            <p>资金可买 {result.sharesByCapital} 股 · 风险最多 {result.maxRiskShares} 股</p></div>
          <dl className="risk-metrics">
            <div><dt>实际仓位</dt><dd>{usd(result.recommendedShares * number(entry))}</dd></div>
            <div><dt>止损损失</dt><dd>{usd(result.recommendedShares * result.perShareRisk)}</dd></div>
            <div><dt>账户风险</dt><dd>{number(account) ? pct(result.recommendedShares * result.perShareRisk / number(account) * 100) : "—"}</dd></div>
          </dl>
          <div className="reverse-stop"><span>按“仓位股数”反推</span><b>最低可承受止损 {result.impliedStop == null ? "—" : usd(result.impliedStop)}</b>
            <small>若仍买 {result.sharesByCapital} 股，这是把单笔损失控制在 {pct(number(riskBudgetPct), 1)} 内的最低止损价。</small></div>
          <p className="risk-equation mono">股数 = min(⌊仓位金额 ÷ 入场价⌋, ⌊风险预算 ÷ 每股风险⌋)</p>
        </section>
      </div>

      <section className="risk-panel risk-chart-panel">
        <div className="risk-panel-head"><div><small>STEP 02 · MARKET CONTEXT</small><h2>日线 · EMA · 止损位置</h2></div>
          {data && <span>{data.ticker}</span>}</div>
        {data ? <>
          <div className="chart-legend"><span><i className="ema10" />EMA10 {data.indicators.ema10 ? usd(data.indicators.ema10) : "—"}</span>
            <span><i className="ema20" />EMA20 {data.indicators.ema20 ? usd(data.indicators.ema20) : "—"}</span>
            <span><i className="ema50" />EMA50 {data.indicators.ema50 ? usd(data.indicators.ema50) : "—"}</span>
            <span>ATR14 {data.indicators.atr14 ? usd(data.indicators.atr14) : "—"}</span></div>
          <PriceChart data={data} entry={number(entry)} stop={number(stop)} />
          <footer className="risk-source"><b>{data.source}</b><span>{data.source_notice}</span></footer>
        </> : <div className="risk-empty"><b>{query.isFetching ? "正在读取日线…" : "输入代码，加载交易上下文"}</b><span>图上会同步标记入场价、止损价与 EMA。</span></div>}
      </section>
    </div>
  </section>;
}
