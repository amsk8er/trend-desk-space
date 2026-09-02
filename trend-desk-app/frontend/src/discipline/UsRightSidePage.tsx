import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getUsRightSideAsset, getUsRightSideAssets, getUsRightSideOverview, getUsRightSidePlot,
  preflightUsRightSideUniverse, refreshUsRightSideUniverse,
  preflightUsRightSideEnrichment, preflightUsRightSideScan,
  runUsRightSideEnrichment, runUsRightSideScan,
  type UsRightSideAsset, type UsRightSideFieldState, type UsRightSidePreflight,
  type UsRightSideSortCapability, type UsRightSideSortDirection,
} from "../api";
import "./us-right-side.css";

const temperatureTone: Record<string, string> = {
  沸: "boiling", 热: "hot", 温: "warm", 平: "flat", 凉: "cool", 寒: "cold", 冻: "frozen",
};
const fieldStateLabel: Record<UsRightSideFieldState, string> = {
  available: "", not_requested: "未请求", not_returned: "未返回",
  not_applicable: "不适用", stale: "日期不一致",
};
const runStatusLabel: Record<string, string> = {
  pending: "等待预检", awaiting_screen_budget: "等待扫描预算", screening: "扫描中",
  screen_partial: "名单不完整", screen_ready: "右侧名单就绪",
  awaiting_age_budget: "等待右侧天数", age_enriching: "右侧天数获取中", age_partial: "右侧天数不完整",
  awaiting_strength_budget: "等待强度预筛", strength_enriching: "候选强度获取中",
  strength_partial: "候选强度不完整",
  awaiting_standard_budget: "等待候选字段", standard_enriching: "候选字段获取中",
  standard_partial: "候选字段不完整", awaiting_industry_budget: "等待行业环境",
  industry_enriching: "行业增强中", ready: "完整就绪", failed: "运行失败",
};

type Stage = "screen" | "age" | "strength" | "standard" | "industry" | "deep";
type Gate = { stage: Stage; runId: string; estimated: number; rows: number; batches: number; preflightHash: string };

const columns = [
  ["ticker_name", "资产"], ["ticker_symbol", "代码"],
  ["temperature_curr", "温度"], ["phase_curr", "阶段"], ["industry_name", "行业"],
  ["industry_temperature_curr", "温度"], ["industry_strength_local_curr", "强度"],
  ["industry_phase_curr", "节气"], ["days_since_trend_entry", "右侧天数(自然日)"],
  ["gain_since_trend_entry", "右侧涨幅"], ["strength_local_curr", "本地强度"], ["risk_flag_count", "风险"],
] as const;
const sortableFields = new Set<string>(columns.map(([field]) => field));

function readUrlState() {
  const params = typeof window === "undefined" ? new URLSearchParams() : new URLSearchParams(window.location.search);
  const requestedSort = params.get("sort_by") ?? params.get("us_sort") ?? "";
  return {
    search: params.get("us_q") ?? "", temperature: params.get("us_temp") ?? "",
    phase: params.get("us_phase") ?? "", industry: params.get("us_industry") ?? "",
    daysRange: params.get("us_days") ?? "", strengthMin: params.get("us_strength") ?? "",
    riskOnly: params.get("us_risk") === "1",
    sortBy: sortableFields.has(requestedSort) ? requestedSort : "ticker_symbol",
    sortDir: ((params.get("sort_dir") ?? params.get("us_dir")) === "desc" ? "desc" : "asc") as UsRightSideSortDirection,
    sortTouched: params.has("sort_by") || params.has("us_sort"),
  };
}

