"""传统指令模式处理器（LLM 未启用时的回退）"""

import re
from typing import Optional

from app.log import logger
from app.schemas import MediaType

from .feishu_api import FeishuAPI
from .llm_client import OpenRouterClient
from .tool_impl import ToolExecutor


class LegacyHandler:
    """传统斜杠指令处理器"""

    def __init__(self, feishu: FeishuAPI, tools: ToolExecutor):
        self.feishu = feishu
        self.tools = tools

    def handle(self, text: str, chat_id: str, msg_id: str, user_id: str):
        """解析指令并执行。未匹配到指令则默认当作搜索。"""
        if text.startswith("/帮助") or text.startswith("/help"):
            self._cmd_help(chat_id, msg_id)
        elif text.startswith("/搜索") or text.startswith("/search"):
            kw = re.sub(r"^/(搜索|search)\s*", "", text).strip()
            self._cmd_search(kw, chat_id, msg_id, user_id)
        elif text.startswith("/订阅") or text.startswith("/subscribe"):
            kw = re.sub(r"^/(订阅|subscribe)\s*", "", text).strip()
            self._cmd_subscribe(kw, chat_id, msg_id, user_id)
        elif text.startswith("/正在下载") or text.startswith("/downloading"):
            self._cmd_downloading(chat_id, msg_id)
        else:
            # 默认当作搜索
            self._cmd_search(text, chat_id, msg_id, user_id)

    def _cmd_search(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        if not keyword:
            return
        self.feishu.send_text(chat_id, f"🔍 正在搜索: {keyword} ...")

        result = self.tools.search_media(keyword, user_id)
        if result.get("error"):
            self.feishu.send_text(chat_id, f"⚠️ {result['error']}")
            return

        items = result.get("results", [])
        if not items:
            msg = result.get("message", f"😔 未找到: {keyword}")
            self.feishu.send_text(chat_id, msg)
            return

        lines = []
        for item in items:
            line = (
                f"{item['index'] + 1}. {item['title']} ({item['year']}) "
                f"[{item['type']}]"
            )
            if item.get("rating"):
                line += f" ⭐{item['rating']}"
            lines.append(line)
        lines.append("\n回复「/订阅 片名」订阅 | 回复片名搜索资源")
        self.feishu.send_text(chat_id, "\n".join(lines))

    def _cmd_subscribe(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        if not keyword:
            return
        self.feishu.send_text(chat_id, f"📥 正在订阅: {keyword} ...")
        result = self.tools.subscribe_media(None, keyword, user_id)
        msg = result.get("message") or result.get("error", "操作失败")
        icon = "✅" if result.get("success") else "⚠️"
        self.feishu.send_text(chat_id, f"{icon} {msg}")

    def _cmd_downloading(self, chat_id: str, msg_id: str):
        result = self.tools.get_downloading()
        tasks = result.get("tasks", [])
        if not tasks:
            self.feishu.send_text(chat_id, "当前没有正在下载的任务")
            return
        lines = [
            f"{i + 1}. {t['title']}  进度: {t['progress']}%"
            for i, t in enumerate(tasks)
        ]
        self.feishu.send_text(chat_id, "\n".join(lines))

    def _cmd_help(self, chat_id: str, msg_id: str):
        self.feishu.send_text(
            chat_id,
            "📖 飞书机器人帮助\n\n"
            "传统指令：\n"
            "/搜索 <片名> — 搜索影视\n"
            "/订阅 <片名> — 订阅追更\n"
            "/正在下载 — 查看下载进度\n"
            "/帮助 — 显示此帮助\n\n"
            "💡 启用 AI Agent 后可直接用自然语言对话",
        )
