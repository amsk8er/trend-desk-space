import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getIndustryHeat, refreshIndustryWind, type IndustryHeatRow,
} from "../api";

const trendStateLabel: Record<string, string> = {
  leading: "趋势领跑", strengthening: "正在增强", rotating: "轮动观察",
  fading: "强中转弱", lagging: "弱势掉队", insufficient: "数据不足",
};
const mainlineStateLabel: Record<string, string> = {
  confirmed_mainline: "主线确认", candidate_unverified: "候选主线",
  concentrated_not_diffused: "抱团未扩散", emerging: "轮动增强",
  rotating_out: "正在轮退", leading: "趋势领跑", strengthening: "正在增强",
  rotating: "轮动观察", fading: "强中转弱", lagging: "弱势掉队",
  insufficient: "数据不足", candidate: "候选",
};
const windStatusLabel: Record<string, string> = {
  verified: "已验证", partial: "部分证据", unavailable: "验证失败",
  not_configured: "未配置", not_requested: "未进入Top榜",
};
const temperatureClass: Record<string, string> = {
  沸: "boiling", 热: "hot", 温: "warm", 平: "flat", 凉: "cool", 寒: "cold", 冻: "frozen",
};
const trendPhases = [
  { phase: "谷雨", stage: "右侧震荡", meaning: "蛰伏", tone: "dormant" },
  { phase: "立夏", stage: "右侧一阶段", meaning: "萌芽", tone: "sprout" },
  { phase: "夏至", stage: "右侧二阶段", meaning: "发展", tone: "develop" },
  { phase: "小暑", stage: "右侧三阶段", meaning: "破圈", tone: "breakout" },
  { phase: "大暑", stage: "右侧四阶段", meaning: "爆发", tone: "climax" },
  { phase: "立秋", stage: "右侧结束", meaning: "结束", tone: "ended" },
] as const;
const knownPhaseNames = new Set<string>(trendPhases.map(item => item.phase));
const NO_PHASE = "__no_phase__";
const OTHER_PHASE = "__other_phase__";

type Filter = "all" | "leader" | "verified" | "fading";