export default function UsRightSidePage() {
  const qc = useQueryClient();
  const initial = useMemo(readUrlState, []);
  const [search, setSearch] = useState(initial.search);
  const [temperature, setTemperature] = useState(initial.temperature);
  const [phase, setPhase] = useState(initial.phase);
  const [industry, setIndustry] = useState(initial.industry);
  const [daysRange, setDaysRange] = useState(initial.daysRange);
  const [strengthMin, setStrengthMin] = useState(initial.strengthMin);
  const [riskOnly, setRiskOnly] = useState(initial.riskOnly);
  const [sortBy, setSortBy] = useState(initial.sortBy);
  const [sortDir, setSortDir] = useState<UsRightSideSortDirection>(initial.sortDir);
  const [sortTouched, setSortTouched] = useState(initial.sortTouched);
  const [sortNotice, setSortNotice] = useState("");
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([null]);
  const [selected, setSelected] = useState<number | null>(null);
  const [gate, setGate] = useState<Gate | null>(null);
  const [budget, setBudget] = useState("");
  const [universeGate, setUniverseGate] = useState(false);
  const [universeBudget, setUniverseBudget] = useState("1.00");
  const [activeExecutionStage, setActiveExecutionStage] = useState<Stage | null>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const detailOpener = useRef<HTMLElement | null>(null);

  const overviewQ = useQuery({
    queryKey: ["us-right-side-overview"], queryFn: getUsRightSideOverview, retry: false,
    refetchInterval: query => activeExecutionStage || ["screening", "age_enriching", "strength_enriching", "standard_enriching", "industry_enriching"]
      .includes(query.state.data?.run?.status ?? "") ? 3_000 : false,
  });
  const freeStatusQ = useQuery({
    queryKey: ["us-right-side-free-status"],
    queryFn: preflightUsRightSideUniverse,
    retry: false,
    staleTime: 60_000,
    refetchInterval: 5 * 60_000,
  });
  const run = overviewQ.data?.run ?? null;
  const ageReady = run?.readiness.age ?? run?.readiness.standard ?? false;
  const strengthReady = run?.readiness.strength ?? run?.readiness.standard ?? false;
  const membershipDate = run?.membership_as_of_date
    ?? overviewQ.data?.universe.membership_as_of_date
    ?? freeStatusQ.data?.current.membership_as_of_date
    ?? null;
  const latestTrendDate = freeStatusQ.data?.upstream.as_of_date ?? run?.as_of_date ?? null;
  const upstreamUpdateDt = freeStatusQ.data?.upstream.update_dt ?? run?.upstream_update_dt ?? null;
  const membershipStale = Boolean(membershipDate && latestTrendDate && membershipDate !== latestTrendDate);
  const resultStale = Boolean(run && latestTrendDate && run.as_of_date !== latestTrendDate);
  const datesAligned = Boolean(membershipDate && latestTrendDate && membershipDate === latestTrendDate);
  const currentCursor = cursorHistory[cursorHistory.length - 1];
  const [daysMin, daysMax] = daysRange ? daysRange.split(":").map(Number) : [undefined, undefined];
  const effectiveDaysMax = daysMax ?? (strengthReady ? run?.counts.strength_max_days ?? 30 : undefined);

  useEffect(() => {
    if (strengthReady && !sortTouched) {
      setSortBy("strength_local_curr"); setSortDir("desc"); setCursorHistory([null]);
    }
  }, [strengthReady, sortTouched]);

  useEffect(() => {
    const url = new URL(window.location.href);
    const values: Record<string, string> = {
      sort_by: sortBy, sort_dir: sortDir, us_q: search, us_temp: temperature,
      us_phase: phase, us_industry: industry, us_days: daysRange,
      us_strength: strengthMin, us_risk: riskOnly ? "1" : "",
    };
    url.searchParams.delete("us_sort");
    url.searchParams.delete("us_dir");
    Object.entries(values).forEach(([key, current]) => current ? url.searchParams.set(key, current) : url.searchParams.delete(key));
    window.history.replaceState(null, "", url);
  }, [sortBy, sortDir, search, temperature, phase, industry, daysRange, strengthMin, riskOnly]);

  const listQ = useQuery({
    queryKey: ["us-right-side-assets", run?.run_id, search, temperature, phase, industry,
      daysMin, effectiveDaysMax, strengthMin, riskOnly, sortBy, sortDir, currentCursor],
    queryFn: () => getUsRightSideAssets({
      runId: run!.run_id, query: search || undefined, temperature: temperature || undefined,
      phase: phase || undefined, industry: industry || undefined,
      daysMin, daysMax: effectiveDaysMax, strengthMin: strengthMin ? Number(strengthMin) : undefined,
      riskOnly, sortBy, sortDir, cursor: currentCursor, pageSize: 40,
    }),
    enabled: Boolean(run), retry: false, placeholderData: previous => previous,
  });
  const caps = listQ.data?.sort_capabilities ?? overviewQ.data?.sort_capabilities ?? {};
  const items = listQ.data?.items ?? [];
  const industries = useMemo(() => listQ.data?.facets?.industries ?? Array.from(new Set(
    items.map(item => item.industry_name).filter((value): value is string => Boolean(value)),
  )).sort((a, b) => a.localeCompare(b, "zh-CN")), [items, listQ.data?.facets?.industries]);

  useEffect(() => {
    const capability = caps[sortBy];
    if (!run || !capability || capability.enabled) return;
    setSortNotice(`${capability.blocked_reason}；已恢复当前可用的默认排序。`);
    setSortBy(strengthReady ? "strength_local_curr" : "ticker_symbol");
    setSortDir(strengthReady ? "desc" : "asc");
    setSortTouched(false);
    setCursorHistory([null]);
  }, [caps, run, sortBy]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["us-right-side-overview"] });
    qc.invalidateQueries({ queryKey: ["us-right-side-free-status"] });
    qc.invalidateQueries({ queryKey: ["us-right-side-assets"] });
  };
  const preflight = useMutation({
    mutationFn: async (stage: Stage) => {
      if (stage === "screen") return preflightUsRightSideScan();
      if (!run) throw new Error("尚无运行");
      return preflightUsRightSideEnrichment(run.run_id, stage);
    },
    onSuccess: (data: UsRightSidePreflight, stage) => {
      setGate({ stage, runId: data.run_id, estimated: data.preflight.estimated_cost_cny,
        rows: data.preflight.row_count, batches: data.preflight.batch_count,
        preflightHash: data.preflight.preflight_hash });
      setBudget((Math.ceil(Math.max(data.preflight.estimated_cost_cny, .01) * 100) / 100).toFixed(2));
      qc.setQueryData(["us-right-side-overview"], (old: typeof overviewQ.data) => old ? { ...old, run: data } : old);
    },
  });
  const execute = useMutation({
    mutationFn: async () => {
      if (!gate) throw new Error("请先预检费用");
      return gate.stage === "screen"
        ? runUsRightSideScan(gate.runId, budget, gate.preflightHash)
        : runUsRightSideEnrichment(gate.runId, gate.stage, budget, gate.preflightHash);
    },
    onMutate: () => setActiveExecutionStage(gate?.stage ?? null),
    onSuccess: data => {
      qc.setQueryData(["us-right-side-overview"], (old: typeof overviewQ.data) => old ? { ...old, run: data } : old);
      setGate(null);
      refresh();
    },
    onSettled: () => setActiveExecutionStage(null),
  });
  const universeRefresh = useMutation({
    mutationFn: () => refreshUsRightSideUniverse(universeBudget),
    onSuccess: () => { setUniverseGate(false); refresh(); },
  });

  const openUniverseGate = async () => {
    if (!freeStatusQ.data) {
      const result = await freeStatusQ.refetch();
      if (!result.data) return;
    }
    setUniverseGate(true);
  };

  const changeSort = (field: string) => {
    const capability = caps[field];
    if (!capability?.enabled) return;
    if (field === sortBy) setSortDir(value => value === "asc" ? "desc" : "asc");
    else { setSortBy(field); setSortDir(capability.default_direction); }
    setSortTouched(true); setCursorHistory([null]);
  };
  const changeDirection = () => {
    setSortDir(value => value === "asc" ? "desc" : "asc");
    setSortTouched(true);
    setCursorHistory([null]);
  };
  const updateFilter = (action: () => void) => { action(); setCursorHistory([null]); };
  const reset = () => {
    setSearch(""); setTemperature(""); setPhase(""); setIndustry(""); setDaysRange("");
    setStrengthMin(""); setRiskOnly(false); setSortTouched(false); setCursorHistory([null]);
    setSortBy(strengthReady ? "strength_local_curr" : "ticker_symbol");
    setSortDir(strengthReady ? "desc" : "asc"); searchInput.current?.focus();
  };

  if (overviewQ.isLoading) return <section className="usrs-page"><div className="usrs-empty">正在翻开美股趋势账簿…</div></section>;
  if (overviewQ.isError) return <section className="usrs-page"><div className="usrs-empty error">美股右侧页面读取失败</div></section>;

  const runNeedsScreen = !run || resultStale
    || ["pending", "awaiting_screen_budget", "screen_partial"].includes(run.status);
  const actionStatus = freeStatusQ.isLoading ? "正在核对上游日期"
    : freeStatusQ.isError ? "上游日期读取失败"
    : membershipStale ? "成员范围已过期"
    : resultStale ? "结果待更新"
    : activeExecutionStage === "screen" ? "名单生成中"
    : run && !ageReady && !runNeedsScreen ? "等待右侧天数初筛"
    : runStatusLabel[run?.status ?? "pending"];
  const actionStatusClass = freeStatusQ.isError || membershipStale || resultStale
    ? "stale" : run?.status ?? "pending";

  const openDetail = (tmId: number) => {
    detailOpener.current = document.activeElement as HTMLElement | null;
    setSelected(tmId);
  };
  const closeDetail = () => {
    setSelected(null);
    window.setTimeout(() => detailOpener.current?.focus(), 0);
  };

  return <section className="usrs-page" data-testid="us-right-side-page">
    <header className="usrs-hero usrs-masthead">
      <div><small>US RIGHT-SIDE LEDGER / TREND ANIMALS</small><h1>美股右侧资产</h1>
        <p>“全部”仅指趋势动物 API 当前覆盖的美股个股，不代表美国交易所全部上市证券；右侧状态是趋势事实，不是买入或仓位指令。</p></div>
      <div className="usrs-stamp"><span>{run?.counts.right_side ?? "—"}</span><b>右侧资产</b>
        <i>{latestTrendDate ?? "读取上游日期"}</i></div>
    </header>

    <div className="usrs-action-strip">
      <div className="usrs-date-note">
        <span>上游趋势数据日 <b>{latestTrendDate ?? (freeStatusQ.isLoading ? "读取中…" : "—")}</b>
          <i className="usrs-auto-mark">免费自动更新</i></span>
        <span className={membershipStale ? "usrs-date-stale" : undefined}>成员范围 <b>{membershipDate ?? "—"}</b>
          {membershipStale && <i className="usrs-stale-mark">已过期</i>}</span>
        {resultStale && <span className="usrs-date-stale">当前结果 <b>{run?.as_of_date}</b><i className="usrs-stale-mark">待重跑</i></span>}
        <span>上游更新 <b>{upstreamUpdateDt ?? "未记录"}</b></span>
        <span className={`usrs-status status-${actionStatusClass}`}>{actionStatus}</span>
      </div>
      <div className="usrs-actions">
        {freeStatusQ.isError && <button onClick={() => freeStatusQ.refetch()}>重试上游日期</button>}
        {!freeStatusQ.isError && (!overviewQ.data?.universe.available || membershipStale) && <button
          onClick={() => void openUniverseGate()}
          disabled={freeStatusQ.isLoading || universeRefresh.isPending}
        >{membershipStale ? "刷新成员范围" : "检查并刷新成员范围"}</button>}
        {freeStatusQ.data && datesAligned && runNeedsScreen
          ? <button onClick={() => preflight.mutate("screen")} disabled={preflight.isPending || execute.isPending}>生成右侧名单</button>
          : null}
        {run && !ageReady && !resultStale && datesAligned && !runNeedsScreen
          ? <button onClick={() => preflight.mutate("age")} disabled={preflight.isPending || execute.isPending}>① 补齐右侧天数</button>
          : null}
        {run && ageReady && !strengthReady && !resultStale && datesAligned
          ? <button onClick={() => preflight.mutate("strength")} disabled={preflight.isPending || execute.isPending}>② 获取 30 天候选强度</button>
          : null}
        {run && strengthReady && !run.readiness.standard && !resultStale && datesAligned
          ? <button onClick={() => preflight.mutate("standard")} disabled={preflight.isPending || execute.isPending}>③ 补齐 Top 100 详情</button>
          : null}
        {run?.readiness.standard && !run.readiness.industry && !resultStale && datesAligned
          ? <button onClick={() => preflight.mutate("industry")} disabled={preflight.isPending || execute.isPending}>④ 补齐行业环境</button>
          : null}
        {run?.status === "ready" && !run.readiness.deep && !resultStale && datesAligned
          ? <button className="quiet" onClick={() => preflight.mutate("deep")} disabled={preflight.isPending || execute.isPending}>深度字段</button>
          : null}
        <button className="quiet" onClick={refresh}>刷新状态</button>
      </div>
    </div>

    {membershipStale && <p className="usrs-date-alert" role="status">
      上游已经更新到 <b>{latestTrendDate}</b>，本地成员仍停留在 <b>{membershipDate}</b>。扫描已暂停；刷新成员范围需要单独确认预算。
    </p>}
    {!membershipStale && resultStale && <p className="usrs-date-alert" role="status">
      当前表格是 <b>{run?.as_of_date}</b> 的历史结果，上游已经更新到 <b>{latestTrendDate}</b>，请重新生成右侧名单。
    </p>}

    <ScreenProgress
      run={run}
      gate={gate?.stage === "screen" ? gate : null}
      isPreflighting={preflight.isPending && preflight.variables === "screen"}
      isExecuting={activeExecutionStage === "screen"}
      fallbackUniverse={overviewQ.data?.universe.stock_count ?? 0}
    />
    {run && <FunnelProgress run={run} gate={gate} activeStage={activeExecutionStage}
      preflightStage={preflight.isPending ? preflight.variables ?? null : null} />}
    {strengthReady && run && <p className="usrs-candidate-note" role="status">
      当前表格展示右侧 <b>30 天内</b>候选，共 <b>{run.counts.strength_target ?? 0}</b> 只，并支持按本地强度排序；
      仅强度前 <b>{run.counts.standard_top_n ?? 100}</b> 只补充温度、节气和行业映射，其余候选不购买详情。
    </p>}

    {universeGate && <section className="usrs-cost-gate" aria-label="覆盖范围费用批准">
      <div><small>全量美股成员范围</small><b>展开行数未知</b>
        <span>上游未承诺展开行数，无法精确预估；预算仅记录批准上限，不代表上游强制止损。</span>
        {freeStatusQ.data && <span>免费证据已核对：{freeStatusQ.data.free_evidence.api_count ?? "—"} 个接口、
          {freeStatusQ.data.free_evidence.billing_field_count} 个计费字段、
          {freeStatusQ.data.free_evidence.missing_required_fields.length ? `缺少 ${freeStatusQ.data.free_evidence.missing_required_fields.join("、")}` : "所需字段齐全"}。</span>}
      </div>
      <label>批准预算（元）<input type="number" min="0.01" step="0.01" value={universeBudget}
        onChange={event => setUniverseBudget(event.target.value)} /></label>
      <button disabled={universeRefresh.isPending || Number(universeBudget) <= 0}
        onClick={() => universeRefresh.mutate()}>{universeRefresh.isPending ? "刷新中…" : "确认风险并刷新"}</button>
      <button className="quiet" onClick={() => setUniverseGate(false)}>取消</button>
    </section>}

    {gate && <section className="usrs-cost-gate" aria-label="费用批准">
      <div><small>{stageLabel(gate.stage)}</small><b>预计 ¥{gate.estimated.toFixed(4)}</b>
        <span>{gate.rows} 行 · {gate.batches} 批 · 同日缓存不重复收费</span></div>
      <label>批准预算（元）<input type="number" min={gate.estimated} step="0.01" value={budget} onChange={event => setBudget(event.target.value)} /></label>
      <button disabled={execute.isPending || Number(budget) + 1e-9 < gate.estimated} onClick={() => execute.mutate()}>
        {execute.isPending ? "执行中…" : "批准并执行"}
      </button>
      <button className="quiet" onClick={() => setGate(null)}>取消</button>
    </section>}
    {(preflight.isError || execute.isError || freeStatusQ.isError || universeRefresh.isError) && <p className="usrs-error">{
      String((preflight.error || execute.error || freeStatusQ.error || universeRefresh.error) as Error)
    }</p>}
    {sortNotice && <p className="usrs-sort-notice" role="status">{sortNotice}</p>}

    <section className="usrs-ruler usrs-stat-strip" aria-label="右侧资产统计">
      <Metric label="覆盖成员" value={run?.counts.universe ?? overviewQ.data?.universe.stock_count ?? 0} note={`${run?.counts.scanned ?? 0} 已扫描`} />
      <Metric label="右侧资产" value={run?.counts.right_side ?? 0} note="isTrendRightSide=true" />
      <Metric label="状态未知" value={run?.counts.unknown ?? 0} note="不视为 false" />
      <Metric label="右侧天数" value={run?.counts.age_covered ?? 0} note={ageReady ? "初筛完成" : "尚未补齐"} />
      <Metric label="30 天强度" value={run?.counts.strength_covered ?? 0} note={strengthReady
        ? `${run?.counts.strength_target ?? 0} 只可排序` : "尚未获取"} />
      <Metric label="Top 100 详情" value={run?.counts.standard_covered ?? 0} note={run?.readiness.standard
        ? `${run.counts.standard_target ?? 0} 只已补齐` : "尚未完整"} />
      <Metric label="行业环境" value={run?.counts.industry_covered ?? 0} note={`${run?.counts.industries ?? 0} 个去重行业`} />
      <Metric label="已发生费用" value={`¥${Number(run?.cost.actual_total_cny ?? 0).toFixed(2)}`} note="不含尚未批准阶段" />
    </section>

    {run && <AuditPanel run={run} />}

    <section className="usrs-toolbar usrs-filter-bar" aria-label="筛选美股右侧资产">
      <label className="search">搜索<input ref={searchInput} value={search} placeholder="名称 / 代码" onChange={event => updateFilter(() => setSearch(event.target.value))} /></label>
      <label>温度<select value={temperature} onChange={event => updateFilter(() => setTemperature(event.target.value))}>
        <option value="">全部</option>{["沸", "热", "温", "平", "凉", "寒", "冻"].map(value => <option key={value}>{value}</option>)}</select></label>
      <label>阶段<select value={phase} onChange={event => updateFilter(() => setPhase(event.target.value))}>
        <option value="">全部</option>{["谷雨", "立夏", "夏至", "小暑", "大暑"].map(value => <option key={value}>{value}</option>)}</select></label>
      <label>行业<select value={industry} onChange={event => updateFilter(() => setIndustry(event.target.value))}>
        <option value="">全部</option>{industries.map(value => <option key={value}>{value}</option>)}</select></label>
      <label>右侧天数<select value={daysRange} onChange={event => updateFilter(() => setDaysRange(event.target.value))}>
        <option value="">全部</option><option value="1:3">1–3</option><option value="4:10">4–10</option>
        <option value="11:30">11–30</option><option value="31:60">31–60</option><option value="61:99999">60+</option></select></label>
      <label>最低强度<select value={strengthMin} onChange={event => updateFilter(() => setStrengthMin(event.target.value))}>
        <option value="">不限</option><option value="90">90+</option><option value="80">80+</option><option value="60">60+</option></select></label>
      <label className="check"><input type="checkbox" checked={riskOnly} onChange={event => updateFilter(() => setRiskOnly(event.target.checked))} />风险复核</label>
      <button className="reset" onClick={reset}>重置</button>
      <div className="usrs-mobile-sort"><label>排序<select value={sortBy} onChange={event => changeSort(event.target.value)}>
        {columns.map(([field, label]) => <option key={field} value={field} disabled={!caps[field]?.enabled}>{label}</option>)}</select></label>
        <button onClick={changeDirection}>{sortDir === "asc" ? "升序 ↑" : "降序 ↓"}</button></div>
    </section>

    <div className={`usrs-table-wrap usrs-table-shell ${listQ.isFetching ? "is-fetching" : ""}`}>
      {!run ? <div className="usrs-empty">先生成今日右侧名单。页面不会自动发起付费请求。</div>
        : listQ.isError ? <div className="usrs-empty error">{String(listQ.error)}</div>
        : items.length === 0 ? <div className="usrs-empty">当前筛选条件下没有右侧资产。</div>
        : <table className="usrs-table">
          <thead><tr>
            <th rowSpan={2} className="rank usrs-sticky-rank">#</th>
            <SortHead rowSpan={2} field="ticker_name" label="资产" capability={caps.ticker_name} active={sortBy} direction={sortDir} onSort={changeSort} />
            <SortHead rowSpan={2} field="ticker_symbol" label="代码" capability={caps.ticker_symbol} active={sortBy} direction={sortDir} onSort={changeSort} />
            <SortHead rowSpan={2} field="temperature_curr" label="温度" capability={caps.temperature_curr} active={sortBy} direction={sortDir} onSort={changeSort} />
            <SortHead rowSpan={2} field="phase_curr" label="阶段" capability={caps.phase_curr} active={sortBy} direction={sortDir} onSort={changeSort} />
            <th colSpan={4} className="usrs-industry-group">所属行业环境</th>
            <SortHead rowSpan={2} field="days_since_trend_entry" label="右侧天数(自然日)" capability={caps.days_since_trend_entry} active={sortBy} direction={sortDir} onSort={changeSort} />
            <SortHead rowSpan={2} field="gain_since_trend_entry" label="右侧涨幅" capability={caps.gain_since_trend_entry} active={sortBy} direction={sortDir} onSort={changeSort} className="usrs-optional-mid" />
            <SortHead rowSpan={2} field="strength_local_curr" label="本地强度" capability={caps.strength_local_curr} active={sortBy} direction={sortDir} onSort={changeSort} />
            <SortHead rowSpan={2} field="risk_flag_count" label="风险" capability={caps.risk_flag_count} active={sortBy} direction={sortDir} onSort={changeSort} />
          </tr><tr>
            <SortHead field="industry_name" label="行业" capability={caps.industry_name} active={sortBy} direction={sortDir} onSort={changeSort} />
            <SortHead field="industry_temperature_curr" label="温度" capability={caps.industry_temperature_curr} active={sortBy} direction={sortDir} onSort={changeSort} />
            <SortHead field="industry_strength_local_curr" label="强度" capability={caps.industry_strength_local_curr} active={sortBy} direction={sortDir} onSort={changeSort} />
            <SortHead field="industry_phase_curr" label="节气" capability={caps.industry_phase_curr} active={sortBy} direction={sortDir} onSort={changeSort} />
          </tr></thead>
          <tbody>{items.map(item => <AssetRow key={item.tm_id} item={item} onOpen={openDetail} />)}</tbody>
        </table>}
    </div>
    {run && !listQ.isError && <footer className="usrs-pagination">
      <span>共 {listQ.data?.total ?? 0} 项 · {(listQ.data?.offset ?? 0) + (items.length ? 1 : 0)}–{(listQ.data?.offset ?? 0) + items.length}</span>
      <div><button disabled={cursorHistory.length <= 1} onClick={() => setCursorHistory(values => values.slice(0, -1))}>上一页</button>
        <button disabled={!listQ.data?.next_cursor} onClick={() => setCursorHistory(values => [...values, listQ.data!.next_cursor])}>下一页</button></div>
    </footer>}
    {selected != null && run && <><div className="usrs-detail-backdrop" onClick={closeDetail} />
      <DetailDrawer runId={run.run_id} tmId={selected} onClose={closeDetail} /></>}
  </section>;
}

