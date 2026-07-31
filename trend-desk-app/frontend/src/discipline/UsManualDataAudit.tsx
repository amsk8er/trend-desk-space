import { useQuery } from "@tanstack/react-query";
import { getUsManualOverview, getUsManualPlans, type UsCandidate } from "../api";
import "./us-manual.css";

const cost = (value: string | null | undefined) => value == null ? "待账单" : `¥${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 4 })}`;
const percent = (value: string | null | undefined) => value == null || !Number.isFinite(Number(value)) ? "—" : `${(Number(value) * 100).toFixed(2)}%`;
const valueOrDash = (value: unknown) => value == null || value === "" ? "—" : String(value);

export default function UsManualDataAudit() {
  const overviewQ = useQuery({ queryKey: ["us-manual-overview"], queryFn: getUsManualOverview, retry: false });
  const plansQ = useQuery({ queryKey: ["us-manual-plans"], queryFn: getUsManualPlans, retry: false });
  const overview = overviewQ.data;
  const run = overview?.run;
  const funnel = run?.funnel ?? {};
  return <section className="us-manual-audit" data-testid="us-manual-data-audit">
    <header className="us-audit-title"><div><small>US DATA & RULES</small><h2>美股数据、调度与规则证据</h2><p>这些信息用于排查与审计，不占用交易执行主界面。</p></div><span>{overview?.capabilities.rules_version ?? "读取中"}</span></header>
    {(overviewQ.isError || plansQ.isError) && <div className="us-error">{String(overviewQ.error ?? plansQ.error)}</div>}
    {overview && <>
      <div className="us-funnel us-audit-funnel" aria-label="美股采集漏斗审计">
        <AuditCard tone="cyan" label="温转热个股 / ETF" value={`${funnel.warm_to_hot_stocks ?? 0} / ${funnel.warm_to_hot_etfs ?? 0}`} note="当日直接组合" />
        <AuditCard tone="yellow" label="Bitget 严格交集" value={`${funnel.bitget_stock_intersection ?? 0} / ${funnel.bitget_etf_intersection ?? 0}`} note="禁止模糊匹配" />
        <AuditCard tone="orange" label="右侧自然日 1–9" value={funnel.right_side_within_window ?? "—"} note="严格小于 10" />
        <AuditCard tone="yellow" label="质量与强度" value={funnel.relative_strength_90_plus ?? "—"} note="强度≥90；个股板块温度≥温" />
        <AuditCard tone="orange" label="ETF 跟踪指数自动核验" value={funnel.etf_benchmark_verified ?? overview.etf_benchmark_status.verified} note={`失败关闭 ${funnel.etf_benchmark_blocked ?? overview.etf_benchmark_status.blocked}`} />
        <AuditCard tone="cyan" label="EP3 风险锚点" value={funnel.risk_anchor_ready ?? overview.risk_anchor_status.ready} note={`待生成 ${overview.risk_anchor_status.pending} · 阻断 ${overview.risk_anchor_status.blocked}`} />
        <AuditCard tone="lime" label="可进入分配" value={funnel.ready_for_plan ?? overview.risk_anchor_status.ready} note={`当前模式 ${overview.capabilities.h6_mode}`} />
      </div>
      <div className="us-audit-grid">
        <article><small>DATA DATE</small><h3>{run?.as_of_date ?? "等待更新"}</h3><dl><dt>美股根</dt><dd>{String(overview.data_update?.us_as_of_date ?? "—")}</dd><dt>美国 ETF 根</dt><dd>{String(overview.data_update?.us_etf_as_of_date ?? "—")}</dd><dt>运行状态</dt><dd>{run?.status ?? overview.state}</dd><dt>触发来源</dt><dd>{String(overview.last_attempt?.trigger ?? "—")}</dd></dl></article>
        <article><small>SCHEDULER</small><h3>{overview.schedule.scheduler_enabled ? "生产自动调度" : "当前关闭"}</h3><dl><dt>时区</dt><dd>{overview.schedule.timezone}</dd><dt>自动时点</dt><dd>{overview.schedule.automatic_slots.join(" / ")}</dd><dt>手动完整采集</dt><dd>{overview.schedule.manual_full_collection_after} 后</dd><dt>截止</dt><dd>{overview.schedule.automatic_cutoff}</dd></dl></article>
        <article><small>DATA COST</small><h3>{cost(overview.cost.estimated_total_cny)}</h3><dl><dt>单日上限</dt><dd>{cost(overview.cost.daily_cap_cny)}</dd><dt>当前运行预计</dt><dd>{cost(overview.cost.current_run_estimated_cny)}</dd><dt>账单实际</dt><dd>{cost(overview.cost.actual_total_cny)}</dd>{Object.entries(overview.cost.breakdown).flatMap(([name, value]) => [<dt key={`${name}-term`}>{name}</dt>, <dd key={`${name}-value`}>{cost(value)}</dd>])}</dl></article>
        <article><small>RULE BOUNDARY</small><h3>失败关闭</h3><ul><li>当日只查两个温转热直接组合，不扫描全量美股。</li><li>个股：右侧 1–9 天、板块温度≥温、相对强度≥90。</li><li>ETF：每日采集自动核验权威跟踪指数；同指数只留最强一只，其他字段仅观察。</li><li>EP3 仅反推仓位，真实退出按危险、温度、沸与开香槟。</li><li>Bitget 只使用公开产品、报价和 K 线；无账户或订单 API。</li></ul></article>
      </div>
      <details className="us-panel us-candidate-evidence"><summary>当日候选底层证据（{overview.candidates.length}）</summary><p>这里集中保存交易主界面隐藏的来源、K 线校验、算法与哈希。默认折叠，不影响日常执行。</p>{overview.candidates.map(candidate => <CandidateAudit key={candidate.candidate_id} candidate={candidate} />)}</details>
      <details className="us-panel us-history"><summary>历史美股清单与版本（{plansQ.data?.length ?? 0}）</summary>{plansQ.data?.map(plan => <p key={plan.plan_id}><b>{plan.plan_id}</b> · {plan.rules_version} · {plan.signal_date} · {plan.status} · {plan.items.length} 项</p>)}</details>
    </>}
  </section>;
}

