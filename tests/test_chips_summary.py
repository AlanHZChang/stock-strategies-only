import json

import pandas as pd
import pytest

from stock_strategies import chips, datasources as ds


@pytest.fixture
def bundle():
    dates = pd.bdate_range("2024-01-02", periods=7)
    institutional = pd.DataFrame({"date": dates, "foreign_net": [-1000, 0, 1000, 2000, 3000, 4000, 5000],
                                  "trust_net": [100] * 7, "dealer_net": [-50] * 7})
    institutional["total_net"] = institutional[["foreign_net", "trust_net", "dealer_net"]].sum(axis=1)
    holders = pd.DataFrame({"date": pd.to_datetime(["2023-12-29", "2024-01-05"]), "large_holder_pct": [40, 42]})
    margin = pd.DataFrame({"date": dates[-2:], "margin_balance": [100, 120], "short_balance": [50, 40]})
    prices = pd.DataFrame({"date": dates, "close": 100})
    return institutional, holders, margin, prices, "2024-01-10"


def test_daily_five_day_and_streak_use_stock_trading_days(bundle):
    report = chips.analyze_chips(*bundle)
    inst = report["institutional"]
    assert inst["foreign_net"] == 5000
    assert inst["foreign_streak"] == {"direction": "buy", "days": 5, "at_least": False}
    assert inst["five_day"]["foreign_net"] == 15000
    assert inst["five_day"]["trust_net"] == 500
    assert inst["five_day"]["dealer_net"] == -250
    assert inst["five_day"]["total_net"] == 15250
    assert inst["five_day"]["days"] == 5
    assert inst["unit"] == "shares"
    json.dumps(report, allow_nan=False)


def test_foreign_selling_and_zero_breaks_streak(bundle):
    inst, holders, margin, prices, cutoff = bundle
    inst.loc[4:, "foreign_net"] *= -1
    report = chips.analyze_chips(inst, holders, margin, prices, cutoff)
    assert report["institutional"]["foreign_streak"]["direction"] == "sell"
    assert report["institutional"]["foreign_streak"]["days"] == 3
    inst.loc[6, "foreign_net"] = 0
    assert chips.analyze_chips(inst, holders, margin, prices, cutoff)["institutional"]["foreign_streak"]["direction"] == "flat"


def test_missing_trading_date_cannot_be_skipped_in_streak_or_five_day(bundle):
    inst, holders, margin, prices, cutoff = bundle
    inst = inst.drop(index=5)
    report = chips.analyze_chips(inst, holders, margin, prices, cutoff)["institutional"]
    assert report["foreign_streak"] == {"direction": "buy", "days": 1, "at_least": True}
    assert report["five_day"]["foreign_net"] is None
    assert report["five_day"]["days"] == 4


def test_holiday_is_not_a_gap_when_stock_has_no_trading_day(bundle):
    inst, holders, margin, prices, cutoff = bundle
    inst = inst.drop(index=5)
    prices = prices.drop(index=5)
    report = chips.analyze_chips(inst, holders, margin, prices, cutoff)["institutional"]
    assert report["foreign_streak"]["days"] == 4
    assert report["five_day"]["days"] == 5


def test_holders_report_percentage_points_and_relative_change(bundle):
    large = chips.analyze_chips(*bundle)["large_holders"]
    assert large["percent"] == 42
    assert large["change_pp"] == 2
    assert large["change_pct"] == 5
    assert large["previous_date"] == "2023-12-29"


def test_missing_previous_week_is_not_compared_with_older_snapshot(bundle):
    inst, holders, margin, prices, cutoff = bundle
    holders.loc[0, "date"] = pd.Timestamp("2023-12-15")
    large = chips.analyze_chips(inst, holders, margin, prices, cutoff)["large_holders"]
    assert large["percent"] == 42
    assert large["change_pp"] is None
    assert large["change_pct"] is None


def test_zero_previous_holding_has_no_relative_percentage(bundle):
    inst, holders, margin, prices, cutoff = bundle
    holders.loc[0, "large_holder_pct"] = 0
    large = chips.analyze_chips(inst, holders, margin, prices, cutoff)["large_holders"]
    assert large["change_pp"] == 42
    assert large["change_pct"] is None


def test_margin_changes_are_in_lots_and_require_adjacent_trading_day(bundle):
    report = chips.analyze_chips(*bundle)["margin"]
    assert report["margin_change"] == 20
    assert report["short_change"] == -10
    assert report["unit"] == "lots"
    inst, holders, margin, prices, cutoff = bundle
    margin.loc[0, "date"] = pd.Timestamp("2024-01-02")
    report = chips.analyze_chips(inst, holders, margin, prices, cutoff)["margin"]
    assert report["margin_change"] is None
    assert report["short_change"] is None


def test_announced_yesterday_balances_work_without_previous_row(bundle):
    inst, holders, margin, prices, cutoff = bundle
    margin = margin.iloc[-1:].copy()
    margin["margin_yesterday"], margin["short_yesterday"] = 110, 45
    report = chips.analyze_chips(inst, holders, margin, prices, cutoff)["margin"]
    assert report["margin_change"] == 10
    assert report["short_change"] == -5


def test_cutoff_filters_all_future_data(bundle):
    expected = chips.analyze_chips(*bundle)
    inst, holders, margin, prices, cutoff = bundle
    for frame in (inst, holders, margin, prices):
        extra = frame.iloc[-1].copy()
        extra["date"] = pd.Timestamp("2024-01-12")
        frame.loc[len(frame) + 10] = extra
    assert chips.analyze_chips(inst, holders, margin, prices, cutoff) == expected


