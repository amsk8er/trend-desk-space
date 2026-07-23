import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  approveDisciplineBudget, checkDisciplineTodayData, confirmBrokerImport,
  confirmDisciplineOcrFallback, confirmDisciplinePositions,
  generateDisciplineReview, getDisciplineDataProbe, getDisciplinePlan,
  getDisciplinePlans, getDisciplinePositionImportStatus, getDisciplineReview,
  getDisciplineTodayData, getDisciplineVersion, getPositions,
  getAutomationStatus, getLedgerStatus, getLlmConfig, importDisciplinePositions,
  lockDisciplinePlan, previewBrokerImport, previewExecutionScreenshots,
  previewDisciplineOcrFallback,
  confirmExecutionOcr, confirmNoExecution, rollForwardLedger,
  runAutomationNow, sendAutomationTestEmail, updateFeeSchedule,
  type BrokerImportPreview, type DailyDatasetStatus, type DisciplineCandidate,
  type DisciplineEvidence, type DisciplinePlan, type DisciplinePlanItem,
  type DisciplineVersion, type Position,
} from "../api";
import "./discipline.css";

const VISION_BACKEND_KEY = "td-vision-backend";
const backendLabel: Record<string, string> = {
  claude_cli: "Claude CLI",
  anthropic_api: "Anthropic API",
  codex_cli: "Codex CLI",
  minimax_coding_plan: "MiniMax Coding Plan（视觉）",
  openai_compatible: "OpenAI 兼容视觉 API",
};

type Page = "desk" | "review" | "data";
type CandidateGroup = "watch" | "shadow" | "rejected";
type DrawerPayload =
  | { kind: "action"; item: DisciplinePlanItem; candidate?: DisciplineCandidate }
  | { kind: "candidate"; candidate: DisciplineCandidate; group: CandidateGroup };

const pages: { id: Page; label: string; mark: string }[] = [
  { id: "desk", label: "交易台", mark: "01" },
  { id: "review", label: "复盘", mark: "02" },
  { id: "data", label: "数据与规则", mark: "03" },
];

const sideLabel: Record<string, string> = {
  sell_all: "全部清仓", reduce: "分批止盈", buy: "可以买入",
  hold: "继续持有", manual_review: "人工确认",
};
const statusLabel: Record<string, string> = {
  pending: "等待采集", checking: "检查更新", fetching: "正在采集",
  waiting_retry: "等待重试", ready: "数据就绪", ready_degraded: "备用数据就绪",
  awaiting_budget: "等待费用批准", manual_required: "需要人工检查", failed: "采集失败",
};
const planStageLabel: Record<string, string> = { signal: "信号草稿", executable: "可执行草稿" };
const planStatusLabel: Record<string, string> = {
  awaiting_account: "等待账户", draft: "草稿", locked: "已锁定", expired: "已替代",
};

export default function DisciplineDesk({
  onOpenPipeline, onOpenSwing,
}: {
  onOpenPipeline: () => void;
  onOpenSwing: () => void;
}) {
  const qc = useQueryClient();
  const [page, setPage] = useState<Page>("desk");
  const [planId, setPlanId] = useState("");
  const [drawer, setDrawer] = useState<DrawerPayload | null>(null);
  const [preview, setPreview] = useState<BrokerImportPreview | null>(null);
  const dataQ = useQuery({
    queryKey: ["discipline-data-today"], queryFn: getDisciplineTodayData,
    refetchInterval: q => ["checking", "fetching", "waiting_retry"].includes(q.state.data?.status ?? "") ? 30_000 : false,
  });
  const plans = useQuery({ queryKey: ["discipline-plans"], queryFn: getDisciplinePlans });
  useEffect(() => {
    if (!planId && plans.data?.[0]?.plan_id) setPlanId(plans.data[0].plan_id);
  }, [planId, plans.data]);
  const planQ = useQuery({
    queryKey: ["discipline-plan", planId], queryFn: () => getDisciplinePlan(planId), enabled: !!planId,
  });
  const plan = planQ.data ?? null;
  const versionQ = useQuery({ queryKey: ["discipline-version"], queryFn: getDisciplineVersion });
  const reviewQ = useQuery({
    queryKey: ["discipline-review", planId], queryFn: () => getDisciplineReview(planId),
    enabled: !!planId && page === "review", retry: false,
  });
  const refreshPlans = (next?: DisciplinePlan) => {
    if (next?.plan_id) setPlanId(next.plan_id);
    qc.invalidateQueries({ queryKey: ["discipline-plans"] });
    qc.invalidateQueries({ queryKey: ["discipline-data-today"] });
    if (next?.plan_id) qc.invalidateQueries({ queryKey: ["discipline-plan", next.plan_id] });
  };
  const lock = useMutation({ mutationFn: () => lockDisciplinePlan(planId), onSuccess: refreshPlans });
  const upload = useMutation({ mutationFn: (file: File) => previewBrokerImport(planId, file), onSuccess: setPreview });
  const confirm = useMutation({
    mutationFn: () => confirmBrokerImport(preview!.import_id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["discipline-plan", planId] });
      qc.invalidateQueries({ queryKey: ["discipline-review", planId] });
    },
  });
  const makeReview = useMutation({
    mutationFn: () => generateDisciplineReview(planId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["discipline-review", planId] }),
  });
  const manualCollect = useMutation({
    mutationFn: checkDisciplineTodayData,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["discipline-data-today"] });
      qc.invalidateQueries({ queryKey: ["discipline-plans"] });
    },
  });
  const planRows = plans.data ?? [];
  const planCaption = plan
    ? `${planStageLabel[plan.plan_stage ?? ""] ?? "计划"} · ${planStatusLabel[plan.status] ?? plan.status}`
    : "尚无计划";

  return <div className="discipline-shell" data-testid="discipline-desk">
    <aside className="discipline-sidebar">
      <div className="discipline-brand">
        <span className="brand-mark">TD</span>
        <div><b>Trend Desk</b><small>纪律交易台</small></div>
      </div>
      <nav>{pages.map(item => <button
        key={item.id}
        data-testid={`nav-${item.id}`}
        className={page === item.id ? "active" : ""}
        onClick={() => { setPage(item.id); setDrawer(null); }}
      ><i>{item.mark}</i><span>{item.label}</span></button>)}</nav>
      <div className="sidebar-foot">
        <span>TOOLS</span>
        <button onClick={onOpenPipeline}>数据流水线 ↗</button>
        <button onClick={onOpenSwing}>重要低点 ↗</button>
        <small>现金也是有效仓位</small>
      </div>
    </aside>
    <main className="discipline-main">
      <header className="discipline-topbar">
        <div className="date-strip">
          <b>信号日</b><span className="mono">{plan?.signal_date ?? dataQ.data?.trade_date ?? "—"}</span>
          <b>执行日</b><span className="mono">{plan?.execute_date ?? "等待生成"}</span>
          <b>纪律版本</b><span className="mono">{plan?.discipline_version ?? String(versionQ.data?.version ?? "—")}</span>
        </div>
        <div className="top-actions">
          <div className="plan-version" title="系统自动显示最新计划，也可回看历史版本。">
            <small>计划版本</small>
            {planRows.length > 1 ? <select aria-label="查看计划版本" value={planId} onChange={e => setPlanId(e.target.value)}>
              {planRows.map((row, index) => <option key={row.plan_id} value={row.plan_id}>
                {index === 0 ? "最新 · " : ""}{row.signal_date} · {planStageLabel[row.plan_stage ?? ""] ?? "计划"} · {planStatusLabel[row.status] ?? row.status}
              </option>)}
            </select> : <b>{planCaption}</b>}
          </div>
          <button
            data-testid="manual-collect"
            className="desk-button lime"
            disabled={manualCollect.isPending || dataQ.data?.before_collection_window}
            title={dataQ.data?.before_collection_window ? "北京时间16:30后可手动采集" : "数据已就绪时只读数据库，不会重复请求趋势动物"}
            onClick={() => manualCollect.mutate()}
          >{manualCollect.isPending ? "采集中" : "采集数据"}</button>
          {plan?.status === "draft" && <button
            data-testid="lock-plan"
            className="desk-button yellow"
            disabled={lock.isPending || !plan.data_health.lockable || plan.plan_stage !== "executable"}
            onClick={() => lock.mutate()}
          >{lock.isPending ? "锁定中" : "锁定计划"}</button>}
          <span className={`status-pill ${plan?.status ?? dataQ.data?.status ?? "pending"}`}>
            {planStatusLabel[plan?.status ?? ""] ?? statusLabel[dataQ.data?.status ?? "pending"]}
          </span>
        </div>
      </header>
      {manualCollect.isError && <ErrorLine error={manualCollect.error as Error} />}
      {page === "desk" && <TradingDesk
        dataset={dataQ.data}
        plan={plan}
        loading={dataQ.isLoading}
        onPlan={refreshPlans}
        onSelect={setDrawer}
      />}
      {page === "review" && <Review plan={plan} preview={preview} upload={upload} confirm={confirm} makeReview={makeReview} reviewQ={reviewQ} />}
      {page === "data" && <DataAudit dataset={dataQ.data} plan={plan} version={versionQ.data} onOpenPipeline={onOpenPipeline} onRefresh={() => qc.invalidateQueries()} />}
    </main>
    {drawer && <DetailDrawer payload={drawer} onClose={() => setDrawer(null)} />}
  </div>;
}

