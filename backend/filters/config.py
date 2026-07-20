# 股票池筛选纪律 v1.1（2026-07-12）：主筛选 M1-M4 + ETF 强度门 + 执行过滤 X1。
# 温度与节气都是 App 状态字。「暖」是旧 prompt 对「温」的误读档名，枚举两者都收。
# M5（右侧涨幅门）已于 2026-06-19 取消：右侧≤3天本就多为早段，涨幅门冗余。
WARM_PLUS_STATUSES = ("温", "暖", "热", "沸")   # 温度 ≥ 温（个股池门与 M1 板块门共用）

MIN_MARKET_CAP_YI = 300         # M2: 流通市值 ≥ 300 亿
MIN_TURNOVER_YI = 5             # M3: 日成交额 ≥ 5 亿
MAX_RIGHT_SIDE_DAYS = 10        # M4: 右侧天数 ≤ 10
JIEQI_MAX = "大暑"               # 仅保留为历史展示/兼容工具；不再是入场或离场硬门
WATCH_DAILY_CHANGE_PCT = 10.0   # X1: 日涨幅 > 10% 不直接追 → 观察池

# ETF 线门槛（小由 2026-06-16）：默认初值，待实跑校准；前端可即时覆盖。
# 个股的板块温度门(M1)、市值门(M2)对 ETF 不成立，ETF 改用自身温度 + 规模门 + ETF 成交额门。
ETF_MIN_AUM_YI = 25         # M2(ETF): 规模 ≥ 25 亿
ETF_MIN_TURNOVER_YI = 2     # M3(ETF): 日成交额 ≥ 2 亿
ETF_MIN_STRENGTH = 80       # M7(ETF): 趋势强度 ≥ 80

# App 的趋势节气隐喻按节气年序走：立春=趋势萌发 … 大暑=过热边界 … 大寒=趋势死亡。
JIEQI_ORDER = (
    "立春", "雨水", "惊蛰", "春分", "清明", "谷雨",
    "立夏", "小满", "芒种", "夏至", "小暑", "大暑",
    "立秋", "处暑", "白露", "秋分", "寒露", "霜降",
    "立冬", "小雪", "大雪", "冬至", "小寒", "大寒",
)


def jieqi_index(name: str | None) -> int | None:
    if not name:
        return None
    try:
        return JIEQI_ORDER.index(name)
    except ValueError:
        return None


def jieqi_lte(name: str | None, limit: str = JIEQI_MAX) -> bool | None:
    """节气是否 ≤ limit。未知节气返回 None（让调用方决定缺失语义）。
    出局检查的「大暑后」边界用它（大暑当天尚不算过界）。"""
    i, lim = jieqi_index(name), jieqi_index(limit)
    if i is None or lim is None:
        return None
    return i <= lim


def jieqi_lt(name: str | None, limit: str = JIEQI_MAX) -> bool | None:
    """节气是否严格 < limit。入场门 M6 用：到大暑即过热边界、不再入场（小由 2026-06-17）。
    与 jieqi_lte 只差 limit 当天——入场比出局保守一格。未知节气返回 None。"""
    i, lim = jieqi_index(name), jieqi_index(limit)
    if i is None or lim is None:
        return None
    return i < lim
