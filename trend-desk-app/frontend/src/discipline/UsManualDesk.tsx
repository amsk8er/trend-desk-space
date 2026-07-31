import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  collectUsManualRun,
  confirmUsAccountOcr,
  confirmUsExecution,
  createUsAllocationPreview,
  createUsExitDecisionPlan,
  createUsManualPlan,
  getLlmConfig,
  getUsAccountOcr,
  getUsManualOverview,
  getUsManualPlans,
  importUsAccountScreenshots,
  lockUsManualPlan,
  markUsRunNoExecution,
  previewUsExecution,
  refreshUsRiskAnchor,
  type UsAllocationPreview,
  type UsAccountOcrBatch,
  type UsAccountState,
  type UsCandidate,
  type UsExecutionSchedule,
  type UsManualPlan,
  type UsManualPlanItem,
  type UsManualOverview,
  type UsPositionAction,
  type UsRiskAnchorStatus,
} from "../api";
import "./us-manual.css";

const money = (value: string | null | undefined, suffix = " USDT") => value == null ? "—" : `${trimDecimal(value)}${suffix}`;
const trimDecimal = (value: string) => {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("zh-CN", { maximumFractionDigits: 8 }) : value;
};
const oneDecimal = (value: string | null | undefined) => {
  if (value == null || value.trim() === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(1) : "—";
};
const percent = (value: string | null | undefined) => {
  if (value == null) return "—";
  const number = Number(value) * 100;
  return Number.isFinite(number) ? `${number.toFixed(2)}%` : "—";
};
const idempotencyKey = () => globalThis.crypto?.randomUUID?.() ?? `manual-${Date.now()}-${Math.random()}`;
const beijingTimestamp = (value: string) => value ? `${value}:00+08:00` : "";
const zonedTimestamp = (value: string | null | undefined, timeZone: string) => {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(parsed);
};

function errorText(error: unknown) {
  return error instanceof Error ? error.message : "请求失败，请检查证据与网络后重试";
}

function nowLocalInput() {
  const parts = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const value = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}T${value.hour}:${value.minute}`;
}

export default function UsManualDesk() {
  const qc = useQueryClient();
  const overviewQ = useQuery({ queryKey: ["us-manual-overview"], queryFn: getUsManualOverview, retry: false });
  const plansQ = useQuery({ queryKey: ["us-manual-plans"], queryFn: getUsManualPlans, retry: false });
  const [selectedCandidateId, setSelectedCandidateId] = useState<number | null>(null);
  const [allocation, setAllocation] = useState<UsAllocationPreview | null>(null);
  const [exclusionReason, setExclusionReason] = useState<Record<number, string>>({});
  const [executionItemId, setExecutionItemId] = useState<number | null>(null);
  const [executionPrice, setExecutionPrice] = useState("");
  const [executionQuantity, setExecutionQuantity] = useState("");
  const [executionFee, setExecutionFee] = useState("0");
  const [executionAt, setExecutionAt] = useState(nowLocalInput());
  const [executionPreview, setExecutionPreview] = useState<Record<string, unknown> | null>(null);
  const [executionKey, setExecutionKey] = useState("");

  const overview = overviewQ.data;
  const run = overview?.run ?? null;
  const candidates = overview?.candidates ?? [];
  const currentRules = !!run && run.rules_version === overview?.capabilities.rules_version && !run.legacy_read_only;
  const selected = selectedCandidateId == null ? null : candidates.find(row => row.candidate_id === selectedCandidateId) ?? null;
  const readyCandidates = useMemo(() => candidates
    .filter(row => (row.asset_type === "stock" || row.asset_type === "etf") && row.screen_status === "ready")
    .sort((left, right) => (left.observation_rank ?? Number.MAX_SAFE_INTEGER) - (right.observation_rank ?? Number.MAX_SAFE_INTEGER)
      || Number(right.strength_local ?? "-999999") - Number(left.strength_local ?? "-999999")
      || left.ticker_symbol.localeCompare(right.ticker_symbol)), [candidates]);
  const auditCandidates = useMemo(() => candidates.filter(row => row.screen_status !== "ready"), [candidates]);
  const positions = overview?.positions ?? [];
  const activePlan = plansQ.data?.find(plan =>
    plan.run_id === run?.run_id
    && !plan.legacy_read_only
    && !["completed", "no_execution"].includes(plan.status)
  ) ?? null;
  const funnel = run?.funnel ?? {};
  const mode = overview?.capabilities.h6_mode ?? "off";
  const environmentAllowsNewBuys = Number(overview?.market_environment?.environment_factor ?? "0") > 0;

  useEffect(() => {
    setAllocation(null);
    setExclusionReason({});
  }, [run?.run_id]);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["us-manual-overview"] });
    void qc.invalidateQueries({ queryKey: ["us-manual-plans"] });
  };
  const collect = useMutation({ mutationFn: collectUsManualRun, onSuccess: refresh });
  const riskRefresh = useMutation({
    mutationFn: (candidateId: number) => refreshUsRiskAnchor(candidateId),
    onSuccess: refresh,
  });
  const allocationMutation = useMutation({
    mutationFn: (payload: Parameters<typeof createUsAllocationPreview>[0]) => createUsAllocationPreview(payload),
    onSuccess: setAllocation,
  });
  const planMutation = useMutation({
    mutationFn: () => createUsManualPlan({ allocation_preview_id: allocation!.allocation_preview_id }),
    onSuccess: () => { setAllocation(null); refresh(); },
  });
  const lockMutation = useMutation({ mutationFn: (planId: string) => lockUsManualPlan(planId), onSuccess: refresh });
  const noTrade = useMutation({ mutationFn: () => markUsRunNoExecution(run!.run_id), onSuccess: refresh });
  const exitPlan = useMutation({ mutationFn: (decisionId: string) => createUsExitDecisionPlan(decisionId), onSuccess: refresh });
  const previewExecution = useMutation({
    mutationFn: (itemId: number) => previewUsExecution(itemId, {
      price_usdt: executionPrice, quantity: executionQuantity, fee_usdt: executionFee,
      trade_date: executionAt.slice(0, 10), executed_at: beijingTimestamp(executionAt),
    }),
    onSuccess: preview => { setExecutionPreview(preview); setExecutionKey(idempotencyKey()); },
  });
  const confirmExecution = useMutation({
    mutationFn: (itemId: number) => confirmUsExecution(itemId, {
      price_usdt: executionPrice, quantity: executionQuantity, fee_usdt: executionFee,
      trade_date: executionAt.slice(0, 10), executed_at: beijingTimestamp(executionAt),
      idempotency_key: executionKey,
    }),
    onSuccess: () => { setExecutionPreview(null); refresh(); },
  });

  const openExecution = (item: UsManualPlanItem) => {
    setExecutionItemId(item.item_id);
    setExecutionPrice(item.entry_reference_price ?? "");
    setExecutionQuantity(item.target_quantity ?? "");
    setExecutionFee("0");
    setExecutionAt(nowLocalInput());
    setExecutionPreview(null);
    setExecutionKey("");
  };
  const generateAllocation = () => run && allocationMutation.mutate({ run_id: run.run_id });
  const excludeAllocation = (candidateId: number, backfill: boolean) => {
    if (!run || !allocation) return;
    allocationMutation.mutate({
      run_id: run.run_id,
      base_preview_id: allocation.allocation_preview_id,
      exclusions: [{ candidate_id: candidateId, reason: exclusionReason[candidateId]?.trim() ?? "" }],
      backfill,
    });
  };

  return <section className="us-manual-page" data-testid="us-manual-page">
    <header className="us-manual-topbar">
      <div>
        <small>US MANUAL / {overview?.capabilities.rules_version?.toUpperCase() ?? "LOADING"}</small>
        <h1>美股手工执行台</h1>
        <p>把趋势、资金、清单和持仓放在同一屏。系统只读取你明确确认的账户截图，不接 Bitget 私有账户，也不会下单。</p>
      </div>
      <div className="us-manual-only"><b>人工执行</b><span>系统生成清单 · 用户成交回填</span></div>
    </header>

    {overviewQ.isLoading && <div className="us-state">正在读取 H6 证据和人工台账…</div>}
    {overviewQ.isError && <ErrorBox error={overviewQ.error} />}
    {overview && <>
      <ExecutionStateStrip overview={overview} funnel={funnel} />

      <section className="us-panel us-action-panel us-action-compact">
        <div className="us-panel-head"><div><small>NEXT ACTION</small><h2>{overview.next_step.title}</h2></div><span>{run?.as_of_date ? `信号日 ${run.as_of_date}` : "等待更新"}</span></div>
        <p className="us-next-detail">{overview.next_step.detail}</p>
        <div className="us-action-buttons">
          <button data-testid="us-collect" className="desk-button cyan" onClick={() => collect.mutate()} disabled={collect.isPending}>
            {collect.isPending ? "正在采集，请勿重复点击" : "立即采集今日温转热清单"}
          </button>
          {currentRules && ["ready", "ready_degraded"].includes(run?.status ?? "") && <button data-testid="us-allocation-preview" className="desk-button lime" onClick={generateAllocation} disabled={allocationMutation.isPending || mode !== "active" || !environmentAllowsNewBuys}>
            {allocationMutation.isPending ? "正在计算容量" : "生成今日推荐分配"}
          </button>}
        </div>
        {mode !== "active" && <p className="us-execution-gate">当前为 {mode}，清单可查看，但买入分配尚未启用。技术验证详情已移至“数据与规则”。</p>}
        {!environmentAllowsNewBuys && overview.market_environment && <p className="us-warning">当前整体温度为 {overview.market_environment.market_temperature}，环境系数为 0：今日不新增仓位。</p>}
        {collect.isError && <ErrorBox error={collect.error} />}
        {allocationMutation.isError && <ErrorBox error={allocationMutation.error} />}
        {currentRules && ["ready", "ready_degraded"].includes(run?.status ?? "") && mode === "active" && (!environmentAllowsNewBuys || !readyCandidates.some(row => row.risk_anchor?.planning_enabled)) && <div className="us-no-trade"><b>暂无可分配候选</b><p>系统不会降低环境、强度、日期、ETF 证据或锚点要求。</p><button data-testid="us-no-trade" className="desk-button pink" onClick={() => noTrade.mutate()} disabled={noTrade.isPending}>{noTrade.isPending ? "记录中" : "确认今日不交易"}</button></div>}
      </section>

      {allocation && <AllocationCard preview={allocation} reasons={exclusionReason} setReasons={setExclusionReason} onExclude={excludeAllocation} planMutation={planMutation} />}

      <section className="us-panel us-candidates">
        <div className="us-panel-head"><div><small>CURRENT LIST</small><h2>今日筛选清单 · 强度降序</h2></div><span>{readyCandidates.length} 只通过门槛</span></div>
        {!candidates.length ? <p className="us-empty">暂无当日证据。北京时间 07:00 前手动按钮只做免费更新检查。</p> : <div className="us-candidate-list">
          {!readyCandidates.length && <p className="us-empty">当前没有同时满足 H6 门槛的个股或 ETF；系统不会自动放宽条件。</p>}
          {readyCandidates.map(row => <CandidateRow key={row.candidate_id} candidate={row} onOpen={() => setSelectedCandidateId(row.candidate_id)} />)}
          {!!auditCandidates.length && <details className="us-candidate-audit"><summary>拒绝清单（{auditCandidates.length}）</summary>{auditCandidates.map(row => <CandidateRow key={row.candidate_id} candidate={row} onOpen={() => setSelectedCandidateId(row.candidate_id)} />)}</details>}
        </div>}
      </section>

      {activePlan && <PlanCard plan={activePlan} activeRulesVersion={overview.capabilities.rules_version} lockMutation={lockMutation} selectedItemId={executionItemId} onSelectItem={openExecution} executionPrice={executionPrice} setExecutionPrice={setExecutionPrice} executionQuantity={executionQuantity} setExecutionQuantity={setExecutionQuantity} executionFee={executionFee} setExecutionFee={setExecutionFee} executionAt={executionAt} setExecutionAt={setExecutionAt} executionPreview={executionPreview} previewMutation={previewExecution} confirmMutation={confirmExecution} />}

      <ExitDesk positions={positions} account={overview.account} mutation={exitPlan} />
      <AccountOcrPanel overview={overview} onRefresh={refresh} />
    </>}

    {selected && <EvidenceDrawer candidate={selected} currentRules={currentRules} mode={mode} riskRefresh={riskRefresh} onClose={() => setSelectedCandidateId(null)} />}
  </section>;
}

function ExecutionStateStrip({ overview, funnel }: { overview: UsManualOverview; funnel: Record<string, unknown> }) {
  const environment = overview.market_environment;
  const capacity = overview.capacity;
  const account = overview.account;
  const warmStocks = Number(funnel.warm_to_hot_stocks ?? overview.combos.warm_to_hot_stocks ?? 0);
  const warmEtfs = Number(funnel.warm_to_hot_etfs ?? overview.combos.warm_to_hot_etfs ?? 0);
  return <section className="us-execution-state" aria-label="美股交易状态概览">
    <article className="cyan"><small>MARKET TREND</small><b>{environment?.market_temperature ?? "待更新"}</b><span>整体强度 {oneDecimal(environment?.market_strength_local == null ? null : String(environment.market_strength_local))}</span><em>{environment?.market_phase ?? "节气待更新"}{environment?.market_labels?.length ? ` · ${environment.market_labels.join(" · ")}` : ""}</em></article>
    <article className="yellow"><small>POSITION FACTOR</small><b>{environment ? `${Number(environment.environment_factor) * 100}%` : "—"}</b><span>今日新增上限 {money(capacity?.daily_limit_usdt)}</span><em>当前可分配 {money(capacity?.available_usdt)}</em></article>
    <article className="lime"><small>ACCOUNT</small><b>{money(account.reported_equity_usdt ?? account.starting_equity_usdt)}</b><span>可用现金 {money(account.cash_usdt)}</span><em>持仓成本 {money(account.open_cost_usdt)} · {account.position_count}/20</em></article>
    <article className="orange"><small>WARM → HOT TODAY</small><b>{warmStocks + warmEtfs}</b><span>个股 {warmStocks} · ETF {warmEtfs}</span><em>通过筛选 {overview.candidates.filter(row => row.screen_status === "ready").length} 只</em></article>
  </section>;
}

function ExitDesk({ positions, account, mutation }: { positions: UsPositionAction[]; account: UsAccountState; mutation: { mutate: (id: string) => void; isPending: boolean; isError: boolean; error: unknown } }) {
  const actionable = positions.filter(row => row.decision && ["exit_all", "reduce_25", "reduce_50"].includes(row.action));
  const reviews = positions.filter(row => row.action === "manual_review");
  const localTickers = new Set(positions.map(row => row.lot.ticker_symbol));
  const external = account.open_positions.filter(row => !localTickers.has(row.ticker_symbol));
  return <section className={`us-panel us-exit-desk ${actionable.length || reviews.length ? "attention" : ""}`} data-testid="us-exit-desk">
    <div className="us-panel-head"><div><small>CURRENT HOLDINGS / EXIT FIRST</small><h2>当前持仓与退出动作</h2></div><span>{account.position_count} 只 · {actionable.length} 个动作</span></div>
    {!positions.length && !external.length && <p className="us-empty">当前账户没有已确认持仓。</p>}
    {positions.map(row => <div className="us-position-row" key={row.lot.lot_id}>
      <span><b>{row.lot.ticker_symbol}</b><small>{row.lot.remaining_quantity} 股 · {row.lot.asset_type ?? "stock"}</small></span>
      <span><b>{exitActionText(row.action)}</b><small>{row.decision ? row.decision.reason_codes.map(exitReasonText).join(" · ") : exitReasonText(row.reason)}</small></span>
      <span><b>{row.decision ? `${row.decision.planned_quantity} 股` : "—"}</b><small>{row.decision ? scheduleOpenText(row.decision.execution_schedule, row.decision.intended_execution_date) : row.notice}</small></span>
      {row.decision && actionable.includes(row) && <button className="desk-button pink" onClick={() => mutation.mutate(row.decision!.decision_id)} disabled={mutation.isPending}>{mutation.isPending ? "生成中" : "生成手工卖出清单"}</button>}
    </div>)}
    {external.map(row => <div className="us-position-row external" key={`ocr-${row.ticker_symbol}`}>
      <span><b>{row.ticker_symbol}</b><small>{row.quantity} 股 · 账户截图</small></span>
      <span><b>待与纪律台账核对</b><small>{reconciliationText(row.reconciliation_status)}</small></span>
      <span><b>{money(row.cost_usdt)}</b><small>未建立成交 lot，不能自动生成退出动作</small></span>
    </div>)}
    {account.reconciliation_status === "review_required" && <p className="us-reconcile-warning">账户截图与成交台账尚未完全一致；容量按更保守的一侧计算。</p>}
    {account.ocr_snapshot_stale && <p className="us-reconcile-warning">账户截图确认后已有新成交，请重新上传最新持仓截图。</p>}
    <footer>全清：危险或温度转平及以下；减仓：沸、开香槟各减当次剩余的 25%，同日可叠加。锚点破位、持仓第 5/10 日和强度下降均不是退出条件。</footer>
    {mutation.isError && <ErrorBox error={mutation.error} />}
  </section>;
}

function AccountOcrPanel({ overview, onRefresh }: { overview: UsManualOverview; onRefresh: () => void }) {
  const latest = overview.latest_account_ocr;
  const llmQ = useQuery({ queryKey: ["llm-config"], queryFn: getLlmConfig, retry: false });
  const [batchId, setBatchId] = useState<string | null>(latest?.batch_id ?? null);
  const [files, setFiles] = useState<File[]>([]);
  const [captureDate, setCaptureDate] = useState(nowLocalInput().slice(0, 10));
  const [backend, setBackend] = useState("");
  const [equity, setEquity] = useState("");
  const [cash, setCash] = useState("");
  const [currency, setCurrency] = useState("USDT");
  const [scopeConfirmed, setScopeConfirmed] = useState(false);
  const [emptyConfirmed, setEmptyConfirmed] = useState(false);
  const [loadedBatch, setLoadedBatch] = useState<string | null>(null);
  const batchQ = useQuery({
    queryKey: ["us-account-ocr", batchId],
    queryFn: () => getUsAccountOcr(batchId!),
    enabled: !!batchId,
    retry: false,
    refetchInterval: query => query.state.data?.status === "running" ? 1200 : false,
  });
  const batch: UsAccountOcrBatch | null = batchQ.data ?? (
    latest?.batch_id === batchId ? latest : null
  );

  useEffect(() => {
    if (!batchId && latest?.batch_id) setBatchId(latest.batch_id);
  }, [batchId, latest?.batch_id]);
  useEffect(() => {
    if (!batch || batch.status === "running" || loadedBatch === batch.batch_id) return;
    setEquity(batch.account.equity_usdt ?? "");
    setCash(batch.account.cash_usdt ?? "");
    setCurrency(batch.account.currency ?? "USDT");
    setScopeConfirmed(false);
    setEmptyConfirmed(false);
    setLoadedBatch(batch.batch_id);
  }, [batch, loadedBatch]);

  const upload = useMutation({
    mutationFn: () => importUsAccountScreenshots(
      captureDate, files, backend || llmQ.data?.backend,
    ),
    onSuccess: value => {
      setBatchId(value.batch_id);
      setLoadedBatch(null);
      setFiles([]);
    },
  });
  const confirm = useMutation({
    mutationFn: () => confirmUsAccountOcr(batch!.batch_id, {
      equity_usdt: equity,
      cash_usdt: cash,
      currency,
      full_snapshot_confirmed: scopeConfirmed,
      confirmed_no_positions: !batch!.rows.length && emptyConfirmed,
      idempotency_key: idempotencyKey(),
    }),
    onSuccess: () => {
      void batchQ.refetch();
      onRefresh();
    },
  });
  const provider = backend || llmQ.data?.backend || "";
  const hasReview = !!batch && (
    batch.conflicts.length > 0
    || batch.rows.some(row => row.status !== "ready" || row.errors.length > 0)
  );
  const canConfirm = batch?.status === "ready" && !!equity && !!cash && currency === "USDT"
    && scopeConfirmed && !hasReview && (batch.rows.length > 0 || emptyConfirmed);

  return <section className="us-panel us-account-ocr" data-testid="us-account-ocr">
    <div className="us-panel-head"><div><small>ACCOUNT SNAPSHOT OCR</small><h2>补充 Bitget 持仓账户信息</h2></div><span>{batch ? accountOcrStatusText(batch.status) : "尚未上传"}</span></div>
    <div className="us-ocr-upload">
      <label>截图日期（北京时间）<input type="date" value={captureDate} onChange={event => setCaptureDate(event.target.value)} /></label>
      <label>OCR 引擎<select value={provider} onChange={event => setBackend(event.target.value)}>{(llmQ.data?.choices ?? []).map(choice => <option value={choice} key={choice}>{llmQ.data?.providers?.[choice]?.label ?? choice}</option>)}</select></label>
      <label className="us-file-picker">选择完整账户截图<input data-testid="us-account-files" type="file" multiple accept="image/png,image/jpeg,image/webp,image/heic,image/heif" onChange={event => setFiles(Array.from(event.target.files ?? []))} /><span>{files.length ? `已选 ${files.length} 张` : "最多 5 张"}</span></label>
      <button className="desk-button cyan" data-testid="us-account-upload" disabled={!captureDate || !files.length || upload.isPending} onClick={() => upload.mutate()}>{upload.isPending ? "正在上传" : "识别账户截图"}</button>
    </div>
    <p className="us-ocr-boundary">先预览、再确认。截图只补充当前账户和外部持仓占用，不会伪造成交、自动买卖或替代趋势退出纪律。</p>
    {batch?.status === "running" && <p className="us-ocr-progress">正在识别 {batch.processed_image_count}/{batch.image_count} 张；可以离开此页，结果会保存在数据库。</p>}
    {batch?.error && <div className="us-error">{batch.error.message}</div>}
    {batch && batch.status !== "running" && <>
      <div className="us-ocr-account-fields">
        <label>账户总额（USDT）<input inputMode="decimal" value={equity} onChange={event => setEquity(event.target.value)} /></label>
        <label>可用现金（USDT）<input inputMode="decimal" value={cash} onChange={event => setCash(event.target.value)} /></label>
        <label>币种<select value={currency} onChange={event => setCurrency(event.target.value)}><option value="USDT">USDT</option><option value="">未确认</option></select></label>
      </div>
      <div className="us-ocr-rows"><header><b>持仓识别预览</b><span>{batch.rows.length} 项</span></header>{!batch.rows.length && <p>没有识别到持仓。</p>}{batch.rows.map(row => <div className={row.status} key={row.row_id}>
        <span><b>{row.ticker_symbol ?? "代码缺失"}</b><small>{row.ticker_name ?? row.venue_instrument ?? "—"}</small></span>
        <span><b>{row.quantity ?? "—"} 股</b><small>成本 {money(row.average_cost_usdt)}</small></span>
        <span><b>{row.market_value_usdt ? money(row.market_value_usdt) : "市值未显示"}</b><small>{row.errors.join(" · ") || "字段通过"}</small></span>
      </div>)}</div>
      {!!batch.conflicts.length && <div className="us-reconcile-warning">识别结果存在多图冲突或失败图片，请补充清晰完整截图后重新识别；本批不能确认。</div>}
      {batch.status === "ready" && <div className="us-ocr-confirm">
        <label><input type="checkbox" checked={scopeConfirmed} onChange={event => setScopeConfirmed(event.target.checked)} />我确认这些截图覆盖的是完整 Bitget 测试子账户，而不是局部列表</label>
        {!batch.rows.length && <label><input type="checkbox" checked={emptyConfirmed} onChange={event => setEmptyConfirmed(event.target.checked)} />我确认当前账户确实没有持仓</label>}
        <button data-testid="us-account-confirm" className="desk-button lime" disabled={!canConfirm || confirm.isPending} onClick={() => confirm.mutate()}>{confirm.isPending ? "正在确认" : "确认账户快照"}</button>
      </div>}
      {batch.status === "confirmed" && <p className="us-ocr-confirmed">账户快照已确认。可用现金和持仓席位已按台账与截图中更保守的一侧更新。</p>}
    </>}
    {(upload.isError || batchQ.isError || confirm.isError || llmQ.isError) && <ErrorBox error={upload.error ?? batchQ.error ?? confirm.error ?? llmQ.error} />}
  </section>;
}

function CandidateRow({ candidate, onOpen }: { candidate: UsCandidate; onOpen: () => void }) {
  const isEtf = candidate.asset_type === "etf" || candidate.asset_type === "etf_observation";
  return <button className={`us-candidate-row ${candidate.screen_status} anchor-${candidate.risk_anchor_status ?? "pending"}`} onClick={onOpen}>
    <span><b>{candidate.observation_rank ? `#${candidate.observation_rank} ` : ""}{candidate.ticker_symbol}{isEtf ? " · ETF" : ""}</b><small>{candidate.ticker_name ?? "—"}</small></span>
    <span><b>趋势相对强度 {oneDecimal(candidate.strength_local)}</b><small>节气 {candidate.trend_phase_curr ?? "—"}</small></span>
    <span><b>{isEtf ? `ETF 基准：${benchmarkStatusText(candidate.benchmark_status)}` : `${candidate.industry_name ?? "板块待补充"} · ${candidate.industry_temperature_curr ?? "—"}`}</b><small>右侧 {candidate.right_side_calendar_days ?? "—"} 天 · {candidate.ticker_labels?.join(" · ") || "无标签"}</small></span>
    <span><b>筛选：{selectionStatusText(candidate.screen_status)}</b><small>风险锚点：{riskStatusText(candidate.risk_anchor_status ?? "pending")}</small></span>
    <i>查看证据 →</i>
  </button>;
}