function TradingDesk({
  dataset, plan, loading, onPlan, onSelect,
}: {
  dataset?: DailyDatasetStatus;
  plan: DisciplinePlan | null;
  loading: boolean;
  onPlan: (p?: DisciplinePlan) => void;
  onSelect: (payload: DrawerPayload) => void;
}) {
  const qc = useQueryClient();
  const [budget, setBudget] = useState("5.00");
  const approve = useMutation({
    mutationFn: () => approveDisciplineBudget(dataset!.trade_date, Number(budget)),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["discipline-data-today"] }),
  });
  const selection = plan?.selection_snapshot ?? {};
  const capacity = plan?.capacity_snapshot ?? {};
  const items = useMemo(() => [...(plan?.items ?? [])].sort((a, b) => a.priority - b.priority), [plan?.items]);
  const counts = Object.fromEntries(["sell_all", "reduce", "buy", "hold"].map(side => [side, items.filter(item => item.side === side).length]));
  const candidateMap = new Map<string, DisciplineCandidate>();
  for (const row of selection.white_list ?? []) candidateMap.set(bareCode(row.code), row);
  const marketTemperature = String(capacity.market_temperature ?? "—");
  const environmentFactor = Number(capacity.environment_factor ?? plan?.environment_factor ?? 0);
  const basePosition = Number(capacity.base_new_position_pct ?? 0.05);
  const perPosition = Number(capacity.per_position_weight ?? basePosition * environmentFactor);
  const account = plan?.account;
  const next = nextStep(dataset, plan);

  return <section data-testid="page-desk" className="discipline-page trading-desk">
    <div className="desk-hero">
      <div>
        <small>ACTION FIRST / EVIDENCE ON DEMAND</small>
        <h1>明日，只做清单里的事。</h1>
        <p>卖出优先，买入受容量约束。点击任何一行查看完整规则证据。</p>
      </div>
      <div className={`next-step ${next.tone}`}>
        <span>当前唯一下一步</span>
        <strong>{next.title}</strong>
        <small>{next.detail}</small>
      </div>
    </div>

    <div className="context-strip" aria-label="交易上下文">
      <ContextCard label="A股环境" value={marketTemperature} note="决定新仓系数" tone="cyan" />
      <ContextCard label="环境系数" value={formatPercent(environmentFactor)} note={`${formatPercent(basePosition)} × 系数`} tone="yellow" />
      <ContextCard label="单个新仓" value={formatPercent(perPosition)} note="容量不足则不拆小仓" tone="pink" />
      <ContextCard label="可用现金" value={account ? `¥${formatCompact(account.cash)}` : "待确认"} note={account ? `净值 ¥${formatCompact(account.nav)}` : "先更新券商账户"} tone="lime" />
      <ContextCard label="行动数量" value={String(items.filter(item => item.side !== "hold").length)} note={`另有 ${counts.hold ?? 0} 项继续持有`} tone="orange" />
    </div>

    <div className="action-workbench">
      <section className="desk-panel action-ledger">
        <div className="panel-head loud">
          <div><small>ONE ACTION LIST</small><h2>明日唯一行动清单</h2></div>
          <span>{plan?.status === "locked" ? "已锁定" : plan?.data_health.lockable ? "可锁定" : "等待闸门"}</span>
        </div>
        {items.length ? <div className="action-groups">
          <ActionGroup side="sell_all" title="必须清仓" items={items} candidateMap={candidateMap} onSelect={onSelect} />
          <ActionGroup side="reduce" title="分批止盈" items={items} candidateMap={candidateMap} onSelect={onSelect} />
          <ActionGroup side="buy" title="可以买入" items={items} candidateMap={candidateMap} onSelect={onSelect} />
          <ActionGroup side="hold" title="继续持有" items={items} candidateMap={candidateMap} onSelect={onSelect} collapsible />
        </div> : <Empty title="尚未生成行动清单" text="日终数据就绪后系统自动生成信号草稿；确认账户后生成可执行股数。" />}
      </section>

      <aside className="desk-rail">
        <section className="desk-panel tally-card">
          <div className="tally-title"><span>ACTION</span><b>行动摘要</b></div>
          <Tally label="必须清仓" value={counts.sell_all ?? 0} tone="red" />
          <Tally label="分批止盈" value={counts.reduce ?? 0} tone="orange" />
          <Tally label="可以买入" value={counts.buy ?? 0} tone="green" />
          <Tally label="继续持有" value={counts.hold ?? 0} tone="blue" />
        </section>
        <SourceDeck dataset={dataset} hasAccount={!!plan?.portfolio_snapshot_id} loading={loading} />
        <section className="desk-panel cost-card">
          <span>DATA COST</span>
          <p>预计 <b className="mono">¥{(dataset?.estimated_cost ?? 0).toFixed(3)}</b></p>
          <p>实际 <b className="mono">{dataset?.actual_cost == null ? "待账单" : `¥${dataset.actual_cost.toFixed(3)}`}</b></p>
        </section>
      </aside>
    </div>

    {(dataset?.error_message || dataset?.status === "awaiting_budget") && <div className="control-bar">
      <div><b>数据采集阻塞</b><span>{dataset.error_message ?? "本次采集预计费用超过自动额度，需要人工批准。"}</span></div>
      {dataset?.status === "awaiting_budget" && <>
        <input aria-label="数据费用额度" className="desk-input mono" value={budget} onChange={e => setBudget(e.target.value)} />
        <button className="desk-button orange" disabled={approve.isPending} onClick={() => approve.mutate()}>批准额度</button>
      </>}
    </div>}
    {approve.isError && <ErrorLine error={approve.error as Error} />}

    <section className="desk-vault">
      <details className="vault-section selection-vault">
        <summary>
          <span className="vault-index">A</span>
          <div><small>SELECTION EVIDENCE</small><b>候选池与拒绝证据</b></div>
          <div className="vault-counts">
            <i className="green">白 {selection.white_list?.length ?? 0}</i>
            <i className="yellow">观 {selection.watch_list?.length ?? 0}</i>
            <i>影 {selection.shadow_pool?.length ?? 0}</i>
            <i className="red">拒 {selection.rejected?.length ?? 0}</i>
          </div>
          <em>展开 ↘</em>
        </summary>
        <div className="vault-body">
          <p className="vault-note">白名单已进入上方行动清单，不重复展示。这里保留观察、权限外影子和硬门拒绝项。</p>
          <CandidateLane title="观察池" tone="yellow" group="watch" rows={selection.watch_list ?? []} onSelect={onSelect} />
          <CandidateLane title="权限外影子池" tone="gray" group="shadow" rows={selection.shadow_pool ?? []} onSelect={onSelect} />
          <CandidateLane title="硬门拒绝" tone="red" group="rejected" rows={selection.rejected ?? []} onSelect={onSelect} />
        </div>
      </details>

      <details className="vault-section account-vault">
        <summary>
          <span className="vault-index">B</span>
          <div><small>ACCOUNT & POSITIONS</small><b>账户与持仓更新</b></div>
          <div className="account-summary">
            <strong>{plan?.portfolio_snapshot_id ? "已确认" : "待确认"}</strong>
            <span>{account ? `净值 ¥${formatCompact(account.nav)} · 现金 ¥${formatCompact(account.cash)}` : "从昨日台账与今日成交自动滚动"}</span>
          </div>
          <em>展开 ↘</em>
        </summary>
        <div className="vault-body"><AccountControl dataset={dataset} plan={plan} onPlan={onPlan} /></div>
      </details>
    </section>
  </section>;
}

