from dataclasses import dataclass, field
from backend.filters import config

# 主筛选纪律 v2（2026-06-12）。缺数据一律 fail 并写明缺什么——
# 「没有数据不允许开仓」，UI 上能看到具体缺口，可重 OCR 或人工放行。

ETF_SECTOR = "中国行业ETF"
_ETF_CODE_PREFIXES = ("159", "51", "56", "58")   # 场内基金代码段，不与个股(60/68/00/30)冲突


def detect_etf(name: str | None, code: str | None, sector: str | None) -> bool:
    """ETF/LOF 识别（任一命中即 ETF 线）：名字含 ETF/LOF、代码 .OF 后缀或场内基金前缀、
    或趋势动物把板块标成「中国行业ETF」。偏宽：宁可个股极少误入 ETF 门，也别漏掉 ETF。"""
    n = (name or "").upper()
    if "ETF" in n or "LOF" in n:
        return True
    c = (code or "").upper()
    if c.endswith(".OF"):
        return True
    if c.split(".")[0].startswith(_ETF_CODE_PREFIXES):
        return True
    return sector == ETF_SECTOR


_US_CODE_SUFFIXES = (".O", ".N", ".A")   # 美股交易所后缀；注意 .OF 是 A 股开放式基金，不算


def is_us_market(name: str | None, code: str | None, sector: str | None) -> bool:
    """美股识别（本版只做 A 股，初筛据此剔除）：板块=美国ETF，或代码带美股交易所
    后缀 .O/.N/.A（.OF 是 A 股开放式基金，明确排除）。"""
    if sector == "美国ETF":
        return True
    c = (code or "").upper()
    if c.endswith(".OF"):
        return False
    return c.endswith(_US_CODE_SUFFIXES)


@dataclass
class Instrument:
    code: str
    name: str
    sector: str | None = None
    sector_status: str | None = None      # 板块温度（同批板块行回查）
    market_cap_yi: float | None = None
    turnover_yi: float | None = None
    right_side_days: int | None = None
    right_side_gain_pct: float | None = None
    jieqi: str | None = None
    daily_change_pct: float | None = None
    strength: int | None = None
    temperature_status: str | None = None
    stop_loss: float | None = None        # C6 一致性检查用
    is_etf: bool = False                  # ETF 线分叉标志（detect_etf 填充）
    etf_min_aum_yi: float | None = None   # 本次运行 ETF 规模门覆盖值（None → config 默认）
    etf_min_turnover_yi: float | None = None  # 本次运行 ETF 成交额门覆盖值
    min_market_cap_yi: float | None = None    # 个股市值门覆盖值（None → config 默认）
    min_turnover_yi: float | None = None      # 个股成交额门覆盖值（None → config 默认）
    tags: list = field(default_factory=list)  # OCR raw_fields["tags"]，纯透传到清单展示，不参与检查


@dataclass
class ManifestCtx:
    entries: dict = field(default_factory=dict)  # code -> [Instrument]


@dataclass
class CheckResult:
    ok: bool; reason: str = ""; check_id: str = ""


def _fail(cid, r):
    return CheckResult(False, r, cid)

def _pass(cid):
    return CheckResult(True, "", cid)


def M1(i: Instrument) -> CheckResult:
    if i.is_etf:
        # ETF 本身就是一个板块/主题，自身温度即等价于该主题热度，不回查板块。
        if i.temperature_status is None:
            return _fail("M1", "ETF 温度未知")
        if i.temperature_status not in config.WARM_PLUS_STATUSES:
            return _fail("M1", f"ETF 温度{i.temperature_status} < 温")
        return _pass("M1")
    if i.sector_status is None:
        return _fail("M1", f"板块温度未知（板块={i.sector or '?'}）")
    if i.sector_status not in config.WARM_PLUS_STATUSES:
        return _fail("M1", f"板块「{i.sector}」温度{i.sector_status} < 温")
    return _pass("M1")

