// frontend/src/api.ts
// Typed fetch helpers for every backend endpoint. All calls go through here;
// stages never fetch() directly. Vite proxies /api -> backend in dev.

// ── shared types (mirror backend/api/read.py + routes.py shapes) ──

export type NodeStatus =
  | "todo"
  | "running"
  | "done"
  | "failed"
  | "skipped"
  | string;

export interface BatchSummary {
  batch_id: string;
  date: string;
  status: string;
}

export interface PipelineState {
  // per-node status keyed by node id (import/ocr/.../push). Free-form because
  // the backend writes whatever node keys it currently tracks.
  [nodeId: string]: unknown;
}

export interface State {
  batch_id: string;
  date: string;
  status: string;
  pipeline_state: PipelineState;
}

export interface OcrStats {
  total: number;
  todo: number;
  running: number;
  done: number;
  failed: number;
  skipped: number;
}

export interface OcrJob {
  job_id: number;
  image: string | null;
  image_index: number | null;
  status: NodeStatus;
  model: string | null;
  backend?: string | null;
  partial_reason: string | null;
  reason_friendly: string | null;
  rows: number;
}

export interface OcrData {
  stats: OcrStats;
  jobs: OcrJob[];
}

// Per-job OCR result (raw_json) for the detail panel.
export interface OcrResult {
  job_id: number;
  status: NodeStatus;
  partial_reason: string | null;
  reason_friendly: string | null;
  raw_json: Record<string, unknown>;
}

export interface Row {
  row_id: number;
  job_id: number;
  row_type: string | null;
  market: string | null;
  code: string | null;
  name: string | null;
  sector: string | null;
  sector_status?: string | null;
  temperature: number | null;
  temperature_status: string | null;
  strength: number | null;
  is_etf?: boolean;
  right_side_days: number | null;
  right_side_gain_pct: number | null;
  jieqi: string | null;
  first_hot_date: string | null;
  last_cool_date: string | null;
  review_status: string | null;
  review_reason: string | null;
  raw_fields: Record<string, unknown> | null;
  is_truncated?: boolean;
}

export interface Position {
  position_id: number;
  batch_id: string;
  // 券商持仓页无代码（OCR 禁脑补）→ 可为 null；真实代码由趋势动物持仓温度页按名称回填。
  code: string | null;
  name: string;
  shares: number;
  avg_cost: number;
  current_price: number;
  pnl_pct: number;
  stop_loss: number | null;
  entered_date: string | null;
  source_image: string | null;
  code_source: string | null; // trend_api / trend_api_fuzzy / holding_temp / manual
  confirmed: boolean;
  confirmed_at: string | null;
}

export interface BFilterData {
  white_list: unknown[];
  watch_list: unknown[];
  rejected: unknown[];
  manifest_json: Record<string, unknown>;
}

export interface ExitListItem {
  exit_id: number;
  batch_id: string;
  position_id: number;
  trigger: string;
  action: string;
  reason: string;
  detail?: Record<string, unknown>;
}

// 持仓状态总览：每只持仓一行（含热而正常持有的），让用户区分「判它持有」与「没数据」。
export interface ExitOverviewItem {
  position_id: number;
  code: string | null;
  name: string;
  temperature_status: string | null;
  temp_source: "trend_api" | "holding_temp" | "ocr_row" | null;
  right_side_days: number | null;
  right_side_gain_pct: number | null;
  jieqi: string | null;
  pnl_pct: number;
  shares: number;
  tags?: string[] | null;
  signal_unavailable?: string[] | null;
  suggest: string;
}

// POST /run/exit_check 的返回（逐条提醒 + 全量总览）。
export interface ExitCheckResult {
  items: ExitListItem[];
  overview: ExitOverviewItem[];
}

export interface ReviewData {
  summary: unknown;
  can_proceed: boolean;
  message: string;
}

export interface ChatToolsMeta {
  [name: string]: { needs_confirm: boolean };
}

// Prescreen returns the raw manifest_json object (free-form).
export type PrescreenData = Record<string, unknown>;

// ── swing（重要低点标注页）──
// time 用 "YYYY-MM-DD"，贴 lightweight-charts 的格式。