function SortHead({ field, label, capability, active, direction, onSort, rowSpan, className }: {
  field: string; label: string; capability?: UsRightSideSortCapability; active: string;
  direction: UsRightSideSortDirection; onSort: (field: string) => void; rowSpan?: number; className?: string;
}) {
  const isActive = active === field;
  const stickyClass = field === "ticker_name" ? "usrs-sticky-asset" : field === "ticker_symbol" ? "usrs-sticky-code" : "";
  return <th rowSpan={rowSpan} className={[stickyClass, className].filter(Boolean).join(" ") || undefined} aria-sort={isActive ? (direction === "asc" ? "ascending" : "descending") : "none"}>
    <button className="usrs-sort-button" type="button" disabled={!capability?.enabled} title={capability?.blocked_reason ?? `按${label}排序`} onClick={() => onSort(field)}>
      <span>{label}</span><i className="usrs-sort-mark" aria-hidden="true">{isActive ? (direction === "asc" ? "↑" : "↓") : "↕"}</i>
    </button>
  </th>;
}

function AssetRow({ item, onOpen }: { item: UsRightSideAsset; onOpen: (tmId: number) => void }) {
  return <tr tabIndex={0} onClick={() => onOpen(item.tm_id)} onKeyDown={event => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onOpen(item.tm_id); }
  }}>
    <td className="rank usrs-rank" data-label="序号">{item.rank}</td>
    <td className="asset usrs-asset-cell" data-label="资产"><b>{item.ticker_name || item.ticker_symbol}</b><small>{item.ticker_labels.join(" · ") || "趋势动物美股"}</small></td>
    <td className="mono code usrs-code-cell" data-label="代码">{item.ticker_symbol}</td>
    <td data-label="个股温度"><span className={`temperature ${temperatureTone[item.temperature_curr ?? ""] ?? "unknown"}`}>{tempChange(item)}</span></td>
    <td data-label="个股阶段">{value(item, "phase_curr")}</td>
    <td data-label="所属行业">{value(item, "industry_name")}</td>
    <td data-label="行业温度"><span className={`temperature ${temperatureTone[item.industry_temperature_curr ?? ""] ?? "unknown"}`}>{industryValue(item, "temperature_curr")}</span></td>
    <td className="number strong" data-label="行业强度">{industryValue(item, "strength_local_curr", formatStrength)}</td>
    <td data-label="行业节气">{industryValue(item, "phase_curr")}</td>
    <td className="number" data-label="右侧天数">{value(item, "days_since_trend_entry", raw => `${raw} 天`)}</td>
    <td className="number usrs-optional-mid" data-label="右侧涨幅">{value(item, "gain_since_trend_entry", formatPercent)}</td>
    <td className="number strong" data-label="本地强度">{value(item, "strength_local_curr", formatStrength)} <small className="usrs-strength-change">{item.strength_local_change}</small></td>
    <td data-label="风险"><RiskFlags item={item} /></td>
  </tr>;
}

