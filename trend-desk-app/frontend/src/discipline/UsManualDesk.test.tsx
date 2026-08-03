import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  confirmUsAccountOcr,
  createUsAllocationPreview,
  createUsExitDecisionPlan,
  createUsManualPlan,
  getUsAccountOcr,
  getUsManualOverview,
  getUsManualPlans,
  importUsAccountScreenshots,
  refreshUsRiskAnchor,
  type UsAccountOcrBatch,
  type UsAllocationPreview,
  type UsCandidate,
  type UsManualOverview,
} from "../api";
import UsManualDesk from "./UsManualDesk";

vi.mock("../api", () => ({
  collectUsManualRun: vi.fn(async () => ({ run_id: "run-h6", status: "ready" })),
  confirmUsExecution: vi.fn(),
  createUsAllocationPreview: vi.fn(),
  createUsExitDecisionPlan: vi.fn(async () => ({ plan_id: "exit-plan" })),
  createUsManualPlan: vi.fn(async () => ({ plan_id: "buy-plan" })),
  getUsManualOverview: vi.fn(),
  getUsManualPlans: vi.fn(),
  getLlmConfig: vi.fn(async () => ({ backend: "codex_cli", choices: ["codex_cli"], providers: { codex_cli: { label: "Codex", vision: true, configured: true, detail: "" } } })),
  getUsAccountOcr: vi.fn(),
  importUsAccountScreenshots: vi.fn(),
  confirmUsAccountOcr: vi.fn(),
  lockUsManualPlan: vi.fn(),
  markUsRunNoExecution: vi.fn(),
  previewUsExecution: vi.fn(),
  refreshUsEtfBenchmark: vi.fn(),
  refreshUsRiskAnchor: vi.fn(async () => ({ anchor_id: "anchor-1" })),
}));

const riskAnchor = {
  anchor_id: "anchor-msft", candidate_id: 2, run_id: "run-h6", status: "ready" as const,
  signal_date: "2026-07-28", algorithm_version: "bitget-rtoken-ep3-v1",
  bitget_symbol: "RMSFTUSDT", quote_usdt: "100.25000000", quote_at: "2026-07-29T00:10:00",
  anchor_date: "2026-07-22", anchor_price_usdt: "95.25000000", anchor_distance: "0.049875311721",
  price_tick_usdt: "0.010000000000", daily_sha256: "daily-hash", hourly_sha256: "hourly-hash",
  error_code: null, error_message: null, semantic: "前期重要低点风险锚点，只用于仓位反推，不是真实止损",
  planning_enabled: true, source_mode: "active" as const,
  evidence_json: {
    daily_validation: { bar_count: 85, largest_jump_ratio: "0.021" },
    aggregated_sessions: Array.from({ length: 60 }, (_, index) => ({
      date: index === 0 ? "2026-05-01" : index === 59 ? "2026-07-28" : `session-${index}`,
    })),
    anchor: {
      future_data_used: false,
      ep3: {
        reference_high: "101.5", reference_high_date: "2026-07-15", confirmed_on: "2026-07-24",
      },
    },
  },
};

function candidate(overrides: Partial<UsCandidate> = {}): UsCandidate {
  return {
    candidate_id: 1, run_id: "run-h6", tm_id: 1, ticker_symbol: "ABC", ticker_name: "Example Corp",
    asset_type: "stock", venue_instrument: "RABCUSDT",
    venue_metadata_json: { price_precision: "2", quantity_precision: "4", min_trade_usdt: "5" },
    temperature_prev: "温", temperature_curr: "热", right_side_calendar_days: 2,
    right_side_age_bucket: "1-3", warm_to_hot: true, gate_passed: true, strength_local: "91.34000000",
    industry_tm_id: 10, industry_name: "软件", industry_temperature_curr: "热",
    industry_strength_local: "75", ticker_labels: ["趋势龙头"], trend_phase_curr: "立夏",
    market_cap: "100000", amount_1d: "1000", price_index: null,
    reference_price_usdt: "100.25", reference_price_at: "2026-07-29T00:10:00",
    reference_price_source: "bitget_public_quote", quote_status: "available", screen_status: "ready",
    primary_reason: "ready_after_h4_disciplines", all_reasons: ["relative_strength_at_least_90"],
    rank: 2, observation_rank: 2, raw_fields: {}, environment_id: "env-1",
    etf_identity_id: null, etf_benchmark_evidence_id: null, benchmark_family_id: null,
    exposure_key: null, benchmark_status: "not_applicable", etf_benchmark_evidence: null,
    risk_anchor_status: "pending",
    risk_anchor: null, legacy_read_only: false, ...overrides,
  };
}

