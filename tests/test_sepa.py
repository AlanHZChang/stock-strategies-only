"""SEPA／VCP 的數學條件、缺值、時間對齊與 API 型別。"""
import importlib
import json

import numpy as np
import pandas as pd
import pytest

from stock_strategies.sepa import analyze_sepa, analyze_vcp, load_sepa_benchmark


def prices(count=300, first=50, last=150):
    close = np.linspace(first, last, count)
    width = close * np.linspace(0.08, 0.01, count)
    volume = np.full(count, 1000.0)
    volume[-min(count, 50):] = np.linspace(1000, 100, min(count, 50))
    return pd.DataFrame({
        "date": pd.bdate_range("2023-01-02", periods=count),
        "open": close, "close": close, "high": close + width / 2,
        "low": close - width / 2, "volume": volume,
    })


def benchmark(count=300, last=110):
    return prices(count, 100, last)[["date", "close"]]


def test_uptrend_passes_all_rules_and_returns_strict_json():
    report = analyze_sepa(prices(), benchmark())
    assert report["status"] == "available"
    assert report["template_score"] == "8/8"
    assert report["template_passed"] is True
    assert report["stage2_candidate"] is True
    assert [rule["rule"] for rule in report["rules"]] == list(range(1, 9))
    assert report["failed_conditions"] == report["unavailable_conditions"] == []
    assert report["vcp"]["detected"] is True
    assert report["pivot_entry"] == 150
    assert report["risk_stops"]["7_percent_stop"] == 139.5
    assert report["metrics"]["benchmark"] == "TAIEX"
    json.dumps(report, allow_nan=False)


def test_low_point_threshold_follows_referenced_25_percent_rule():
    report = analyze_sepa(prices(first=100, last=110), benchmark(last=100))
    assert report["rules"][4]["passed"] is False
    assert report["rules"][5]["passed"] is True
    assert report["template_score"] == "7/8"
    assert report["template_passed"] is False


def test_downtrend_is_not_stage_two():
    report = analyze_sepa(prices(first=150, last=50), benchmark())
    assert report["template_score"] == "0/8"
    assert report["stage2_candidate"] is False


@pytest.mark.parametrize("count", [0, 100, 200, 219])
def test_short_history_does_not_fabricate_ma200_slope(count):
    report = analyze_sepa(prices(count), benchmark(count))
    assert report["status"] == "insufficient_data"
    assert report["rules"][2]["passed"] is None
    assert report["stage2_candidate"] is False
    assert report["template_passed"] is False
    json.dumps(report, allow_nan=False)


def test_year_high_and_low_require_full_252_observations():
    report = analyze_sepa(prices(220), benchmark(220))
    assert report["rules"][2]["passed"] is True
    assert report["rules"][4]["passed"] is None
    assert report["rules"][5]["passed"] is None
    assert report["metrics"]["52_week_low"] is None
    assert report["status"] == "partial"


def test_missing_benchmark_is_unknown_instead_of_failed_or_passed():
    report = analyze_sepa(prices())
    assert report["status"] == "partial"
    assert report["rules"][6]["passed"] is None
    assert report["evaluated_count"] == 7
    assert report["template_passed"] is False
    assert report["metrics"]["benchmark_3m_return"] is None
    assert len(report["unavailable_conditions"]) == 1
    assert report["failed_conditions"] == []


def test_future_benchmark_prices_cannot_change_current_diagnosis():
    stock = prices()
    index = benchmark(320)
    first = analyze_sepa(stock, index)
    index.loc[300:, "close"] = 100000
    second = analyze_sepa(stock, index)
    assert first == second


def test_benchmark_dates_must_cover_latest_stock_date():
    report = analyze_sepa(prices(), benchmark().iloc[:-1])
    assert report["rules"][6]["passed"] is None


def test_out_of_order_rows_are_sorted_without_mutating_input():
    stock = prices()
    original = stock.copy(deep=True)
    assert analyze_sepa(stock.iloc[::-1], benchmark().iloc[::-1]) == analyze_sepa(stock, benchmark())
    pd.testing.assert_frame_equal(stock, original)


