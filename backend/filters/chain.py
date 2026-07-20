from backend.filters.checks import (
    M1, M2, M3, M4, M7, X1, C5, C6,
    Instrument, ManifestCtx, CheckResult,
)

def run_main_filter(i: Instrument) -> list[CheckResult]:
    """初筛口径 M1-M4：盘后主截图即有的数据。
    M6(节气) 的数据来自温转热页主 OCR；放在初筛会在数据未到时误拒 —— 留给 B 筛全量检查。
    （M5 右侧涨幅门已于 2026-06-19 取消：右侧≤3天本就多为早段，涨幅门冗余。）"""
    # ETF 强度来自初筛后的 enrichment 快照；此处尚可能为空，留给 B/纪律计划层。
    return [M1(i), M2(i), M3(i), M4(i)]

def run_b_phase(i: Instrument, mctx: ManifestCtx) -> list[CheckResult]:
    """B 筛 = v1.1 硬门 + 跨截图一致性；节气只展示，不再拒绝。"""
    return [M1(i), M2(i), M3(i), M4(i), M7(i), C5(i, mctx), C6(i, mctx)]

def run_execution_filter(i: Instrument) -> CheckResult:
    """交易执行过滤：fail = 进观察池，不是拒绝。"""
    return X1(i)

def fails(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if not r.ok]