const msft = candidate({ candidate_id: 2, tm_id: 2, ticker_symbol: "MSFT", ticker_name: "Microsoft",
  venue_instrument: "RMSFTUSDT", strength_local: "96.04000000", observation_rank: 1,
  risk_anchor_status: "ready", risk_anchor: riskAnchor });
const abc = candidate();
const spy = candidate({ candidate_id: 3, tm_id: 3, ticker_symbol: "SPY", ticker_name: "SPDR S&P 500 ETF",
  asset_type: "etf", venue_instrument: "RSPYUSDT", strength_local: "94", observation_rank: 3,
  industry_tm_id: null, industry_name: null, industry_temperature_curr: null, industry_strength_local: null,
  etf_identity_id: "identity-spy", etf_benchmark_evidence_id: "evidence-spy",
  benchmark_family_id: "sp-500", exposure_key: "sp-500|long|1|passive|unhedged", benchmark_status: "verified",
  etf_benchmark_evidence: {
    evidence_id: "evidence-spy", ticker_symbol: "SPY", status: "verified",
    benchmark_family_id: "sp-500", benchmark_name_raw: "S&P 500 Index",
    benchmark_canonical_name: "S&P 500 Index", strategy_type: "passive_index",
    exposure_direction: "long", leverage_multiplier: "1", currency_hedge: "none",
    exposure_key: "sp-500|long|1|passive|unhedged", source_type: "sec_filing",
    source_url: "https://www.sec.gov/Archives/edgar/data/example/spy.htm",
    retrieved_at: "2026-07-29T00:00:00", expires_at: "2026-08-29T00:00:00",
    fingerprint: {}, identity: { series_id: "S-SPY", class_id: "C-SPY" },
  },
});
const weak = candidate({ candidate_id: 4, tm_id: 4, ticker_symbol: "COR", ticker_name: "Cencora",
  strength_local: "89.94", observation_rank: null, screen_status: "observe",
  primary_reason: "relative_strength_below_90", all_reasons: ["relative_strength_below_90"] });
const executionSchedule = {
  calendar: "XNYS" as const, signal_date: "2026-07-28", session_date: "2026-07-29",
  generated_at_utc: "2026-07-29T00:00:00+00:00", generated_at_beijing: "2026-07-29T08:00:00+08:00",
  open_utc: "2026-07-29T13:30:00+00:00", open_new_york: "2026-07-29T09:30:00-04:00",
  open_beijing: "2026-07-29T21:30:00+08:00",
};