function EvidenceDrawer({ candidate, currentRules, mode, riskRefresh, onClose }: {
  candidate: UsCandidate;
  currentRules: boolean;
  mode: "off" | "shadow" | "active";
  riskRefresh: { mutate: (id: number) => void; isPending: boolean; isError: boolean; error: unknown };
  onClose: () => void;
}) {
  const isEtf = candidate.asset_type === "etf" || candidate.asset_type === "etf_observation";
  const anchor = candidate.risk_anchor;
  const etfEvidence = candidate.etf_benchmark_evidence;
  const planEligible = currentRules && !candidate.legacy_read_only && ["stock", "etf"].includes(candidate.asset_type) && candidate.screen_status === "ready";
  return <aside className="us-evidence-drawer" role="dialog" aria-modal="true" aria-label={`${candidate.ticker_symbol} 证据`} data-testid="us-evidence-drawer">
    <button className="us-close" aria-label="关闭证据抽屉" onClick={onClose}>×</button>
    <small>H6 IMMUTABLE EVIDENCE</small><h2>{candidate.ticker_symbol} <span>{candidate.ticker_name}</span></h2>
    <div className={`us-stop-badge ${candidate.risk_anchor_status}`}>{riskStatusText(candidate.risk_anchor_status)}</div>

    <h3>选股证据</h3>
    <dl><dt>信号</dt><dd>当日温转热组合直接成分</dd><dt>右侧自然日</dt><dd>{candidate.right_side_calendar_days ?? "—"}（门槛 1–9）</dd><dt>相对趋势强度</dt><dd>{oneDecimal(candidate.strength_local)}（门槛 ≥90）</dd><dt>所属板块</dt><dd>{isEtf ? "ETF 不适用" : candidate.industry_name ?? "—"}</dd><dt>板块温度</dt><dd>{isEtf ? "ETF 不设板块温度门槛" : candidate.industry_temperature_curr ?? "—"}</dd><dt>标签</dt><dd>{candidate.ticker_labels?.join(" · ") || "无标签"}</dd><dt>节气</dt><dd>{candidate.trend_phase_curr ?? "—"}</dd><dt>筛选结果</dt><dd>{candidate.all_reasons?.map(reasonText).join(" · ") || reasonText(candidate.primary_reason)}</dd></dl>

    {isEtf && <><h3>ETF 跟踪指数</h3><p className="us-detail-route">用途：确认 ETF 跟踪哪个指数，避免同一指数重复持仓；只要权威来源唯一确认跟踪指数即可进入计划。</p><dl><dt>自动核验状态</dt><dd>{benchmarkStatusText(candidate.benchmark_status)}</dd><dt>跟踪指数</dt><dd>{etfEvidence?.benchmark_canonical_name ?? etfEvidence?.benchmark_name_raw ?? "—"}</dd><dt>方向/杠杆</dt><dd>{etfEvidence ? `${etfEvidence.exposure_direction ?? "仅观察"} · ${etfEvidence.leverage_multiplier ? `${etfEvidence.leverage_multiplier}x` : "仅观察"}` : "—"}</dd><dt>去重结果</dt><dd>{candidate.benchmark_family_id ? "已按跟踪指数形成去重键" : "跟踪指数证据不足，失败关闭"}</dd></dl><p className="us-detail-route">SEC 身份和权威来源由每日采集自动核验；失败原因与可选观察字段已移至“数据与规则”，无需手工点击。</p></>}

    <h3>Bitget EP3 风险锚点</h3>
    {anchor ? <dl className="us-source-evidence"><dt>Bitget 公共参考价</dt><dd>{money(anchor.quote_usdt)}<small>{anchor.quote_at ? `${anchor.quote_at} UTC` : "时间缺失"}</small></dd><dt>前期重要低点</dt><dd>{money(anchor.anchor_price_usdt)}</dd><dt>低点日期</dt><dd>{anchor.anchor_date ?? "—"}（XNYS 交易日）</dd><dt>锚点距离</dt><dd>{percent(anchor.anchor_distance)}</dd><dt>可用于分配</dt><dd>{anchor.planning_enabled ? "是" : "否"}</dd><dt>异常</dt><dd>{anchor.error_message ?? "无"}</dd></dl> : <p className="us-empty">尚未生成 Bitget 1D、1H 与公开报价的 EP3 证据。</p>}
    <p className="us-detail-route">1D/1H 连续性、摆动低点确认过程、哈希和算法版本已移至“数据与规则”。</p>
    <p className="us-anchor-warning"><b>这不是实盘止损价。</b>它只用于用 2.5U 锚点风险反推单票金额；真实卖出只依据趋势动物的危险、温度、沸与开香槟。</p>

    {!currentRules || candidate.legacy_read_only ? <p className="us-warning">这是 H1–H5 历史候选，只读展示；不能补证据或生成新计划。</p> : !planEligible ? <p className="us-warning">该标的已失败关闭：{reasonText(candidate.primary_reason)}。</p> : <>
      {mode === "off" && <p className="us-warning">当前 off：风险锚点不参与分配。先进入 shadow 验证。</p>}
      {mode === "shadow" && <p className="us-warning">当前 shadow：只核验风险锚点，不能生成买入清单。</p>}
      <button data-testid="us-refresh-risk-anchor" className="desk-button cyan" onClick={() => riskRefresh.mutate(candidate.candidate_id)} disabled={riskRefresh.isPending}>{riskRefresh.isPending ? "正在读取 Bitget 证据" : anchor ? "重新生成风险锚点" : "生成风险锚点"}</button>
      {riskRefresh.isError && <ErrorBox error={riskRefresh.error} />}
    </>}
    <footer>Trend Desk 只生成分析证据与手工清单；不读取 Bitget 账户，不提交订单。</footer>
  </aside>;
}

