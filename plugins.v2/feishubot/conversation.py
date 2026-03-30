"""飞书机器人插件 — 对话管理器"""

import time as _time


_MAX_CONVERSATION_MESSAGES = 30


class _ConversationManager:
    """管理每个用户的多轮对话历史（安全截断，不破坏 tool_call 配对）"""

    def __init__(self, system_prompt: str, max_messages: int = _MAX_CONVERSATION_MESSAGES):
        self._system_prompt = system_prompt
        self._max_messages = max_messages
        self._store: Dict[str, list] = {}

    def get(self, user_id: str) -> list:
        """返回对话历史的副本（含 system prompt），防止意外修改"""
        if user_id not in self._store:
            self._store[user_id] = [{"role": "system", "content": self._system_prompt}]
        return list(self._store[user_id])

    def save(self, user_id: str, messages: list):
        """保存对话历史，智能截断 — 不在 tool_call 序列中间切断"""
        if len(messages) <= self._max_messages:
            self._store[user_id] = messages
            return

        system = messages[0]
        candidates = messages[1:]
        max_recent = self._max_messages - 1
        cut_start = len(candidates) - max_recent

        # 向后移动截断点直到找到安全位置
        while cut_start < len(candidates):
            msg = candidates[cut_start]
            role = msg.get("role", "")
            if role == "user":
                break
            if role == "assistant" and not msg.get("tool_calls"):
                break
            cut_start += 1

        self._store[user_id] = [system] + candidates[cut_start:]

    def clear(self, user_id: str):
        self._store.pop(user_id, None)

    @property
    def active_users(self) -> int:
        return len(self._store)


# ╔════════════════════════════════════════════════════════════════════╗
# ║  5. Agent 工具定义                                                 ║
# ╚════════════════════════════════════════════════════════════════════╝



def _sanitize_assistant_message(raw_msg: dict) -> dict:
    """
    清洗 LLM 返回的 assistant 消息，只保留标准字段。
    防止 API 响应的额外字段（refusal, annotations 等）污染对话历史。
    """
    clean: dict = {"role": "assistant"}

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


# ╔════════════════════════════════════════════════════════════════════╗
# ║  8. 主插件类                                                       ║
# ╚════════════════════════════════════════════════════════════════════╝
