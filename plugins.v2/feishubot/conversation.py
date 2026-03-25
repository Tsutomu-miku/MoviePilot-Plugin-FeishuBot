"""对话历史管理 — 安全的追加/截断/清除"""

from typing import List, Dict, Optional

from app.log import logger


# 对话默认上限
MAX_CONVERSATION_MESSAGES = 30


class ConversationManager:
    """管理每个用户的多轮对话历史"""

    def __init__(self, system_prompt: str, max_messages: int = MAX_CONVERSATION_MESSAGES):
        self._system_prompt = system_prompt
        self._max_messages = max_messages
        self._store: Dict[str, list] = {}  # user_id -> messages

    def get(self, user_id: str) -> list:
        """获取用户对话历史（含 system prompt）。返回列表的浅拷贝。"""
        if user_id not in self._store:
            self._store[user_id] = [
                {"role": "system", "content": self._system_prompt}
            ]
        return list(self._store[user_id])  # 返回副本，防止意外修改

    def save(self, user_id: str, messages: list):
        """
        保存对话历史，智能截断以保持消息序列完整性。

        截断规则：
        - 始终保留 system prompt
        - 不在 tool_call 序列中间截断
          （assistant 带 tool_calls 和对应 tool 结果必须成对保留）
        """
        if len(messages) <= self._max_messages:
            self._store[user_id] = messages
            return

        system = messages[0]
        # 从最新消息向前保留，但要找到安全的截断点
        max_recent = self._max_messages - 1  # 减去 system prompt
        candidates = messages[1:]  # 去掉 system

        if len(candidates) <= max_recent:
            self._store[user_id] = messages
            return

        # 从截断点开始向后搜索安全位置
        cut_start = len(candidates) - max_recent

        # 安全截断：确保不在 tool_call 序列中间切断
        # 向后移动截断点直到找到安全位置（user 消息或纯 assistant 回复）
        while cut_start < len(candidates):
            msg = candidates[cut_start]
            role = msg.get("role", "")
            # user 消息是安全的截断起点
            if role == "user":
                break
            # 纯文本 assistant 消息（无 tool_calls）也是安全的
            if role == "assistant" and not msg.get("tool_calls"):
                break
            cut_start += 1

        recent = candidates[cut_start:]
        self._store[user_id] = [system] + recent

        logger.debug(
            f"对话历史截断: user={user_id}, "
            f"原始={len(messages)}, 保留={1 + len(recent)}"
        )

    def clear(self, user_id: str):
        """清除指定用户的对话历史"""
        self._store.pop(user_id, None)

    @property
    def active_users(self) -> int:
        """当前有对话缓存的用户数"""
        return len(self._store)
