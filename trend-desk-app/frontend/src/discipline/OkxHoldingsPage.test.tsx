import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getOkxMonitorOverview, updateOkxPositionPolicy } from "../api";
import OkxHoldingsPage from "./OkxHoldingsPage";

vi.mock("../api", async importOriginal => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getOkxMonitorCapabilities: vi.fn(async () => ({
      read_only: true, trading_routes: false, supported_products: [], policy_modes: ["auto_ema10", "manual"],
      email_provider: "gmail_smtp", monitor_enabled: true, shadow_mode: true,
    })),
    getOkxMonitorOverview: vi.fn(async () => ({
      status: "ready", stale: false,
      heartbeat: { enabled: true, shadow_mode: true, status: "healthy", last_tick_at: "2026-08-18T15:00:00", last_sync_at: "2026-08-18T15:00:00", error: null },
      positions: [{
        position_key: "main:XAAPL-USDT:long", captured_at: "2026-08-18T15:00:00",
        inst_id: "XAAPL-USDT", inst_type: "SPOT", product_kind: "us_stock_spot",
        underlying_symbol: "AAPL", side: "long", quantity: "5", available_quantity: "5",
        avg_price: "210", mark_price: null, last_price: "230", liquidation_price: null,
        leverage: null, unrealized_pnl: "100", is_cash: false,
        policy: { position_key: "main:XAAPL-USDT:long", mode: "auto_ema10", manual_stop: null,
          manual_reason: null, last_auto_stop: "220", effective_stop: "220",
          effective_source: "completed_rth_ema10", last_completed_session: "2026-08-17", updated_at: "2026-08-18T15:00:00" },
        state: { state: "active", product_day: "2026-08-18", cooldown_until: null,
          reentry_frozen_line: null, reentry_attempts: 0, consecutive_confirmed_bars: 0 },
        protections: [{ order_key: "stop-1", order_type: "conditional", side: "sell", quantity: "5",
          trigger_price: "218", trigger_price_type: "last", reduce_only: true, status: "live" }],
      }],
      cash: [], events: [{ event_id: 1, position_key: "main:XAAPL-USDT:long", event_type: "weak_protection",
        severity: "critical", first_seen_at: "2026-08-18T15:00:00", last_seen_at: "2026-08-18T15:00:00",
        email_count: 0, details: { inst_id: "XAAPL-USDT" } }],
    })),
    getOkxPositionHistory: vi.fn(async () => ({ position_key: "main:XAAPL-USDT:long", snapshots: [], events: [] })),
    updateOkxPositionPolicy: vi.fn(async () => ({})),
  };
});

afterEach(() => { cleanup(); vi.clearAllMocks(); });

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><OkxHoldingsPage /></QueryClientProvider>);
}

describe("OkxHoldingsPage", () => {
  it("keeps the read-only boundary and protection gap visible", async () => {
    renderPage();
    expect(await screen.findByText("OKX 持仓哨塔")).toBeInTheDocument();
    expect(screen.getByText("无下单、改单、撤单、划转接口")).toBeInTheDocument();
    expect(await screen.findByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("保护单弱于纪律线")).toBeInTheDocument();
    expect(vi.mocked(getOkxMonitorOverview)).toHaveBeenCalled();
  });

  it("requires a reason before saving a manual override", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("AAPL"));
    fireEvent.click(screen.getByText("手工纪律线"));
    const save = screen.getByRole("button", { name: "保存纪律设置" });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText("必须大于 0"), { target: { value: "221" } });
    fireEvent.change(screen.getByPlaceholderText("例如：结构低点上移；必须填写"), { target: { value: "结构低点上移" } });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(updateOkxPositionPolicy).toHaveBeenCalledWith("main:XAAPL-USDT:long", {
      mode: "manual", manual_stop: 221, reason: "结构低点上移",
    }));
  });
});
