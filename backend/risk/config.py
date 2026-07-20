import os

# 账户权益：环境变量可覆盖（secrets.env / launchd plist），默认对齐纪律示例 50 万。
ACCOUNT_EQUITY = float(os.getenv("TREND_DESK_ACCOUNT_EQUITY", "500000"))
RISK_PCT = 0.005               # 单笔风险比例（权益的 0.5%）
MAX_POSITION_RATIO = 0.10      # 单只个股初始仓位上限 10%
MIN_POSITION_RATIO = 0.03      # 反推仓位 < 3% → 止损过远，只能观察
MIN_STOP_DISTANCE = 0.01       # 距离 < 1% 视为分时级噪音档，不作止损依据