function AllocationCard({ preview, reasons, setReasons, onExclude, planMutation }: {
  preview: UsAllocationPreview;
  reasons: Record<number, string>;
  setReasons: Dispatch<SetStateAction<Record<number, string>>>;
  onExclude: (candidateId: number, backfill: boolean) => void;
  planMutation: { mutate: () => void; isPending: boolean; isError: boolean; error: unknown };
}) {
  const allocated = preview.items.filter(item => item.status === "allocated");
  return <section className="us-panel us-allocation" data-testid="us-allocation-card">
    <div className="us-panel-head"><div><small>IMMUTABLE ALLOCATION</small><h2>推荐分配 · {preview.intended_execution_date}</h2></div><span>{money(preview.allocated_usdt)}</span></div>
    <ExecutionTimeline schedule={preview.execution_schedule} fallbackDate={preview.intended_execution_date} />
    <div className="us-capacity-strip"><span>今日剩余 <b>{money(preview.daily_remaining_usdt)}</b></span><span>组合剩余 <b>{money(preview.portfolio_remaining_usdt)}</b></span><span>现金剩余 <b>{money(preview.cash_remaining_usdt)}</b></span><span>可用席位 <b>{preview.available_ticker_slots}</b></span></div>
    {preview.items.map(item => <div className={`us-allocation-row ${item.status}`} key={item.allocation_item_id}>
      <span><b>#{item.priority} {item.candidate.ticker_symbol}</b><small>{item.candidate.asset_type === "etf" ? "ETF" : item.candidate.industry_name ?? "个股"} · 强度 {oneDecimal(item.candidate.strength_local)}</small></span>
      <span><b>{item.status === "allocated" ? money(item.allocated_notional_usdt) : allocationReasonText(item.reason_code)}</b><small>{item.target_quantity ? `${trimDecimal(item.target_quantity)} 股` : "不分配"} · Bitget 公共参考价 {money(item.reference_price_usdt)}</small></span>
      <span><b>锚点 {money(item.anchor_price_usdt)}</b><small>距离 {percent(item.anchor_distance)} · 估算锚点风险 {money(item.anchor_loss_estimate_usdt)}</small></span>
      {item.status === "allocated" && <div className="us-exclusion"><input aria-label={`排除 ${item.candidate.ticker_symbol} 的理由`} placeholder="排除理由（必填）" value={reasons[item.candidate_id] ?? ""} onChange={event => setReasons(current => ({ ...current, [item.candidate_id]: event.target.value }))} /><button onClick={() => onExclude(item.candidate_id, false)} disabled={!reasons[item.candidate_id]?.trim()}>排除，不补位</button><button onClick={() => onExclude(item.candidate_id, true)} disabled={!reasons[item.candidate_id]?.trim()}>排除并重新补位</button></div>}
    </div>)}
    <p className="us-anchor-warning">分配按强度降序自动填充。排除不会静默补位；只有点击“排除并重新补位”才重新计算。草稿不占容量，锁定清单才预留。</p>
    {!!allocated.length && <button data-testid="us-create-plan" className="desk-button lime" onClick={() => planMutation.mutate()} disabled={planMutation.isPending}>{planMutation.isPending ? "正在冻结证据" : `生成 ${allocated.length} 只手工买入清单`}</button>}
    {planMutation.isError && <ErrorBox error={planMutation.error} />}
  </section>;
}