function ContextCard({ label, value, note, tone }: { label: string; value: string; note: string; tone: string }) {
  return <article className={`context-card ${tone}`}><small>{label}</small><strong>{value}</strong><span>{note}</span></article>;
}

function Tally({ label, value, tone }: { label: string; value: number; tone: string }) {
  return <div className="tally-row"><i className={tone} /><span>{label}</span><b className="mono">{value}</b></div>;
}

function SourceDeck({ dataset, hasAccount, loading }: { dataset?: DailyDatasetStatus; hasAccount: boolean; loading: boolean }) {
  const source = (name: string) => dataset?.source_status?.[name]?.status ?? "pending";
  return <section className="desk-panel source-deck">
    <span>DATA HEALTH</span>
    <SourceLine label="趋势动物" status={source("trend_animals")} detail={`${dataset?.trend_rows ?? 0} 行`} />
    <SourceLine label="Tushare" status={source("tushare")} detail={`${dataset?.market_rows ?? 0} 行`} />
    <SourceLine label="券商账户" status={hasAccount ? "ready" : "waiting"} detail={hasAccount ? "已确认" : "待确认"} />
    <small>{loading ? "读取中…" : statusLabel[dataset?.status ?? "pending"]}</small>
  </section>;
}

function SourceLine({ label, status, detail }: { label: string; status: string; detail: string }) {
  const ok = ["ready", "fallback"].includes(status);
  return <div className="source-line"><i className={ok ? "ok" : status === "running" || status === "fetching" ? "running" : "waiting"} /><b>{label}</b><span>{detail}</span></div>;
}

function ActionGroup({
  side, title, items, candidateMap, onSelect, collapsible = false,
}: {
  side: DisciplinePlanItem["side"];
  title: string;
  items: DisciplinePlanItem[];
  candidateMap: Map<string, DisciplineCandidate>;
  onSelect: (payload: DrawerPayload) => void;
  collapsible?: boolean;
}) {
  const rows = items.filter(item => item.side === side);
  if (!rows.length) return null;
  const content = <div className="action-rows">{rows.map(item => <button
    key={item.item_id}
    className="action-row"
    data-side={item.side}
    aria-label={`查看 ${item.name} 的完整证据`}
    onClick={() => onSelect({ kind: "action", item, candidate: candidateMap.get(bareCode(item.instrument_id)) })}
  >
    <span className="action-code"><b>{item.name}</b><small className="mono">{item.instrument_id}</small></span>
    <strong>{actionText(item)}</strong>
    <span className="action-evidence">{actionEvidenceSummary(item)}</span>
    <i>查看证据 →</i>
  </button>)}</div>;
  if (collapsible) return <details className={`action-group ${side}`}>
    <summary><span>{title}</span><b>{rows.length}</b><i>展开</i></summary>{content}
  </details>;
  return <section className={`action-group ${side}`}>
    <header><span>{title}</span><b>{rows.length}</b></header>{content}
  </section>;
}

function CandidateLane({
  title, tone, group, rows, onSelect,
}: {
  title: string;
  tone: string;
  group: CandidateGroup;
  rows: DisciplineCandidate[];
  onSelect: (payload: DrawerPayload) => void;
}) {
  return <section className={`candidate-lane ${tone}`}>
    <h3>{title}<span>{rows.length}</span></h3>
    {rows.length ? <div>{rows.map((row, index) => <button
      key={`${row.code}-${index}`}
      onClick={() => onSelect({ kind: "candidate", candidate: row, group })}
      aria-label={`查看 ${row.name} 的筛选证据`}
    >
      <span className="candidate-identity"><b>{row.name}</b><small className="mono">{row.code}</small></span>
      <span className="candidate-primary"><b>{row.temperature_prev ?? "—"}→{row.temperature_curr ?? "—"}</b><small>节气 {row.phase ?? "—"}</small></span>
      <span className="candidate-primary"><b>强 {formatNumber(row.strength)}</b><small>右侧 {row.right_side_days == null ? "—" : `${row.right_side_days}日`}</small></span>
      <strong className="candidate-decision" data-reason={row.capacity_reason ?? (row.failed_rules?.length ? "failed" : "passed")}>{candidateDecision(row)}</strong>
      <span className="candidate-facts">
        <span><small>价</small><b>{formatNumber(row.price)}</b></span>
        <span><small>额</small><b>{formatYi(row.amount_yi)}</b></span>
        <span><small>{row.asset_type === "etf" ? "规模" : "流值"}</small><b>{formatYi(row.asset_type === "etf" ? row.aum_yi : row.float_market_cap_yi)}</b></span>
        <span className="candidate-sizing">{candidateSizing(row)}</span>
      </span>
      <i aria-hidden="true">→</i>
    </button>)}</div> : <p>暂无记录</p>}
  </section>;
}

function DetailDrawer({ payload, onClose }: { payload: DrawerPayload; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => { document.body.style.overflow = previous; window.removeEventListener("keydown", onKey); };
  }, [onClose]);
  const item = payload.kind === "action" ? payload.item : null;
  const candidate = payload.kind === "action" ? payload.candidate : payload.candidate;
  const tone = item?.side ?? (payload.kind === "candidate" ? payload.group : "hold");
  const title = item?.name ?? candidate?.name ?? "标的详情";
  const code = item?.instrument_id ?? candidate?.code ?? "—";
  return <div className="drawer-layer" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <aside className={`evidence-drawer ${tone}`} role="dialog" aria-modal="true" aria-labelledby="drawer-title">
      <header>
        <div><small>{item ? "ACTION EVIDENCE" : "SELECTION EVIDENCE"}</small><h2 id="drawer-title">{title}</h2><span className="mono">{code}</span></div>
        <button className="drawer-close" onClick={onClose} aria-label="关闭详情">×</button>
      </header>
      <div className="drawer-body">
        {item && <section className="drawer-action-card">
          <span>{sideLabel[item.side] ?? item.side}</span>
          <strong>{actionText(item)}</strong>
          <small>优先级 P{item.priority} · {item.status}</small>
        </section>}
        {candidate && <CandidateFacts row={candidate} />}
        {candidate?.failed_rules?.length ? <section className="drawer-section danger-section">
          <h3>未通过的纪律</h3>
          <ul>{candidate.failed_rules.map((evidence, index) => <li key={index}>{ruleReason(evidence)}</li>)}</ul>
        </section> : candidate && <section className="drawer-section pass-section"><h3>筛选结论</h3><p>{candidateReasons(candidate).join("；")}</p></section>}
        {item && <EvidenceSection title="行动规则证据" data={item.rule_evidence} />}
        {item && <EvidenceSection title="数据日期" data={item.source_dates} />}
        {item && <EvidenceSection title="数据来源" data={item.data_sources} />}
      </div>
      <footer><span>ESC 关闭</span><b>结论来自锁定纪律与已保存证据</b></footer>
    </aside>
  </div>;
}

function CandidateFacts({ row }: { row: DisciplineCandidate }) {
  const facts = [
    ["价格", formatNumber(row.price)],
    ["温度", `${row.temperature_prev ?? "—"}→${row.temperature_curr ?? "—"}`],
    ["节气", row.phase ?? "—"],
    ["右侧", row.right_side_days == null ? "—" : `${row.right_side_days}日`],
    ["强度", formatNumber(row.strength)],
    ["日成交额", formatYi(row.amount_yi)],
    [row.asset_type === "etf" ? "规模" : "流通市值", formatYi(row.asset_type === "etf" ? row.aum_yi : row.float_market_cap_yi)],
    ["容量预算", formatCurrency(row.allocation_budget)],
    ["理论手数", row.theoretical_lots == null ? "待账户" : `${formatNumber(row.theoretical_lots)}手`],
    ["可执行数量", row.executable_shares == null ? "待账户" : `${row.executable_shares}股`],
    ["板块", row.sector ?? "—"],
  ];
  return <section className="drawer-facts">{facts.map(([label, value]) => <div key={label}><small>{label}</small><b>{value}</b></div>)}</section>;
}