function overview(mode: "off" | "shadow" | "active" = "active"): UsManualOverview {
  const candidates = [abc, spy, msft, weak];
  return {
    capabilities: {
      enabled: true, manual_only: true, automated_trading: false, automated_collection: true,
      scheduler_enabled: true, private_bitget_access: false, bitget_public_market_only: true,
      order_api_enabled: false, etf_execution_enabled: true, etf_mode: "trade_pool",
      notice: "只生成清单，不会下单", rules_version: "us-manual-h6", h6_mode: mode,
      risk_anchor_source: "bitget_public_1d_1h_quote", risk_anchor_is_exit_stop: false,
      real_exit_source: "trend_animals_temperature_danger_boiling_champagne", wind_required: false,
      max_distinct_tickers: 20,
    },
    notice: "只生成清单，不会下单", state: "ready",
    policy: { account_usdt: "1000", risk_budget_usdt_per_trade: "2.5", min_single_notional_usdt: "25", target_single_notional_usdt: "50", max_open_positions: 20, environment_factors: { "温": "1", "平": "0.5", "凉": "0.25" } },
    run: {
      run_id: "run-h6", as_of_date: "2026-07-28", status: "ready", rules_version: "us-manual-h6",
      universe_count: 8, returned_count: 4, estimated_base_cost_cny: "0.25", approved_base_budget_cny: null,
      actual_base_cost_cny: "0.25", estimated_enrichment_cost_cny: "0.08", approved_enrichment_budget_cny: null,
      actual_enrichment_cost_cny: "0.08", estimated_total_cost_cny: "0.33", actual_total_cost_cny: "0.33",
      daily_cost_cap_cny: "5", cost_breakdown_json: {}, cache_hit: true,
      exit_status: "ready", environment_status: "ready", etf_mapping_status: "ready",
      funnel: { warm_to_hot_stocks: 6, warm_to_hot_etfs: 2, bitget_stock_intersection: 4,
        bitget_etf_intersection: 1, right_side_within_window: 4, relative_strength_90_plus: 3,
        etf_benchmark_verified: 1, etf_benchmark_blocked: 0, risk_anchor_ready: 1,
        risk_anchor_pending: 2, risk_anchor_blocked: 0, ready_for_plan: 1 },
      candidates,
    },
    candidates,
    positions: [{
      lot: { lot_id: 9, ticker_symbol: "KO", ticker_name: "Coca-Cola", remaining_quantity: "2", average_cost_usdt: "60", asset_type: "stock", holding_trading_days: 4 },
      decision: { decision_id: "exit-9", snapshot_id: "snapshot-9", lot_id: 9, as_of_date: "2026-07-28", action: "reduce_25", priority: 3, sell_ratio: "0.25", remaining_quantity_before: "2", planned_quantity: "0.5", reason_codes: ["champagne"], status: "pending", intended_execution_date: "2026-07-29", execution_schedule: executionSchedule, evidence_json: {} },
      as_of_date: "2026-07-28", action: "reduce_25", reason: "champagne", priority: 3,
      notice: "只生成 Bitget 手工卖出清单",
    }],
    exit_actions: { actionable: 1, manual_review: 0, legacy_read_only: 0 },
    market_environment: { environment_id: "env-1", run_id: "run-h6", as_of_date: "2026-07-28", market_tm_id: 99, market_temperature: "凉", environment_factor: "0.25", status: "ready", contract_hash: "hash", created_at: "2026-07-29T00:00:00" },
    account: { starting_equity_usdt: "1000", reported_equity_usdt: "998.5", cash_usdt: "900", ledger_cash_usdt: "900", open_cost_usdt: "100", position_count: 1, open_positions: [] },
    latest_account_ocr: null,
    capacity: { daily_limit_usdt: "250", daily_remaining_usdt: "200", portfolio_remaining_usdt: "900", cash_remaining_usdt: "850", available_usdt: "200", open_ticker_count: 1, occupied_ticker_count: 2, available_ticker_slots: 18, reservations: { all_locked_unfilled_usdt: "50", session_locked_unfilled_usdt: "50", session_confirmed_usdt: "0", locked_buy_tickers: ["NVDA"] }, account: { starting_equity_usdt: "1000", reported_equity_usdt: "998.5", cash_usdt: "900", open_cost_usdt: "100", position_count: 1, open_positions: [] } },
    next_step: { code: "execute_exits", title: "先处理趋势退出", detail: "1 个持仓有待执行卖出清单" },
    schedule: { timezone: "Asia/Shanghai", manual_full_collection_after: "07:00", automatic_slots: ["08:00", "08:30", "09:00", "09:30", "10:00"], automatic_cutoff: "10:00", scheduler_enabled: true, now_local: "2026-07-29T08:00:00+08:00" },
    cost: { daily_cap_cny: "5", estimated_total_cny: "0.33", actual_total_cny: "0.33", breakdown: {} },
    combos: { warm_to_hot_stocks: 6, warm_to_hot_etfs: 2, bitget_stock_intersection: 4, bitget_etf_intersection: 1 },
    quote_status: { available: 3, unavailable: 0 }, risk_anchor_status: { pending: 2, ready: 1, blocked: 0 },
    etf_benchmark_status: { verified: 1, blocked: 0 }, legacy: { read_only: true },
  };
}