function PlanCard({ plan, activeRulesVersion, lockMutation, selectedItemId, onSelectItem, executionPrice, setExecutionPrice, executionQuantity, setExecutionQuantity, executionFee, setExecutionFee, executionAt, setExecutionAt, executionPreview, previewMutation, confirmMutation }: {
  plan: UsManualPlan;
  activeRulesVersion: string;
  lockMutation: { mutate: (id: string) => void; isPending: boolean; isError: boolean; error: unknown };
  selectedItemId: number | null;
  onSelectItem: (item: UsManualPlanItem) => void;
  executionPrice: string; setExecutionPrice: (value: string) => void;
  executionQuantity: string; setExecutionQuantity: (value: string) => void;
  executionFee: string; setExecutionFee: (value: string) => void;
  executionAt: string; setExecutionAt: (value: string) => void;
  executionPreview: Record<string, unknown> | null;
  previewMutation: { mutate: (id: number) => void; isPending: boolean; isError: boolean; error: unknown };
  confirmMutation: { mutate: (id: number) => void; isPending: boolean; isError: boolean; error: unknown };
}) {
  const mutable = plan.rules_version === activeRulesVersion && !plan.legacy_read_only;
  const locked = ["locked", "partially_executed"].includes(plan.status);
  const selected = plan.items.find(item => item.item_id === selectedItemId) ?? null;
  return <section className="us-panel us-plan-card">
    <div className="us-panel-head"><div><small>MANUAL CHECKLIST</small><h2>{plan.status === "no_execution" ? "今日不交易已记录" : `${plan.items.some(item => item.side === "sell") ? "卖出" : "买入"}清单 ${plan.plan_id.slice(-8)}`}</h2></div><span>{plan.status}</span></div>
    <ExecutionTimeline schedule={plan.execution_schedule} fallbackDate={plan.intended_execution_date} />
    {plan.items.map(item => <div className="us-plan-item" key={item.item_id}>
      <span><b>{item.ticker_symbol} · {item.side === "sell" ? "卖出" : "买入"}</b><small>{item.target_quantity ?? "—"} 股 · {money(item.target_notional_usdt)}</small></span>
      <span><b>{item.side === "buy" ? `风险锚点 ${money(item.risk_anchor_price)}` : exitActionFromItem(item)}</b><small>{item.side === "buy" ? `${item.risk_anchor_date ?? "—"} · 估算锚点风险 ${money(item.anchor_loss_estimate_usdt)}` : "趋势退出决定已冻结"}</small></span>
      <span><b>Bitget 公共参考价 {money(item.entry_reference_price)}</b><small>{item.side === "buy" ? "锚点不是止损；真实退出按温度纪律" : "卖出成交前不计入可用现金"}</small></span>
      {mutable && locked && item.status !== "completed" && <button className="desk-button cyan" onClick={() => onSelectItem(item)}>回填此项成交</button>}
    </div>)}
    {!mutable && <p className="us-warning">这是 H1–H5 历史清单，只读展示；不能锁定、回填或伪造成 H6 数据。</p>}
    {mutable && plan.status === "draft" && <button data-testid="us-lock-plan" className="desk-button yellow" onClick={() => lockMutation.mutate(plan.plan_id)} disabled={lockMutation.isPending}>{lockMutation.isPending ? "重新核验容量" : "锁定手工清单并预留容量"}</button>}
    {lockMutation.isError && <ErrorBox error={lockMutation.error} />}
    {mutable && locked && selected && <div className="us-execution"><h3>回填 {selected.ticker_symbol} 实际成交</h3><p>先在 Bitget 手工成交，再预览并确认。只有确认后才更新现金、持仓或卖出完成状态。</p><div><label>成交价（USDT）<input value={executionPrice} inputMode="decimal" onChange={event => setExecutionPrice(event.target.value)} /></label><label>成交数量（股）<input value={executionQuantity} inputMode="decimal" onChange={event => setExecutionQuantity(event.target.value)} /></label><label>费用（USDT）<input value={executionFee} inputMode="decimal" onChange={event => setExecutionFee(event.target.value)} /></label><label>成交时间（北京时间）<input type="datetime-local" value={executionAt} onChange={event => setExecutionAt(event.target.value)} /></label></div><button data-testid="us-execution-preview" className="desk-button cyan" onClick={() => previewMutation.mutate(selected.item_id)} disabled={previewMutation.isPending || !executionPrice || !executionQuantity}>{previewMutation.isPending ? "预览中" : "预览人工成交"}</button>{executionPreview && <div className="us-execution-preview"><b>预览：尚未落账</b><p>成交金额 {String(executionPreview.gross_usdt ?? "—")} USDT；确认后不可直接改写，只能冲正。</p><button data-testid="us-confirm-execution" className="desk-button lime" onClick={() => confirmMutation.mutate(selected.item_id)} disabled={confirmMutation.isPending}>{confirmMutation.isPending ? "确认中" : "确认写入人工台账"}</button></div>}{previewMutation.isError && <ErrorBox error={previewMutation.error} />}{confirmMutation.isError && <ErrorBox error={confirmMutation.error} />}</div>}
  </section>;
}

