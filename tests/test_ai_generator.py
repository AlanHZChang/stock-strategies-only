"""AI 生成的期限、服務錯誤與策略驗證；不呼叫外部 Gemini API。"""
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as google_errors
from requests.exceptions import Timeout

from api.main import app
from api.services import ai_generator
from stock_strategies import loader


@pytest.fixture
def gemini(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(loader, "STRATEGY_DIR", tmp_path)
    model = Mock()
    model.generate_content.return_value = SimpleNamespace(text='{"name":"test", "params":{}}')
    sdk = ModuleType("google.generativeai")
    sdk.configure = Mock()
    sdk.GenerativeModel = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "google.generativeai", sdk)
    return sdk, model


def test_generation_has_bounded_timeout_and_returns_unsaved_valid_draft(gemini, tmp_path):
    sdk, model = gemini
    response = TestClient(app).post("/api/strategies/generate", json={"prompt": "test strategy"})
    assert response.status_code == 200
    assert response.json()["source"] == "ai"
    assert response.json()["params"]["hold_days"] > 0
    assert not list(tmp_path.iterdir())
    sdk.configure.assert_called_once_with(api_key="test-key", transport="rest")
    assert model.generate_content.call_args.kwargs["request_options"] == {
        "timeout": 90, "retry": None,
    }


@pytest.mark.parametrize("error,status,message", [
    (google_errors.DeadlineExceeded("private provider error"), 504, "90 秒"),
    (Timeout("private provider error"), 504, "90 秒"),
    (TimeoutError("private provider error"), 504, "90 秒"),
    (google_errors.ResourceExhausted("private provider error"), 429, "額度"),
    (google_errors.Unauthenticated("private provider error"), 503, "授權"),
    (google_errors.PermissionDenied("private provider error"), 503, "授權"),
    (google_errors.NotFound("private provider error"), 503, "GEMINI_MODEL"),
    (google_errors.ServiceUnavailable("private provider error"), 502, "暫時"),
    (google_errors.from_http_status(402, "private provider error"), 402, "預付額度"),
])
def test_provider_errors_return_actionable_json_instead_of_generic_500(gemini, error, status, message):
    _, model = gemini
    model.generate_content.side_effect = error
    response = TestClient(app).post("/api/strategies/generate", json={"prompt": "test strategy"})
    assert response.status_code == status
    assert message in response.json()["detail"]
    assert "private provider error" not in response.text


@pytest.mark.parametrize("text", ["", "not json", '{"name":"test","params":{"hold_days":0}}'])
def test_invalid_generated_strategy_is_a_readable_bad_gateway(gemini, text):
    _, model = gemini
    model.generate_content.return_value = SimpleNamespace(text=text)
    response = TestClient(app).post("/api/strategies/generate", json={"prompt": "test strategy"})
    assert response.status_code == 502
    assert "有效的策略參數" in response.json()["detail"]


def test_missing_key_explains_configuration(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    response = TestClient(app).post("/api/strategies/generate", json={"prompt": "test strategy"})
    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]


def test_google_api_key_fallback_remains_supported(gemini, monkeypatch):
    sdk, _ = gemini
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.setenv("GOOGLE_API_KEY", "fallback-key")
    ai_generator.generate_strategy_with_ai("test strategy")
    sdk.configure.assert_called_once_with(api_key="fallback-key", transport="rest")