function EvidenceSection({ title, data }: { title: string; data: Record<string, unknown> }) {
  const entries = Object.entries(data ?? {});
  return <section className="drawer-section"><h3>{title}</h3>
    {entries.length ? <dl>{entries.map(([key, value]) => <div key={key}><dt>{evidenceLabel(key)}</dt><dd>{displayValue(value)}</dd></div>)}</dl> : <p>暂无记录</p>}
  </section>;
}

function AccountControl({ dataset, plan, onPlan }: { dataset?: DailyDatasetStatus; plan: DisciplinePlan | null; onPlan: (p?: DisciplinePlan) => void }) {
  const qc = useQueryClient();
  const tradeDate = dataset?.trade_date ?? plan?.signal_date ?? "";
  const [files, setFiles] = useState<File[]>([]);
  const [preview, setPreview] = useState<BrokerImportPreview | null>(null);
  const [acceptAnomalies, setAcceptAnomalies] = useState(false);
  const [backend, setBackend] = useState(() => localStorage.getItem(VISION_BACKEND_KEY) || "");
  const llmCfg = useQuery({ queryKey: ["llm-config"], queryFn: getLlmConfig });
  const effectiveBackend = backend || llmCfg.data?.backend || undefined;
  const ledgerQ = useQuery({
    queryKey: ["discipline-ledger", tradeDate],
    queryFn: () => getLedgerStatus(tradeDate),
    enabled: !!tradeDate,
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["discipline-ledger", tradeDate] });
    qc.invalidateQueries({ queryKey: ["discipline-plans"] });
    qc.invalidateQueries({ queryKey: ["discipline-plan"] });
  };
  const upload = useMutation({
    mutationFn: () => previewExecutionScreenshots(tradeDate, files, effectiveBackend),
    onSuccess: result => { setPreview(result); setAcceptAnomalies(false); },
  });
  const confirm = useMutation({
    mutationFn: () => confirmExecutionOcr(preview!.batch_id!, acceptAnomalies),
    onSuccess: refresh,
  });
  const noExecution = useMutation({
    mutationFn: () => confirmNoExecution(tradeDate),
    onSuccess: refresh,
  });
  const settle = useMutation({
    mutationFn: () => rollForwardLedger(tradeDate),
    onSuccess: refresh,
  });
  const runNow = useMutation({
    mutationFn: () => runAutomationNow("finalize"),
    onSuccess: refresh,
  });
  const ledger = ledgerQ.data;
  const confirmationLabel = ledger?.confirmation?.status === "no_execution"
    ? "今日无成交"
    : ledger?.confirmation?.status === "executions_confirmed"
      ? "成交已确认"
      : "等待确认";
  return <section className="ledger-control">
    <div className="ledger-summary-grid">
      <article><small>账户来源</small><b>{ledger?.snapshot?.source === "derived_ledger" ? "数据库滚动" : ledger?.snapshot?.source ?? "待建立期初"}</b><span>{ledger?.snapshot?.trade_date ?? "—"}</span></article>
      <article><small>账户净值</small><b>{formatCurrency(ledger?.snapshot?.nav)}</b><span>{ledger?.snapshot?.reconciliation_status ?? "等待快照"}</span></article>
      <article><small>可用现金</small><b>{formatCurrency(ledger?.snapshot?.cash)}</b><span>{ledger?.positions.length ?? 0} 个持仓</span></article>
      <article><small>今日成交</small><b>{confirmationLabel}</b><span>{tradeDate || "—"}</span></article>
    </div>

    <section className="execution-capture">
      <div className="panel-head"><div><small>DAILY EXECUTIONS</small><h3>上传当日成交清单</h3></div><span>只识别成交，不重扫账户</span></div>
      <div className="execution-actions">
        <label className="upload-button">选择成交截图<input type="file" accept="image/*" multiple onChange={event => setFiles(Array.from(event.target.files ?? []))} /></label>
        <span>{files.length ? `已选 ${files.length} 张` : "支持多张券商 APP 截图"}</span>
        <select className="desk-input" value={backend || llmCfg.data?.backend || ""} onChange={event => { setBackend(event.target.value); localStorage.setItem(VISION_BACKEND_KEY, event.target.value); }}>
          {(llmCfg.data?.choices ?? ["openai_compatible", "minimax_coding_plan"]).map(choice => <option key={choice} value={choice}>{llmCfg.data?.providers?.[choice]?.label ?? backendLabel[choice] ?? choice}</option>)}
        </select>
        <button className="desk-button pink" disabled={!files.length || !tradeDate || upload.isPending} onClick={() => upload.mutate()}>{upload.isPending ? "识别中…" : "识别成交"}</button>
        <button className="desk-button yellow" disabled={!tradeDate || noExecution.isPending || !!ledger?.confirmation} onClick={() => {
          if (window.confirm("确认今天没有任何买入或卖出成交？")) noExecution.mutate();
        }}>今日无成交</button>
      </div>
      {preview && <div className="execution-preview">
        <div><b>识别预览</b><span>有效 {preview.parsed_rows.length} · 异常 {preview.anomaly_rows.length}</span></div>
        {preview.parsed_rows.map((row, index) => <div className="execution-row" key={index}>
          <b>{String(row.name || row.code || "未命名")}</b>
          <span className="mono">{String(row.code ?? "—")}</span>
          <span>{row.side === "buy" ? "买入" : "卖出"}</span>
          <span className="mono">{String(row.shares ?? "—")} 股 × {String(row.price ?? "—")}</span>
          <span>{row.fees == null && row.net_amount == null ? "费用将保守估算" : "费用可核对"}</span>
        </div>)}
        {!!preview.anomaly_rows.length && <label className="anomaly-ack"><input type="checkbox" checked={acceptAnomalies} onChange={event => setAcceptAnomalies(event.target.checked)} />忽略异常行，只确认上方有效成交</label>}
        <button className="desk-button lime" disabled={!preview.parsed_rows.length || confirm.isPending || (!!preview.anomaly_rows.length && !acceptAnomalies)} onClick={() => confirm.mutate()}>{confirm.isPending ? "写入中…" : "确认写入成交台账"}</button>
      </div>}
      <div className="ledger-finalize-actions">
        <button className="desk-button cyan" disabled={!ledger?.confirmation || settle.isPending} onClick={() => settle.mutate()}>结算账户台账</button>
        <button className="desk-button lime" disabled={!ledger?.confirmation || runNow.isPending} onClick={() => runNow.mutate()}>生成并发送明日清单</button>
      </div>
      {(upload.isError || confirm.isError || noExecution.isError || settle.isError || runNow.isError) && <ErrorLine error={(upload.error ?? confirm.error ?? noExecution.error ?? settle.error ?? runNow.error) as Error} />}
    </section>

    <details className="account-reconciliation">
      <summary><b>账户对账与期初建立</b><span>仅初始化、异常修正或周期核对时使用</span><em>展开 ↘</em></summary>
      <AccountReconciliation dataset={dataset} plan={plan} onPlan={onPlan} />
    </details>
  </section>;
}

