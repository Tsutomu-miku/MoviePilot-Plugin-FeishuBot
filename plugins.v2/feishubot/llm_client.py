"""飞书机器人插件 — OpenRouter LLM 客户端"""

import requests
from app.log import logger


class _OpenRouterClient:
    """零依赖 OpenRouter Chat Completions 客户端"""

    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODEL = "google/gemini-2.5-flash-preview:free"

    def __init__(self, api_key, model: str = ""):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL

    def chat(
        self,
        messages: list,
        tools: list = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> dict:
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

        resp = requests.post(self.BASE_URL, headers=headers, json=payload, timeout=90)

        if resp.status_code != 200:
            logger.error(f"OpenRouter API {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()

        data = resp.json()
        if "error" in data:
            error_msg = data["error"].get("message", str(data["error"]))
            raise RuntimeError(f"OpenRouter API 错误: {error_msg}")

        return data


# ╔════════════════════════════════════════════════════════════════════╗
# ║  4. 对话历史管理                                                   ║
# ╚════════════════════════════════════════════════════════════════════╝