function value(item: UsRightSideAsset, key: string, formatter: (value: string | number) => string = String) {
  const state = item.field_states[key] ?? (item[key as keyof UsRightSideAsset] != null ? "available" : "not_returned");
  const raw = item[key as keyof UsRightSideAsset] as string | number | null;
  return state === "available" && raw != null ? formatter(raw) : <em>{fieldStateLabel[state]}</em>;
}
function industryValue(item: UsRightSideAsset, key: "temperature_curr" | "strength_local_curr" | "phase_curr", formatter: (value: string | number) => string = String) {
  const state = item.industry_field_states[key] ?? "not_requested";
  const raw = item[`industry_${key}` as keyof UsRightSideAsset] as string | null;
  return state === "available" && raw != null ? formatter(raw) : <em>{fieldStateLabel[state]}</em>;
}
function tempChange(item: UsRightSideAsset) {
  const state = item.field_states.temperature_curr;
  if (state !== "available" || !item.temperature_curr) return <em>{fieldStateLabel[state ?? "not_requested"]}</em>;
  return item.temperature_prev ? `${item.temperature_prev}→${item.temperature_curr}` : item.temperature_curr;
}
function RiskFlags({ item }: { item: UsRightSideAsset }) {
  if (item.field_states.risk_flag_count !== "available") return <em>{fieldStateLabel[item.field_states.risk_flag_count ?? "not_requested"]}</em>;
  const labels = [item.danger_flag && "危险", item.boiling_flag && "沸", item.champagne_flag && "开香槟"].filter(Boolean);
  return labels.length ? <span className="risk-flags">{labels.map(label => <i key={String(label)}>{label}</i>)}</span> : <span className="no-risk">无已知触发</span>;
}
function Metric({ label, value: metricValue, note }: { label: string; value: string | number; note: string }) {
  return <article className="usrs-stat"><small>{label}</small><b>{metricValue}</b><span>{note}</span></article>;
}
function FunnelProgress({ run, gate, activeStage, preflightStage }: {
  run: NonNullable<Awaited<ReturnType<typeof getUsRightSideOverview>>["run"]>;
  gate: Gate | null;
  activeStage: Stage | null;
  preflightStage: Stage | null;
}) {
  const ageReady = run.readiness.age ?? run.readiness.standard;
  const strengthReady = run.readiness.strength ?? run.readiness.standard;
  const ageState = ageReady ? "done"
    : activeStage === "age" || run.status === "age_enriching" ? "live"
    : gate?.stage === "age" || preflightStage === "age" ? "ready" : "next";
  const strengthState = strengthReady ? "done"
    : activeStage === "strength" || run.status === "strength_enriching" ? "live"
    : gate?.stage === "strength" || preflightStage === "strength" ? "ready"
    : ageReady ? "next" : "locked";
  const standardState = run.readiness.standard ? "done"
    : activeStage === "standard" || run.status === "standard_enriching" ? "live"
    : gate?.stage === "standard" || preflightStage === "standard" ? "ready"
    : strengthReady ? "next" : "locked";
  const industryState = run.readiness.industry ? "done"
    : activeStage === "industry" || run.status === "industry_enriching" ? "live"
    : gate?.stage === "industry" || preflightStage === "industry" ? "ready"
    : run.readiness.standard ? "next" : "locked";
  const label = (state: string) => ({ done: "已完成", live: "执行中", ready: "待批准", next: "下一步", locked: "未解锁" })[state];
  const ageProgress = run.progress.age ?? {};
  const strengthProgress = run.progress.strength ?? {};
  const standardProgress = run.progress.standard ?? {};
  const industryProgress = run.progress.industry ?? {};

  return <section className="usrs-funnel" aria-label="低成本增强流程">
    <header><small>LOW-COST ENRICHMENT FUNNEL</small><b>30 天强度预筛，再为 Top 100 购买详情</b></header>
    <div className="usrs-funnel-steps">
      <article className={`state-${ageState}`}><i>1</i><div><b>右侧天数初筛</b>
        <span>全部 {run.counts.right_side.toLocaleString()} 只 · 仅 1 个字段</span>
        <small>{label(ageState)} · {ageProgress.completed_batches ?? 0}/{ageProgress.total_batches ?? "—"} 批</small></div></article>
      <article className={`state-${strengthState}`}><i>2</i><div><b>30 天候选强度</b>
        <span>{run.counts.strength_target ?? 0} 只 · 仅 1 个字段</span>
        <small>{label(strengthState)} · {strengthProgress.completed_batches ?? 0}/{strengthProgress.total_batches ?? "—"} 批</small></div></article>
      <article className={`state-${standardState}`}><i>3</i><div><b>Top 100 个股详情</b>
        <span>温度 · 节气 · 行业映射</span>
        <small>{label(standardState)} · {run.counts.standard_target ?? 0}/{run.counts.standard_top_n ?? 100} 只 · {standardProgress.completed_batches ?? 0}/{standardProgress.total_batches ?? "—"} 批</small></div></article>
      <article className={`state-${industryState}`}><i>4</i><div><b>去重行业环境</b>
        <span>只读取 Top 100 涉及的行业</span>
        <small>{label(industryState)} · {run.counts.industries} 个行业 · {industryProgress.completed_batches ?? 0}/{industryProgress.total_batches ?? "—"} 批</small></div></article>
    </div>
  </section>;
}
function ScreenProgress({ run, gate, isPreflighting, isExecuting, fallbackUniverse }: {
  run: NonNullable<Awaited<ReturnType<typeof getUsRightSideOverview>>["run"]> | null;
  gate: Gate | null;
  isPreflighting: boolean;
  isExecuting: boolean;
  fallbackUniverse: number;
}) {
  const screen = run?.progress.screen;
  const completedBatches = screen?.completed_batches ?? 0;
  const totalBatches = screen?.total_batches ?? gate?.batches ?? 0;
  const scanned = run?.counts.scanned ?? 0;
  const universe = run?.counts.universe ?? gate?.rows ?? fallbackUniverse;
  const rawPercent = totalBatches > 0
    ? completedBatches / totalBatches * 100
    : universe > 0 ? scanned / universe * 100 : 0;
  const percent = Math.max(0, Math.min(100, rawPercent));
  const complete = Boolean(run && [
    "screen_ready", "awaiting_age_budget", "age_enriching", "age_partial",
    "awaiting_strength_budget", "strength_enriching", "strength_partial",
    "awaiting_standard_budget", "standard_enriching", "standard_partial",
    "awaiting_industry_budget", "industry_enriching", "ready",
  ].includes(run.status));
  const partial = run?.status === "screen_partial";
  const awaitingBudget = Boolean(gate && !isExecuting);
  const visible = isPreflighting || awaitingBudget || isExecuting || run?.status === "screening" || partial || complete;
  if (!visible) return null;

  const title = isPreflighting ? "正在核算扫描规模与费用"
    : awaitingBudget ? "扫描计划已就绪，等待预算确认"
    : isExecuting || run?.status === "screening" ? "正在生成右侧名单"
    : partial ? "名单生成中断，可从未完成批次继续"
    : "右侧名单生成完成";
  const indeterminate = isPreflighting || ((isExecuting || run?.status === "screening") && totalBatches === 0);
  const displayPercent = complete ? 100 : Math.round(percent);

  return <section className={`usrs-generation-progress${partial ? " is-partial" : ""}${complete ? " is-complete" : ""}`}
    aria-label="名单生成状态" aria-live="polite">
    <div className="usrs-progress-heading">
      <div><small>RIGHT-SIDE SCREEN / 批次进度</small><strong>{title}</strong></div>
      <b>{indeterminate ? "准备中" : `${displayPercent}%`}</b>
    </div>
    <div className={`usrs-progress-track${indeterminate ? " is-indeterminate" : ""}`}
      role="progressbar" aria-label="名单生成进度" aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : displayPercent}>
      <span style={indeterminate ? undefined : { width: `${displayPercent}%` }} />
    </div>
    <div className="usrs-progress-facts">
      <span>批次 <b>{completedBatches} / {totalBatches || "—"}</b></span>
      <span>已扫描 <b>{scanned.toLocaleString()} / {universe ? universe.toLocaleString() : "—"}</b></span>
      <span>右侧 <b>{run?.counts.right_side ?? 0}</b></span>
      <span>状态未知 <b>{run?.counts.unknown ?? 0}</b></span>
      {awaitingBudget && <span className="usrs-progress-note">批准后开始调用上游；当前尚未执行</span>}
      {(isExecuting || run?.status === "screening") && <span className="usrs-progress-note is-live">正在按批次写入，可离开页面后再回来查看</span>}
    </div>
  </section>;
}
function AuditPanel({ run }: { run: NonNullable<Awaited<ReturnType<typeof getUsRightSideOverview>>["run"]> }) {
  const stages: Array<[Stage, string, string | null, string | null]> = [
    ["screen", "右侧扫描", run.cost.estimated_screen_cny, run.cost.actual_screen_cny],
    ["age", "右侧天数", run.cost.estimated_age_cny ?? null, run.cost.actual_age_cny ?? null],
    ["strength", "30 天候选强度", run.cost.estimated_strength_cny ?? null, run.cost.actual_strength_cny ?? null],
    ["standard", "Top 100 详情", run.cost.estimated_standard_cny, run.cost.actual_standard_cny],
    ["industry", "行业环境", run.cost.estimated_industry_cny, run.cost.actual_industry_cny],
    ["deep", "深度字段", run.cost.estimated_deep_cny, run.cost.actual_deep_cny],
  ];
  return <details className="usrs-audit">
    <summary>数据与费用审计 <span>{run.audit?.manifest.available ? "归档已校验" : "归档生成中"}</span></summary>
    <div className="usrs-audit-meta">
      <span>运行 <b>{run.run_id}</b></span><span>缓存 <b>{run.cache_hit ? "命中" : "未命中"}</b></span>
      <span>成员哈希 <b>{run.audit?.cache_key.universe_sha256.slice(0, 12) ?? "—"}</b></span>
      <span>Manifest <b>{run.audit?.manifest.sha256?.slice(0, 12) ?? "—"}</b></span>
    </div>
    <div className="usrs-audit-table-wrap"><table>
      <thead><tr><th>阶段</th><th>接口</th><th>字段数</th><th>进度</th><th>预计费用</th><th>实际费用</th><th>字段哈希</th></tr></thead>
      <tbody>{stages.map(([stage, label, estimated, actual]) => {
        const progress = run.progress[stage] ?? {};
        const fieldSet = run.audit?.field_sets[stage];
        return <tr key={stage}><td>{label}</td><td>getTickerSnapshot</td><td>{fieldSet?.fields?.length ?? 0}</td>
          <td>{progress.completed_batches ?? 0}/{progress.total_batches ?? 0} 批</td>
          <td>{estimated == null ? "未预检" : `¥${Number(estimated).toFixed(4)}`}</td>
          <td>{actual == null ? "待账单" : `¥${Number(actual).toFixed(4)}`}</td>
          <td className="mono">{fieldSet?.sha256?.slice(0, 12) || "—"}</td></tr>;
      })}</tbody>
    </table></div>
  </details>;
}
function stageLabel(stage: Stage) {
  return { screen: "全覆盖右侧筛选", age: "右侧天数初筛", strength: "30 天候选强度", standard: "Top 100 个股详情", industry: "去重行业环境", deep: "全量深度字段" }[stage];
}
function formatNumber(raw: string | number) { return Number(raw).toLocaleString("en-US", { maximumFractionDigits: 2 }); }
function formatCompact(raw: string | number) { return Number(raw).toLocaleString("en-US", { maximumFractionDigits: 1 }); }
function formatStrength(raw: string | number) { return Number(raw).toFixed(1); }
function formatPercent(raw: string | number) {
  const number = Number(raw) * 100;
  return `${number >= 0 ? "+" : ""}${number.toFixed(1)}%`;
}

