"""SEPA 趨勢模板與 VCP 診斷，使用本專案 FinMind OHLCV，無分析函式內 I/O。

規則參考 tbdavid2019/telegram-bot-stock2 的 tools/stock_analysis.py：
https://github.com/tbdavid2019/telegram-bot-stock2/blob/master/tools/stock_analysis.py
此處依數學條件獨立實作；台股相對強弱基準採 TAIEX，未知條件回傳 None。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd

from .datasources import get_index_history


def load_sepa_benchmark() -> pd.DataFrame:
    """批次執行時只呼叫一次；大盤失效不影響原有個股評分。"""
    try:
        start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        return get_index_history("TAIEX", start=start)
    except Exception:
        return pd.DataFrame()


def _number(value, digits: int = 4) -> float | None:
    try:
        number = float(value)
        return round(number, digits) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _ordered(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or "close" not in frame.columns:
        return pd.DataFrame(columns=["close"])
    data = frame.copy()
    if "date" in data.columns:
        data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
        data = data.dropna(subset=["date"]).sort_values("date").drop_duplicates("date", keep="last")
    for column in ("close", "high", "low", "volume"):
        if column in data.columns:
            values = pd.to_numeric(data[column], errors="coerce")
            data[column] = values.where(values.map(lambda value: pd.notna(value) and math.isfinite(value)))
    data["close"] = data["close"].where(data["close"] > 0)
    return data.reset_index(drop=True)


def _complete_mean(values: pd.Series, count: int) -> float | None:
    tail = values.iloc[-count:]
    if len(tail) != count or not tail.notna().all():
        return None
    return _number(tail.mean(), 10)


def analyze_vcp(prices: pd.DataFrame) -> dict:
    """比較 5/10/20 日的相鄰區間振幅，以及近 10／50 日均量。"""
    data = _ordered(prices)
    ratios = {str(window): None for window in (5, 10, 20)}
    raw_ratios = []
    if all(column in data.columns for column in ("high", "low")):
        spread = (data["high"] - data["low"]).where(data["high"] >= data["low"])
        ranges = spread / data["close"]
        for window in (5, 10, 20):
            recent = _complete_mean(ranges, window)
            previous = _complete_mean(ranges.iloc[:-window], window)
            if recent is not None and previous is not None and previous > 0:
                ratio = recent / previous
                ratios[str(window)] = _number(ratio, 6)
                raw_ratios.append(ratio)
    volume_ratio = None
    if "volume" in data.columns:
        volume = data["volume"].where(data["volume"] >= 0)
        recent_volume = _complete_mean(volume, 10)
        average_volume = _complete_mean(volume, 50)
        if recent_volume is not None and average_volume is not None and average_volume > 0:
            volume_ratio = recent_volume / average_volume
    ready = len(raw_ratios) == 3 and volume_ratio is not None
    contraction = bool(all(ratio < 1 for ratio in raw_ratios)) if len(raw_ratios) == 3 else None
    return {
        "status": "available" if ready else "insufficient_data",
        "detected": bool(contraction and volume_ratio < 1) if ready else None,
        "range_contraction_ratios": ratios,
        "volume_10d_vs_50d": _number(volume_ratio, 6),
        "contraction_pass": contraction,
        "note": "量化收斂診斷，仍需確認基底與突破；缺少有效量能時不判定成立。",
    }


def _relative_returns(data: pd.DataFrame, benchmark: pd.DataFrame | None) -> tuple:
    index = _ordered(benchmark)
    if data.empty or index.empty or "date" not in data.columns or "date" not in index.columns:
        return None, None
    # 以共同交易日及個股最後日期對齊，禁止使用較晚的大盤報價。
    paired = data[["date", "close"]].merge(index[["date", "close"]], on="date", suffixes=("_stock", "_index"))
    if len(paired) < 63 or paired.iloc[-1]["date"] != data.iloc[-1]["date"]:
        return None, None
    sample = paired.iloc[-63:]
    if sample[["close_stock", "close_index"]].isna().any().any():
        return None, None
    return tuple(float(sample.iloc[-1][column] / sample.iloc[0][column] - 1)
                 for column in ("close_stock", "close_index"))


def analyze_sepa(prices: pd.DataFrame, benchmark: pd.DataFrame | None = None) -> dict:
    """八項趨勢模板、參考樞紐價／停損與 VCP；不改寫買賣訊號或權重。"""
    data = _ordered(prices)
    close = data["close"]
    price = _number(close.iloc[-1], 10) if len(close) else None
    averages = {window: _complete_mean(close, window) for window in (50, 150, 200)}
    old_average = _complete_mean(close.iloc[:-20], 200)
    year_available = len(close) >= 252 and close.iloc[-252:].notna().all()
    high = float(close.iloc[-252:].max()) if year_available else None
    low = float(close.iloc[-252:].min()) if year_available else None
    stock_return, benchmark_return = _relative_returns(data, benchmark)

    def check(values, condition):
        return bool(condition()) if all(value is not None for value in values) else None

    ma50, ma150, ma200 = (averages[window] for window in (50, 150, 200))
    definitions = [
        ("價格在長期均線上方", check((price, ma150, ma200), lambda: price > ma150 and price > ma200)),
        ("150 日線高於 200 日線", check((ma150, ma200), lambda: ma150 > ma200)),
        ("200 日線高於 20 個交易日前", check((ma200, old_average), lambda: ma200 > old_average)),
        ("50 日線高於兩條長期均線", check((ma50, ma150, ma200), lambda: ma50 > ma150 and ma50 > ma200)),
        ("較 52 週收盤低點上漲至少 25%", check((price, low), lambda: price >= low * 1.25)),
        ("距 52 週收盤高點不超過 25%", check((price, high), lambda: price >= high * 0.75)),
        ("共同交易日的近三月報酬不弱於加權指數", check((stock_return, benchmark_return), lambda: stock_return >= benchmark_return)),
        ("價格站在 50 日線之上", check((price, ma50), lambda: price > ma50)),
    ]
    rules = [{"rule": index, "criterion": criterion, "passed": passed}
             for index, (criterion, passed) in enumerate(definitions, 1)]
    passed_count = sum(rule["passed"] is True for rule in rules)
    evaluated_count = sum(rule["passed"] is not None for rule in rules)
    stage2 = bool(passed_count >= 6 and price is not None and ma200 is not None and price > ma200 and old_average is not None)
    pivot = float(close.iloc[-20:].max()) if len(close) >= 20 and close.iloc[-20:].notna().all() else None
    atr = None
    if all(column in data.columns for column in ("high", "low")):
        true_range = pd.concat([
            (data["high"] - data["low"]).where(data["high"] >= data["low"]),
            (data["high"] - close.shift()).abs(), (data["low"] - close.shift()).abs(),
        ], axis=1).max(axis=1, skipna=False)
        atr = _complete_mean(true_range, 14)
    return {
        "status": "available" if evaluated_count == 8 else "insufficient_data" if len(close) < 220 else "partial",
        "template_score": f"{passed_count}/8",
        "passed_count": passed_count,
        "evaluated_count": evaluated_count,
        "template_passed": bool(passed_count == 8),
        "stage2_candidate": stage2,
        "stage": "Stage 2 候選" if stage2 else "未確認 Stage 2",
        "rules": rules,
        "failed_conditions": [rule["criterion"] for rule in rules if rule["passed"] is False],
        "unavailable_conditions": [rule["criterion"] for rule in rules if rule["passed"] is None],
        "metrics": {
            "price": _number(price), "ma50": _number(ma50), "ma150": _number(ma150), "ma200": _number(ma200),
            "ma200_20d_ago": _number(old_average), "52_week_high": _number(high), "52_week_low": _number(low),
            "relative_3m_return": _number(stock_return * 100, 2) if stock_return is not None else None,
            "benchmark_3m_return": _number(benchmark_return * 100, 2) if benchmark_return is not None else None,
            "benchmark": "TAIEX", "atr14": _number(atr),
        },
        "pivot_entry": _number(pivot),
        "risk_stops": {
            "7_percent_stop": _number(pivot * 0.93) if pivot is not None else None,
            "2_atr_stop": _number(max(0, pivot - 2 * atr)) if pivot is not None and atr is not None else None,
        },
        "vcp": analyze_vcp(data),
        "note": "趨勢模板及 VCP 為附加診斷；高低點採 252 日收盤價，非完整 SEPA 選股系統或 RS 百分位排名。",
    }
