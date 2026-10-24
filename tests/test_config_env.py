"""Verify configuration loading without reading the developer's real .env."""
import os
import runpy
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv
import pytest


DEFAULTS = {
    "eps_threshold": 5.0,
    "roe_threshold": 15.0,
    "backtest_years": 3,
    "hold_days": 20,
    "target_return": 0.10,
    "stop_loss": 0.08,
    "weight_fundamental": 0.3,
    "weight_technical": 0.3,
    "weight_backtest": 0.4,
    "min_tech_score_for_signal": 60,
    "min_total_score_for_buy": 65,
    "night_gap_big": 1.5,
    "night_gap_small": 0.5,
}


@pytest.mark.parametrize("use_dotenv", [False, True])
def test_config_defaults_and_dotenv(tmp_path, monkeypatch, use_dotenv):
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    for key in DEFAULTS:
        monkeypatch.delenv(key.upper(), raising=False)
    expected = dict(DEFAULTS)
    if use_dotenv:
        expected = {
            "eps_threshold": 7.5, "roe_threshold": 18.0,
            "backtest_years": 5, "hold_days": 10,
            "target_return": 0.15, "stop_loss": 0.05,
            "weight_fundamental": 0.2, "weight_technical": 0.5, "weight_backtest": 0.3,
            "min_tech_score_for_signal": 70, "min_total_score_for_buy": 75,
            "night_gap_big": 2.0, "night_gap_small": 0.8,
        }
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{key.upper()}={value}" for key, value in expected.items())
        if use_dotenv else "",
        encoding="utf-8",
    )
    if use_dotenv:
        monkeypatch.setenv("EPS_THRESHOLD", "9.0")
        expected["eps_threshold"] = 9.0
    config_path = Path(__file__).resolve().parents[1] / "stock_strategies" / "config.py"
    with patch.dict(os.environ), patch(
        "dotenv.load_dotenv", side_effect=lambda path, **kw: load_dotenv(env_file, **kw)
    ):
        actual = runpy.run_path(str(config_path))["CONFIG"]
    assert actual == expected
    for key, value in actual.items():
        assert type(value) is type(DEFAULTS[key])

    # 確認環境設定能一路傳到 evaluate 使用的 merge_params，策略仍可覆寫。
    from stock_strategies import config

    with patch.dict(config.CONFIG, actual):
        loader = runpy.run_path(
            str(config_path.with_name("loader.py")),
            run_name="stock_strategies._config_test_loader",
        )
    for key in ("weight_fundamental", "weight_technical", "weight_backtest"):
        assert loader["merge_params"](None)[key] == expected[key]
        assert loader["param_defaults"]()[key] == expected[key]
    overridden = loader["merge_params"]({"params": {"weight_technical": 0.8}})
    assert overridden["weight_technical"] == 0.8