function DetailDrawer({ runId, tmId, onClose }: { runId: string; tmId: number; onClose: () => void }) {
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeButton.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);
  const detailQ = useQuery({ queryKey: ["us-right-side-detail", runId, tmId], queryFn: () => getUsRightSideAsset(runId, tmId) });
  const plot = useMutation({ mutationFn: () => getUsRightSidePlot(runId, tmId) });
  const item = detailQ.data?.asset;
  const detailRun = detailQ.data?.run;
  return <aside className="usrs-drawer usrs-detail-drawer" role="dialog" aria-modal="true" aria-label="美股右侧证据">
    <button ref={closeButton} className="close usrs-detail-close" aria-label="关闭详情" onClick={onClose}>×</button>
    {detailQ.isLoading ? <div className="usrs-empty">读取证据…</div> : !item ? <div className="usrs-empty error">详情读取失败</div> : <>
      <small>RIGHT-SIDE EVIDENCE / {item.as_of_date}</small><h2>{item.ticker_name || item.ticker_symbol}</h2><p className="ticker mono">{item.ticker_symbol} · tmId {item.tm_id}</p>
      <section><h3>身份与时效</h3><dl>
        <dt>资产类型</dt><dd>{item.asset}</dd><dt>币种</dt><dd>{item.currency_default ?? "接口未返回"}</dd>
        <dt>趋势数据日</dt><dd>{item.as_of_date}</dd><dt>上游更新时间</dt><dd>{detailRun?.upstream_update_dt ?? "未记录"}</dd>
        <dt>可交易标志</dt><dd>{item.tradable_flag == null ? "接口未返回" : item.tradable_flag ? "可交易" : "不可交易"}</dd>
        <dt>行业映射</dt><dd>{item.industry_tm_id == null ? "行业映射未返回" : `${item.industry_name ?? "未命名"} · ${item.industry_tm_id}`}</dd>
      </dl></section>
      <section><h3>右侧事实</h3><dl><dt>右侧状态</dt><dd>是</dd><dt>右侧自然日</dt><dd>{item.days_since_trend_entry ?? "接口未返回"}</dd><dt>进入后涨幅</dt><dd>{
        item.field_states.gain_since_trend_entry === "available" && item.gain_since_trend_entry != null
          ? formatPercent(item.gain_since_trend_entry)
          : fieldStateLabel[item.field_states.gain_since_trend_entry ?? "not_requested"]
      }</dd><dt>当前节气</dt><dd>{item.phase_curr ?? "接口未返回"}</dd><dt>前期节气</dt><dd>未纳入首版字段集</dd></dl></section>
      <section><h3>趋势相对强度</h3><dl>
        <dt>当前本地强度</dt><dd>{item.strength_local_curr == null ? "接口未返回" : formatStrength(item.strength_local_curr)}</dd>
        <dt>强度变化</dt><dd>{item.strength_local_change ?? fieldStateLabel[item.field_states.strength_local_change ?? "not_requested"]}</dd>
        <dt>周/月前值</dt><dd>未纳入首版字段集</dd><dt>全局强度</dt><dd>未纳入首版字段集</dd>
      </dl></section>
      <section><h3>行业背景</h3><div className="comparison">
        <b>指标</b><b>个股 · {item.as_of_date}</b><b>行业 · {item.industry_as_of_date ?? "未请求"}</b>
        <span>右侧</span><strong>是</strong><strong>{item.industry_is_right_side == null ? "未请求" : item.industry_is_right_side ? "是" : "否"}</strong>
        <span>温度</span><strong>{item.temperature_curr ?? "接口未返回"}</strong><strong>{item.industry_temperature_curr ?? "接口未返回"}</strong>
        <span>强度</span><strong>{item.strength_local_curr == null ? "接口未返回" : formatStrength(item.strength_local_curr)}</strong><strong>{item.industry_strength_local_curr == null ? "接口未返回" : formatStrength(item.industry_strength_local_curr)}</strong>
        <span>节气</span><strong>{item.phase_curr ?? "接口未返回"}</strong><strong>{item.industry_field_states.phase_curr === "not_applicable" ? "非右侧，不适用" : item.industry_phase_curr ?? "接口未返回"}</strong>
      </div><p className="usrs-source-note">个股来源：同日 getTickerSnapshot；行业来源：{item.industry_source_method ?? "未请求"}。这里只展示事实差异，不生成买卖结论。</p></section>
      <section><h3>行情参考</h3><dl>
        <dt>趋势动物参考价</dt><dd>{item.price_index == null ? "接口未返回" : formatNumber(item.price_index)}</dd>
        <dt>市值</dt><dd>{item.market_cap == null ? fieldStateLabel[item.field_states.market_cap ?? "not_requested"] : `${formatCompact(item.market_cap)} 亿美元`}</dd>
        <dt>1日成交额</dt><dd>{item.amount_1d == null ? fieldStateLabel[item.field_states.amount_1d ?? "not_requested"] : `${formatCompact(item.amount_1d)} 亿美元`}</dd>
        <dt>1月收益</dt><dd>{item.return_1m == null ? fieldStateLabel[item.field_states.return_1m ?? "not_requested"] : formatPercent(item.return_1m)}</dd>
      </dl></section>
      <section><h3>关注与风险</h3><dl><dt>7日浏览热度</dt><dd>{item.heat_score_7d == null ? fieldStateLabel[item.field_states.heat_score_7d ?? "not_requested"] : formatStrength(item.heat_score_7d)}</dd>
        <dt>风险标志</dt><dd><RiskFlags item={item} /></dd><dt>标签</dt><dd>{item.ticker_labels.length ? item.ticker_labels.join(" · ") : fieldStateLabel[item.field_states.ticker_labels ?? "not_requested"]}</dd>
      </dl></section>
      <section><h3>来源与费用</h3><dl>
        <dt>个股来源</dt><dd>getTickerSnapshot</dd><dt>行业来源</dt><dd>{item.industry_source_method ?? "未请求"}</dd>
        <dt>本数据日累计费用</dt><dd>{detailRun ? `¥${Number(detailRun.cost.actual_total_cny ?? 0).toFixed(4)}` : "读取中"}</dd>
        <dt>缓存键</dt><dd className="mono">{detailRun?.audit?.cache_key.universe_sha256.slice(0, 12) ?? "未记录"}</dd>
      </dl></section>
      <section><h3>趋势图</h3>{plot.data?.image ? <img alt={`${item.ticker_symbol} 趋势图`} src={plot.data.image.startsWith("data:") ? plot.data.image : `data:image/png;base64,${plot.data.image}`} />
        : <button className="plot" onClick={() => plot.mutate()} disabled={plot.isPending}>{plot.isPending ? "生成中…" : "生成趋势图 · 预计 ¥0.10"}</button>}</section>
      <footer>趋势动物指标仅用于趋势研究与纪律复核，不构成投资建议或仓位指令。</footer>
    </>}
  </aside>;
}
