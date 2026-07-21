import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- 静态存档：截图 / JSON / HTML 副本 + DB 一致性快照 (spec §8.2) ---
# 这些文件「写一次就不再被进程持续读写」，放 iCloud 同步目录是安全的，还能白捡
# iCloud 的异地备份。默认就在仓库 data/ 下（.gitignore）。可用 TREND_DESK_DATA_DIR 覆盖。
DATA = Path(os.getenv("TREND_DESK_DATA_DIR", ROOT / "data"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BACKUPS = DATA / "backups"          # VACUUM INTO 一致性快照，随 iCloud 走 = 免费异地备份 (F2)
INBOX = DATA / "inbox"; ARCHIVE = DATA / "archive"; FAILED = DATA / "failed"
REPORTS = DATA / "reports"; MANIFESTS = DATA / "manifests"

# --- SQLite 活动主库：必须放「非 iCloud」的本机快盘 (spec 决策 D7) ---
# 活动库运行时被持续读写，主库 .db 与旁路文件（rollback 的 -journal / WAL 的
# -wal/-shm）必须保持一致；iCloud 同步「打开中的文件」会上传残缺副本或用旧版盖新版，
# 是已知的 SQLite 损坏成因。故主库落在 ~/Library/Application Support/（iCloud 不碰）。
# 旧库自动迁移见 backup.relocate_legacy_db / app.py lifespan。
STATE_DIR = Path(os.getenv(
    "TREND_DESK_STATE_DIR",
    Path.home() / "Library" / "Application Support" / "trend-desk",
))
DB_PATH = Path(os.getenv("TREND_DESK_DB_PATH", STATE_DIR / "trend-desk.db"))
LEGACY_DB_PATH = ROOT / "data" / "trend-desk.db"   # D7 之前的旧位置（仓库内 / iCloud）

LLM_BACKEND = os.getenv("LLM_BACKEND", "claude_cli")
CHAT_MODEL_DEFAULT = os.getenv("CHAT_MODEL", "claude-sonnet-4-6")  # chatbox default; pill can override
PORT = int(os.getenv("PORT", "8848"))

# 节点⑨日报 LLM 趋势研判（自动+缓存；LLM 不可用时优雅降级，节点不 fail）
TREND_BRIEF_ENABLED = os.getenv("TREND_BRIEF_ENABLED", "true").lower() == "true"
TREND_BRIEF_LOOKBACK = int(os.getenv("TREND_BRIEF_LOOKBACK", "5"))   # 市场节奏回看 batch 数
TREND_BRIEF_MODEL = os.getenv("TREND_BRIEF_MODEL", CHAT_MODEL_DEFAULT)  # 默认 sonnet-4-6，控成本

# --- 趋势动物官方 API（Key 运行时从 secrets.env / 环境变量读取，不在 config 冻结）---
TREND_ANIMALS_ENABLED = os.getenv("TREND_ANIMALS_ENABLED", "false").lower() == "true"
TREND_ANIMALS_BASE_URL = os.getenv(
    "TREND_ANIMALS_BASE_URL", "https://www.trendtrader.cn/apiData/data")
TREND_ANIMALS_TIMEOUT_S = float(os.getenv("TREND_ANIMALS_TIMEOUT_S", "30"))
TREND_ANIMALS_DEFAULT_BUDGET = float(os.getenv("TREND_ANIMALS_DEFAULT_BUDGET", "0.50"))
TREND_ANIMALS_SELECTION_BUDGET = float(os.getenv("TREND_ANIMALS_SELECTION_BUDGET", "3.00"))

# --- 纪律交易台每日自动采集（默认关闭，避免开发/测试误产生费用）---
TREND_DAILY_SCHEDULER_ENABLED = os.getenv(
    "TREND_DAILY_SCHEDULER_ENABLED", "false").lower() == "true"
TREND_DAILY_TIMEZONE = os.getenv("TREND_DAILY_TIMEZONE", "Asia/Shanghai")
TREND_DAILY_START = os.getenv("TREND_DAILY_START", "16:30")
TREND_DAILY_RETRY_MINUTES = int(os.getenv("TREND_DAILY_RETRY_MINUTES", "30"))
TREND_DAILY_CUTOFF = os.getenv("TREND_DAILY_CUTOFF", "20:00")
TREND_ANIMALS_DAILY_AUTO_BUDGET = float(os.getenv(
    "TREND_ANIMALS_DAILY_AUTO_BUDGET", "3.50"))

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
for d in (DATA, BACKUPS, INBOX, ARCHIVE, FAILED, REPORTS, MANIFESTS):
    d.mkdir(parents=True, exist_ok=True)

# --- iCloud 截图录入/归档目录（外部，机器相关，env 可覆盖）---
# 与上方内部 DATA/inbox|archive|failed 是两套：这些指向 iCloud 同步盘，
# 是用户丢截图、app 归档原图的地方。不在 boot 时 mkdir（iCloud 未同步时不建空目录），
# 归档目录由写入方按需创建。
_SCREENSHOTS = Path(os.getenv(
    "TREND_DESK_SCREENSHOTS_ROOT",
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "stock-screenshots",
))

# --- 自动运行与邮件（密钥只允许由服务端环境变量注入）---
AUTOMATION_ENABLED = os.getenv("TREND_AUTOMATION_ENABLED", "false").lower() == "true"
AUTOMATION_SHADOW_MODE = os.getenv("TREND_AUTOMATION_SHADOW_MODE", "true").lower() == "true"
AUTOMATION_SECRET = os.getenv("TREND_AUTOMATION_SECRET", "")
PUBLIC_URL = os.getenv("TREND_DESK_PUBLIC_URL", "http://localhost:5173").rstrip("/")
EMAIL_FROM = os.getenv("TREND_EMAIL_FROM", "zhangzidi86@gmail.com")
EMAIL_TO = os.getenv("TREND_EMAIL_TO", "zhangzidi86@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("TREND_GMAIL_APP_PASSWORD", "")
IMPORT_DIR = Path(os.getenv("TREND_DESK_IMPORT_DIR", _SCREENSHOTS / "inbox" / "main"))
POS_DIR = Path(os.getenv("TREND_DESK_POS_DIR", _SCREENSHOTS / "inbox" / "pos"))
SCREENSHOTS_ARCHIVE = Path(os.getenv("TREND_DESK_SCREENSHOTS_ARCHIVE", _SCREENSHOTS / "archive"))
