"""止损参考与仓位反推（纪律 v2 §五）。

止损参考不是预测，而是回答「这笔交易错了最多亏多少」。没有止损参考不允许开仓。
止损价数据截图不提供 —— 由外部喂入 raw_fields["stop_refs"]（Chatbox / 人工 / 未来行情源），
键名限 STOP_SOURCES。全缺 → no_entry。
"""
from dataclasses import dataclass, asdict
from backend.risk import config

STOP_SOURCES = {
    "hot_day_low": "温转热确认日低点",
    "low_20d": "最近20日低点",
    "ma21": "MA21",
    "ma42": "MA42",
    "platform_low": "最近有效平台下沿",
    "fixed_pct": "固定百分比止损",   # Q4：不接行情源，按现价下方固定 % 合成止损
}
PREFERRED = ("hot_day_low", "low_20d", "ma21")   # 默认优先三档
FALLBACK = ("ma42", "platform_low")


@dataclass(frozen=True)
class StopRef:
    source: str
    price: float


@dataclass
class SizingResult:
    verdict: str                 # "open" | "watch" | "no_entry"
    reason: str
    current_price: float | None = None
    stop_price: float | None = None
    stop_source: str | None = None
    stop_label: str | None = None
    distance_pct: float | None = None    # 0.05 = 5%
    max_loss: float | None = None
    position_amount: float | None = None
    position_ratio: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def collect_stop_refs(raw_fields: dict | None) -> list[StopRef]:
    out = []
    for src, price in ((raw_fields or {}).get("stop_refs") or {}).items():
        if src in STOP_SOURCES and isinstance(price, (int, float)) and not isinstance(price, bool):
            out.append(StopRef(src, float(price)))
    return out


def _distance(price: float, ref: StopRef) -> float:
    return (price - ref.price) / price


def pick_stop(current_price: float, refs: list[StopRef]) -> StopRef | None:
    """默认三档中距离 ≥ MIN_STOP_DISTANCE 的最近档；都没有再看 ma42/平台下沿。
    不使用过近的分时级低点作为止损依据（纪律 §五.2）。"""
    def usable(group):
        cands = [r for r in refs if r.source in group
                 and r.price < current_price
                 and _distance(current_price, r) >= config.MIN_STOP_DISTANCE]
        return min(cands, key=lambda r: _distance(current_price, r), default=None)
    return usable(PREFERRED) or usable(FALLBACK)


def size_position(*, current_price: float | None, refs: list[StopRef],
                  equity: float | None = None, risk_pct: float | None = None,
                  fixed_stop_pct: float | None = None) -> SizingResult:
    """fixed_stop_pct（Q4）：给定则不依赖行情/截图的止损参考，直接以现价下方
    fixed_stop_pct 合成止损（stop = price × (1 - pct)），覆盖结构止损 refs。"""
    equity = config.ACCOUNT_EQUITY if equity is None else equity
    risk_pct = config.RISK_PCT if risk_pct is None else risk_pct
    if not current_price or current_price <= 0:
        return SizingResult("no_entry", "现价缺失，无法计算止损距离 → 不允许开仓")
    if fixed_stop_pct:
        # 固定百分比止损直接定档（不走 pick_stop 的结构档筛选）
        chosen = StopRef("fixed_pct", current_price * (1 - fixed_stop_pct))
    else:
        if not refs:
            return SizingResult("no_entry", "无止损参考 → 不允许开仓，只能观察",
                                current_price=current_price)
        chosen = pick_stop(current_price, refs)
    if chosen is None:
        return SizingResult("no_entry",
                            "止损参考全部无效（高于现价或距离过近的分时噪音档）→ 不适合开仓",
                            current_price=current_price)
    dist = _distance(current_price, chosen)
    max_loss = equity * risk_pct
    ratio = risk_pct / dist
    base = dict(current_price=current_price, stop_price=chosen.price,
                stop_source=chosen.source, stop_label=STOP_SOURCES[chosen.source],
                distance_pct=dist, max_loss=max_loss)
    if ratio < config.MIN_POSITION_RATIO:
        return SizingResult("watch",
                            f"止损距离{dist:.1%}过远，反推仓位{ratio:.1%} < "
                            f"{config.MIN_POSITION_RATIO:.0%} → 不强行交易，进观察池",
                            **base, position_amount=equity * ratio, position_ratio=ratio)
    reason = f"按风险反推：{max_loss:.0f}元 ÷ {dist:.1%}"
    if ratio > config.MAX_POSITION_RATIO:
        ratio = config.MAX_POSITION_RATIO
        reason += f"，超单只上限截到 {config.MAX_POSITION_RATIO:.0%}"
    return SizingResult("open", reason, **base,
                        position_amount=equity * ratio, position_ratio=ratio)
