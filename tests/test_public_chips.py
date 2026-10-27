"""官方報表契約、集保跨週查詢與快取；不連外、不使用付費資料。"""
import json

import pandas as pd
import pytest
import requests

from stock_strategies import chips, public_chips as public
from stock_strategies.notify import format_premarket


@pytest.fixture(autouse=True)
def offline_public_sources(monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_CHIPS_CACHE_DIR", str(tmp_path / "public"))
    def unexpected_request(*args, **kwargs):
        raise AssertionError("test must mock public network requests")
    monkeypatch.setattr(public, "_request", unexpected_request)


def csv_data(day="20240328", percent="62.5", stock="2330"):
    return ("\ufeff資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"
            f'{day},{stock},14,10,"500,000",25\n'
            f'{day},{stock},15,20,"1,000,001",{percent}\n'
            f'{day},{stock},16,30,"1,500,001",100\n').encode("utf-8")


def history_page(*days, selected=None, percent=None):
    html = '<input name="SYNCHRONIZER_TOKEN" value="public-form-token"><select name="scaDate">'
    for day in days:
        html += f'<option value="{day}" {"selected" if day == selected else ""}>{day}</option>'
    html += '</select>'
    if percent is not None:
        html += f'<table><tr><td>15</td><td>1,000,001以上</td><td>20</td><td>100,000,000</td><td>{percent}</td></tr></table>'
    page = public._HistoryPage()
    page.feed(html)
    return page, html.encode()


@pytest.mark.parametrize("market,kind,fields,row,expected", [
    ("TWSE", "institutional", ["證券代號", "證券名稱", "外陸資買賣超股數(不含外資自營商)",
        "外資自營商買賣超股數", "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數"],
     ["2330", "台積電", "1,000", "200", "-300", "500", "1,400"],
     {"foreign_net": 1200, "trust_net": -300, "dealer_net": 500, "total_net": 1400}),
    ("TPEX", "institutional", ["代號", "名稱"] + ["買進股數", "賣出股數", "買賣超股數"] * 7 + ["三大法人買賣超股數合計"],
     ["2330", "測試"] + [0] * 8 + [1200, 0, 0, -300] + [0] * 8 + [500, 1400],
     {"foreign_net": 1200, "trust_net": -300, "dealer_net": 500, "total_net": 1400}),
    ("TWSE", "margin", ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額", "限額",
        "買進", "賣出", "現券償還", "前日餘額", "今日餘額"],
     ["2330", "台積電", 0, 0, 0, "1,000", "1,020", 0, 0, 0, 0, 300, 290],
     {"margin_balance": 1020, "short_balance": 290, "margin_yesterday": 1000, "short_yesterday": 300}),
    ("TPEX", "margin", ["代號", "名稱", "前資餘額(張)", "資餘額", "前券餘額(張)", "券餘額"],
     ["2330", "測試", 1000, 1020, 300, 290],
     {"margin_balance": 1020, "short_balance": 290, "margin_yesterday": 1000, "short_yesterday": 300}),
])
def test_official_report_units_and_net_amounts(market, kind, fields, row, expected):
    payload = {"fields": fields, "data": [row]}
    if market == "TPEX" or kind == "margin":
        payload = {"tables": [{"fields": ["統計項目"], "data": [["總計"]]}, payload]}
    parsed = public._parse_report(market, kind, json.dumps(payload))
    assert parsed["2330"] == expected


def test_changed_institutional_schema_does_not_guess_positions():
    with pytest.raises(ValueError, match="欄位"):
        public._parse_report("TWSE", "institutional", json.dumps({"fields": ["證券代號"], "data": [["2330"]]}))


def test_changed_margin_schema_is_unavailable_instead_of_zero_balances():
    with pytest.raises(ValueError, match="欄位"):
        public._parse_report("TWSE", "margin", json.dumps({"fields": ["代號"], "data": [["2330"]]}))


