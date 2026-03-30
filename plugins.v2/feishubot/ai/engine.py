"""
AI 对话系统 — ChatEngine 主引擎

这是 AI 对话系统的唯一入口。外部（__init__.py）只需调用:
    engine = ChatEngine(api_key, model)
    result = engine.chat(user_text)
"""

import json as _json
import time as _time
from typing import Callable, Optional, Tuple, List

from app.log import logger

from .types import ChatState, ToolResult
from .history import ChatHistory
from .llm import LLMClient
from .tools import TOOL_SCHEMAS, friendly_tool_name
from .executor import ToolExecutor
from .prompts import SYSTEM_PROMPT, MAX_TOOL_ROUNDS


def _sanitize_assistant_message(raw_msg: dict) -> dict:
    """
    清洗 LLM 返回的 assistant 消息，只保留标准字段。
    防止 API 响应的额外字段（refusal, annotations 等）污染对话历史。
    """
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
    """
    AI 对话主引擎 — 单用户版本。

    职责：
    - 管理对话历史 (ChatHistory)
    - 调用 LLM (LLMClient)
    - 执行工具循环 (ToolExecutor)
    - 提供进度回调接口

    Usage:
        engine = ChatEngine(api_key="...", model="...")
        engine.executor.bind(extract_tags=_extract_tags)

        # 简单模式
        reply = engine.chat("搜索流浪地球")

        # 带进度回调
        reply, steps = engine.chat_with_progress("搜索流浪地球")
    """

    def __init__(self, api_key: str, model: str = "", base_url: str = ""):
        self.state = ChatState()
        self.history = ChatHistory(SYSTEM_PROMPT)
        self.llm = LLMClient(api_key=api_key, model=model, base_url=base_url)
        self.executor = ToolExecutor(self.state)

    # ════════════════════════════════════════════════════════════════
    #  公开接口
    # ════════════════════════════════════════════════════════════════

    def chat(self, text: str) -> str:
        """
        处理用户消息，返回 AI 回复文本。

        最简接口 — 不需要进度回调时使用。
        """
        reply, _ = self.chat_with_progress(text)
        return reply

    def chat_with_progress(
        self,
        text: str,
        on_tool_start: Optional[Callable[[str, dict], None]] = None,
        on_tool_done: Optional[Callable[[str, dict], None]] = None,
    ) -> Tuple[str, List[str]]:
        """
        处理用户消息，返回 (AI 回复文本, 工具步骤列表)。

        Args:
            text:           用户消息
            on_tool_start:  工具开始执行时的回调 (tool_name, tool_args)
            on_tool_done:   工具完成时的回调 (tool_name, tool_args)

        Returns:
            (reply_text, step_log)
        """
        # 对话过期自动重置
        if self.history.is_stale():
            self.reset()

        self.state.is_processing = True
        try:
            # 添加用户消息
            self.history.append({"role": "user", "content": text})

            # 进入 agent loop
            reply, step_log = self._agent_loop(on_tool_start, on_tool_done)
            return reply, step_log
        finally:
            self.state.is_processing = False

    def reset(self):
        """重置对话（清空历史 + 全部状态）"""
        self.history.clear()
        self.state.clear_all()
        logger.info("ChatEngine: 对话已重置")

    @property
    def is_processing(self) -> bool:
        return self.state.is_processing

    @property
    def model_name(self) -> str:
        return self.llm.model

    # ════════════════════════════════════════════════════════════════
    #  Agent Loop（核心循环）
    # ════════════════════════════════════════════════════════════════

    def _agent_loop(
        self,
        on_tool_start: Optional[Callable] = None,
        on_tool_done: Optional[Callable] = None,
    ) -> Tuple[str, List[str]]:
        """
        LLM 多轮 tool-calling 循环。

        流程：
        1. 发送当前历史给 LLM
        2. 如果 LLM 返回 tool_calls → 执行工具 → 结果加入历史 → 回到 1
        3. 如果 LLM 返回纯文本 → 加入历史 → 返回
        4. 最多循环 MAX_TOOL_ROUNDS 次

        Returns:
            (reply_text, step_log)
        """
        step_log = []

        for iteration in range(MAX_TOOL_ROUNDS):
            # ── 调用 LLM ──
            try:
                result = self.llm.chat(
                    messages=self.history.to_api_messages(),
                    tools=TOOL_SCHEMAS,
                )
            except Exception as e:
                logger.error(f"LLM 调用失败 (第{iteration + 1}轮): {e}")
                err = f"⚠️ AI 调用失败: {e}"
                self.history.append({"role": "assistant", "content": err})
                return err, step_log

            # ── 解析响应 ──
            choices = result.get("choices")
            if not choices:
                logger.error(f"LLM 无 choices: {_json.dumps(result, ensure_ascii=False)[:500]}")
                err = "⚠️ AI 返回异常，请稍后重试"
                self.history.append({"role": "assistant", "content": err})
                return err, step_log

            raw_message = choices[0].get("message", {})
            tool_calls = raw_message.get("tool_calls")

            logger.info(
                f"Agent 第{iteration + 1}轮: "
                f"tool_calls={len(tool_calls) if tool_calls else 0}, "
                f"has_content={bool(raw_message.get('content'))}"
            )

            # ── 无 tool_calls → 最终回复 ──
            if not tool_calls:
                reply = raw_message.get("content", "") or ""
                self.history.append({"role": "assistant", "content": reply})
                return reply, step_log

            # ── 有 tool_calls → 清洗消息 + 执行工具 ──
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

                # 进度回调
                friendly = friendly_tool_name(fn_name, fn_args)
                step_log.append(friendly)
                if on_tool_start:
                    on_tool_start(fn_name, fn_args)

                # 执行工具
                tool_result = self.executor.execute(fn_name, fn_args)

                if on_tool_done:
                    on_tool_done(fn_name, fn_args)

                # 结果加入历史
                self.history.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result.text,
                })

        # 超过最大轮数
        timeout_msg = "⚠️ 处理步骤过多，请尝试简化请求。"
        self.history.append({"role": "assistant", "content": timeout_msg})
        return timeout_msg, step_log
