"""通知規則與評分共用參數，不傳送 Telegram 訊息。"""
from stock_strategies.loader import merge_params
from stock_strategies.notify import format_message, format_messages


def _watch():
    return {
        "stock_id": "2330", "name": "test", "action": "WATCH", "signal_score": 70,
        "entry_price": 100, "stop_loss_price": 94.5, "target_price": 112.5,
        "risk_reward_ratio": 2.27, "position_size_pct": 20,
        "components": {"fundamental_pass": False, "tech_score": 60,
                       "backtest_winrate": 0.6, "backtest_samples": 10},
    }


def test_strategy_overrides_change_rules_and_stock_explanations_together():
    messages = format_messages([_watch()], strategy={"params": {
        "eps_threshold": 7.5, "roe_threshold": 18.5, "backtest_years": 5,
        "hold_days": 10, "stop_loss": 0.055, "target_return": 0.125,
        "weight_fundamental": 0.2, "weight_technical": 0.5, "weight_backtest": 0.3,
        "min_total_score_for_buy": 78, "min_tech_score_for_buy": 68,
        "min_tech_score_for_signal": 72,
    }})
    assert len(messages) == 4
    first, second = messages[:2]
    assert "EPS>7.5,ROE>18.5%" in first
    assert "5年回測" in first
    assert "基本面20% + 技術50% + 回測30%" in first
    assert "BUY≥78(基本面通過、技術≥68)" in first
    assert "回測訊號：技術≥72" in first
    assert "停損5.5% / 停利12.5% / 持有10交易日" in first
    assert "基本面未達標(EPS>7.5,ROE>18.5%)" in second
    assert "技術分僅60(<68)" in second
    assert "綜合分70(<78)" in second
    assert "停損 94.5 (-5.5%) / 目標 112.5 (+12.5%)" in second
    assert "三關全過" not in first + second


def test_report_displays_normalized_weights_used_by_evaluate():
    message = format_messages([], strategy={"params": {
        "weight_fundamental": 0.1, "weight_technical": 0.1, "weight_backtest": 0.2,
    }})[0]
    assert "基本面25% + 技術25% + 回測50%" in message


def test_report_respects_disabled_signals_and_optional_fundamentals():
    stock = _watch()
    stock["action"] = "BUY"
    stock["components"]["tech_score"] = 70
    messages = format_messages([stock], strategy={"params": {
        "fundamental_pass_required": False,
        "use_ma_alignment": False, "use_bollinger_bounce": False,
        "use_kd_golden_cross": False, "use_macd_bullish": False,
        "use_volume_patterns": False,
    }})
    assert "技術面(四項訊號皆關閉)" in messages[0]
    assert "基本面不強制" in messages[0]
    assert "基本面未達標" not in messages[1]
    assert "為何買: 所有條件皆達標" in messages[1]


def test_default_and_compatibility_entrypoints_use_current_defaults():
    params = merge_params(None)
    messages = format_messages([])
    assert f"EPS>{params['eps_threshold']:g},ROE>{params['roe_threshold']:g}%" in messages[0]
    assert f"{params['backtest_years']}年回測" in messages[0]
    assert f"BUY≥{params['min_total_score_for_buy']:g}" in messages[0]
    assert format_message([]) == messages[0]
