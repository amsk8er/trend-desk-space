"""美股手工执行台。

该包只处理趋势动物数据、Bitget 公开报价和用户明确确认的本地台账。它不
保存交易所私钥，不调用私有接口，也没有任何下单能力。
"""

from .contracts import UsManualError

__all__ = ["UsManualError"]