function AccountReconciliation({ dataset, plan, onPlan }: { dataset?: DailyDatasetStatus; plan: DisciplinePlan | null; onPlan: (p?: DisciplinePlan) => void }) {
  const qc = useQueryClient();
  const tradeDate = dataset?.trade_date ?? plan?.signal_date ?? "";
  const batchId = tradeDate ? `account_${tradeDate.split("-").join("")}` : "";
  const [files, setFiles] = useState<File[]>([]);
  const [nav, setNav] = useState("");
  const [cash, setCash] = useState("");
  const [navFromOcr, setNavFromOcr] = useState(false);
  const [cashFromOcr, setCashFromOcr] = useState(false);
  const [navTouched, setNavTouched] = useState(false);
  const [cashTouched, setCashTouched] = useState(false);
  const [backend, setBackend] = useState(() => localStorage.getItem(VISION_BACKEND_KEY) || "");
  const [confirmMsg, setConfirmMsg] = useState<{ tone: "ok" | "warn"; text: string } | null>(null);
  const llmCfg = useQuery({ queryKey: ["llm-config"], queryFn: getLlmConfig });
  const effBackend = backend || llmCfg.data?.backend || undefined;
  const upload = useMutation({
    mutationFn: () => importDisciplinePositions(tradeDate, files, effBackend),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["discipline-account-status", batchId] }),
  });
  const statusQ = useQuery({
    queryKey: ["discipline-account-status", batchId],
    queryFn: () => getDisciplinePositionImportStatus(batchId), enabled: !!batchId,
    refetchInterval: q => q.state.data?.status === "running" ? 1000 : false,
  });
  const positionsQ = useQuery({ queryKey: ["positions", batchId], queryFn: () => getPositions(batchId), enabled: !!batchId });
  useEffect(() => {
    if (statusQ.data?.status === "done" || statusQ.data?.status === "error") qc.invalidateQueries({ queryKey: ["positions", batchId] });
  }, [statusQ.data?.status, batchId, qc]);
  useEffect(() => {
    const account = statusQ.data?.account;
    if (account?.nav != null && !navTouched) {
      setNav(String(account.nav));
      setNavFromOcr(true);
    }
    if (account?.cash != null && !cashTouched) {
      setCash(String(account.cash));
      setCashFromOcr(true);
    }
  }, [statusQ.data?.account, navTouched, cashTouched]);
  const confirm = useMutation({
    mutationFn: () => confirmDisciplinePositions(batchId, (positionsQ.data ?? []).map(p => p.position_id), Number(nav), Number(cash)),
    onSuccess: result => {
      qc.invalidateQueries();
      if (result.plan) onPlan(result.plan);
      setConfirmMsg({
        tone: result.plan ? "ok" : "warn",
        text: result.message ?? (result.plan ? `已确认 ${result.confirmed} 项持仓，可执行计划已生成。` : `已确认 ${result.confirmed} 项持仓，请先完成当日数据采集。`),
      });
    },
    onError: () => setConfirmMsg(null),
  });
  const ocrStatus = statusQ.data;
  const ocrFailed = (ocrStatus?.failed ?? 0) > 0 || (ocrStatus?.failed_items?.length ?? 0) > 0;
  const ocrEmptyDone = ocrStatus?.status === "done" && !(positionsQ.data?.length) && (ocrStatus.count ?? 0) === 0;
  const provider = effBackend ? llmCfg.data?.providers?.[effBackend] : undefined;
  const account = ocrStatus?.account;
  const accountRecognized = account?.nav != null || account?.cash != null;
  const accountValid = Number(nav) > 0 && cash.trim() !== "" && Number(cash) >= 0 && Number(cash) <= Number(nav);
  return <section className="account-control">
    <div className="account-form">
      <label className="upload-button">选择持仓截图<input type="file" accept="image/*" multiple onChange={e => setFiles(Array.from(e.target.files ?? []))} /></label>
      <span>{files.length ? `已选 ${files.length} 张` : "支持多张券商 APP 截图"}</span>
      <select className="desk-input" value={backend || llmCfg.data?.backend || ""} onChange={e => { setBackend(e.target.value); localStorage.setItem(VISION_BACKEND_KEY, e.target.value); }}>
        {(llmCfg.data?.choices ?? ["minimax_coding_plan", "codex_cli", "anthropic_api", "openai_compatible", "claude_cli"]).map(choice => <option key={choice} value={choice}>{llmCfg.data?.providers?.[choice]?.label ?? backendLabel[choice] ?? choice}{llmCfg.data?.providers?.[choice]?.configured === false ? " · 未配置" : ""}</option>)}
      </select>
      <button className="desk-button pink" disabled={!files.length || upload.isPending || !tradeDate} onClick={() => upload.mutate()}>{upload.isPending ? "已提交" : "识别截图"}</button>
      <label><span>账户净值 {navFromOcr && <i>截图识别</i>}</span><input className="desk-input mono" type="number" value={nav} onChange={e => { setNav(e.target.value); setNavTouched(true); setNavFromOcr(false); }} /></label>
      <label><span>可用现金 {cashFromOcr && <i>截图识别</i>}</span><input className="desk-input mono" type="number" value={cash} onChange={e => { setCash(e.target.value); setCashTouched(true); setCashFromOcr(false); }} /></label>
      <button className="desk-button lime" disabled={!positionsQ.data?.length || !accountValid || confirm.isPending} onClick={() => { setConfirmMsg(null); confirm.mutate(); }}>{confirm.isPending ? "确认中…" : "确认账户并生成计划"}</button>
    </div>
    {provider?.configured === false && <p className="provider-note"><b>{provider.label}尚未配置。</b>{provider.detail}</p>}
    {ocrStatus?.status === "running" && <p className="progress-line">OCR 识别中 {ocrStatus.current ?? 0}/{ocrStatus.total ?? 0} · {ocrStatus.image ?? "准备中"}</p>}
    {ocrStatus?.status === "done" && <p className="progress-line">OCR 完成：识别 {ocrStatus.count ?? 0} 项 · {account?.complete ? "账户净值与现金已回填" : accountRecognized ? "部分账户金额已回填，缺项请手填" : "账户金额未识别，请手填"} · 失败 {ocrStatus.failed ?? 0}/{ocrStatus.total ?? 0} 张</p>}
    {!!account?.conflicts.length && <div className="ocr-account-warning"><b>多张截图的账户金额不一致，请核对</b>{account.conflicts.map((item, index) => <p key={index}>{item.field === "nav" ? "账户净值" : "可用现金"}：保留 {formatCurrency(item.kept)}，{item.image} 识别为 {formatCurrency(item.other)}</p>)}</div>}
    {(ocrStatus?.status === "error" || ocrFailed || ocrEmptyDone) && <div className="ocr-fail-box"><b>{ocrEmptyDone ? "OCR 未产出有效持仓" : "部分截图识别失败"}</b>{ocrStatus?.error && <p>{ocrStatus.error}</p>}{(ocrStatus?.failed_items ?? []).map((item, index) => <p key={index} className="mono">{item.image.split("/").pop()}: {item.error}</p>)}</div>}
    {!!positionsQ.data?.length && <PositionPreview rows={positionsQ.data} />}
    {confirmMsg && <div className={confirmMsg.tone === "ok" ? "confirm-ok-box" : "ocr-fail-box"}><b>{confirmMsg.tone === "ok" ? "确认成功" : "计划尚未完成"}</b><p>{confirmMsg.text}</p></div>}
    {(upload.isError || confirm.isError) && <ErrorLine error={(upload.error ?? confirm.error) as Error} />}
  </section>;
}

function PositionPreview({ rows }: { rows: Position[] }) {
  return <div className="position-preview"><div className="preview-head"><b>OCR 预览</b><span>{rows.length} 项 · 核对后再确认</span></div>{rows.map(row => <div key={row.position_id}><b>{row.name}</b><span className="mono">{row.code ?? "代码待匹配"}</span><span className="mono">{row.shares} 股</span><span className="mono">成本 {row.avg_cost}</span></div>)}</div>;
}

function Review({ plan, preview, upload, confirm, makeReview, reviewQ }: any) {
  return <section data-testid="page-review" className="discipline-page"><PageTitle eyebrow="DISCIPLINE REVIEW" title="结果与纪律分开评价" aside="计划与成交逐条对账" />
    <div className="review-score"><article><label>计划完成率</label><strong>{reviewQ.data ? `${Math.round(reviewQ.data.plan_completion_rate * 100)}%` : "—"}</strong></article><article><label>纪律评分</label><strong>{reviewQ.data?.discipline_score ?? "—"}</strong></article><blockquote>{reviewQ.data?.trade_result ?? "亏损但按计划执行，仍是合格交易；盈利但违纪，不是合格交易。"}</blockquote></div>
    <section className="desk-panel broker-import"><div className="panel-head"><div><small>BROKER EXECUTIONS</small><h2>导入券商成交 / 交割数据</h2></div><span>{plan?.status ?? "等待计划"}</span></div><p>CSV / Excel 先预览，人工确认后才写入成交台账。</p><label className="upload-button">选择成交文件<input data-testid="broker-file" type="file" accept=".csv,.xlsx,.xls" disabled={!plan} onChange={e => e.target.files?.[0] && upload.mutate(e.target.files[0])} /></label>
      {preview && <div data-testid="broker-preview" className="import-preview"><div><b>字段映射</b><span>{Object.entries(preview.field_mapping).map(([a, b]) => `${a}←${b}`).join(" · ")}</span></div><div><b>有效 {preview.parsed_rows.length}</b><b>异常 {preview.anomaly_rows.length}</b></div><button data-testid="confirm-import" className="desk-button lime" disabled={confirm.isPending} onClick={() => confirm.mutate()}>确认写入</button></div>}
      {confirm.isSuccess && <button data-testid="generate-review" className="desk-button yellow" disabled={makeReview.isPending} onClick={() => makeReview.mutate()}>生成日复盘</button>}
      {(upload.isError || confirm.isError || makeReview.isError) && <ErrorLine error={(upload.error ?? confirm.error ?? makeReview.error) as Error} />}
    </section>
    {reviewQ.data && <div className="review-results"><article><label>交易结果</label><b>{reviewQ.data.trade_result}</b></article><article><label>纪律结果</label><b>{reviewQ.data.discipline_result}</b></article><article><label>数据问题</label><b>{reviewQ.data.data_issues.length}</b></article></div>}
  </section>;
}

