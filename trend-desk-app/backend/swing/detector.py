"""swing_detector — 价格行为学 EP3「重要低点」判定（纯算法，不碰网络/不画图）。

规则：上涨趋势中，一个回调低点之后价格强势突破创新高 → 该低点 = 重要低点；
否则 = 次要低点。重要低点是追踪止损的锚点。

移植自「开拓者一号」v2/swing/swing_detector.py，逻辑不改（6 单测锁定回归基准）。
"""
from dataclasses import dataclass
from typing import List


@dataclass
class Swing:
    """一个摆动点。pos = bar 在序列中的整数位置；kind ∈ {'high','low'}。"""
    pos: int
    price: float
    kind: str
    date: object = None


def find_swings(df, k: int = 2) -> List[Swing]:
    """fractal 检测：bar i 的 low 严格低于前后各 k 根 → swing low；high 对称。

    返回按时间（pos）排序的摆动点列表。
    """
    lows = df["low"].tolist()
    highs = df["high"].tolist()
    dates = list(df.index)
    n = len(df)

    swings: List[Swing] = []
    for i in range(k, n - k):
        neighbours_low = lows[i - k:i] + lows[i + 1:i + k + 1]
        if all(lows[i] < x for x in neighbours_low):
            swings.append(Swing(pos=i, price=lows[i], kind="low", date=dates[i]))

        neighbours_high = highs[i - k:i] + highs[i + 1:i + k + 1]
        if all(highs[i] > x for x in neighbours_high):
            swings.append(Swing(pos=i, price=highs[i], kind="high", date=dates[i]))

    swings.sort(key=lambda s: s.pos)
    return swings


# A 股最小价格变动单位
TICK = 0.01


def _ref_high(low: Swing, swing_highs: List[Swing]):
    """L 的"前高"参照：L 之前最近的摆动高点；
    若 L 是趋势起点（之前无高点），退而用 L 之后第一个摆动高点。"""
    prior = [h for h in swing_highs if h.pos < low.pos]
    if prior:
        return prior[-1].price
    after = [h for h in swing_highs if h.pos > low.pos]
    if after:
        return after[0].price
    return None


def detect(df, k: int = 2, breakout_pct: float = 0.0) -> dict:
    """标注重要 / 次要低点 + 追踪止损阶梯。

    判定（EP3 规则）：对每个摆动低点 L，从其后逐根看收盘价——
      - 先跌破 L（close < L.low）→ L 失效，次要低点
      - 先强势创新高（close > 前高 ×(1+breakout_pct)）→ 重要低点
    breakout_pct 默认 0（方案①：收盘价 > 前高即算）；调高即开启方案②强势过滤。
    """
    swings = find_swings(df, k)
    swing_lows = [s for s in swings if s.kind == "low"]
    swing_highs = [s for s in swings if s.kind == "high"]
    closes = df["close"].tolist()
    n = len(closes)

    important: List[Swing] = []
    minor: List[Swing] = []
    for low in swing_lows:
        ref_high = _ref_high(low, swing_highs)
        verdict = "minor"
        if ref_high is not None:
            threshold = ref_high * (1.0 + breakout_pct)
            for j in range(low.pos + 1, n):
                if closes[j] < low.price:
                    break  # 先跌破 → 失效
                if closes[j] > threshold:
                    verdict = "important"
                    break
        (important if verdict == "important" else minor).append(low)

    stop_ladder = [(low.pos, round(low.price - TICK, 2)) for low in important]
    return {
        "important_lows": important,
        "minor_lows": minor,
        "stop_ladder": stop_ladder,
    }
