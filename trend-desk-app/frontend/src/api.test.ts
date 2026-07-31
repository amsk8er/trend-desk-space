import { describe, expect, it } from "vitest";
import { apiErrorMessage } from "./api";


describe("apiErrorMessage", () => {
  it("turns H6 structured failures into one Chinese next action", () => {
    expect(apiErrorMessage({
      detail: {
        code: "risk_anchor_not_confirmed",
        message: "窗口内没有已确认的 EP3 前期重要低点",
      },
    }, 409, "Conflict")).toBe(
      "窗口内没有已确认的 EP3 前期重要低点（risk_anchor_not_confirmed）；下一步：该标的暂无已确认 EP3 前期重要低点，保持观察",
    );
  });

  it("prefers a backend-provided structured next step", () => {
    expect(apiErrorMessage({
      detail: {
        code: "custom_failure",
        message: "数据尚未就绪",
        next_step: { title: "等待更新", detail: "请在 09:00 后重试" },
      },
    }, 409, "Conflict")).toBe(
      "数据尚未就绪（custom_failure）；下一步：请在 09:00 后重试",
    );
  });

  it("prefers the H6 next_action contract over legacy next_step", () => {
    expect(apiErrorMessage({
      detail: {
        code: "etf_benchmark_not_verified",
        message: "ETF 基准尚未核验",
        next_action: { code: "refresh_evidence", title: "唯一下一步", detail: "刷新 SEC 法定文件证据" },
        next_step: "旧提示",
      },
    }, 409, "Conflict")).toBe(
      "ETF 基准尚未核验（etf_benchmark_not_verified）；下一步：刷新 SEC 法定文件证据",
    );
  });
});