export default function IndustryHeatPage() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<Filter>("all");
  const [phaseFilter, setPhaseFilter] = useState<string | null>(null);
  const [topN, setTopN] = useState(8);
  const reportQ = useQuery({
    queryKey: ["industry-heat"], queryFn: () => getIndustryHeat(),
  });
  const refreshWind = useMutation({
    mutationFn: () => refreshIndustryWind(reportQ.data!.trade_date!, topN),
    onSuccess: data => qc.setQueryData(["industry-heat"], data),
  });
  const rows = reportQ.data?.rows ?? [];
  const phaseDistribution = useMemo(() => {
    const counts = new Map<string, number>();
    rows.forEach(row => {
      const phase = row.phase_curr?.trim();
      if (phase) counts.set(phase, (counts.get(phase) ?? 0) + 1);
    });
    const expected = trendPhases.map(meta => ({
      ...meta, count: counts.get(meta.phase) ?? 0,
      share: rows.length ? (counts.get(meta.phase) ?? 0) / rows.length : 0,
    }));
    return expected;
  }, [rows]);
  const dominantPhase = useMemo(() => [...phaseDistribution].filter(item => item.count > 0)
    .sort((left, right) => right.count - left.count || left.phase.localeCompare(right.phase, "zh-CN"))[0], [phaseDistribution]);
  const phaseCovered = phaseDistribution.reduce((sum, item) => sum + item.count, 0);
  const otherPhaseCount = rows.filter(row => {
    const phase = row.phase_curr?.trim();
    return Boolean(phase && !knownPhaseNames.has(phase));
  }).length;
  const noPhaseCount = rows.filter(row => !row.phase_curr?.trim()).length;
  const filtered = useMemo(() => rows.filter(row => {
    const phase = row.phase_curr?.trim();
    if (phaseFilter === NO_PHASE && phase) return false;
    if (phaseFilter === OTHER_PHASE && (!phase || knownPhaseNames.has(phase))) return false;
    if (phaseFilter && ![NO_PHASE, OTHER_PHASE].includes(phaseFilter) && phase !== phaseFilter) return false;
    if (filter === "leader") return ["leading", "strengthening"].includes(row.trend_state);
    if (filter === "verified") return ["verified", "partial"].includes(row.wind_status);
    if (filter === "fading") return ["fading", "lagging"].includes(row.trend_state);
    return true;
  }), [rows, filter, phaseFilter]);
  const top = rows.slice(0, 3);
  const coverage = reportQ.data?.coverage ?? {};
  const windRatio = coverage.wind_requested
    ? Math.round((coverage.wind_verified ?? 0) / coverage.wind_requested * 100)
    : 0;
  const hotCount = rows.filter(row => ["热", "沸"].includes(row.temperature_curr ?? "")).length;
  const warmingCount = rows.filter(row => (row.strength_slope_5d ?? 0) > 0).length;

  if (reportQ.isLoading) return <section className="industry-page"><div className="industry-empty">正在读取行业气象站…</div></section>;
  if (reportQ.isError) return <section className="industry-page"><div className="industry-empty error">行业热度读取失败</div></section>;

  return <section className="industry-page" data-testid="industry-heat-page">
    <header className={`industry-hero confidence-${reportQ.data?.headline.confidence ?? "none"}`}>
      <div className="industry-hero-copy">
        <small>SECTOR ROTATION / A-SHARE</small>
        <h1>{reportQ.data?.headline.title}</h1>
        <p>{reportQ.data?.headline.summary}</p>
      </div>
      <div className="industry-radar-stamp" aria-label="行业覆盖">
        <span>{coverage.industry_count ?? 0}</span>
        <b>行业全景</b>
        <i>{reportQ.data?.trade_date ?? "等待采集"}</i>
      </div>
    </header>

    <div className="industry-kpis">
      <Metric label="高温行业" value={`${hotCount}`} note="温度处于热 / 沸" tone="orange" />
      <Metric label="强度上行" value={`${warmingCount}`} note="近5日斜率为正" tone="lime" />
      <Metric label="趋势完整" value={`${coverage.trend_complete ?? 0}/${coverage.industry_count ?? 0}`} note="温度与强度齐全" tone="cyan" />
      <Metric label="Wind验证" value={`${windRatio}%`} note={`${coverage.wind_verified ?? 0}/${coverage.wind_requested ?? 0} 个Top行业`} tone="pink" />
    </div>

    <section className="phase-spectrum" aria-labelledby="phase-spectrum-title">
      <header className="phase-spectrum-head">
        <div><small>RIGHT-SIDE LIFECYCLE / TREND SEASON</small><h2 id="phase-spectrum-title">行业节气分布</h2></div>
        <p>{phaseSummary(dominantPhase, phaseDistribution, phaseCovered, rows.length)}</p>
      </header>
      <div className="phase-lifecycle" role="group" aria-label="按趋势节气筛选行业">
        {phaseDistribution.map((item, index) => <button
          key={item.phase} className={`phase-stage tone-${item.tone} ${phaseFilter === item.phase ? "active" : ""}`}
          aria-pressed={phaseFilter === item.phase} onClick={() => setPhaseFilter(current => current === item.phase ? null : item.phase)}
        ><i>{String(index + 1).padStart(2, "0")}</i><span><b>{item.phase}</b><small>{item.stage} · {item.meaning}</small></span><strong>{item.count}</strong><em>{percent(item.share)}</em></button>)}
      </div>
      <div className="phase-controls" role="group" aria-label="节气覆盖筛选">
        <button className={phaseFilter == null ? "active" : ""} aria-pressed={phaseFilter == null} onClick={() => setPhaseFilter(null)}>
          <span>全部行业</span><b>{rows.length}</b><i>100%</i>
        </button>
        <button className={phaseFilter === NO_PHASE ? "active" : ""} aria-pressed={phaseFilter === NO_PHASE} onClick={() => setPhaseFilter(current => current === NO_PHASE ? null : NO_PHASE)}>
          <span>无节气</span><b>{noPhaseCount}</b><i>源数据未标注</i>
        </button>
        <button className={phaseFilter === OTHER_PHASE ? "active" : ""} aria-pressed={phaseFilter === OTHER_PHASE} onClick={() => setPhaseFilter(current => current === OTHER_PHASE ? null : OTHER_PHASE)}>
          <span>其他节气</span><b>{otherPhaseCount}</b><i>非本右侧六阶段</i>
        </button>
        <p><b>右侧节气 {phaseCovered}/{rows.length}</b><span>六阶段严格采用方法论定义；立春、立冬、冬至等原始值不混入右侧分布。</span></p>
      </div>
      <footer><b>{phaseFilter === NO_PHASE ? "正在查看：无节气" : phaseFilter === OTHER_PHASE ? "正在查看：其他节气" : phaseFilter ? `正在查看：${phaseFilter}` : "节气不是预测"}</b><span>立夏之后不一定演进到夏至或大暑；节气只回答右侧走到哪里，主线仍需结合温度、强度、持续性与 Wind 扩散证据。</span></footer>
    </section>

    <div className="industry-leaders">
      <div className="industry-section-title">
        <div><small>LEADERSHIP BOARD</small><h2>主线候选席</h2></div>
        <p>先看趋势动物原生热度，再看 Wind 是否证明量价与市场广度正在扩散。</p>
      </div>
      <div className="leader-grid">
        {top.map((row, index) => <LeaderCard key={row.industry_tm_id} row={row} index={index} />)}
        {!top.length && <div className="industry-empty">当前数据集还没有全行业快照；下一次盘后采集会自动补齐。</div>}
      </div>
    </div>

    <div className="industry-toolbar">
      <div className="industry-filters" role="group" aria-label="行业状态筛选">
        {([
          ["all", "全部行业"], ["leader", "领跑 / 增强"],
          ["verified", "Wind已验证"], ["fading", "轮退 / 掉队"],
        ] as [Filter, string][]).map(([id, label]) => <button
          key={id} className={filter === id ? "active" : ""} onClick={() => setFilter(id)}
        >{label}</button>)}
      </div>
      <div className="wind-refresh-box">
        <label>Wind验证<select value={topN} onChange={event => setTopN(Number(event.target.value))}>
          <option value={5}>Top 5</option><option value={8}>Top 8</option><option value={10}>Top 10</option>
        </select></label>
        <button
          className="desk-button cyan" disabled={!reportQ.data?.trade_date || refreshWind.isPending}
          onClick={() => refreshWind.mutate()}
        >{refreshWind.isPending ? "验证中…" : "重新验证"}</button>
      </div>
    </div>
    {refreshWind.isError && <p className="industry-inline-error">Wind验证失败；趋势热度仍可正常使用。</p>}

    <div className="industry-table" role="table" aria-label="全部A股行业热度">
      <div className="industry-table-head" role="row">
        <span>排名 / 行业</span><span>趋势动物热度</span><span>状态轨迹</span>
        <span>参与度</span><span>Wind验证</span><span>主线判断</span>
      </div>
      {filtered.map(row => <IndustryRow key={row.industry_tm_id} row={row} />)}
      {!filtered.length && <div className="industry-empty">当前筛选没有行业。</div>}
    </div>

    <footer className="industry-method">
      <b>评分不是预测</b>
      <p>{reportQ.data?.methodology?.trend_score}</p>
      <p>{reportQ.data?.methodology?.mainline_score}</p>
      <p>Wind验证来源：{reportQ.data?.methodology?.wind_source ?? "—"}</p>
    </footer>
  </section>;
}

