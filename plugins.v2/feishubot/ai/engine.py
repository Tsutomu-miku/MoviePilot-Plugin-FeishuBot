"""
AI 对话系统 - ChatEngine 主引擎
这是 AI 对话系统的唯一入口。外部（__init__.py）只需调用:
    engine = ChatEngine(api_key, model)
    result = engine.chat(user_text)
"""

import json as _json
import threading
from typing import Callable, Iterable, List, Optional, Tuple

from app.log import logger

from .types import ChatState
from .history import ChatHistory
from .llm import LLMClient
from .tools import TOOL_SCHEMAS, friendly_tool_name
from .executor import ToolExecutor
from .prompts import SYSTEM_PROMPT, MAX_TOOL_ROUNDS


def _sanitize_assistant_message(raw_msg: dict) -> dict:
    """清洗 LLM 返回的 assistant 消息，只保留标准字段。"""
    clean = {"role": "assistant"}

    tool_calls = raw_msg.get("tool_calls")
    if tool_calls:
        clean["tool_calls"] = [
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", "{}"),
                },
            }
            for tc in tool_calls
        ]

    content = raw_msg.get("content")
    clean["content"] = content if isinstance(content, str) else ""
    return clean


class ChatEngine:
    """AI 对话主引擎，内置工具调用与串行并发控制。"""

    def __init__(
        self,
        api_key: str,
        model: str = "",
        base_url: str = "",
        fallback_models: Optional[Iterable[str]] = None,
        auto_fallback: bool = True,
    ):
        self.state = ChatState()
        self.history = ChatHistory(SYSTEM_PROMPT)
        self.llm = LLMClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
            fallback_models=fallback_models,
            auto_fallback=auto_fallback,
        )
        self.executor = ToolExecutor(self.state)

        self._lock = threading.Lock()
        self._pending_messages: List[dict] = []

    @property
    def is_busy(self) -> bool:
        return self.state.is_processing

    def enqueue(self, message: dict):
        queued = dict(message or {})
        self._pending_messages.append(queued)
        text = queued.get("text", "")
        logger.info(
            f"ChatEngine: 消息已排队 '{text[:50]}', queue_size={len(self._pending_messages)}"
        )

    def drain_pending(self) -> Optional[dict]:
        if not self._pending_messages:
            return None
        message = self._pending_messages.pop(0)
        return message

    def chat(self, text: str) -> str:
        reply, _ = self.chat_with_progress(text)
        return reply

    def chat_with_progress(
        self,
        text: str,
        on_tool_start: Optional[Callable[[str, dict], None]] = None,
        on_tool_done: Optional[Callable[[str, dict], None]] = None,
    ) -> Tuple[str, List[str]]:
        with self._lock:
            return self._do_chat(text, on_tool_start, on_tool_done)

    def reset(self):
        self.history.clear()
        self.state.clear_all()
        self._pending_messages.clear()
        logger.info("ChatEngine: 对话已重置")

    @property
    def model_name(self) -> str:
        return self.llm.last_used_model or self.llm.primary_model

    @property
    def resolved_model_name(self) -> str:
        return self.llm.last_resolved_model or self.model_name

    @property
    def configured_model_name(self) -> str:
        return self.llm.primary_model

    @property
    def fallback_model_names(self) -> List[str]:
        return list(self.llm.model_chain[1:])

    @property
    def model_chain(self) -> List[str]:
        return list(self.llm.model_chain)

    def _do_chat(
        self,
        text: str,
        on_tool_start: Optional[Callable] = None,
        on_tool_done: Optional[Callable] = None,
    ) -> Tuple[str, List[str]]:
        if self.history.is_stale():
            self.reset()

        self.state.is_processing = True
        try:
            self.history.append({"role": "user", "content": text})
            reply, step_log = self._agent_loop(on_tool_start, on_tool_done)
            return reply, step_log
        finally:
            self.state.is_processing = False

    def _agent_loop(
        self,
        on_tool_start: Optional[Callable] = None,
        on_tool_done: Optional[Callable] = None,
    ) -> Tuple[str, List[str]]:
        step_log = []

        for iteration in range(MAX_TOOL_ROUNDS):
            try:
                result = self.llm.chat(
                    messages=self.history.to_api_messages(),
                    tools=TOOL_SCHEMAS,
                )
            except Exception as exc:
                logger.error(f"LLM 调用失败 (第{iteration + 1}轮): {exc}")
                err = f"⚠️ AI 调用失败: {exc}"
                self.history.append({"role": "assistant", "content": err})
                return err, step_log

            choices = result.get("choices")
            if not choices:
                logger.error(f"LLM 无 choices: {_json.dumps(result, ensure_ascii=False)[:500]}")
                err = "⚠️ AI 返回异常，请稍后重试"
                self.history.append({"role": "assistant", "content": err})
                return err, step_log

            raw_message = choices[0].get("message", {})
            tool_calls = raw_message.get("tool_calls")

            logger.info(
                f"Agent 第{iteration + 1}轮 tool_calls={len(tool_calls) if tool_calls else 0}, "
                f"model={self.resolved_model_name}, has_content={bool(raw_message.get('content'))}"
            )

            if not tool_calls:
                reply = raw_message.get("content", "") or ""
                self.history.append({"role": "assistant", "content": reply})
                return reply, step_log

            clean_msg = _sanitize_assistant_message(raw_message)
            self.history.append(clean_msg)

            for tc in tool_calls:
                fn_name = tc.get("function", {}).get("name", "")
                fn_args_raw = tc.get("function", {}).get("arguments", "{}")
                tc_id = tc.get("id", "")

                try:
                    fn_args = _json.loads(fn_args_raw) if fn_args_raw else {}
                except (_json.JSONDecodeError, TypeError):
                    fn_args = {}

                logger.info(f"Agent tool [{iteration + 1}]: {fn_name}({fn_args})")

                friendly = friendly_tool_name(fn_name, fn_args)
                step_log.append(friendly)
                if on_tool_start:
                    on_tool_start(fn_name, fn_args)

                tool_result = self.executor.execute(fn_name, fn_args)

                if on_tool_done:
                    on_tool_done(fn_name, fn_args)

                self.history.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result.text,
                })

        timeout_msg = "⚠️ 处理步骤过多，请尝试简化请求。"
        self.history.append({"role": "assistant", "content": timeout_msg})
        return timeout_msg, step_log