def test_wrong_report_date_is_rejected(monkeypatch):
    raw = json.dumps({"stat": "OK", "date": "113/03/22", "fields": ["代號"], "data": [["2330"]]}).encode()
    monkeypatch.setattr(public, "_cached_get", lambda *a, **k: raw)
    with pytest.raises(ValueError, match="日期"):
        public._report("TPEX", "institutional", "2024-03-28", False)


def test_daily_tpex_fallback_and_partial_failures_keep_dated_rows(monkeypatch):
    calls = []
    def rows(market, kind, day, refresh):
        calls.append((market, day))
        if market == "TWSE":
            raise requests.HTTPError("temporarily unavailable")
        return {"6488": {"foreign_net": 1000, "trust_net": 200, "dealer_net": -100, "total_net": 1100}}
    monkeypatch.setattr(public, "_market_rows", rows)
    data = public.get_institutional("6488", "2024-03-20", "2024-03-28", trading_dates=["2024-03-27", "2024-03-28"])
    assert data["date"].tolist() == list(pd.to_datetime(["2024-03-27", "2024-03-28"]))
    assert data.attrs["source"] == "TPEX"
    assert data.attrs["warning"]
    assert sum(market == "TWSE" for market, day in calls) == 1


def test_offline_markets_do_not_retry_every_historical_day(monkeypatch):
    calls = []
    def failed(market, *args):
        calls.append(market)
        raise requests.Timeout()
    monkeypatch.setattr(public, "_market_rows", failed)
    data = public.get_institutional("2330", "2024-01-01", "2024-03-28")
    assert data.empty
    assert calls == ["TWSE", "TPEX"]
    assert "失敗" in data.attrs["unavailable_reason"]


def test_tdcc_only_level_fifteen_and_preserves_leading_zero():
    assert public._tdcc_csv(csv_data(stock="0050", percent="0")) == {"20240328": {"0050": 0}}
    assert public._tdcc_csv(csv_data(percent="nan")) == {}
    with pytest.raises(ValueError, match="欄位"):
        public._tdcc_csv(b"unexpected,columns\n1,2\n")


def test_history_form_posts_stock_and_week_and_checks_selected_date(monkeypatch):
    page, _ = history_page("20240328", "20240322")
    posted = []
    def request(session, method, url, **kwargs):
        posted.append(kwargs["data"])
        return history_page("20240328", "20240322", selected="20240322", percent="60.00")[1]
    monkeypatch.setattr(public, "_request", request)
    assert public._history_value(None, page, "0050", "20240322") == 60
    assert posted[0]["stockNo"] == "0050"
    assert posted[0]["scaDate"] == "20240322"
    assert posted[0]["SYNCHRONIZER_TOKEN"] == "public-form-token"
    with pytest.raises(ValueError, match="週別"):
        public._history_value(None, page, "0050", "20240328")


def test_latest_csv_and_previous_holiday_week_use_calendar_weeks(monkeypatch):
    monkeypatch.setattr(public, "_cached_get", lambda *a, **kw: csv_data())
    monkeypatch.setattr(public, "_history_page", lambda session: history_page("20240328", "20240322")[0])
    queried = []
    def value(session, page, stock, day):
        queried.append(day)
        return 60
    monkeypatch.setattr(public, "_history_value", value)
    data = public.get_large_holders("2330", "2024-03-01", "2024-03-29", force_refresh=True)
    result = chips.analyze_chips(None, data, None, None, "2024-03-29")["large_holders"]
    assert result["date"] == "2024-03-28"
    assert result["previous_date"] == "2024-03-22"
    assert result["change_pp"] == 2.5
    assert queried == ["20240322"]
    # 第二檔相同週資料已存在時，不再查歷史表單。
    public.get_large_holders("2330", "2024-03-01", "2024-03-29")
    assert queried == ["20240322"]