function Metric({ label, value, note, tone }: { label: string; value: string; note: string; tone: string }) {
  return <article className={`industry-kpi ${tone}`}><small>{label}</small><b>{value}</b><span>{note}</span></article>;
}

function LeaderCard({ row, index }: { row: IndustryHeatRow; index: number }) {
  const wind = row.wind_score == null ? "待验证" : row.wind_score.toFixed(1);
  return <article className={`leader-card rank-${index + 1}`}>
    <header><span>0{index + 1}</span><b>{mainlineStateLabel[row.mainline_state] ?? row.mainline_state}</b></header>
    <h3>{row.industry_name}</h3>
    <div className="dual-score">
      <ScoreGauge label="趋势热度" value={row.trend_score} />
      <ScoreGauge label="Wind验证" value={row.wind_score} placeholder={wind} />
    </div>
    <p><i className={`temp-chip ${temperatureClass[row.temperature_curr ?? ""] ?? "unknown"}`}>{row.temperature_curr ?? "—"}</i>
      强度 {number(row.strength_curr)} {row.strength_change ?? ""} · 节气 {row.phase_curr ?? "—"}</p>
  </article>;
}

function ScoreGauge({ label, value, placeholder }: { label: string; value: number | null; placeholder?: string }) {
  const safe = value == null ? 0 : Math.max(0, Math.min(100, value));
  return <div className="score-gauge"><span><small>{label}</small><b>{value == null ? placeholder ?? "—" : value.toFixed(1)}</b></span>
    <i><em style={{ width: `${safe}%` }} /></i></div>;
}

