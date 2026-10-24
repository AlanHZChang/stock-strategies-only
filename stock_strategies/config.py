import os
from pathlib import Path

from dotenv import load_dotenv

# 直接匯入設定時也載入專案 .env；部署環境變數優先於檔案內容。
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# ── 快取（parquet）──
FINMIND_CACHE_DIR = os.environ.get(
    "FINMIND_CACHE_DIR",
    str(Path(__file__).resolve().parent.parent / ".cache" / "finmind"),
)
# 各頻率快取新鮮天數：超過則增量更新
CACHE_FRESH_DAYS = {"daily": 1, "monthly": 20, "weekly": 5, "quarterly": 60, "static": 7}

# ── 限流（FinMind 免費版約 600 req/hr）──
FINMIND_MIN_INTERVAL = 0.12       # 相鄰請求最小間隔秒
RATE_LIMIT_BACKOFF_BASE = 5       # 限流退避基數秒
RATE_LIMIT_MAX_RETRIES = 4

# ── context ──
MIN_PRICE_ROWS = 60               # 少於此列數視為新股／資料不足

# 台指期在 FinMind 的 data_id（夜盤盤前快報用）
FUTURES_ID = "TX"

CONFIG = {
    "eps_threshold": float(os.environ.get("EPS_THRESHOLD", "5.0")),
    "roe_threshold": float(os.environ.get("ROE_THRESHOLD", "15.0")),
    "backtest_years": int(os.environ.get("BACKTEST_YEARS", "3")),
    "hold_days": int(os.environ.get("HOLD_DAYS", "20")),
    "target_return": float(os.environ.get("TARGET_RETURN", "0.10")),
    "stop_loss": float(os.environ.get("STOP_LOSS", "0.08")),
    "min_tech_score_for_signal": int(os.environ.get("MIN_TECH_SCORE_FOR_SIGNAL", "60")),
    "min_total_score_for_buy": int(os.environ.get("MIN_TOTAL_SCORE_FOR_BUY", "65")),
    # 夜盤開盤方向分類門檻（漲跌幅 %）
    "night_gap_big": float(os.environ.get("NIGHT_GAP_BIG", "1.5")),
    "night_gap_small": float(os.environ.get("NIGHT_GAP_SMALL", "0.5")),
}
