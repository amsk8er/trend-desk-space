import { cleanup, render, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DisciplineCandidate, DisciplinePlanItem } from "../api";
import { ActionMetrics } from "./DisciplineDesk";

afterEach(cleanup);

const item: DisciplinePlanItem = {
  item_id: 1,
  plan_id: "plan-1",
  instrument_id: "600001.SH",
  name: "示例股份",
  asset_type: "stock",
  side: "buy",
  target_weight: 0.05,
  target_shares: 500,
  reduce_fraction: null,
  priority: 3,
  rule_evidence: {},
  source_dates: {},
  data_sources: {},
  status: "draft",
};

const candidate: DisciplineCandidate = {
  code: "600001.SH",
  name: "示例股份",
  asset_type: "stock",
  eligible: true,
  shadow: false,
  temperature_prev: "温",
  temperature_curr: "热",
  strength: 96,
  strength_change: "↑",
  phase: "立夏",
  price: 21.36,
  sector: "软件",
  tags: ["历史新高", "开香槟"],
};

describe("ActionMetrics", () => {
  it("keeps the decision metrics visible in the action row", () => {
    const view = render(<ActionMetrics item={item} candidate={candidate} />);
    const metrics = view.getByLabelText("关键指标");

    expect(within(metrics).getByText("温→热")).toBeInTheDocument();
    expect(within(metrics).getByText("96 ↑")).toBeInTheDocument();
    expect(within(metrics).getByText("立夏")).toBeInTheDocument();
    expect(within(metrics).getByText("¥21.36")).toBeInTheDocument();
    expect(within(metrics).getByText("历史新高 · 开香槟")).toBeInTheDocument();
  });

  it("uses saved action evidence when a position is not in the candidate pool", () => {
    const view = render(<ActionMetrics item={{
      ...item,
      side: "reduce",
      rule_evidence: {
        temperature_curr: "热",
        strength: 88,
        strength_change: "↓",
        tags: ["开香槟"],
      },
    }} />);
    const metrics = view.getByLabelText("关键指标");

    expect(within(metrics).getByText("—→热")).toBeInTheDocument();
    expect(within(metrics).getByText("88 ↓")).toBeInTheDocument();
    expect(within(metrics).getByText("开香槟")).toBeInTheDocument();
  });
});