const allocation: UsAllocationPreview = {
  allocation_preview_id: "allocation-1", run_id: "run-h6", environment_id: "env-1",
  intended_execution_date: "2026-07-29", execution_schedule: executionSchedule, status: "ready", daily_limit_usdt: "250",
  daily_remaining_usdt: "200", portfolio_remaining_usdt: "900", cash_remaining_usdt: "850",
  available_usdt: "200", allocated_usdt: "50", open_ticker_count: 1, available_ticker_slots: 18,
  exclusions_json: [], backfill_requested: false, snapshot_json: {}, created_at: "2026-07-29T00:00:00",
  notice: "只生成手工清单",
  items: [{ allocation_item_id: 1, allocation_preview_id: "allocation-1", candidate_id: 2,
    risk_anchor_id: "anchor-msft", priority: 1, status: "allocated", reason_code: null,
    reference_price_usdt: "100.25", anchor_price_usdt: "95.25", anchor_distance: "0.049875",
    risk_ceiling_usdt: "50.125", target_notional_usdt: "50", allocated_notional_usdt: "49.995",
    target_quantity: "0.4987", anchor_loss_estimate_usdt: "2.4935", minimum_notional_usdt: "25",
    evidence_json: {}, candidate: msft }],
};

const accountOcrReady: UsAccountOcrBatch = {
  batch_id: "us-account-20260729-test", capture_date: "2026-07-29", provider: "codex_cli",
  status: "ready", image_count: 1, processed_image_count: 1, failed_image_count: 0,
  account: { equity_usdt: "1000", cash_usdt: "850", currency: "USDT" },
  source_images: ["account-1.png"], conflicts: [], error: null,
  confirmed_snapshot_id: null, confirmed_at: null,
  created_at: "2026-07-29T00:00:00", updated_at: "2026-07-29T00:01:00",
  rows: [{ row_id: 1, ticker_symbol: "AJG", ticker_name: "Arthur J. Gallagher",
    venue_instrument: "RAJGUSDT", quantity: "0.5", average_cost_usdt: "300",
    current_price_usdt: "310", market_value_usdt: "155", unrealized_pnl_usdt: "5",
    source_image: "account-1.png", status: "ready", errors: [] }],
  notice: "OCR 结果仅为待确认账户快照",
};

afterEach(() => { cleanup(); vi.clearAllMocks(); });
beforeEach(() => {
  vi.mocked(getUsManualOverview).mockResolvedValue(overview());
  vi.mocked(getUsManualPlans).mockResolvedValue([]);
  vi.mocked(createUsAllocationPreview).mockResolvedValue(allocation);
  vi.mocked(getUsAccountOcr).mockResolvedValue(accountOcrReady);
  vi.mocked(importUsAccountScreenshots).mockResolvedValue(accountOcrReady);
  vi.mocked(confirmUsAccountOcr).mockResolvedValue({
    ...accountOcrReady, status: "confirmed", confirmed_snapshot_id: 7,
  });
});

function renderDesk() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><UsManualDesk /></QueryClientProvider>);
}

