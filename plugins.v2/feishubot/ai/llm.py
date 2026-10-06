"""OpenRouter client: use the explicitly configured model."""

import requests

DEFAULT_MODEL = ""


def normalize_model_name(model: str) -> str:
    return str(model or "").strip()


class LLMClient:
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "", base_url: str = ""):
        self.api_key = api_key
        self.primary_model = normalize_model_name(model)
        self.base_url = base_url or self.DEFAULT_BASE_URL
        self.last_used_model = self.primary_model
        self.last_resolved_model = self.primary_model

    def chat(self, messages: list, tools: list = None, temperature: float = 0.3, max_tokens: int = 2048) -> dict:
        if not self.primary_model:
            raise RuntimeError("请在插件设置中填写模型名称")
        payload = {"model": self.primary_model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        response = requests.post(
            self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload, timeout=90,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("error"):
            raise RuntimeError("模型请求失败，请检查模型名称、额度和服务状态")
        self.last_used_model = self.primary_model
        self.last_resolved_model = str(result.get("model") or self.primary_model)
        return result