function DataAudit({ dataset, plan, version, onOpenPipeline, onRefresh }: { dataset?: DailyDatasetStatus; plan: DisciplinePlan | null; version?: DisciplineVersion; onOpenPipeline: () => void; onRefresh: () => void }) {
  const [batchId, setBatchId] = useState("");
  const [fallbackPreview, setFallbackPreview] = useState<Record<string, unknown> | null>(null);
  const probe = useMutation({ mutationFn: getDisciplineDataProbe });
  const preview = useMutation({ mutationFn: () => previewDisciplineOcrFallback(dataset!.trade_date, batchId), onSuccess: setFallbackPreview });
  const confirm = useMutation({ mutationFn: () => confirmDisciplineOcrFallback(dataset!.trade_date, batchId), onSuccess: onRefresh });
  return <section data-testid="page-data" className="discipline-page"><PageTitle eyebrow="DATA & RULE AUDIT" title="数据与规则可信度" aside="历史页面只读数据库" />
    <div className="audit-grid"><article><label>纪律版本</label><strong>{plan?.discipline_version ?? String(version?.version ?? "—")}</strong><p className="mono">{plan?.rules_hash?.slice(0, 16) ?? "等待计划"}</p></article><article><label>每日数据集</label><strong>{statusLabel[dataset?.status ?? "pending"]}</strong><p className="mono">{dataset?.dataset_id ?? "—"}</p></article><article><label>来源模式</label><strong>{dataset?.source_mode ?? "—"}</strong><p>{plan?.data_health.lockable ? "全部闸门通过" : (plan?.data_health.errors ?? []).join(" · ") || "等待生成"}</p></article></div>
    <RuleBook version={version} />
    <AutomationPanel tradeDate={dataset?.trade_date} />
    <section className="desk-panel fallback-panel"><div className="panel-head"><div><small>MANUAL FALLBACK</small><h2>趋势数据 OCR 备用</h2></div><span>非默认路径</span></div><p>仅当趋势动物 API 未形成正式数据集时，才把旧流水线批次人工确认为备用数据。</p><div className="fallback-actions"><input className="desk-input mono" placeholder="输入旧流水线 batch_id" value={batchId} onChange={e => setBatchId(e.target.value)} /><button className="desk-button" disabled={!batchId || !dataset || preview.isPending} onClick={() => preview.mutate()}>预览备用数据</button>{fallbackPreview && <button className="desk-button orange" disabled={confirm.isPending} onClick={() => confirm.mutate()}>人工确认发布</button>}<button className="desk-button yellow" onClick={onOpenPipeline}>打开数据流水线</button></div>{fallbackPreview && <pre className="probe-output">{JSON.stringify(fallbackPreview, null, 2)}</pre>}</section>
    <section className="desk-panel probe-panel"><div className="panel-head"><div><small>CONNECTION PROBE</small><h2>数据源连通性</h2></div><button className="desk-button cyan" onClick={() => probe.mutate()}>运行探针</button></div>{probe.data && <pre className="probe-output">{JSON.stringify(probe.data, null, 2)}</pre>}</section>
    {(preview.isError || confirm.isError || probe.isError) && <ErrorLine error={(preview.error ?? confirm.error ?? probe.error) as Error} />}
  </section>;
}

function AutomationPanel({ tradeDate }: { tradeDate?: string }) {
  const qc = useQueryClient();
  const statusQ = useQuery({ queryKey: ["automation-status"], queryFn: getAutomationStatus });
  const ledgerQ = useQuery({ queryKey: ["discipline-ledger", tradeDate], queryFn: () => getLedgerStatus(tradeDate), enabled: !!tradeDate });
  const fee = ledgerQ.data?.fee_schedule;
  const [form, setForm] = useState({
    commission_rate: "", minimum_commission: "", transfer_fee_rate: "",
    stamp_duty_rate: "", safety_multiplier: "1.2", configured: true,
  });
  useEffect(() => {
    if (!fee) return;
    setForm({
      commission_rate: String(fee.commission_rate),
      minimum_commission: String(fee.minimum_commission),
      transfer_fee_rate: String(fee.transfer_fee_rate),
      stamp_duty_rate: String(fee.stamp_duty_rate),
      safety_multiplier: String(fee.safety_multiplier),
      configured: fee.configured,
    });
  }, [fee]);
  const saveFee = useMutation({
    mutationFn: () => updateFeeSchedule({
      commission_rate: Number(form.commission_rate),
      minimum_commission: Number(form.minimum_commission),
      transfer_fee_rate: Number(form.transfer_fee_rate),
      stamp_duty_rate: Number(form.stamp_duty_rate),
      safety_multiplier: Number(form.safety_multiplier),
      configured: true,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["discipline-ledger"] });
      qc.invalidateQueries({ queryKey: ["automation-status"] });
      qc.invalidateQueries({ queryKey: ["discipline-plans"] });
    },
  });
  const testEmail = useMutation({ mutationFn: sendAutomationTestEmail, onSuccess: () => qc.invalidateQueries({ queryKey: ["automation-status"] }) });
  const runNow = useMutation({ mutationFn: () => runAutomationNow("finalize"), onSuccess: () => qc.invalidateQueries() });
  const status = statusQ.data;
  return <section className="desk-panel automation-panel">
    <div className="panel-head"><div><small>AUTOMATION & STORAGE</small><h2>持久数据库与自动邮件</h2></div><span>{status?.shadow_mode ? "影子验证" : status?.enabled ? "正式运行" : "尚未启用"}</span></div>
    <div className="automation-status-grid">
      <article><small>数据库</small><b>{status?.database.persistent ? "Supabase Postgres" : "本地 SQLite"}</b><span className="mono">{status?.database.revision ?? "读取中"}</span></article>
      <article><small>邮件</small><b>{status?.email.configured ? "Gmail 已配置" : "等待应用密码"}</b><span>{status?.email.recipient ?? "zhangzidi86@gmail.com"}</span></article>
      <article><small>每日节奏</small><b>17:00 → 19:30</b><span>影子核对 {status?.shadow_verified_days ?? 0}/3 天</span></article>
      <article><small>最近任务</small><b>{String(status?.latest_run?.status ?? "尚无记录")}</b><span>{String(status?.latest_run?.trade_date ?? "—")}</span></article>
    </div>
    {status?.readiness && <div className={`automation-readiness ${status.readiness.ready ? "ready" : "waiting"}`}>
      <header>
        <div><small>TODAY READINESS</small><b>{status.readiness.ready ? "今日闸门已齐" : `待完成 ${status.readiness.blockers.length} 项`}</b></div>
        <span>温转热 个股 {status.readiness.collection_summary.warm_to_hot_stock} · ETF {status.readiness.collection_summary.warm_to_hot_etf}</span>
      </header>
      {!!status.readiness.blockers.length && <div>{status.readiness.blockers.map(row => <p key={row.code}>
        <b>{row.message}</b><span>{row.action}</span>
      </p>)}</div>}
    </div>}
    <div className="fee-editor">
      <div><b>券商费率</b><span>截图缺少费用时按下列费率 × 安全倍数估算</span></div>
      <label>佣金率<input className="desk-input mono" type="number" step="0.00001" value={form.commission_rate} onChange={event => setForm({ ...form, commission_rate: event.target.value })} /></label>
      <label>最低佣金<input className="desk-input mono" type="number" step="0.01" value={form.minimum_commission} onChange={event => setForm({ ...form, minimum_commission: event.target.value })} /></label>
      <label>过户费率<input className="desk-input mono" type="number" step="0.000001" value={form.transfer_fee_rate} onChange={event => setForm({ ...form, transfer_fee_rate: event.target.value })} /></label>
      <label>卖出印花税率<input className="desk-input mono" type="number" step="0.00001" value={form.stamp_duty_rate} onChange={event => setForm({ ...form, stamp_duty_rate: event.target.value })} /></label>
      <label>安全倍数<input className="desk-input mono" type="number" step="0.1" min="1" value={form.safety_multiplier} onChange={event => setForm({ ...form, safety_multiplier: event.target.value })} /></label>
      <button className="desk-button yellow" disabled={saveFee.isPending} onClick={() => saveFee.mutate()}>保存费率</button>
    </div>
    <div className="automation-buttons">
      <button className="desk-button cyan" disabled={!status?.email.configured || testEmail.isPending} onClick={() => testEmail.mutate()}>发送测试邮件</button>
      <button className="desk-button lime" disabled={!tradeDate || runNow.isPending} onClick={() => runNow.mutate()}>立即运行</button>
      <span>加密备份由 GitHub Actions 每日 21:00 执行，保留 30 份。</span>
    </div>
    {(statusQ.isError || ledgerQ.isError || saveFee.isError || testEmail.isError || runNow.isError) && <ErrorLine error={(statusQ.error ?? ledgerQ.error ?? saveFee.error ?? testEmail.error ?? runNow.error) as Error} />}
  </section>;
}