function ErrorBox({ error }: { error: unknown }) { return <div className="us-error" role="alert">{errorText(error)}</div>; }

function scheduleOpenText(schedule: UsExecutionSchedule | undefined, fallbackDate: string) {
  if (!schedule) return `计划于 ${fallbackDate} 美股常规开盘手工执行`;
  return `美东 ${zonedTimestamp(schedule.open_new_york, "America/New_York")} / 北京 ${zonedTimestamp(schedule.open_beijing, "Asia/Shanghai")}`;
}

function ExecutionTimeline({ schedule, fallbackDate }: { schedule?: UsExecutionSchedule; fallbackDate: string }) {
  if (!schedule) return <p className="us-execution-timeline">预定执行：{fallbackDate} 美股常规开盘（历史记录未保存精确时区时间轴）</p>;
  return <div className="us-execution-timeline" data-testid="us-execution-timeline">
    <span>趋势信号日 <b>{schedule.signal_date}</b></span>
    <span>清单生成（北京时间）<b>{zonedTimestamp(schedule.generated_at_beijing, "Asia/Shanghai")}</b></span>
    <span>预定开盘（美国东部）<b>{zonedTimestamp(schedule.open_new_york, "America/New_York")}</b></span>
    <span>对应北京时间 <b>{zonedTimestamp(schedule.open_beijing, "Asia/Shanghai")}</b></span>
  </div>;
}

