"""美股右侧资产研究页。

本模块只保存趋势动物事实与本地浏览状态，不生成仓位、订单或买卖建议。
"""

from .contracts import RULES_VERSION, SCOPE

__all__ = ["RULES_VERSION", "SCOPE"]
