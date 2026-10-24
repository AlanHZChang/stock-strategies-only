"""用 Gemini Flash 生策略 JSON"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from google.api_core import exceptions as google_errors
from requests.exceptions import Timeout as RequestsTimeout

from stock_strategies import loader


# 預設用 Gemini 2.5 Flash；可用 GEMINI_MODEL 環境變數覆寫
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GENERATION_TIMEOUT_SECONDS = 90


class AIGenerationError(RuntimeError):
    """可由 API 回傳給使用者的 AI 服務錯誤。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code


SYSTEM_PROMPT = """你是台股量化選股策略設計師。使用者會用自然語言描述他想要的選股風格，
你的工作是把它翻成一份**符合 schema 的策略 JSON**。

## 嚴格規則
- 你只能回覆一份 JSON，不要 markdown code fence、不要說明文字、不要前後綴
- 必須包含 `name`、`description`、`params` 三個頂層欄位
- `params` 內所有鍵都必須來自下方白名單；型別必須正確
- weight_fundamental + weight_technical + weight_backtest 加總應 ≈ 1
- target_return 與 stop_loss 都是小數（例如 0.10 表示 10%）

## params 白名單與型別
{
  "eps_threshold": float,            // 最近年度 EPS 最小值需 > 此數，台股常見 2~10
  "roe_threshold": float,            // 最近年度 ROE 最小值需 > 此數 (%)，常見 8~25
  "fundamental_pass_required": bool, // 是否強制基本面要過才能 BUY
  "backtest_years": int,             // 1~10
  "hold_days": int,                  // 1~120
  "min_tech_score_for_signal": int,  // 0~100，回測時技術分達多少算訊號
  "target_return": float,            // 0.01~0.5
  "stop_loss": float,                // 0.01~0.5
  "weight_fundamental": float,
  "weight_technical": float,
  "weight_backtest": float,
  "min_total_score_for_buy": int,    // 0~100
  "min_tech_score_for_buy": int,     // 0~100
  "use_ma_alignment": bool,
  "use_bollinger_bounce": bool,
  "use_kd_golden_cross": bool,
  "use_macd_bullish": bool,
  "use_volume_patterns": bool,
  "market_filter_enabled": bool,
  "market_filter_ma_period": int     // 5~120
}

## 設計指引
- 偏短線 / 動能 → backtest_years 2-3、hold_days 5-10、target +5~8%、stop -3~5%、技術權重高
- 偏存股 / 價值 → EPS/ROE 門檻提高、backtest_years 5、hold_days 60+、基本面權重高
- 偏保守 → 基本面強制要過、min_total_score 75+、停損縮緊到 -3~5%
- 偏激進 / 抓飆股 → 量價型態打開、技術權重 0.5+、target 拉到 +15~20%
"""


def _extract_json(text: str) -> dict:
    """從模型回覆抽出第一個 JSON 物件"""
    # 砍掉可能的 ```json ... ``` 包裝
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())
    # 找第一個 { 與最後一個 }
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        raise ValueError(f"模型回覆找不到 JSON: {text[:200]}")
    return json.loads(text[s : e + 1])


def generate_strategy_with_ai(user_prompt: str, name: Optional[str] = None) -> dict:
    """呼叫 Gemini，把使用者敘述轉為策略 JSON，再經過驗證後回傳乾淨版本。

    注意：本函式不寫檔；前端拿到後讓使用者預覽，按下「儲存」才打 POST /api/strategies。
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise AIGenerationError(503, "未設定 GEMINI_API_KEY，請在專案 .env 設定後重新啟動後端")

    try:
        import google.generativeai as genai
    except ImportError as e:
        raise AIGenerationError(
            503, "缺少 AI 服務套件，請執行 uv sync 後重新啟動後端"
        ) from e

    # REST 能直接保留 Google HTTP 錯誤（例如 402），避免 gRPC 連線長時間等待。
    genai.configure(api_key=api_key, transport="rest")
    model = genai.GenerativeModel(
        DEFAULT_MODEL,
        system_instruction=SYSTEM_PROMPT,
        generation_config={
            "response_mime_type": "application/json",
        },
    )

    full_prompt = (
        f"使用者描述：\n{user_prompt}\n\n"
        "請輸出一份符合 schema 的策略 JSON。"
    )
    if name:
        full_prompt += f" 策略名稱請使用：「{name}」。"

    try:
        # SDK 預設期限為 600 秒，且會重試；必須早於前端代理期限回應。
        resp = model.generate_content(
            full_prompt,
            request_options={"timeout": GENERATION_TIMEOUT_SECONDS, "retry": None},
        )
    except (google_errors.DeadlineExceeded, RequestsTimeout, TimeoutError) as exc:
        raise AIGenerationError(504, "AI 生成超過 90 秒，請稍後重試或改用回應較快的 GEMINI_MODEL") from exc
    except google_errors.ResourceExhausted as exc:
        raise AIGenerationError(429, "Gemini API 額度不足或請求過於頻繁，請檢查配額後重試") from exc
    except (google_errors.Unauthenticated, google_errors.PermissionDenied) as exc:
        raise AIGenerationError(503, "Gemini API 授權失敗，請檢查 GEMINI_API_KEY 與 API 使用權限") from exc
    except google_errors.NotFound as exc:
        raise AIGenerationError(503, "Gemini 模型無法使用，請檢查 GEMINI_MODEL 與模型使用權限") from exc
    except google_errors.GoogleAPICallError as exc:
        if exc.code == 402:
            raise AIGenerationError(402, "Gemini 專案的預付額度已用完，請到 Google AI Studio 檢查帳務與額度後重試") from exc
        raise AIGenerationError(502, "Gemini API 暫時無法完成生成，請檢查模型設定或稍後重試") from exc

    try:
        text = getattr(resp, "text", None) or ""
        data = _extract_json(text)

        # 強制 source = ai，並讓 validator 補齊缺漏。
        data["source"] = "ai"
        if name and not data.get("name"):
            data["name"] = name

        return loader.validate_strategy(data)
    except (ValueError, TypeError) as exc:
        raise AIGenerationError(502, "AI 未回傳有效的策略參數，請調整描述後重新生成") from exc
