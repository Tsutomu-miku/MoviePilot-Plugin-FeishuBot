"""AI 对话系统 - LLM 客户端（零第三方依赖，纯 requests）"""

from typing import Iterable, List, Optional

import requests
from app.log import logger


FREE_MODEL_CHOICES = [
    {
        "title": "OpenRouter 免费路由（推荐）",
        "value": "openrouter/free",
        "desc": "自动挑选当前可用的免费模型，最适合作为默认入口。",
    },
    {
        "title": "Google Gemini 2.0 Flash Experimental",
        "value": "google/gemini-2.0-flash-exp:free",
        "desc": "响应快，工具调用体验稳定。",
    },
    {
        "title": "StepFun Step 3.5 Flash",
        "value": "stepfun/step-3.5-flash:free",
        "desc": "长上下文能力不错，适合复杂指令。",
    },
    {
        "title": "Meta Llama 3.3 70B Instruct",
        "value": "meta-llama/llama-3.3-70b-instruct:free",
        "desc": "中文和多语言对话都比较稳。",
    },
    {
        "title": "Qwen3 Next 80B A3B Instruct",
        "value": "qwen/qwen3-next-80b-a3b-instruct:free",
        "desc": "当前更稳定的免费 Qwen 选项，适合中文和多轮对话。",
    },
    {
        "title": "NVIDIA Nemotron 3 Nano 30B A3B",
        "value": "nvidia/nemotron-3-nano-30b-a3b:free",
        "desc": "免费容量较稳定，适合作为额外后备模型。",
    },
]

DEFAULT_MODEL = "openrouter/free"
DEFAULT_FALLBACK_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "stepfun/step-3.5-flash:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
]

MODEL_ALIASES = {
    "qwen/qwen3-4b:free": "qwen/qwen3-next-80b-a3b-instruct:free",
}


def normalize_model_name(model: str) -> str:
    value = str(model or "").strip()
    if not value:
        return ""
    return MODEL_ALIASES.get(value, value)


def _normalize_model_list(models: Optional[Iterable[str]]) -> List[str]:
    if not models:
        return []

    normalized = []
    seen = set()
    for model in models:
        value = normalize_model_name(model)
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


class LLMClient:
    """
    OpenRouter / OpenAI 兼容的 LLM 客户端。
    职责单一：接收 messages + tools 定义，返回原始 API 响应 dict。
    新增能力：支持主模型、免费模型候选池和自动降级重试。
    """

    DEFAULT_MODEL = DEFAULT_MODEL
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_FALLBACK_MODELS = DEFAULT_FALLBACK_MODELS
    FREE_MODEL_CHOICES = FREE_MODEL_CHOICES

    def __init__(
        self,
        api_key: str,
        model: str = "",
        base_url: str = "",
        fallback_models: Optional[Iterable[str]] = None,
        auto_fallback: bool = True,
    ):
        self.api_key = api_key
        self.primary_model = normalize_model_name(model) or self.DEFAULT_MODEL
        self.base_url = base_url or self.DEFAULT_BASE_URL
        self.auto_fallback = bool(auto_fallback)
        self.fallback_models = _normalize_model_list(fallback_models)
        self.model_chain = self._build_model_chain()
        self.model = self.primary_model
        self.last_used_model = self.primary_model
        self.last_resolved_model = self.primary_model
        if str(model or "").strip() and self.primary_model != str(model).strip():
            logger.warning(
                f"LLM 模型别名已重写: {str(model).strip()} -> {self.primary_model}"
            )

    @classmethod
    def free_model_options(cls) -> List[dict]:
        return list(cls.FREE_MODEL_CHOICES)

    def _build_model_chain(self) -> List[str]:
        chain = [self.primary_model]
        chain.extend(self.fallback_models)
        if self.auto_fallback:
            chain.extend(self.DEFAULT_FALLBACK_MODELS)
        normalized = _normalize_model_list(chain)
        return normalized or [self.DEFAULT_MODEL]

    def describe_model_chain(self) -> str:
        return " -> ".join(self.model_chain)

    def chat(
        self,
        messages: list,
        tools: list = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> dict:
        """单次 LLM 调用，必要时自动切换后备模型。"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Tsutomu-miku/MoviePilot-Plugin-FeishuBot",
            "X-OpenRouter-Title": "MoviePilot-FeishuBot",
        }

        errors = []
        for idx, candidate in enumerate(self.model_chain):
            payload = {
                "model": candidate,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            try:
                resp = requests.post(self.base_url, headers=headers, json=payload, timeout=90)
                response_text = resp.text[:500]

                if resp.status_code != 200:
                    errors.append(f"{candidate}: HTTP {resp.status_code} {response_text}")
                    if idx < len(self.model_chain) - 1 and self._should_fallback(resp.status_code, response_text):
                        logger.warning(
                            f"LLM 模型不可用，切换到后备模型: {candidate} -> {self.model_chain[idx + 1]}"
                        )
                        continue
                    logger.error(f"LLM API {resp.status_code}: {response_text}")
                    raise RuntimeError(f"{candidate} 请求失败: HTTP {resp.status_code}")

                data = resp.json()
                if "error" in data:
                    error_msg = data["error"].get("message", str(data["error"]))
                    errors.append(f"{candidate}: {error_msg}")
                    if idx < len(self.model_chain) - 1 and self._should_fallback(resp.status_code, error_msg):
                        logger.warning(
                            f"LLM 模型返回错误，切换到后备模型: {candidate} -> {self.model_chain[idx + 1]}"
                        )
                        continue
                    raise RuntimeError(f"LLM API 错误: {error_msg}")

                self.model = candidate
                self.last_used_model = candidate
                self.last_resolved_model = str(data.get("model") or candidate)
                return data
            except requests.RequestException as exc:
                errors.append(f"{candidate}: {exc}")
                if idx < len(self.model_chain) - 1:
                    logger.warning(
                        f"LLM 请求异常，切换到后备模型: {candidate} -> {self.model_chain[idx + 1]} ({exc})"
                    )
                    continue
                raise RuntimeError(f"LLM 请求失败: {exc}") from exc

        raise RuntimeError("全部模型调用失败: " + " | ".join(errors[-5:]))

    @staticmethod
    def _should_fallback(status_code: int, detail: str) -> bool:
        lower = (detail or "").lower()
        keywords = (
            "no endpoints found",
            "no provider available",
            "temporarily unavailable",
            "rate limit",
            "rate-limit",
            "quota",
            "capacity",
            "provider returned error",
            "model not found",
            "unknown model",
            "unsupported model",
        )
        if status_code in {408, 409, 429} or status_code >= 500:
            return True
        if status_code == 404:
            return True
        if any(keyword in lower for keyword in keywords):
            return True
        return status_code in {400, 404} and "model" in lower