def test_missing_and_non_finite_values_are_not_zero(bundle):
    inst, holders, margin, prices, cutoff = bundle
    inst.loc[6, "foreign_net"] = float("nan")
    margin["margin_balance"] = margin["margin_balance"].astype(float)
    margin.loc[margin.index[-1], "margin_balance"] = float("inf")
    report = chips.analyze_chips(inst, pd.DataFrame(), margin, prices, cutoff)
    assert report["institutional"]["foreign_net"] is None
    assert report["large_holders"]["status"] == "unavailable"
    assert report["margin"]["margin_balance"] is None
    json.dumps(report, allow_nan=False)


def test_loader_failure_isolated_and_refresh_passed_to_sources(monkeypatch, bundle):
    inst, holders, margin, prices, cutoff = bundle
    calls = []
    def institutional(*args, **kwargs):
        calls.append(kwargs)
        return inst
    def denied(*args, **kwargs):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(chips, "get_institutional", institutional)
    monkeypatch.setattr(chips, "get_large_holders", denied)
    monkeypatch.setattr(chips, "get_margin", lambda *args, **kwargs: margin)
    report = chips.load_chip_summary("2330", cutoff, prices, force_refresh=True)
    assert calls[0]["force_refresh"] is True
    assert calls[0]["as_of"] == cutoff
    assert report["institutional"]["status"] == "available"
    assert report["large_holders"]["status"] == "unavailable"
    assert report["margin"]["status"] == "available"


def test_refresh_failure_retains_dated_snapshot_and_marks_fallback(bundle):
    saved = chips.analyze_chips(*bundle)
    fresh = chips.analyze_chips(None, None, None, None, "2024-01-11")
    merged = chips.merge_chip_snapshot(fresh, saved)
    assert merged["as_of"] == "2024-01-11"
    assert merged["institutional"]["date"] == "2024-01-10"
    assert merged["institutional"]["fallback"] is True
    assert "fallback" not in saved["institutional"]


def test_institutional_revisions_and_dealer_subtotals_are_not_double_counted(monkeypatch):
    raw = pd.DataFrame({"date": ["2024-01-05"] * 6,
        "name": ["Foreign_Investor", "Foreign_Investor", "Investment_Trust", "Dealer", "Dealer_self", "Dealer_Hedging"],
        "buy": [1000, 2000, 100, 500, 200, 300], "sell": [0] * 6})
    monkeypatch.setattr(ds, "fetch_finmind_cached", lambda *args, **kwargs: raw)
    row = ds.get_institutional("2330", "2024-01-01").iloc[0]
    assert row["foreign_net"] == 2000
    assert row["dealer_net"] == 500
    assert row["total_net"] == 2600


def test_missing_institutional_category_is_unknown(monkeypatch):
    raw = pd.DataFrame({"date": ["2024-01-05"], "name": ["Foreign_Investor"], "buy": [1000], "sell": [0]})
    monkeypatch.setattr(ds, "fetch_finmind_cached", lambda *args, **kwargs: raw)
    row = ds.get_institutional("2330", "2024-01-01").iloc[0]
    assert pd.isna(row["trust_net"])
    assert pd.isna(row["total_net"])


def test_margin_loader_prefers_announced_yesterday_balance(monkeypatch):
    raw = pd.DataFrame({"date": ["2024-01-02", "2024-01-10"],
                        "MarginPurchaseTodayBalance": [100, 120], "MarginPurchaseYesterdayBalance": [90, 115],
                        "ShortSaleTodayBalance": [50, 40], "ShortSaleYesterdayBalance": [45, 42]})
    calls = []
    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return raw
    monkeypatch.setattr(ds, "fetch_finmind_cached", fetch)
    data = ds.get_margin("2330", "2024-01-01", force_refresh=True)
    assert data.iloc[-1]["margin_chg"] == 5
    assert data.iloc[-1]["short_chg"] == -2
    assert calls[0]["fresh_days"] == -1
    assert calls[0]["max_retries"] == 0


def test_incremental_refresh_keeps_history_and_prefers_revised_institutional_rows(monkeypatch):
    from stock_strategies import cache
    snapshots = [
        [{"date": "2024-01-04", "name": "Foreign_Investor", "buy": 500, "sell": 0},
         {"date": "2024-01-05", "name": "Foreign_Investor", "buy": 1000, "sell": 0},
         {"date": "2024-01-05", "name": "Investment_Trust", "buy": 100, "sell": 0},
         {"date": "2024-01-05", "name": "Dealer", "buy": 500, "sell": 0}],
        [{"date": "2024-01-05", "name": "Foreign_Investor", "buy": 2000, "sell": 0},
         {"date": "2024-01-05", "name": "Investment_Trust", "buy": 200, "sell": 0},
         {"date": "2024-01-05", "name": "Dealer", "buy": 400, "sell": 0}],
    ]
    def fetch(params, timeout, max_retries):
        return {"status": 200, "data": snapshots.pop(0)}
    monkeypatch.setattr(cache, "_rate_limited_get", fetch)
    ds.get_institutional("2330", "2024-01-01")
    updated = ds.get_institutional("2330", "2024-01-01", force_refresh=True)
    assert updated.iloc[-1]["foreign_net"] == 2000
    assert updated.iloc[-1]["total_net"] == 2600
    assert updated.iloc[0]["date"] == pd.Timestamp("2024-01-04")