function selectionStatusText(value: string) {
  return ({ ready: "通过", screened: "质量筛选中", observe: "观察", data_incomplete: "数据不足", etf_observation: "历史 ETF 观察" } as Record<string, string>)[value] ?? value;
}
function riskStatusText(value: UsRiskAnchorStatus) { return ({ pending: "待生成", ready: "已确认（仅反推仓位）", blocked: "证据阻断" } as Record<UsRiskAnchorStatus, string>)[value]; }
function benchmarkStatusText(value: string) { return ({ verified: "已核验", not_applicable: "不适用", benchmark_review_required: "待权威证据", stale: "证据过期", duplicate_exposure: "相同暴露已去重", conflict: "证据冲突" } as Record<string, string>)[value] ?? value; }
function exitActionText(value: string) { return ({ exit_all: "下一常规开盘全部卖出", reduce_25: "下一常规开盘减仓 25%", reduce_50: "下一常规开盘减仓 50%", manual_review: "退出证据待人工复核", hold: "按趋势纪律继续持有", legacy_read_only: "历史持仓只读" } as Record<string, string>)[value] ?? value; }
function exitReasonText(value: string) { return ({ danger: "危险", temperature_flat_or_below: "温度转平或以下", boiling: "沸", champagne: "开香槟", trend_hold: "趋势仍允许持有", exit_data_missing: "当日退出字段缺失", exit_fields_incomplete: "危险/沸/开香槟字段不完整", temperature_missing: "当前温度缺失", partial_exit_below_minimum: "减仓数量低于交易所最低额", partial_exit_quote_unavailable: "减仓参考报价缺失", historical_h1_h5_position: "H1–H5 历史持仓" } as Record<string, string>)[value] ?? value; }
function allocationReasonText(value: string | null) { return ({ existing_position_duplicate: "已有持仓/锁定计划，不加仓", position_capacity_full: "20 个标的席位已满", daily_capacity_below_policy_floor: "今日剩余额度低于 25U", etf_benchmark_not_verified: "ETF 基准未核验", quote_unavailable: "公开报价不可用", risk_anchor_not_ready: "风险锚点未就绪", risk_anchor_stale: "报价与锚点快照不一致", risk_anchor_not_below_quote: "锚点不低于报价", risk_ceiling_below_minimum: "2.5U 风险上限低于最小仓位", allocation_below_minimum: "精度取整后低于最小仓位", user_excluded_without_backfill: "用户排除，未补位", user_excluded_with_backfill: "用户排除，已重新补位" } as Record<string, string>)[value ?? ""] ?? value ?? "未分配"; }
function accountOcrStatusText(value: UsAccountOcrBatch["status"]) { return ({ running: "识别中", ready: "待确认", failed: "识别失败", confirmed: "已确认" } as Record<UsAccountOcrBatch["status"], string>)[value]; }
function reconciliationText(value: string | undefined) { return ({ ocr_only: "仅存在于账户截图", quantity_mismatch: "截图数量与成交台账不一致", matched: "已与成交台账一致", ledger_only: "仅存在于成交台账" } as Record<string, string>)[value ?? ""] ?? "等待核对"; }
function exitActionFromItem(item: UsManualPlanItem) { const decision = item.reason_json?.exit_decision as { action?: string } | undefined; return exitActionText(decision?.action ?? "sell"); }
function reasonText(value: string | null | undefined) {
  return ({ ready_after_h4_disciplines: "已通过全部选股纪律", sector_temperature_below_warm: "板块温度低于温", sector_temperature_missing: "板块温度缺失", right_side_age_outside_1_9: "右侧自然日不在 1–9 天", right_side_days_missing: "右侧自然日缺失", gate_snapshot_missing: "门槛数据缺失", quality_snapshot_missing: "详情数据缺失", enrichment_required_fields_missing: "详情必需字段不完整", relative_strength_below_90: "相对趋势强度低于 90", relative_strength_at_least_90: "相对趋势强度 ≥90", sector_temperature_warm_plus: "个股板块温度 ≥温", etf_warm_to_hot_membership: "ETF 自身温转热", quality_complete: "质量字段完整", etf_benchmark_missing: "ETF 权威基准证据缺失", etf_benchmark_stale: "ETF 基准证据已过期", duplicate_etf_exposure: "相同 ETF 执行暴露已去重" } as Record<string, string>)[value ?? ""] ?? value ?? "—";
}
