"""AI 对话系统 — LLM 客户端（零第三方依赖，纯 requests）"""

import requests
from app.log import logger


class LLMClient:
    """
    OpenRouter / OpenAI 兼容的 LLM 客户端。

    职责单一：接收 messages + tools 定义，返回原始 API 响应 dict。
    不管理状态，不处理业务逻辑。
    """

    DEFAULT_MODEL = "google/gemini-2.5-flash-preview:free"
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "", base_url: str = ""):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.base_url = base_url or self.DEFAULT_BASE_URL

    def chat(
        self,
        messages: list,
        tools: list = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> dict:
        """
        单次 LLM 调用。

        Returns:
            OpenAI 格式的原始响应 dict（含 choices）
        Raises:
            RuntimeError / requests.HTTPError
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Tsutomu-miku/MoviePilot-Plugin-FeishuBot",
            "X-OpenRouter-Title": "MoviePilot-FeishuBot",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        resp = requests.post(self.base_url, headers=headers, json=payload, timeout=90)

        if resp.status_code != 200:
            logger.error(f"LLM API {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()

        data = resp.json()
        if "error" in data:
            error_msg = data["error"].get("message", str(data["error"]))
            raise RuntimeError(f"LLM API 错误: {error_msg}")

        return data
