import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createDecisionEvent, type DisciplinePlan } from "../api";
import DecisionTimeline from "./DecisionTimeline";


vi.mock("../api", async importOriginal => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getDecisionEvents: vi.fn(async () => [{
      event_id: "event-1",
      idempotency_key: "discipline:plan_generated:plan-1",
      trade_date: "2026-08-13",
      market: "a_share",
      event_type: "plan_generated",
      instrument_id: null,
      plan_id: "plan-1",
      candidate_id: null,
      reason_code: null,
      note: null,
      discipline_version: "v1.5",
      dataset_id: "dataset-1",
      facts_hash: "hash",
      payload_json: {},
      created_at: "2026-08-13T09:00:00",
    }]),
    createDecisionEvent: vi.fn(async payload => ({
      event_id: "event-2", idempotency_key: "manual:event-2", created_at: "2026-08-13T10:00:00",
      instrument_id: null, candidate_id: null, facts_hash: null, payload_json: {},
      ...payload,
    })),
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const plan: DisciplinePlan = {
  plan_id: "plan-1", signal_date: "2026-08-13", execute_date: "2026-08-14",
  discipline_version: "v1.5", rules_hash: "rules", status: "locked",
  market_mode: "normal", environment_factor: 1, dataset_id: "dataset-1",
  capacity_snapshot: {}, data_health: { lockable: true, errors: [], warnings: [], source_modes: {} },
  selection_snapshot: {}, items: [],
};

describe("DecisionTimeline", () => {
  it("shows system facts and appends a manual risk block", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><DecisionTimeline plan={plan} /></QueryClientProvider>);

    expect(await screen.findByText("计划生成")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("决定类型"), { target: { value: "risk_blocked" } });
    fireEvent.change(screen.getByLabelText("决定说明"), { target: { value: "数据日期不一致，停止执行" } });
    fireEvent.click(screen.getByRole("button", { name: "记入日志" }));

    await waitFor(() => expect(createDecisionEvent).toHaveBeenCalledWith(expect.objectContaining({
      trade_date: "2026-08-14",
      market: "a_share",
      event_type: "risk_blocked",
      plan_id: "plan-1",
      note: "数据日期不一致，停止执行",
    })));
  });
});