def M2(i: Instrument) -> CheckResult:
    if i.is_etf:
        floor = i.etf_min_aum_yi if i.etf_min_aum_yi is not None else config.ETF_MIN_AUM_YI
        if i.market_cap_yi is None:
            return _fail("M2", "ETF 规模未知")
        if i.market_cap_yi < floor:        # 规模门：≥ floor 通过
            return _fail("M2", f"ETF 规模{i.market_cap_yi:g}亿 < {floor:g}亿")
        return _pass("M2")
    floor = i.min_market_cap_yi if i.min_market_cap_yi is not None else config.MIN_MARKET_CAP_YI
    if i.market_cap_yi is None:
        return _fail("M2", "市值未知")
    if i.market_cap_yi < floor:
        return _fail("M2", f"市值{i.market_cap_yi:g}亿 < {floor:g}亿")
    return _pass("M2")

def M3(i: Instrument) -> CheckResult:
    if i.is_etf:
        floor = i.etf_min_turnover_yi if i.etf_min_turnover_yi is not None else config.ETF_MIN_TURNOVER_YI
    else:
        floor = i.min_turnover_yi if i.min_turnover_yi is not None else config.MIN_TURNOVER_YI
    if i.turnover_yi is None:
        return _fail("M3", "日成交额未知")
    if i.turnover_yi < floor:
        return _fail("M3", f"日成交额{i.turnover_yi:g}亿 < {floor:g}亿")
    return _pass("M3")

def M4(i: Instrument) -> CheckResult:
    if i.right_side_days is None:
        return _fail("M4", "右侧天数未知（无法判断入场时效）")
    if i.right_side_days > config.MAX_RIGHT_SIDE_DAYS:
        return _fail("M4", f"右侧第{i.right_side_days}天 > {config.MAX_RIGHT_SIDE_DAYS}天入场窗口")
    return _pass("M4")


def M7(i: Instrument) -> CheckResult:
    """ETF 自身趋势强度门；个股不适用。"""
    if not i.is_etf:
        return _pass("M7")
    if i.strength is None:
        return _fail("M7", "ETF 趋势强度未知")
    if i.strength < config.ETF_MIN_STRENGTH:
        return _fail("M7", f"ETF 趋势强度{i.strength:g} < {config.ETF_MIN_STRENGTH:g}")
    return _pass("M7")

def M6(i: Instrument) -> CheckResult:
    """历史兼容检查；v1.1 主/B 筛不再调用。"""
    ok = config.jieqi_lt(i.jieqi)   # 严格 < 大暑：到大暑即过热边界，不入场
    if ok is None:
        return _fail("M6", f"节气未知（{i.jieqi or '空'}）")
    if not ok:
        return _fail("M6", f"节气{i.jieqi} ≥ {config.JIEQI_MAX}（过热边界，不入场）")
    return _pass("M6")


def X1(i: Instrument) -> CheckResult:
    """执行过滤：fail ≠ 拒绝，调用方解释为进观察池。"""
    if i.daily_change_pct is not None and i.daily_change_pct > config.WATCH_DAILY_CHANGE_PCT:
        return _fail("X1", f"日涨幅+{i.daily_change_pct:g}% > {config.WATCH_DAILY_CHANGE_PCT:g}% 不直接追"
                            " → 观察池：次日不破温转热点或回踩不破关键均线，再考虑")
    return _pass("X1")


# --- 数据一致性检查（保留自 v1）：同一标的多张截图字段必须一致 ---
_KEY_FIELDS = ("strength", "right_side_days", "stop_loss")

def C5(i: Instrument, mctx: ManifestCtx) -> CheckResult:
    rows = mctx.entries.get(i.code, [])
    if len(rows) < 2:
        return _pass("C5")
    for f in _KEY_FIELDS:
        vals = {getattr(r, f) for r in rows}
        if len(vals) > 1:
            return _fail("C5", f"manifest 内 {i.code} 字段 {f} 取值不一致: {vals}")
    return _pass("C5")

def C6(i: Instrument, mctx: ManifestCtx) -> CheckResult:
    rows = mctx.entries.get(i.code, [])
    sls = {r.stop_loss for r in rows if r.stop_loss is not None}
    if len(sls) > 1:
        return _fail("C6", f"止损取值多个: {sls}")
    return _pass("C6")
