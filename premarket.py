"""夜盤盤前快報

早上排程（台灣 08:00，夜盤 05:00 收完、開盤 09:00 前）讀昨晚台指期夜盤
+ 昨日選股訊號，推播今日開盤方向預判與個股順風/逆風對照。

執行: uv run python premarket.py
"""

import os
import sys
import time
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from stock_strategies.night_session import get_night_session
from stock_strategies.sheet import read_latest_signals
from stock_strategies.notify import send_telegram, format_premarket_messages, select_premarket_signals
from stock_strategies.evaluate import evaluate_chips
from stock_strategies.chips import merge_chip_snapshot


REQUIRED_ENV = [
    "FINMIND_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GOOGLE_SHEET_ID",
    "GOOGLE_CREDS_JSON",
]


def enrich_premarket_signals(signals: list[dict], as_of: str | None = None) -> list[dict]:
    """只更新最新 BUY/WATCH 籌碼，不改分數、動作或歷史 Sheet 紀錄。"""
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    enriched = []
    for signal in select_premarket_signals(signals):
        row = dict(signal)
        saved = row.get("chips") or row.get("components", {}).get("chips")
        try:
            fresh = evaluate_chips(str(row["stock_id"]), as_of, refresh=True)
        except Exception:
            fresh = {"as_of": as_of, **{name: {"status": "unavailable", "reason": "盤前更新失敗"}
                     for name in ("institutional", "large_holders", "margin")}}
        row["chips"] = merge_chip_snapshot(fresh, saved)
        enriched.append(row)
    return enriched


def main():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"❌ 缺少環境變數: {missing}", file=sys.stderr)
        sys.exit(1)

    # 1. 取得台指期夜盤
    print(f"[{datetime.now()}] 取得台指期夜盤...")
    night = get_night_session()
    if night:
        print(f"  → {night['date']} 夜盤 {night['pct']:+.2f}% ({night['label']})")
    else:
        print("  → 夜盤資料暫時取不到")

    # 2. 讀昨日訊號（沿用 14:30 排程寫進 Signals 分頁的結果）
    #    讀 300 筆以涵蓋多日（單日掃描可能就數十筆 SKIP），
    #    format_premarket 會自動挑「最近一批有 BUY/WATCH」的日期。
    print("讀取昨日訊號...")
    try:
        signals = read_latest_signals(limit=300)
    except Exception as e:
        print(f"⚠️ 讀取訊號失敗: {e}", file=sys.stderr)
        signals = []
    print(f"  → {len(signals)} 筆")

    # 3. 法人／資券晚間才更新，盤前補抓；不重跑選股或覆寫昨日訊號。
    print("更新盤前籌碼摘要...")
    signals = enrich_premarket_signals(signals)

    # 4. 發送 Telegram 盤前快報
    print("發送 Telegram 盤前快報...")
    for message in format_premarket_messages(night, signals):
        send_telegram(message)
        time.sleep(0.5)
    print("✅ 完成")


if __name__ == "__main__":
    main()