function RuleBook({ version }: { version?: DisciplineVersion }) {
  if (!version?.rules_json) return <section className="desk-panel rulebook rulebook-empty"><b>纪律正文读取中…</b><span>等待当前激活版本返回规则快照。</span></section>;
  const rules = version.rules_json;
  const stock = rules.selection?.stock;
  const etf = rules.selection?.etf;
  const capacity = rules.capacity;
  const exits = rules.exit;
  const observation = rules.observation?.strength_change;
  const sourceName = version.source_path?.split(/[\\/]/).pop() || "规则源文件";
  const environment = Object.entries(capacity?.environment_factors ?? {});
  return <section className="desk-panel rulebook" aria-labelledby="rulebook-title">
    <header className="rulebook-head">
      <div className="rulebook-version"><small>ACTIVE RULESET</small><strong>{version.version}</strong></div>
      <div><small>CURRENT DISCIPLINE / WEB EDITION</small><h2 id="rulebook-title">当前纪律手册</h2><p>机器规则快照已转换为可读页面；每日计划仍按同一份规则计算。</p></div>
      <span className={version.status === "active" ? "active" : ""}>{version.status === "active" ? "当前生效" : version.status}</span>
    </header>

    <div className="rulebook-strip">
      <span>生效日期 <b className="mono">{version.effective_from}</b></span>
      <span>规则哈希 <b className="mono">{version.rules_hash.slice(0, 16)}</b></span>
      <span>规则来源 <b>{sourceName}</b></span>
    </div>

    <div className="rulebook-grid">
      <RuleCard index="01" kicker="ENTRY WINDOW" title="建仓时机" tone="yellow">
        <RuleList items={[
          `节气必须早于“${rules.selection?.max_entry_phase_exclusive ?? "—"}”`,
          stock?.requires_warm_to_hot || etf?.requires_warm_to_hot ? "趋势温度只接受“温 → 热”" : "按版本定义的温度迁移执行",
          stock?.exclude_warm_to_boiling || etf?.exclude_warm_to_boiling ? "禁止“温 → 沸”一步到位" : "温度跃迁不设额外限制",
        ]} />
      </RuleCard>

      <RuleCard index="02" kicker="A-SHARE GATES" title="A股硬门" tone="cyan">
        <RuleList items={[
          `流通市值 ≥ ${showNumber(stock?.min_float_market_cap_yi, "亿")}`,
          `日成交额 ≥ ${showNumber(stock?.min_amount_yi, "亿")}`,
          `右侧天数 ≤ ${showNumber(stock?.max_right_side_days, "天")}`,
          `板块温度 ≥ ${stock?.min_sector_temperature ?? "—"}`,
        ]} />
      </RuleCard>

      <RuleCard index="03" kicker="ETF GATES" title="ETF硬门" tone="pink">
        <RuleList items={[
          `基金规模 ≥ ${showNumber(etf?.min_aum_yi, "亿")}`,
          `日成交额 ≥ ${showNumber(etf?.min_amount_yi, "亿")}`,
          `趋势强度 ≥ ${showNumber(etf?.min_strength)}`,
          etf?.deduplicate_by_benchmark ? `同一基准只保留最优：${(etf.benchmark_tiebreakers ?? []).map(tiebreakerLabel).join(" → ")}` : "同一基准不去重",
        ]} />
      </RuleCard>

      <RuleCard index="04" kicker="CAPACITY" title="仓位与容量" tone="lime" wide>
        <div className="factor-row">{environment.map(([temperature, factor]) => <span key={temperature}><b>{temperature}</b>{showPercent(factor)}</span>)}</div>
        <div className="capacity-cases">
          <p><b>单个新仓</b><strong>{showPercent(capacity?.base_new_position_pct)}</strong></p>
          <p><b>常态上限</b><strong>{showNumber(capacity?.normal?.max_new_tools, "只")} / {showPercent(capacity?.normal?.max_added_weight)}</strong></p>
          <p><b>强共振上限</b><strong>{showNumber(capacity?.resonance?.max_new_tools, "只")} / {showPercent(capacity?.resonance?.max_added_weight)}</strong></p>
          <p><b>总仓位 / 工具数</b><strong>{showPercent(capacity?.max_total_weight)} / {showNumber(capacity?.max_tools, "只")}</strong></p>
        </div>
      </RuleCard>

      <RuleCard index="05" kicker="EXIT" title="离场纪律" tone="orange">
        <RuleList items={[
          `温度进入 ${(exits?.full_exit_temperatures ?? []).join(" / ") || "—"}：全部清仓`,
          `止盈信号：${(exits?.profit_signals ?? []).map(profitSignalLabel).join(" + ") || "—"}`,
          `每个止盈信号减仓 ${showPercent(exits?.fraction_per_signal)}`,
          `按 ${showNumber(exits?.round_lot, "股")} 整手执行；清仓优先于减仓`,
        ]} />
      </RuleCard>

      <RuleCard index="06" kicker="OBSERVATION" title="观察项不改动作" tone="blue">
        <RuleList items={[
          "强度周变化用于候选与持仓观察",
          observation?.decision_effect === "none" ? "只展示，不改变买卖决策" : `决策影响：${observation?.decision_effect ?? "—"}`,
          `已知标记：${Object.keys(observation?.documented_values ?? {}).filter(Boolean).join(" / ") || "无变化"}`,
          observation?.unknown_value_policy === "display_raw_only" ? "未知值仅原样展示，不推断" : `未知值策略：${observation?.unknown_value_policy ?? "—"}`,
        ]} />
      </RuleCard>
    </div>

    <footer><span>规则正文来自当前激活快照</span><span className="mono">source {String(rules.source_hash ?? "unavailable").slice(0, 16)}</span></footer>
  </section>;
}

function RuleCard({ index, kicker, title, tone, wide = false, children }: { index: string; kicker: string; title: string; tone: string; wide?: boolean; children: ReactNode }) {
  return <article className={`rule-card ${tone}${wide ? " wide" : ""}`}><header><span>{index}</span><div><small>{kicker}</small><h3>{title}</h3></div></header>{children}</article>;
}

function RuleList({ items }: { items: string[] }) {
  return <ul className="rule-list">{items.map(item => <li key={item}>{item}</li>)}</ul>;
}

function showNumber(value?: number, unit = "") { return value == null ? "—" : `${formatNumber(value)}${unit}`; }
function showPercent(value?: number) { return value == null ? "—" : formatPercent(value); }
function profitSignalLabel(value: string) { return ({ champagne: "开香槟", boiling: "温度沸" } as Record<string, string>)[value] ?? value; }
function tiebreakerLabel(value: string) { return ({ strength: "强度", amount_yi: "成交额", aum_yi: "规模", code: "代码" } as Record<string, string>)[value] ?? value; }