function CandidateAudit({ candidate }: { candidate: UsCandidate }) {
  const anchor = candidate.risk_anchor;
  const anchorEvidence = (anchor?.evidence_json ?? {}) as Record<string, unknown>;
  const sessions = Array.isArray(anchorEvidence.aggregated_sessions)
    ? anchorEvidence.aggregated_sessions as Array<Record<string, unknown>> : [];
  const anchorCalculation = anchorEvidence.anchor && typeof anchorEvidence.anchor === "object"
    ? anchorEvidence.anchor as Record<string, unknown> : {};
  const ep3 = anchorCalculation.ep3 && typeof anchorCalculation.ep3 === "object"
    ? anchorCalculation.ep3 as Record<string, unknown> : {};
  const dailyValidation = anchorEvidence.daily_validation && typeof anchorEvidence.daily_validation === "object"
    ? anchorEvidence.daily_validation as Record<string, unknown> : {};
  const etf = candidate.etf_benchmark_evidence;
  const sourceUrl = etf?.source_url?.startsWith("https://") ? etf.source_url : null;
  const firstSession = sessions[0]?.date;
  const lastSession = sessions[sessions.length - 1]?.date;
  return <details className="us-candidate-evidence-row">
    <summary><b>{candidate.ticker_symbol}</b><span>{candidate.asset_type.toUpperCase()} · 筛选 {candidate.screen_status} · 锚点 {candidate.risk_anchor_status}</span></summary>
    <div className="us-candidate-evidence-grid">
      <dl><dt>组合证据</dt><dd>当日温转热直接成分</dd><dt>tmId</dt><dd>{candidate.tm_id}</dd><dt>右侧自然日</dt><dd>{valueOrDash(candidate.right_side_calendar_days)}</dd><dt>相对强度</dt><dd>{valueOrDash(candidate.strength_local)}</dd><dt>板块 / 温度</dt><dd>{candidate.industry_name ?? "—"} / {candidate.industry_temperature_curr ?? "—"}</dd><dt>标签 / 节气</dt><dd>{candidate.ticker_labels.join(" · ") || "—"} / {candidate.trend_phase_curr ?? "—"}</dd><dt>全部筛选理由</dt><dd>{candidate.all_reasons.join(" · ") || "—"}</dd></dl>
      {candidate.asset_type !== "stock" && <dl><dt>ETF 跟踪指数</dt><dd>{etf?.benchmark_canonical_name ?? etf?.benchmark_name_raw ?? "—"}</dd><dt>指数去重键</dt><dd>{candidate.benchmark_family_id ?? "—"}</dd><dt>SEC 身份</dt><dd>{etf ? `${valueOrDash(etf.identity.series_id)} / ${valueOrDash(etf.identity.class_id)}` : "—"}</dd><dt>策略 / 方向（仅观察）</dt><dd>{etf ? `${valueOrDash(etf.strategy_type)} / ${valueOrDash(etf.exposure_direction)} / ${valueOrDash(etf.leverage_multiplier)}x` : "—"}</dd><dt>汇率对冲（仅观察）</dt><dd>{etf?.currency_hedge ?? "—"}</dd><dt>跟踪指数哈希</dt><dd>{candidate.exposure_key ?? "—"}</dd><dt>权威来源</dt><dd>{sourceUrl ? <a href={sourceUrl} target="_blank" rel="noreferrer">打开原始资料</a> : "—"}<small>{etf?.retrieved_at ? `${etf.retrieved_at} UTC` : "—"}</small></dd></dl>}
      <dl><dt>公开参考价</dt><dd>{valueOrDash(anchor?.quote_usdt)} USDT</dd><dt>EP3 低点 / 日期</dt><dd>{valueOrDash(anchor?.anchor_price_usdt)} / {valueOrDash(anchor?.anchor_date)}</dd><dt>参考摆动高点</dt><dd>{valueOrDash(ep3.reference_high)} / {valueOrDash(ep3.reference_high_date)}</dd><dt>确认日期</dt><dd>{valueOrDash(ep3.confirmed_on)}</dd><dt>锚点距离</dt><dd>{percent(anchor?.anchor_distance)}</dd><dt>1H 常规时段</dt><dd>{sessions.length ? `${valueOrDash(firstSession)}–${valueOrDash(lastSession)} · ${sessions.length} 个完整交易日` : "—"}</dd><dt>1D 连续性</dt><dd>{anchor?.daily_sha256 ? `${valueOrDash(dailyValidation.bar_count)} 根；最大跳变 ${percent(valueOrDash(dailyValidation.largest_jump_ratio))}` : "—"}</dd><dt>未来数据</dt><dd>{anchorCalculation.future_data_used === false ? "未使用" : "证据缺失"}</dd><dt>算法版本</dt><dd>{anchor?.algorithm_version ?? "—"}</dd><dt>1D / 1H 哈希</dt><dd>{anchor ? `${anchor.daily_sha256 ?? "—"} / ${anchor.hourly_sha256 ?? "—"}` : "—"}</dd><dt>错误</dt><dd>{anchor?.error_message ?? "无"}</dd></dl>
    </div>
  </details>;
}

function AuditCard({ tone, label, value, note }: { tone: string; label: string; value: string | number; note: string }) {
  return <div className={`us-funnel-card ${tone}`}><small>{label}</small><b>{value}</b><span>{note}</span></div>;
}