def test_non_finite_or_invalid_quotes_never_escape_as_nan_or_infinity():
    stock = prices()
    stock.loc[299, ["close", "volume"]] = [float("inf"), -1]
    report = analyze_sepa(stock, benchmark())
    assert report["metrics"]["price"] is None
    assert report["rules"][0]["passed"] is None
    assert report["vcp"]["detected"] is None
    json.dumps(report, allow_nan=False)


def test_vcp_uses_all_adjacent_windows_and_volume_dry_up():
    stock = prices(60, 100, 100)
    widths = np.array([0.08] * 40 + [0.04] * 10 + [0.02] * 5 + [0.01] * 5) * 100
    stock["high"], stock["low"] = 100 + widths / 2, 100 - widths / 2
    stock["volume"] = [1000] * 50 + [500] * 10
    report = analyze_vcp(stock)
    assert report["range_contraction_ratios"] == {"5": 0.5, "10": 0.375, "20": 0.34375}
    assert report["volume_10d_vs_50d"] == 0.555556
    assert report["detected"] is True


@pytest.mark.parametrize("volume", [None, 0, -1])
def test_vcp_missing_or_invalid_volume_cannot_pass(volume):
    stock = prices()
    if volume is None:
        stock = stock.drop(columns="volume")
    else:
        stock["volume"] = volume
    report = analyze_vcp(stock)
    assert report["status"] == "insufficient_data"
    assert report["detected"] is None


def test_vcp_requires_strict_contraction_in_ranges_and_volume():
    stock = prices()
    stock["volume"] = 1000
    assert analyze_vcp(stock)["detected"] is False
    stock = prices()
    widths = stock["close"] * np.linspace(0.01, 0.08, len(stock))
    stock["high"], stock["low"] = stock["close"] + widths / 2, stock["close"] - widths / 2
    assert analyze_vcp(stock)["detected"] is False


def test_vcp_zero_range_baseline_is_unknown():
    stock = prices()
    stock["high"] = stock["low"] = stock["close"]
    report = analyze_vcp(stock)
    assert report["detected"] is None
    assert report["range_contraction_ratios"] == {"5": None, "10": None, "20": None}


def test_atr_stop_includes_overnight_gaps():
    stock = prices(first=100, last=100)
    stock["high"], stock["low"] = stock["close"] + 1, stock["close"] - 1
    stock.loc[299, ["close", "high", "low"]] = [110, 111, 109]
    report = analyze_sepa(stock)
    assert report["metrics"]["atr14"] == 2.6429
    assert report["risk_stops"]["2_atr_stop"] == 104.7143


def test_benchmark_outage_is_non_fatal(monkeypatch):
    from stock_strategies import sepa
    def unavailable(*args, **kwargs):
        raise RuntimeError("benchmark unavailable")
    monkeypatch.setattr(sepa, "get_index_history", unavailable)
    assert load_sepa_benchmark().empty


def test_evaluate_exposes_diagnosis_without_changing_scoring(monkeypatch):
    module = importlib.import_module("stock_strategies.evaluate")
    monkeypatch.setattr(module, "get_fundamental", lambda _: {
        "eps": {2023: 8, 2024: 9}, "roe": {2023: 20, 2024: 21},
    })
    monkeypatch.setattr(module, "get_price_history", lambda *_: prices())
    monkeypatch.setattr(module, "backtest", lambda *_: {"winrate": 0.7, "samples": 20})
    first = module.evaluate("2330", "test", benchmark=benchmark())
    second = module.evaluate("2330", "test", benchmark=benchmark(last=10000))
    assert first["action"] != "ERROR"
    assert first["components"]["sepa"]["template_score"] == "8/8"
    assert second["components"]["sepa"]["template_score"] == "7/8"
    assert first["signal_score"] == second["signal_score"]
    assert first["action"] == second["action"]
    json.dumps(first, allow_nan=False)


def test_short_stock_still_returns_diagnosis_when_skipped(monkeypatch):
    module = importlib.import_module("stock_strategies.evaluate")
    monkeypatch.setattr(module, "get_fundamental", lambda _: {"eps": {}, "roe": {}})
    monkeypatch.setattr(module, "get_price_history", lambda *_: prices(80))
    report = module.evaluate("2330", "test", benchmark=pd.DataFrame())
    assert report["action"] == "SKIP"
    assert report["components"]["sepa"]["status"] == "insufficient_data"
