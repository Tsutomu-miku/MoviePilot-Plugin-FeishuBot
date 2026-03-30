"""AI 对话系统 — 对话历史管理（单用户版）"""

import time as _time

from .prompts import MAX_HISTORY_MESSAGES


class ChatHistory:
    """
    单用户对话历史管理器。

    职责：
    - 维护 messages 列表（自动裁剪）
    - 确保 system prompt 始终在首位
    - 输出 OpenAI API 格式的消息列表
    - 安全截断：不在 tool_calls → tool result 中间切断
    """

    def __init__(self, system_prompt: str):
        self._system_msg = {"role": "system", "content": system_prompt}
        self._messages: list = []
        self._last_active: float = _time.time()

    # ── 读 ──

    def to_api_messages(self) -> list:
        """输出 OpenAI API 格式的完整消息列表（含 system）"""
        return [self._system_msg] + list(self._messages)

    @property
    def count(self) -> int:
        return len(self._messages)

    def is_stale(self, ttl_seconds: int = 3600) -> bool:
        """对话是否超过 TTL（默认 1 小时）"""
        return (_time.time() - self._last_active) > ttl_seconds

    # ── 写 ──

    def append(self, message: dict):
        """添加一条消息"""
        self._messages.append(message)
        self._last_active = _time.time()
        self._trim()

    def extend(self, messages: list):
        """批量添加消息（一次 tool-calling 轮次的多条结果）"""
        self._messages.extend(messages)
        self._last_active = _time.time()
        self._trim()

    def save_snapshot(self, messages: list):
        """
        用完整的消息列表覆盖历史（兼容原 _agent_loop 返回 working 列表的模式）。
        传入的 messages 应包含 system 消息在首位，本方法会自动去除。
        """
        if messages and messages[0].get("role") == "system":
            self._messages = messages[1:]
        else:
            self._messages = list(messages)
        self._last_active = _time.time()
        self._trim()

    def clear(self):
        """清空历史"""
        self._messages.clear()

    # ── 内部 ──

    def _trim(self):
        """裁剪到最大消息数，保留最近的消息。不在 tool 序列中间切断。"""
        if len(self._messages) <= MAX_HISTORY_MESSAGES:
            return

        cut = len(self._messages) - MAX_HISTORY_MESSAGES

        # 向后移动截断点，找到安全位置（user 或无 tool_calls 的 assistant）
        while cut < len(self._messages):
            msg = self._messages[cut]
            role = msg.get("role", "")
            if role == "user":
                break
            if role == "assistant" and not msg.get("tool_calls"):
                break
            cut += 1

        self._messages = self._messages[cut:]
