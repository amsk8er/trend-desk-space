"""趋势动物 API 客户端与 Trend Desk 适配层。"""

from backend.trend_animals.client import TrendAnimalsClient
from backend.trend_animals.errors import TrendAnimalsError

__all__ = ["TrendAnimalsClient", "TrendAnimalsError"]
