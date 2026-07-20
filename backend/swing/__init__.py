"""swing — 重要低点标注（价格行为学 EP3 规则代码化）。

从「开拓者一号」v2/swing 移植，使 trend-desk 自包含（不跨项目 import）：
- detector:  纯算法核心（OHLC DataFrame → 重要/次要低点 + 止损阶梯）。逻辑与来源一致，6 单测锁定。
- eastmoney: 东财前复权日线获取（httpx）。
"""