describe("UsManualDesk H6", () => {
  it("renders H6 environment, exits first, and strength ordering with one decimal", async () => {
    const view = renderDesk();
    expect(await screen.findByRole("heading", { name: "美股手工执行台" })).toBeInTheDocument();
    expect(await screen.findByText(/US-MANUAL-H6/)).toBeInTheDocument();
    expect(screen.getByTestId("us-exit-desk")).toHaveTextContent("当前持仓与退出动作");
    expect(screen.getByTestId("us-exit-desk")).toHaveTextContent("下一常规开盘减仓 25%");
    expect(screen.getByTestId("us-exit-desk")).toHaveTextContent("美东 2026/07/29 09:30 / 北京 2026/07/29 21:30");
    expect(view.container).toHaveTextContent("整体强度");
    expect(view.container).toHaveTextContent("25%");
    expect(view.container).toHaveTextContent("今日新增上限 250 USDT");
    expect(view.container).toHaveTextContent("补充 Bitget 持仓账户信息");
    expect(view.container).not.toHaveTextContent("DATA COST");
    const rows = Array.from(view.container.querySelectorAll(".us-candidate-row"));
    expect(rows[0]).toHaveTextContent("#1 MSFT");
    expect(rows[0]).toHaveTextContent("趋势相对强度 96.0");
    expect(rows[1]).toHaveTextContent("#2 ABC");
    expect(rows[2]).toHaveTextContent("#3 SPY · ETF");
    expect(view.container).not.toHaveTextContent("Wind");
  });

  it("labels EP3 as sizing-only and refreshes the H6 risk anchor", async () => {
    renderDesk();
    fireEvent.click((await screen.findByText(/#1 MSFT/)).closest("button")!);
    const drawer = screen.getByTestId("us-evidence-drawer");
    expect(drawer).toHaveTextContent("Bitget EP3 风险锚点");
    expect(drawer).toHaveTextContent("这不是实盘止损价");
    expect(drawer).toHaveTextContent("真实卖出只依据趋势动物");
    expect(drawer).toHaveTextContent("1D/1H 连续性");
    expect(drawer).not.toHaveTextContent("60 个完整交易日");
    expect(drawer).not.toHaveTextContent("参考摆动高点");
    expect(drawer).not.toHaveTextContent("未使用；所有判断截止信号日");
    expect(within(drawer).queryByLabelText(/止损/)).not.toBeInTheDocument();
    fireEvent.click(within(drawer).getByTestId("us-refresh-risk-anchor"));
    await waitFor(() => expect(vi.mocked(refreshUsRiskAnchor)).toHaveBeenCalledWith(2));
  });

  it("keeps only action-relevant ETF exposure facts in the execution drawer", async () => {
    renderDesk();
    fireEvent.click((await screen.findByText(/#3 SPY · ETF/)).closest("button")!);
    const drawer = screen.getByTestId("us-evidence-drawer");
    expect(drawer).toHaveTextContent("S&P 500 Index");
    expect(drawer).toHaveTextContent("只要权威来源唯一确认跟踪指数即可进入计划");
    expect(drawer).toHaveTextContent("由每日采集自动核验");
    expect(within(drawer).queryByRole("button", { name: /核验 ETF/ })).not.toBeInTheDocument();
    expect(drawer).not.toHaveTextContent("S-SPY / C-SPY");
    expect(within(drawer).queryByRole("link")).not.toBeInTheDocument();
  });

  it("creates an immutable multi-candidate allocation and requires explicit backfill", async () => {
    renderDesk();
    fireEvent.click(await screen.findByTestId("us-allocation-preview"));
    await waitFor(() => expect(vi.mocked(createUsAllocationPreview)).toHaveBeenCalledWith({ run_id: "run-h6" }));
    const card = await screen.findByTestId("us-allocation-card");
    expect(card).toHaveTextContent("推荐分配 · 2026-07-29");
    expect(card).toHaveTextContent("趋势信号日 2026-07-28");
    expect(card).toHaveTextContent("预定开盘（美国东部）2026/07/29 09:30");
    expect(card).toHaveTextContent("对应北京时间 2026/07/29 21:30");
    expect(card).toHaveTextContent("估算锚点风险 2.4935 USDT");
    const reason = within(card).getByLabelText("排除 MSFT 的理由");
    fireEvent.change(reason, { target: { value: "与现有行业暴露重复" } });
    fireEvent.click(within(card).getByRole("button", { name: "排除，不补位" }));
    await waitFor(() => expect(vi.mocked(createUsAllocationPreview)).toHaveBeenLastCalledWith({
      run_id: "run-h6", base_preview_id: "allocation-1",
      exclusions: [{ candidate_id: 2, reason: "与现有行业暴露重复" }], backfill: false,
    }));
  });

  it("creates the buy checklist with allocation id only", async () => {
    renderDesk();
    fireEvent.click(await screen.findByTestId("us-allocation-preview"));
    const card = await screen.findByTestId("us-allocation-card");
    fireEvent.click(within(card).getByTestId("us-create-plan"));
    await waitFor(() => expect(vi.mocked(createUsManualPlan)).toHaveBeenCalledWith({ allocation_preview_id: "allocation-1" }));
  });

  it("creates exits from the immutable exit decision", async () => {
    renderDesk();
    const exitDesk = await screen.findByTestId("us-exit-desk");
    fireEvent.click(within(exitDesk).getByRole("button", { name: "生成手工卖出清单" }));
    await waitFor(() => expect(vi.mocked(createUsExitDecisionPlan)).toHaveBeenCalledWith("exit-9"));
  });

  it("keeps shadow evidence visible but disables allocation", async () => {
    vi.mocked(getUsManualOverview).mockResolvedValueOnce(overview("shadow"));
    renderDesk();
    expect(await screen.findByText(/当前为 shadow/)).toBeInTheDocument();
    expect(screen.getByTestId("us-allocation-preview")).toBeDisabled();
  });

  it("keeps active mode fail-closed when the environment factor is zero", async () => {
    const frozen = overview("active");
    frozen.market_environment!.market_temperature = "寒";
    frozen.market_environment!.environment_factor = "0";
    frozen.capacity!.daily_limit_usdt = "0";
    frozen.capacity!.available_usdt = "0";
    vi.mocked(getUsManualOverview).mockResolvedValueOnce(frozen);
    renderDesk();
    expect(await screen.findByText(/环境系数为 0/)).toBeInTheDocument();
    expect(screen.getByTestId("us-allocation-preview")).toBeDisabled();
    expect(screen.getByTestId("us-no-trade")).toBeInTheDocument();
  });

  it("previews and explicitly confirms a fractional Bitget account screenshot", async () => {
    renderDesk();
    const file = new File(["image"], "account.png", { type: "image/png" });
    fireEvent.change(await screen.findByTestId("us-account-files"), { target: { files: [file] } });
    fireEvent.click(screen.getByTestId("us-account-upload"));
    await waitFor(() => expect(vi.mocked(importUsAccountScreenshots)).toHaveBeenCalled());
    const accountPanel = screen.getByTestId("us-account-ocr");
    expect(await within(accountPanel).findByText("AJG")).toBeInTheDocument();
    expect(within(accountPanel).getByText("0.5 股")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/我确认这些截图覆盖的是完整 Bitget 测试子账户/));
    fireEvent.click(screen.getByTestId("us-account-confirm"));
    await waitFor(() => expect(vi.mocked(confirmUsAccountOcr)).toHaveBeenCalledWith(
      "us-account-20260729-test",
      expect.objectContaining({
        equity_usdt: "1000", cash_usdt: "850", currency: "USDT",
        full_snapshot_confirmed: true,
      }),
    ));
  });
});
