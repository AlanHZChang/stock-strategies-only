import json
from unittest.mock import Mock

import gspread
import pandas as pd

import premarket
from stock_strategies import sheet
from stock_strategies.chips import analyze_chips
from stock_strategies.notify import format_premarket, format_premarket_messages, select_premarket_signals


def snapshot():
    dates = pd.bdate_range("2024-01-02", periods=7)
    inst = pd.DataFrame({"date": dates, "foreign_net": [0, 0, 1000, 2000, 3000, 4000, 5000],
                         "trust_net": 1000, "dealer_net": -1000})
    inst["total_net"] = inst["foreign_net"]
    holders = pd.DataFrame({"date": pd.to_datetime(["2023-12-29", "2024-01-05"]), "large_holder_pct": [40, 42]})
    margin = pd.DataFrame({"date": dates[-2:], "margin_balance": [100, 120], "short_balance": [50, 40]})
    return analyze_chips(inst, holders, margin, pd.DataFrame({"date": dates}), "2024-01-10")


def signal(stock_id="2330", **kwargs):
    return {"date": "2024-01-10", "stock_id": stock_id, "name": "test", "action": "WATCH", "signal_score": 60, **kwargs}


def test_watch_chips_display_dates_units_and_weekly_changes():
    text = format_premarket(None, [signal(chips=snapshot())])
    assert "WATCH 2330" in text
    assert "法人 2024-01-10（張）：外資 +5.00" in text
    assert "外資連買 5 個交易日" in text
    assert "近5日法人（2024-01-04～2024-01-10，張）：外資 +15.00" in text
    assert "千張大戶 2024-01-05：42.00% / 較上週 +2.00 個百分點（增幅 +5.00%" in text
    assert "融資增減 +20.00（餘 120.00） / 融券增減 -10.00（餘 40.00）" in text


def test_missing_data_is_explicit_and_old_signals_still_render():
    text = format_premarket(None, [signal(chips=analyze_chips(None, None, None, None, "2024-01-10"))])
    assert "資料不足" in text
    assert "集保" in text
    assert "外資 +0.00" not in text
    assert "WATCH 2330" in format_premarket(None, [signal()])


def test_selecting_batch_deduplicates_and_keeps_all_watches():
    signals = [signal(), signal(), signal("9999", date="2024-01-09")]
    signals += [signal(str(3000 + index)) for index in range(20)]
    selected = select_premarket_signals(signals)
    assert len(selected) == 21
    assert all(row["date"] == "2024-01-10" for row in selected)
    assert sum(row["stock_id"] == "2330" for row in selected) == 1


def test_refresh_does_not_rerun_scoring_or_change_signal(monkeypatch):
    calls = []
    def evaluate_chips(stock_id, as_of, refresh):
        calls.append((stock_id, as_of, refresh))
        return snapshot()
    monkeypatch.setattr(premarket, "evaluate_chips", evaluate_chips)
    original = signal()
    enriched = premarket.enrich_premarket_signals([original, signal()], "2024-01-11")
    assert calls == [("2330", "2024-01-11", True)]
    assert enriched[0]["action"] == "WATCH"
    assert enriched[0]["signal_score"] == 60
    assert "chips" in enriched[0] and "chips" not in original


def test_refresh_exception_falls_back_to_saved_dated_snapshot(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("outage")
    monkeypatch.setattr(premarket, "evaluate_chips", unavailable)
    enriched = premarket.enrich_premarket_signals([signal(chips=snapshot())], "2024-01-11")
    text = format_premarket(None, enriched)
    assert "2024-01-10（沿用舊資料，更新受限）" in text
    assert enriched[0]["chips"]["institutional"]["foreign_net"] == 5000


def test_premarket_splits_long_chips_messages_and_keeps_all_stocks():
    rows = [signal(str(3000 + index), chips=snapshot()) for index in range(30)]
    messages = format_premarket_messages(None, rows)
    assert len(messages) > 1
    assert all(len(message.encode("utf-16-le")) // 2 <= 4000 for message in messages)
    assert all(row["stock_id"] in "\n".join(messages) for row in rows)


def test_signals_adds_snapshot_column_preserving_header_order(monkeypatch):
    ws = Mock()
    original_headers = ["custom_note"] + sheet.SIGNAL_HEADERS[:-1]
    ws.row_values.return_value = original_headers.copy()
    ws.col_count = len(original_headers)
    sh = Mock()
    sh.worksheet.return_value = ws
    monkeypatch.setattr(sheet, "get_gsheet", lambda: sh)
    sheet.append_signals([signal(components={"chips": snapshot()})])
    new_headers = ws.update.call_args.kwargs["values"][0]
    assert new_headers[:-1] == original_headers
    assert new_headers[-1] == "chips_json"
    ws.add_cols.assert_called_once_with(1)
    row = ws.append_rows.call_args.args[0][0]
    assert row[0] == ""
    assert row[new_headers.index("stock_id")] == "2330"
    assert json.loads(row[-1])["institutional"]["foreign_net"] == 5000


def test_signals_creation_and_readback_with_legacy_or_invalid_json(monkeypatch):
    ws = Mock()
    ws.row_values.return_value = []
    sh = Mock()
    sh.worksheet.side_effect = gspread.WorksheetNotFound("Signals")
    sh.add_worksheet.return_value = ws
    monkeypatch.setattr(sheet, "get_gsheet", lambda: sh)
    saved = snapshot()
    sheet.append_signals([signal(components={"chips": saved})])
    assert ws.append_row.call_args.args[0] == sheet.SIGNAL_HEADERS
    sh.worksheet.side_effect = None
    sh.worksheet.return_value = ws
    ws.get_all_records.return_value = [signal(chips_json="not-json"), signal(chips_json=json.dumps(saved)), signal()]
    rows = sheet.read_latest_signals()
    assert "chips" not in rows[0]
    assert rows[1]["chips"] == saved
    assert "chips" not in rows[2]


def test_premarket_main_refreshes_chips_and_sends_all_chunks_with_mock_telegram(monkeypatch):
    for key in premarket.REQUIRED_ENV:
        monkeypatch.setenv(key, "test-only")
    rows = [signal(str(3000 + index)) for index in range(30)]
    monkeypatch.setattr(premarket, "read_latest_signals", lambda limit: rows)
    monkeypatch.setattr(premarket, "get_night_session", lambda: None)
    monkeypatch.setattr(premarket, "evaluate_chips", lambda *args, **kwargs: snapshot())
    monkeypatch.setattr(premarket.time, "sleep", lambda _: None)
    sent = []
    monkeypatch.setattr(premarket, "send_telegram", sent.append)
    premarket.main()
    assert len(sent) > 1
    assert all(len(message.encode("utf-16-le")) // 2 <= 4000 for message in sent)
    assert all(row["stock_id"] in "\n".join(sent) for row in rows)