function PageTitle({ eyebrow, title, aside }: { eyebrow: string; title: string; aside: string }) {
  return <div className="page-title"><div><small>{eyebrow}</small><h1>{title}</h1></div><span>{aside}</span></div>;
}
function Empty({ title, text }: { title: string; text: string }) {
  return <div className="discipline-empty"><span>—</span><h3>{title}</h3><p>{text}</p></div>;
}
function ErrorLine({ error }: { error: Error }) { return <div className="error-line">{error?.message ?? "操作失败"}</div>; }

function nextStep(dataset?: DailyDatasetStatus, plan?: DisciplinePlan | null): { title: string; detail: string; tone: string } {
  if (dataset?.before_collection_window) return { title: "等待收盘数据窗口", detail: "北京时间 16:30 后再采集日终事实", tone: "wait" };
  if (dataset?.status === "awaiting_budget") return { title: "批准数据费用额度", detail: "费用闸门未通过，计划不会继续生成", tone: "warn" };
  if (!dataset || !["ready", "ready_degraded"].includes(dataset.status)) return { title: "采集日终数据", detail: "先让趋势事实与市场事实落库", tone: "go" };
  if (!plan) return { title: "等待生成信号草稿", detail: "数据已就绪，系统正在编排行动", tone: "wait" };
  if (!plan.portfolio_snapshot_id || plan.status === "awaiting_account") return { title: "确认券商账户", detail: "导入持仓、净值与现金后计算可执行股数", tone: "warn" };
  if (plan.status === "draft" && !plan.data_health.lockable) return { title: "处理数据闸门", detail: plan.data_health.errors.join(" · ") || "计划暂不可锁定", tone: "warn" };
  if (plan.status === "draft") return { title: "检查清单并锁定", detail: "确认动作与证据后，冻结明日计划", tone: "go" };
  if (plan.status === "locked") return { title: "按锁定计划执行", detail: "盘中不新增临时选股，不改纪律", tone: "done" };
  return { title: "检查最新计划", detail: "确认当前版本是否仍有效", tone: "wait" };
}

function bareCode(code?: string | null) { return String(code || "").toUpperCase().split(".", 1)[0]; }
function formatNumber(value?: number | null) { return value == null ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 }); }
function formatCurrency(value?: number | null) { return value == null ? "—" : `¥${formatNumber(value)}`; }
function formatYi(value?: number | null) { return value == null ? "—" : `${formatNumber(value)}亿`; }
function formatCompact(value?: number | null) { return value == null ? "—" : Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value); }
function formatPercent(value: number) { return `${Number(value * 100).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}%`; }

function actionText(item: DisciplinePlanItem) {
  if (item.side === "reduce") return `减 ${Math.round((item.reduce_fraction ?? 0) * 100)}% · ${item.target_shares ?? "待算"}股`;
  if (item.side === "buy") return item.target_shares == null ? "等待账户容量" : `买入至 ${item.target_shares}股`;
  if (item.side === "sell_all") return item.target_shares == null ? "全部清仓" : `卖出 ${item.target_shares}股`;
  if (item.side === "manual_review") return "人工确认";
  return "继续持有";
}

function actionEvidenceSummary(item: DisciplinePlanItem) {
  const keys = Object.keys(item.rule_evidence ?? {}).filter(key => !["source", "sources"].includes(key)).slice(0, 3);
  return keys.length ? keys.map(evidenceLabel).join(" · ") : "完整证据已保存";
}

function candidateReasons(row: DisciplineCandidate) {
  const failed = row.failed_rules ?? [];
  if (failed.length) return failed.map(ruleReason);
  if (row.capacity_reason === "environment_factor_zero") return ["硬门通过；环境系数为 0，当日不允许开新仓"];
  if (row.capacity_reason === "insufficient_cash") return [
    `硬门通过；单标的预算 ${formatCurrency(row.allocation_budget)}，理论 ${formatNumber(row.theoretical_lots)} 手，不足 1 手`,
  ];
  if (row.capacity_reason === "capacity_rank_exceeded") return [
    `硬门通过；今日最多 ${row.capacity_limit ?? "—"} 个新仓名额，当前排名第 ${row.selection_rank ?? "—"}`,
  ];
  if (row.capacity_reason === "same_index_duplicate") return [
    `硬门通过；同一基准指数只保留一只 ETF，本次择优 ${row.replaced_by ?? "其他候选"}`,
  ];
  if (row.capacity_reason === "price_unavailable") return ["硬门通过；收盘价缺失，无法计算整手数量"];
  return ["全部硬门通过"];
}

function candidateDecision(row: DisciplineCandidate) {
  if (row.failed_rules?.length) return `未过 ${row.failed_rules.length} 项`;
  const labels: Record<string, string> = {
    environment_factor_zero: "环境不开仓",
    insufficient_cash: "预算不足 1 手",
    capacity_rank_exceeded: "今日名额外",
    same_index_duplicate: "同指数去重",
    price_unavailable: "缺收盘价",
  };
  return labels[row.capacity_reason ?? ""] ?? "硬门通过";
}

function candidateSizing(row: DisciplineCandidate) {
  if (row.allocation_budget == null || row.theoretical_lots == null) return <><small>容量</small><b>待确认账户</b></>;
  return <>
    <small>预算 {formatCurrency(row.allocation_budget)}</small>
    <b>理论 {formatNumber(row.theoretical_lots)}手 · 可执行 {row.executable_shares ?? 0}股</b>
  </>;
}

function ruleReason(evidence: DisciplineEvidence) {
  const value = evidence.value;
  const missing = value == null || value === "";
  const shown = missing ? "未取到" : String(value);
  const rules: Record<string, string> = {
    permission: "当前账户没有交易权限",
    warm_to_hot: `温度为 ${shown}，要求温→热`,
    not_warm_to_boiling: `温度为 ${shown}，不允许温→沸一步到位`,
    "phase<大暑": `节气为 ${shown}，要求早于大暑`,
    "sector_temperature>=温": `板块温度为 ${shown}，要求 ≥ 温`,
    "float_market_cap_yi>=300": missing ? "流通市值未取到，要求 ≥300 亿" : `流通市值 ${formatNumber(Number(value))} 亿，要求 ≥300 亿`,
    "amount_yi>=5": missing ? "日成交额未取到，要求 ≥5 亿" : `日成交额 ${formatNumber(Number(value))} 亿，要求 ≥5 亿`,
    "right_side_days<=10": missing ? "右侧天数未取到，要求 ≤10 天" : `右侧 ${value} 天，要求 ≤10 天`,
    "aum_yi>=25": missing ? "ETF 规模未取到，要求 ≥25 亿" : `ETF 规模 ${formatNumber(Number(value))} 亿，要求 ≥25 亿`,
    "amount_yi>=2": missing ? "日成交额未取到，要求 ≥2 亿" : `日成交额 ${formatNumber(Number(value))} 亿，要求 ≥2 亿`,
    "strength>=80": missing ? "趋势强度未取到，要求 ≥80" : `趋势强度 ${formatNumber(Number(value))}，要求 ≥80`,
  };
  return rules[evidence.rule ?? ""] ?? `未通过：${evidence.rule ?? "未知规则"}`;
}

function evidenceLabel(key: string) {
  const labels: Record<string, string> = {
    danger: "危险信号", profit_signals: "止盈信号", champagne: "开香槟", boiling: "温度沸",
    volatility_up: "波动率放大", temperature_curr: "当前温度", temperature_prev: "前一温度",
    strength: "当前强度", strength_change: "强度变化", target_shares: "目标股数",
    current_shares: "当前股数", reduce_fraction: "减仓比例", reason: "原因",
    trend: "趋势动物", market: "市场数据", account: "券商账户",
  };
  return labels[key] ?? key.replace(/_/g, " ");
}

function displayValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return formatNumber(value);
  if (Array.isArray(value)) return value.length ? value.map(displayValue).join(" · ") : "—";
  if (typeof value === "object") return Object.entries(value as Record<string, unknown>).map(([key, nested]) => `${evidenceLabel(key)} ${displayValue(nested)}`).join(" · ");
  return String(value);
}
