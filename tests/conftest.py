"""共用測試 fixture。FinMind 一律 mock，不打真 API。"""
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """每個測試用獨立快取目錄，避免互相污染。"""
    cache_dir = tmp_path / "finmind_cache"
    monkeypatch.setenv("FINMIND_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("FINMIND_TOKEN", "test-token")
    # 新籌碼來源預設離線；資料 loader 與摘要測試會另外提供自己的 mock。
    from stock_strategies import chips
    for name in ("get_institutional", "get_large_holders", "get_margin"):
        monkeypatch.setattr(chips, name, lambda *args, **kwargs: pd.DataFrame())
    yield


def make_price_df(n=120, start="2023-01-02", base=100.0):
    """造一段遞增日 K，欄位符合 add_indicators 契約。"""
    dates = pd.bdate_range(start=start, periods=n)
    close = [base + i * 0.5 for i in range(n)]
    return pd.DataFrame({
        "date": dates,
        "open": [c - 0.3 for c in close],
        "high": [c + 0.6 for c in close],
        "low": [c - 0.6 for c in close],
        "close": close,
        "volume": [1000 + i for i in range(n)],
    })
