"""OpenRouter Chat Completions 客户端"""

from typing import List, Optional

import requests
from app.log import logger


class OpenRouterClient:
    """零依赖 OpenRouter Chat Completions 客户端，支持 Tool Calling"""

    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODEL = "google/gemini-2.5-flash-preview:free"

    def __init__(self, api_key: str, model: str = ""):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL

    def chat(
        self,
        messages: list,
        tools: Optional[list] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> dict:
        """
        调用 OpenRouter Chat Completions API。

        返回原始 API 响应 dict。调用方应检查
        choices[0].message.tool_calls 和 choices[0].message.content。
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

        resp = requests.post(
            self.BASE_URL, headers=headers, json=payload, timeout=90
        )

        # 记录异常状态码
        if resp.status_code != 200:
            logger.error(
                f"OpenRouter API 返回 {resp.status_code}: {resp.text[:500]}"
            )
            resp.raise_for_status()

        data = resp.json()

        # 检查 API 级错误
        if "error" in data:
            error_msg = data["error"].get("message", str(data["error"]))
            raise RuntimeError(f"OpenRouter API 错误: {error_msg}")

        return data