def test_historical_cutoff_excludes_future_csv_and_fetches_two_past_weeks(monkeypatch):
    monkeypatch.setattr(public, "_cached_get", lambda *a, **kw: csv_data(day="20240405"))
    monkeypatch.setattr(public, "_history_page", lambda session: history_page("20240405", "20240328", "20240322")[0])
    monkeypatch.setattr(public, "_history_value", lambda session, page, stock, day: {"20240328": 62, "20240322": 60}[day])
    data = public.get_large_holders("2330", "2024-03-01", "2024-03-29")
    assert data["large_holder_pct"].tolist() == [60, 62]
    assert data["date"].max() == pd.Timestamp("2024-03-28")


def test_missing_previous_week_is_not_replaced_by_older_week(monkeypatch):
    monkeypatch.setattr(public, "_cached_get", lambda *a, **kw: csv_data())
    monkeypatch.setattr(public, "_history_page", lambda session: history_page("20240328", "20240315")[0])
    public._save_week("20240315", {"2330": 55})
    data = public.get_large_holders("2330", "2024-03-01", "2024-03-29")
    assert chips.analyze_chips(None, data, None, None, "2024-03-29")["large_holders"]["change_pp"] is None


def test_history_failure_retains_current_csv_and_warning(monkeypatch):
    monkeypatch.setattr(public, "_cached_get", lambda *a, **kw: csv_data())
    def failed(session):
        raise requests.Timeout("public portal timeout")
    monkeypatch.setattr(public, "_history_page", failed)
    data = public.get_large_holders("2330", "2024-03-01", "2024-03-29")
    assert data["large_holder_pct"].tolist() == [62.5]
    assert data.attrs["warning"]


def test_csv_failure_still_checks_new_week_despite_cached_two_weeks(monkeypatch):
    public._save_week("20240315", {"2330": 55})
    public._save_week("20240322", {"2330": 60})
    def failed(*a, **kw):
        raise requests.Timeout()
    monkeypatch.setattr(public, "_cached_get", failed)
    monkeypatch.setattr(public, "_history_page", lambda session: history_page("20240328", "20240322")[0])
    monkeypatch.setattr(public, "_history_value", lambda *a: 62.5)
    data = public.get_large_holders("2330", "2024-03-01", "2024-03-29")
    assert data["date"].max() == pd.Timestamp("2024-03-28")


def test_common_report_cache_coalesces_refresh_and_does_not_cache_unpublished(monkeypatch):
    calls = []
    def request(*a, **kw):
        calls.append(kw)
        return b"published"
    monkeypatch.setattr(public, "_request", request)
    for _ in range(3):
        assert public._cached_get("https://example.test/report", {"date": "20240328"}, refresh=True) == b"published"
    assert len(calls) == 1
    for _ in range(2):
        public._cached_get("https://example.test/unpublished", {}, valid=lambda raw: False)
    assert len(calls) == 3


def test_failed_request_has_short_batch_cooldown(monkeypatch):
    calls = []
    def failed(*args, **kwargs):
        calls.append(1)
        raise requests.Timeout()
    monkeypatch.setattr(public, "_request", failed)
    for _ in range(3):
        with pytest.raises(requests.Timeout):
            public._cached_get("https://example.test/report", {}, refresh=True)
    assert len(calls) == 1


def test_provenance_and_partial_update_warning_reach_notification(monkeypatch):
    data = pd.DataFrame({"date": pd.to_datetime(["2024-03-22", "2024-03-28"]), "large_holder_pct": [60, 62.5]})
    data.attrs.update(source="TDCC", warning="部分更新失敗")
    monkeypatch.setattr(chips, "get_large_holders", lambda *a, **kw: data)
    result = chips.load_chip_summary("2330", "2024-03-29")
    assert result["large_holders"]["source"] == "TDCC"
    text = format_premarket(None, [{"stock_id": "2330", "date": "2024-03-29", "action": "WATCH", "chips": result}])
    assert "集保結算所公開資料" in text
    assert "部分更新失敗" in text
    assert "+2.50 個百分點" in text
