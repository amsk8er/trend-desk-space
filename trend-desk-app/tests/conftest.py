import os
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

TEMP_ROOT = Path(tempfile.mkdtemp(prefix="cindy-portal-tests-"))
os.environ.setdefault("TREND_DESK_DB_PATH", str(TEMP_ROOT / "trend-desk.db"))
os.environ.setdefault("TREND_DESK_STATE_DIR", str(TEMP_ROOT / "state"))
os.environ.setdefault("TREND_DESK_DATA_DIR", str(TEMP_ROOT / "data"))
os.environ.setdefault("TREND_DAILY_SCHEDULER_ENABLED", "false")