function IndustryRow({ row }: { row: IndustryHeatRow }) {
  const metrics = row.wind_metrics ?? {};
  const breadth = breadthText(metrics);
  return <article className={`industry-row state-${row.mainline_state}`} role="row">
    <div className="industry-identity"><b>#{String(row.trend_rank).padStart(2, "0")}</b><span><strong>{row.industry_name}</strong><small>tmId {row.industry_tm_id}</small></span></div>
    <div className="trend-score-cell"><strong>{row.trend_score.toFixed(1)}</strong><i><em style={{ width: `${Math.max(0, Math.min(100, row.trend_score))}%` }} /></i><small>{trendStateLabel[row.trend_state] ?? row.trend_state}</small></div>
    <div className="industry-trajectory"><span className={`temp-chip ${temperatureClass[row.temperature_curr ?? ""] ?? "unknown"}`}>{row.temperature_curr ?? "—"}</span><b>强 {number(row.strength_curr)} {row.strength_change ?? ""}</b><small>斜率 {signed(row.strength_slope_5d)} · 热度持续 {row.hot_duration_days}日</small></div>
    <div className="industry-participation"><b>{row.warm_to_hot_count}</b><span>温转热个股</span><small>连续升温 {row.warming_streak}日 · {row.phase_curr ?? "节气未知"}</small></div>
    <div className="industry-wind"><b>{row.wind_score == null ? "—" : row.wind_score.toFixed(1)}</b><span>{windStatusLabel[row.wind_status] ?? row.wind_status}</span><small>{breadth}</small></div>
    <div className="industry-verdict"><b>{mainlineStateLabel[row.mainline_state] ?? row.mainline_state}</b><span>综合 {row.mainline_score.toFixed(1)}</span></div>
  </article>;
}

function number(value: number | null) { return value == null ? "—" : value.toFixed(1); }
function signed(value: number | null) { return value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}`; }
function percent(value: number) { return `${Math.round(value * 100)}%`; }
function phaseSummary(
  dominant: { phase: string; count: number; share: number } | undefined,
  phaseData: { phase: string; count: number }[],
  covered: number,
  total: number,
) {
  if (!dominant) return `当前没有行业带趋势节气标签；可能尚未进入右侧，也可能源数据未标注。节气覆盖 0/${total}。`;
  const countOf = (names: string[]) => phaseData.filter(item => names.includes(item.phase)).reduce((sum, item) => sum + item.count, 0);
  const early = countOf(["立夏", "夏至"]);
  const late = countOf(["小暑", "大暑"]);
  const ended = countOf(["立秋"]);
  const lifecycle = late > early * 1.25
    ? "小暑、大暑较多，右侧后段的加速与拥挤风险更突出。"
    : early > late * 1.25
      ? "立夏、夏至较多，右侧前中段的萌芽与发展更集中。"
      : ended > Math.max(early, late)
        ? "立秋数量偏多，需要重点观察趋势结束与轮退。"
        : "前段与后段分布接近，行业轮动阶段较分散。";
  return `主导节气 ${dominant.phase}：${dominant.count} 个行业（${percent(dominant.share)}）。${lifecycle} 节气覆盖 ${covered}/${total}。`;
}
function breadthText(metrics: IndustryHeatRow["wind_metrics"]) {
  const up = typeof metrics.advancers === "number" ? metrics.advancers : null;
  const down = typeof metrics.decliners === "number" ? metrics.decliners : null;
  const flow = typeof metrics.main_inflow_ratio_pct === "number" ? metrics.main_inflow_ratio_pct : null;
  if (up != null || down != null) return `涨 ${up ?? "—"} / 跌 ${down ?? "—"}${flow == null ? "" : ` · 主力 ${signed(flow)}%`}`;
  return "量价 / 广度待取";
}