export interface SwingBar {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface SwingPoint {
  time: string;
  price: number;
}

export interface SwingStop {
  time: string;
  stop: number;
}

export interface SwingData {
  code: string;
  name: string;
  start: string;
  end: string;
  ohlc: SwingBar[];
  important_lows: SwingPoint[];
  minor_lows: SwingPoint[];
  stop_ladder: SwingStop[];
}

export interface SwingParams {
  code: string;
  start?: string;
  end?: string;
  k?: number;
  breakout_pct?: number;
}

// ── 纪律交易闭环 ──
export interface DisciplineEvidence { rule?: string; value?: unknown; passed?: boolean; source?: string; [k: string]: unknown }
export interface DisciplinePlanItem {
  item_id: number; plan_id: string; instrument_id: string; name: string;
  asset_type: "stock" | "etf"; side: "buy" | "sell_all" | "reduce" | "hold" | "manual_review";
  target_weight: number | null; target_shares: number | null; reduce_fraction: number | null;
  priority: number; rule_evidence: Record<string, unknown>; source_dates: Record<string, string>;
  data_sources: Record<string, string>; status: string;
}
export interface DisciplineCandidate {
  code: string; name: string; asset_type: string; eligible: boolean; shadow: boolean;
  price?: number; temperature_prev?: string; temperature_curr?: string; phase?: string;
  strength?: number; strength_change?: string | null; amount_yi?: number;
  float_market_cap_yi?: number; aum_yi?: number;
  right_side_days?: number; capacity_reason?: string;
  selection_rank?: number; selected_rank?: number; replaced_by?: string;
  capacity_limit?: number; allocation_budget?: number; theoretical_lots?: number;
  executable_lots?: number; executable_shares?: number; one_lot_cost?: number;
  budget_shortfall_to_one_lot?: number; estimated_gross?: number;
  estimated_fee?: number | null; estimated_cash_required?: number | null;
  fee_configured?: boolean;
  sector?: string | null; sector_temperature?: string | null;
  evidence?: DisciplineEvidence[]; failed_rules?: DisciplineEvidence[];
}
export interface DisciplinePlanAccount {
  nav: number; cash: number; market_value: number; confirmed: boolean;
  as_of_date?: string; source?: string;
}
export interface DisciplinePlan {
  plan_id: string; signal_date: string; execute_date: string; discipline_version: string;
  rules_hash: string; status: string; market_mode: string; environment_factor: number;
  dataset_id?: string | null; portfolio_snapshot_id?: number | null;
  plan_stage?: "signal" | "executable"; supersedes_plan_id?: string | null;
  change_notice?: string | null;
  account?: DisciplinePlanAccount | null;
  capacity_snapshot: Record<string, unknown>; data_health: {
    lockable: boolean; errors: string[]; warnings: string[]; source_modes: Record<string, string>;
  };
  selection_snapshot: {
    white_list?: DisciplineCandidate[]; watch_list?: DisciplineCandidate[];
    shadow_pool?: DisciplineCandidate[]; rejected?: DisciplineCandidate[];
  };
  items: DisciplinePlanItem[];
}
export interface DailyDatasetStatus {
  dataset_id: string; trade_date: string;
  status: "pending" | "checking" | "waiting_retry" | "fetching" | "ready" |
    "ready_degraded" | "awaiting_budget" | "manual_required" | "failed";
  source_mode: "trend_api" | "ocr_fallback" | "mixed";
  source_status: Record<string, { status?: string; as_of_date?: string; rows?: number; [k: string]: unknown }>;
  source_dates: Record<string, string>; attempt_count: number;
  next_retry_at: string | null; estimated_cost: number; actual_cost: number | null;
  approved_budget: number; error_code: string | null; error_message: string | null;
  capability_flags: { volatility_supported?: boolean; volatility_field?: string | null; [k: string]: unknown };
  cached: boolean; network_calls: number | null; trend_rows: number; market_rows: number;
  can_generate_plan: boolean; before_collection_window?: boolean; server_time_china?: string;
}
export interface DisciplineReview {
  review_id: number; plan_id: string; trade_date: string; plan_completion_rate: number;
  discipline_score: number; trade_result: string; discipline_result: string;
  violations: Record<string, unknown>[]; data_issues: string[]; metrics: Record<string, number>;
}
export interface BrokerImportPreview {
  import_id: number; plan_id: string | null; batch_id?: string | null; filename: string; status: string;
  field_mapping: Record<string, string>; parsed_rows: Record<string, unknown>[];
  anomaly_rows: Record<string, unknown>[];
}

export interface LedgerStatus {
  trade_date: string | null;
  snapshot: null | {
    snapshot_id: number; trade_date: string; nav: number; cash: number;
    market_value: number; source: string; reconciliation_status: string;
  };
  confirmation: null | {
    confirmation_id: number; trade_date: string; status: string; source: string;
  };
  positions: { code: string; name: string; asset_type: string; shares: number; avg_cost: number; as_of_date: string }[];
  fee_schedule: {
    commission_rate: number; minimum_commission: number; transfer_fee_rate: number;
    stamp_duty_rate: number; safety_multiplier: number; configured: boolean;
  };
  ready_for_roll_forward: boolean;
}

export interface AutomationStatus {
  enabled: boolean; shadow_mode: boolean; timezone: string;
  reminder_time: string; finalize_time: string; late_deadline: string;
  shadow_verified_days: number; shadow_ready_for_live: boolean;
  database: { backend: string; persistent: boolean; revision: string | null };
  email: { configured: boolean; sender: string; recipient: string; provider: string };
  readiness?: {
    trade_date: string; ready: boolean; human_action_required: boolean;
    blockers: { code: string; message: string; action: string; human_required: boolean }[];
    collection_summary: {
      status: string; source_mode?: string; warm_to_hot_stock: number;
      warm_to_hot_etf: number; warm_to_hot_total: number;
    };
    account_snapshot_id: number | null; fee_configured: boolean;
  };
  latest_run: Record<string, unknown> | null;
  latest_email: Record<string, unknown> | null;
}

export interface DisciplineRules {
  version?: string;
  effective_from?: string;
  source_hash?: string | null;
  selection?: {
    max_entry_phase_exclusive?: string;
    stock?: {
      min_float_market_cap_yi?: number; min_amount_yi?: number;
      max_right_side_days?: number; min_sector_temperature?: string;
      requires_warm_to_hot?: boolean; exclude_warm_to_boiling?: boolean;
    };
    etf?: {
      min_aum_yi?: number; min_amount_yi?: number; min_strength?: number;
      requires_warm_to_hot?: boolean; exclude_warm_to_boiling?: boolean;
      deduplicate_by_benchmark?: boolean; benchmark_tiebreakers?: string[];
    };
  };
  observation?: {
    strength_change?: {
      field?: string; applies_to?: string[]; decision_effect?: string;
      documented_values?: Record<string, string>; unknown_value_policy?: string;
    };
  };
  capacity?: {
    base_new_position_pct?: number;
    environment_factors?: Record<string, number>;
    normal?: { max_new_tools?: number; max_added_weight?: number };
    resonance?: { max_new_tools?: number; max_added_weight?: number };
    max_total_weight?: number; max_tools?: number;
  };
  exit?: {
    full_exit_temperatures?: string[]; profit_signals?: string[];
    fraction_per_signal?: number; round_lot?: number;
    full_exit_priority?: number; reduce_priority?: number; hold_priority?: number;
  };
}

export interface DisciplineVersion {
  version: string; effective_from: string; status: string;
  source_path: string; rules_json: DisciplineRules; rules_hash: string;
  created_at: string;
}

// ── core request helpers ──

const APP_BASE = import.meta.env.BASE_URL.replace(/\/$/, "");
const appUrl = (path: string) => `${APP_BASE}${path}`;

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(appUrl(path), init);
  if (!res.ok) {
    let detail = "";
    try {
      detail = await res.text();
    } catch {
      /* ignore */
    }
    throw new Error(`${init?.method ?? "GET"} ${path} -> ${res.status} ${res.statusText}${detail ? `: ${detail}` : ""}`);
  }
  // 204 / empty body guard
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function jsonPost<T>(path: string, body: unknown): Promise<T> {
  return req<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ── GET endpoints ──

export interface AuthStatus {
  required: boolean;
  authenticated: boolean;
}
export const getAuthStatus = () => req<AuthStatus>("/api/auth/status");
export const loginWithAccessKey = (accessKey: string) =>
  jsonPost<{ ok: boolean }>("/api/auth/login", { access_key: accessKey });
export const logout = () => jsonPost<{ ok: boolean }>("/api/auth/logout", {});

export const getState = (batchId: string) => req<State>(`/api/state/${batchId}`);

export const getDisciplineVersion = () => req<DisciplineVersion>(`/api/discipline/version`);
export const getDisciplinePlans = () => req<DisciplinePlan[]>(`/api/discipline/plans`);
export const getDisciplinePlan = (planId: string) => req<DisciplinePlan>(`/api/discipline/plan/${planId}`);
export const lockDisciplinePlan = (planId: string) => jsonPost<DisciplinePlan>(`/api/discipline/plan/${planId}/lock`, {});
export const getDisciplineReview = (planId: string) => req<DisciplineReview>(`/api/discipline/review/${planId}`);
export const generateDisciplineReview = (planId: string) => jsonPost<DisciplineReview>(`/api/discipline/review/${planId}`, {});
export const getDisciplineDataProbe = () => req<Record<string, unknown>>(`/api/discipline/data/probe`);
export const getDisciplineTodayData = () => req<DailyDatasetStatus>(`/api/discipline/data/today`);
export const checkDisciplineTodayData = () => jsonPost<DailyDatasetStatus>(`/api/discipline/data/today/check`, {});
export const approveDisciplineBudget = (tradeDate: string, amount: number) =>
  jsonPost<DailyDatasetStatus>(`/api/discipline/data/${tradeDate}/budget-approval`, { amount });
export const getDisciplineTodayPlan = () => req<DisciplinePlan>(`/api/discipline/plans/today`);
export async function importDisciplinePositions(
  tradeDate: string, files: File[], backend?: string,
): Promise<ScanTrigger> {
  const body = new FormData(); body.append("trade_date", tradeDate);
  if (backend) body.append("backend", backend);
  files.forEach(file => body.append("files", file));
  return req<ScanTrigger>(`/api/discipline/positions/import`, { method: "POST", body });
}
export const getDisciplinePositionImportStatus = (batchId: string) =>
  req<ScanStatus>(`/api/discipline/positions/import/status?batch_id=${encodeURIComponent(batchId)}`);
export interface ConfirmPositionsResult {
  batch_id: string;
  confirmed: number;
  position_lots_created?: number;
  portfolio_snapshot_id?: number;
  plan?: DisciplinePlan | null;
  message?: string | null;
  batch_trade_date?: string;
  signal_trade_date?: string;
  confirmed_at?: string;
}
export const confirmDisciplinePositions = (
  batchId: string, positionIds: number[] | null, nav: number, cash: number,
) => jsonPost<ConfirmPositionsResult>(
  `/api/discipline/positions/${batchId}/confirm`,
  { position_ids: positionIds, nav, cash },
);
export const previewDisciplineOcrFallback = (tradeDate: string, batchId: string) =>
  jsonPost<Record<string, unknown>>(`/api/discipline/data/${tradeDate}/ocr-fallback/preview`, { batch_id: batchId });
export const confirmDisciplineOcrFallback = (tradeDate: string, batchId: string) =>
  jsonPost<DailyDatasetStatus>(`/api/discipline/data/${tradeDate}/ocr-fallback/confirm`, { batch_id: batchId });
export async function previewBrokerImport(planId: string, file: File): Promise<BrokerImportPreview> {
  const body = new FormData(); body.append("file", file);
  return req<BrokerImportPreview>(`/api/discipline/broker/import/preview?plan_id=${encodeURIComponent(planId)}`, { method: "POST", body });
}
export const confirmBrokerImport = (importId: number) =>
  jsonPost<{ import: BrokerImportPreview; executions: Record<string, unknown>[] }>(`/api/discipline/broker/import/${importId}/confirm`, {});
export async function previewExecutionScreenshots(
  tradeDate: string, files: File[], backend?: string,
): Promise<BrokerImportPreview> {
  const body = new FormData(); body.append("trade_date", tradeDate);
  if (backend) body.append("backend", backend);
  files.forEach(file => body.append("files", file));
  return req<BrokerImportPreview>("/api/discipline/executions/ocr/preview", { method: "POST", body });
}
export const confirmExecutionOcr = (batchId: string, acceptValidRowsOnly: boolean) =>
  jsonPost<{ import: BrokerImportPreview; executions: Record<string, unknown>[] }>(
    `/api/discipline/executions/ocr/${batchId}/confirm`,
    { confirmed: true, accept_valid_rows_only: acceptValidRowsOnly },
  );
export const confirmNoExecution = (tradeDate: string) =>
  jsonPost<Record<string, unknown>>(`/api/discipline/trading-days/${tradeDate}/no-execution`, { confirmed: true });
export const getLedgerStatus = (tradeDate?: string) =>
  req<LedgerStatus>(`/api/discipline/ledger/status${tradeDate ? `?trade_date=${encodeURIComponent(tradeDate)}` : ""}`);
export const rollForwardLedger = (tradeDate: string) =>
  jsonPost<Record<string, unknown>>(`/api/discipline/ledger/${tradeDate}/roll-forward`, {});
export const updateFeeSchedule = (payload: LedgerStatus["fee_schedule"]) =>
  jsonPost<LedgerStatus["fee_schedule"]>("/api/discipline/ledger/fee-schedule", payload);
export const getAutomationStatus = () => req<AutomationStatus>("/api/automation/status");
export const runAutomationNow = (stage = "finalize") =>
  jsonPost<Record<string, unknown>>("/api/automation/run-now", { stage });
export const sendAutomationTestEmail = () =>
  jsonPost<Record<string, unknown>>("/api/automation/send-test", {});

export const getBatches = () => req<BatchSummary[]>(`/api/batches`);

export const getOcr = (batchId: string) => req<OcrData>(`/api/ocr/${batchId}`);

// Per-job raw OCR result (for the detail panel: view / download JSON).
export const getOcrResult = (jobId: number) =>
  req<OcrResult>(`/api/ocr/result/${jobId}`);

// Direct URL for the screenshot file (used as <img src> + download link).
export const ocrImageUrl = (jobId: number) => `/api/ocr/image/${jobId}`;

export const getRows = (batchId: string) => req<Row[]>(`/api/rows/${batchId}`);

export interface AggregateRow {
  row_id: number;
  code: string | null;
  name: string | null;
  sector: string | null;
  market: string | null;
  row_type: string | null;
  temperature_status: string | null;
  strength_a_share: number | null;   // A股内排名
  strength_intraday: number | null;  // 温转热页内排名
  right_side_days: number | null;
  right_side_gain_pct: number | null;
  jieqi: string | null;
  tags: string[] | null;
  price: number | null;
  market_cap_yi: number | null;
  turnover_yi: number | null;
  review_status: string | null;
  raw_fields: Record<string, unknown> | null;
}
export interface AggregateCategory {
  category: string;
  row_count_before_dedup: number;
  row_count_after_dedup: number;
  dropped_truncated: number;
  rows: AggregateRow[];
}
export interface AggregateData {
  batch_id: string;
  categories: AggregateCategory[];
}
export const runAggregate = (batchId: string) =>
  jsonPost<AggregateData>(`/api/run/aggregate`, { batch_id: batchId });

// 按类别聚合 + 按 code 去重（trend-desk 版 by_market）
export const getAggregate = (batchId: string) =>
  req<AggregateData>(`/api/aggregate/${batchId}`);

export const getPrescreen = (batchId: string) =>
  req<PrescreenData>(`/api/prescreen/${batchId}`);

export const getPositions = (batchId: string) =>
  req<Position[]>(`/api/positions/${batchId}`);
export const confirmOcrPositions = (batchId: string, positionIds?: number[]) =>
  jsonPost<{ batch_id: string; confirmed: number; position_lots_created: number }>(
    `/api/discipline/positions/${batchId}/confirm`, { position_ids: positionIds ?? null },
  );

export interface HoldingTempLite {
  holding_id: number;
  code: string | null;
  name: string;
  temperature_status: string | null;
  market: string | null;
  right_side_days?: number | null;
  right_side_gain_pct?: number | null;
  jieqi?: string | null;
  strength?: number | null;
  tags?: string[] | null;
  signal_unavailable?: string[] | null;
  data_source?: "ocr" | "trend_api" | null;
  as_of_date?: string | null;
  [k: string]: unknown;
}
export const getHoldingTemps = (batchId: string) =>
  req<HoldingTempLite[]>(`/api/holding_temp/${batchId}`);
export const pairPosition = (batchId: string, positionId: number, code: string) =>
  jsonPost<{ ok: boolean; code?: string; note?: string }>(`/api/positions/pair`, {
    batch_id: batchId,
    position_id: positionId,
    code,
  });

export const getBFilter = (batchId: string) =>
  req<BFilterData>(`/api/b_filter/${batchId}`);

export const getExitCheck = (batchId: string) =>
  req<ExitCheckResult>(`/api/exit_check/${batchId}`);

// 重要低点：输代码+区间 → 东财前复权日线 → detect() → 标注 JSON。
export const getSwing = (p: SwingParams) => {
  const q = new URLSearchParams({ code: p.code });
  if (p.start) q.set("start", p.start);
  if (p.end) q.set("end", p.end);
  if (p.k != null) q.set("k", String(p.k));
  if (p.breakout_pct != null) q.set("breakout_pct", String(p.breakout_pct));
  return req<SwingData>(`/api/swing?${q.toString()}`);
};

export const getReview = (batchId: string) =>
  req<ReviewData>(`/api/review/${batchId}`);

export const getChatTools = () => req<ChatToolsMeta>(`/api/chat/tools`);

export interface ConfigPaths {
  import_dir: string;
  pos_dir: string;
  archive_dir: string;
}
export const getConfigPaths = () => req<ConfigPaths>(`/api/config/paths`);

export interface LlmProviderInfo {
  label: string;
  vision: boolean;
  configured: boolean;
  detail: string;
}

// Which LLM backend OCR runs on: {backend} = server default; {choices} = selectable set.
export const getLlmConfig = () =>
  req<{ backend: string; choices: string[]; providers: Record<string, LlmProviderInfo> }>(`/api/config/llm`);

// ── 趋势动物官方 API：持仓同步 + 温转热选股流水线 ──
export interface TrendAnimalsConfig {
  enabled: boolean;
  configured: boolean;
  default_budget: number;
  selection_budget: number;
  ocr_fallback_available: boolean;
}
export interface TrendHoldingEstimate {
  ok: boolean;
  as_of_dates: Record<string, string>;
  tm_count: number;
  fields: string[];
  estimated_cost: number;
}
export interface TrendHoldingSyncResult {
  ok: boolean;
  source: "trend_api";
  as_of_date: string;
  rows: number;
  backfilled: number;
  incomplete_rows: unknown[];
  estimated_cost: number;
  actual_cost: number | null;
  cached: boolean;
}
export interface TrendSelectionEstimate {
  ok: boolean;
  as_of_date: string;
  counts: Record<string, number>;
  estimated_cost: number;
  estimate_breakdown: Record<string, number>;
  note: string;
}
export interface TrendSelectionResult {
  ok: boolean;
  batch_id: string;
  as_of_date: string;
  estimated_cost: number;
  actual_cost: number | null;
  component_counts: Record<string, number>;
  basic_component_counts: Record<string, number>;
  component_count_warnings: Array<{
    combo: string;
    constituent_count: number;
    returned_basic_count: number;
    note: string;
  }>;
  unique_components: number;
  sector_count: number;
  candidates: Array<Record<string, unknown>>;
  rejected: Array<Record<string, unknown>>;
  market: Record<string, unknown> | null;
  enrichment_warning: string | null;
}

export const getTrendAnimalsConfig = () =>
  req<TrendAnimalsConfig>(`/api/config/trend-animals`);
export const estimateTrendHolding = (batchId: string) =>
  jsonPost<TrendHoldingEstimate>(`/api/trend-animals/holding/estimate`, { batch_id: batchId });
export const syncTrendHolding = (batchId: string, approvedBudget: number) =>
  jsonPost<TrendHoldingSyncResult>(`/api/trend-animals/holding/sync`, {
    batch_id: batchId, approved_budget: approvedBudget,
  });
export const estimateTrendSelection = (date: string) =>
  jsonPost<TrendSelectionEstimate>(`/api/trend-animals/selection/estimate`, { date });
export const runTrendSelection = (params: {
  date: string;
  batch_id?: string | null;
  approved_budget: number;
  etf_min_aum_yi?: number | null;
  etf_min_turnover_yi?: number | null;
  min_market_cap_yi?: number | null;
  min_turnover_yi?: number | null;
}) => jsonPost<TrendSelectionResult>(`/api/trend-animals/selection/run`, params);

// ── POST endpoints (JSON body) ──

export const postImport = (source: string, date: string) =>
  jsonPost<{ batch_id: string }>(`/api/import`, { source, date });

// Rerun OCR. indices given → those screenshots (0-based image_index);
// omitted → first run / rerun all 未成功(skip+failed). Returns immediately
// (backend runs in background); `queued` = how many jobs were scheduled.
export const rerunOcr = (batchId: string, indices?: number[], backend?: string) =>
  jsonPost<{ ok: boolean; queued: number }>(`/api/run/ocr`, {
    batch_id: batchId,
    ...(indices ? { indices } : {}),
    ...(backend ? { backend } : {}),
  });

export const runOcr = (batchId: string, backend?: string) => rerunOcr(batchId, undefined, backend);

// Force-stop a stuck OCR run: cancels the background task, unsticks running jobs
// (→ todo), unlocks the node. `reset` = how many wedged jobs were reset.
export const cancelOcr = (batchId: string) =>
  jsonPost<{ cancelled: boolean; reset: number }>(`/api/run/ocr/cancel`, { batch_id: batchId });

// ETF 线全局参数（规模门 / ETF 成交额门，单位亿）。不传 → 后端用 config 默认。
// 个股线参数（市值门 / 成交额门，单位亿）同理。
export const runPrescreen = (
  batchId: string,
  opts?: {
    etfMinAumYi?: number | null;
    etfMinTurnoverYi?: number | null;
    minMarketCapYi?: number | null;
    minTurnoverYi?: number | null;
  },
) =>
  jsonPost<unknown>(`/api/run/prescreen`, {
    batch_id: batchId,
    etf_min_aum_yi: opts?.etfMinAumYi ?? null,
    etf_min_turnover_yi: opts?.etfMinTurnoverYi ?? null,
    min_market_cap_yi: opts?.minMarketCapYi ?? null,
    min_turnover_yi: opts?.minTurnoverYi ?? null,
  });

// Q4：riskPct/fixedStopPct 为可选全局参数（单笔风险% + 固定止损距离%，小数比例）。
// 不传 → 后端落回默认（1% 风险 + 结构止损参考）。
export const runBFilter = (
  batchId: string,
  opts?: { riskPct?: number | null; fixedStopPct?: number | null },
) =>
  jsonPost<unknown>(`/api/run/b_filter`, {
    batch_id: batchId,
    risk_pct: opts?.riskPct ?? null,
    fixed_stop_pct: opts?.fixedStopPct ?? null,
  });

export const runExitCheck = (batchId: string) =>
  jsonPost<ExitCheckResult>(`/api/run/exit_check`, { batch_id: batchId });

// 节点⑨ 日报：触发机械段 + 顶部 LLM 趋势研判（自动+缓存）。
// backend: claude_cli/codex_cli/anthropic_api；省略 → 服务端默认（config.LLM_BACKEND）。
// 缓存按 (batch, backend, facts_hash) 隔离，切换后端会强制重生成。
export const runReport = (batchId: string, backend?: string) =>
  jsonPost<unknown>(`/api/run/report`, { batch_id: batchId, ...(backend ? { backend } : {}) });

export const runPush = (batchId: string) =>
  jsonPost<{ url: string }>(`/api/run/push`, { batch_id: batchId });

export const chatTool = (name: string, args: Record<string, unknown>) =>
  jsonPost<{ result: unknown }>(`/api/chat/tool`, { name, args });

// ── chatbox conversation (Task 9.12) ──

export type ChatRole = "user" | "assistant" | "tool_call" | "tool_result";

export interface ChatHistoryMsg {
  msg_id: number;
  role: ChatRole;
  content: string;
  tool_name: string | null;
  tool_args: Record<string, unknown> | null;
}

export interface PendingTool {
  name: string;
  args: Record<string, unknown>;
}

// One driven turn: "done" = plain reply; "needs_confirm" = a write tool is
// waiting on the user; "max_rounds" = the tool loop hit its cap.
export interface ChatTurn {
  status: "done" | "needs_confirm" | "max_rounds";
  assistant: string;
  tool?: PendingTool;
}

interface ChatOpts {
  model?: string;
  currentNode?: string;
}

function chatBody(base: Record<string, unknown>, opts?: ChatOpts) {
  return {
    ...base,
    ...(opts?.model ? { model: opts.model } : {}),
    ...(opts?.currentNode ? { current_node: opts.currentNode } : {}),
  };
}

export const getChatHistory = (batchId: string) =>
  req<ChatHistoryMsg[]>(`/api/chat/history/${batchId}`);

export const sendChatMessage = (batchId: string, content: string, opts?: ChatOpts) =>
  jsonPost<ChatTurn>(`/api/chat/message`, chatBody({ batch_id: batchId, content }, opts));

export const confirmChatTool = (
  batchId: string,
  name: string,
  args: Record<string, unknown>,
  confirmed: boolean,
  opts?: ChatOpts,
) =>
  jsonPost<ChatTurn>(`/api/chat/confirm`, chatBody({ batch_id: batchId, name, args, confirmed }, opts));

// ── POST endpoints (multipart: files + batch_id) ──

function filesPost<T>(path: string, batchId: string, files: File[], backend?: string): Promise<T> {
  const fd = new FormData();
  fd.append("batch_id", batchId);
  if (backend) fd.append("backend", backend);
  for (const f of files) fd.append("files", f);
  return req<T>(path, { method: "POST", body: fd });
}

export const runPositions = (batchId: string, files: File[], backend?: string) =>
  filesPost<ScanTrigger>(`/api/run/positions`, batchId, files, backend);

function dirPost<T>(path: string, batchId: string, source: string, backend?: string): Promise<T> {
  const fd = new FormData();
  fd.append("batch_id", batchId);
  fd.append("source", source);
  if (backend) fd.append("backend", backend);
  return req<T>(path, { method: "POST", body: fd });
}

// 与上面的 run* 命中同一后端路由：带 source(目录) 走扫目录模式，带 files 走上传模式。
export const scanPositions = (batchId: string, source: string, backend?: string) =>
  dirPost<ScanTrigger>(`/api/run/positions`, batchId, source, backend);
export const scanHoldingTemp = (batchId: string, source: string, backend?: string) =>
  dirPost<ScanTrigger>(`/api/run/holding_temp`, batchId, source, backend);

// 温度页/券商持仓页识别现为异步后台：触发立即返回 {ok,total}，进度走 status 轮询。
export interface ScanTrigger { ok: boolean; total: number; reason?: string }
export interface ScanStatus {
  status: "idle" | "running" | "done" | "error";
  current?: number; total?: number; image?: string | null;
  ok?: number; failed?: number;
  rows?: number; backfilled?: number;  // 温度页
  count?: number;                       // 券商持仓页
  account?: {
    nav: number | null;
    cash: number | null;
    currency: string | null;
    source_images: string[];
    conflicts: {
      field: "nav" | "cash"; kept: number; other: number; image: string;
    }[];
    complete: boolean;
  };
  failed_items?: { image: string; error: string }[]; error?: string;
}
export const getHoldingTempStatus = (batchId: string) =>
  req<ScanStatus>(`/api/run/holding_temp/status?batch_id=${encodeURIComponent(batchId)}`);
export const getPositionsStatus = (batchId: string) =>
  req<ScanStatus>(`/api/run/positions/status?batch_id=${encodeURIComponent(batchId)}`);

export interface RunAutoResult {
  ran: string[];
  failed: { node: string; error: string }[];
}
export const runAuto = (batchId: string) =>
  jsonPost<RunAutoResult>(`/api/run/auto`, { batch_id: batchId });

// 趋势动物「持仓」温度页：写 HoldingTemp + 按名称回填 Position.code（真实代码权威源）。
export const runHoldingTemp = (batchId: string, files: File[], backend?: string) =>
  filesPost<ScanTrigger>(`/api/run/holding_temp`, batchId, files, backend);

// ── SSE ──

// Open an EventSource on the pipeline channel. Caller attaches listeners for
// "state" (JSON pipeline_state) and "error" events, and is responsible for
// calling .close().
export function openPipelineSse(batchId: string): EventSource {
  return new EventSource(appUrl(`/api/sse/pipeline/${batchId}`));
}
