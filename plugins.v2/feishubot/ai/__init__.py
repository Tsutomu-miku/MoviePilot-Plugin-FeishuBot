"""
AI 对话子包 — 对外暴露 ChatEngine 作为唯一入口。

Usage:
    from .ai import ChatEngine

    engine = ChatEngine(api_key="...", model="...")
    reply = engine.chat("搜索流浪地球")
"""

from .engine import ChatEngine
from .types import ChatState, ToolResult

__all__ = ["ChatEngine", "ChatState", "ToolResult"]
