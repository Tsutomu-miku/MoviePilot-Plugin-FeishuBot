"""飞书机器人插件 v3.0.0 — MoviePilot Agent Mode

重构说明：
- 模块化拆分：飞书 API / LLM 客户端 / Agent 循环 / 工具实现 / 对话管理 分离
- 修复 Agent 不生效的多个致命 Bug（消息格式污染、对话历史腐败、截断破坏配对）
- 下载操作增加工具层面的强制确认机制（confirmed 参数）
- 对话历史安全管理（副本操作 + 智能截断）
"""

import json as _json
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .feishu_api import FeishuAPI
from .llm_client import OpenRouterClient
from .agent import AgentRunner, SYSTEM_PROMPT
from .tool_impl import ToolExecutor
from .conversation import ConversationManager
from .legacy_cmd import LegacyHandler


class FeishuBot(_PluginBase):

    # ── 插件元信息 ──
    plugin_name = "飞书机器人"
    plugin_desc = "飞书群机器人消息通知与交互，支持 AI Agent 智能体模式"
    plugin_icon = "Feishu_A.png"
    plugin_version = "3.0.0"
    plugin_author = "Tsutomu-miku"
    author_url = "https://github.com/Tsutomu-miku"
    plugin_config_prefix = "feishubot_"
    plugin_order = 28
    auth_level = 1

    # ── 配置属性 ──
    _enabled: bool = False
    _app_id: str = ""
    _app_secret: str = ""
    _chat_id: str = ""
    _msgtypes: list = []
    _llm_enabled: bool = False
    _openrouter_key: str = ""
    _openrouter_model: str = ""

    # ── 运行时组件（init_plugin 中初始化）──
    _feishu: Optional[FeishuAPI] = None
    _llm_client: Optional[OpenRouterClient] = None
    _agent: Optional[AgentRunner] = None
    _tools: Optional[ToolExecutor] = None
    _conversations: Optional[ConversationManager] = None
    _legacy: Optional[LegacyHandler] = None
    _user_locks: Dict[str, threading.Lock] = {}

    # ════════════════════════════════════════════════════════════════
    #  生命周期
    # ════════════════════════════════════════════════════════════════

    def init_plugin(self, config: dict = None):
        logger.info(
            f"飞书机器人插件初始化, config keys="
            f"{list(config.keys()) if config else 'None'}"
        )
        if config:
            self._enabled = config.get("enabled", False)
            self._app_id = config.get("app_id", "")
            self._app_secret = config.get("app_secret", "")
            self._chat_id = config.get("chat_id", "")
            self._msgtypes = config.get("msgtypes") or []

            # MoviePilot VSwitch 可能存为字符串
            llm_raw = config.get("llm_enabled")
            if isinstance(llm_raw, bool):
                self._llm_enabled = llm_raw
            elif isinstance(llm_raw, str):
                self._llm_enabled = llm_raw.lower() in ("true", "1", "yes", "on")
            else:
                self._llm_enabled = bool(llm_raw) if llm_raw is not None else False

            self._openrouter_key = str(config.get("openrouter_key", "") or "").strip()
            self._openrouter_model = str(config.get("openrouter_model", "") or "").strip()

        # ── 初始化组件 ──
        self._feishu = FeishuAPI(self._app_id, self._app_secret, self._chat_id)
        self._tools = ToolExecutor()
        self._user_locks = {}
        self._llm_client = None
        self._agent = None
        self._conversations = None
        self._legacy = LegacyHandler(self._feishu, self._tools)

        logger.info(
            f"飞书机器人配置: enabled={self._enabled}, "
            f"llm_enabled={self._llm_enabled}, "
            f"api_key={'已配置(长度' + str(len(self._openrouter_key)) + ')' if self._openrouter_key else '未配置'}, "
            f"model={self._openrouter_model or 'default'}"
        )

        if self._llm_enabled and self._openrouter_key:
            try:
                self._llm_client = OpenRouterClient(
                    api_key=self._openrouter_key,
                    model=self._openrouter_model,
                )
                self._agent = AgentRunner(self._llm_client, self._tools)
                self._conversations = ConversationManager(SYSTEM_PROMPT)
                logger.info(
                    f"飞书 Agent 模式已启用 ✓ 模型: "
                    f"{self._openrouter_model or OpenRouterClient.DEFAULT_MODEL}"
                )
            except Exception as e:
                logger.error(f"飞书 Agent 初始化失败: {e}", exc_info=True)
        elif self._llm_enabled:
            logger.warning("飞书 AI Agent 已启用但 OpenRouter API Key 未配置，回退到传统模式")
        else:
            logger.info("飞书传统模式（AI Agent 未启用）")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def stop_service(self):
        pass

    # ════════════════════════════════════════════════════════════════
    #  API 端点
    # ════════════════════════════════════════════════════════════════

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/feishu_event",
                "endpoint": self._feishu_event,
                "methods": ["POST"],
                "summary": "飞书事件回调",
            }
        ]

    def _feishu_event(self, request_data: dict = None, **kwargs) -> dict:
        data = request_data or {}

        # URL 验证
        if data.get("type") == "url_verification":
            return {"challenge": data.get("challenge", "")}

        # 卡片回调
        if data.get("type") == "card.action.trigger":
            return self._handle_card_action(data)

        # 消息事件
        header = data.get("header", {})
        event = data.get("event", {})
        if header.get("event_type") == "im.message.receive_v1":
            threading.Thread(
                target=self._handle_message, args=(event,), daemon=True
            ).start()

        return {"code": 0}

    # ════════════════════════════════════════════════════════════════
    #  消息路由
    # ════════════════════════════════════════════════════════════════

    def _handle_message(self, event: dict):
        msg = event.get("message", {})
        chat_id = msg.get("chat_id", "") or self._chat_id
        msg_id = msg.get("message_id", "")
        msg_type = msg.get("message_type", "")
        sender = event.get("sender", {}).get("sender_id", {})
        user_id = sender.get("open_id", "")

        if msg_type != "text":
            self._feishu.send_text(chat_id, "暂时只支持文字消息哦~")
            return

        try:
            text = _json.loads(msg.get("content", "{}")).get("text", "").strip()
        except Exception:
            text = ""
        if not text:
            return

        logger.info(
            f"飞书收到: user={user_id}, text={text}, "
            f"agent_mode={self._agent is not None}"
        )

        # ── 诊断指令（始终可用）──
        if text.startswith("/status") or text.startswith("/状态"):
            self._cmd_status(chat_id, msg_id)
            return

        # ── 清除对话（Agent 模式专用）──
        if text in ("/clear", "/清除", "清除对话", "重新开始"):
            if self._conversations:
                self._conversations.clear(user_id)
            self._feishu.send_text(chat_id, "🗑️ 对话已清除，可以重新开始啦~")
            return

        # ── Agent 模式 ──
        if self._agent and self._conversations:
            logger.info(f"[Agent] 路由到 Agent: {text[:50]}")
            self._agent_handle(text, chat_id, msg_id, user_id)
            return

        # ── 传统模式 ──
        logger.info(f"[Legacy] 路由到传统指令: {text[:50]}")
        self._legacy.handle(text, chat_id, msg_id, user_id)

    # ════════════════════════════════════════════════════════════════
    #  Agent 入口
    # ════════════════════════════════════════════════════════════════

    def _get_user_lock(self, user_id: str) -> threading.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = threading.Lock()
        return self._user_locks[user_id]

    def _agent_handle(self, text: str, chat_id: str, msg_id: str, user_id: str):
        """Agent 入口：构建上下文 → 执行循环 → 发送回复 → 安全保存历史"""
        lock = self._get_user_lock(user_id)
        if not lock.acquire(blocking=False):
            self._feishu.send_text(chat_id, "⏳ 上一个请求还在处理中，请稍候...")
            return

        try:
            # 获取对话历史副本，追加新用户消息
            messages = self._conversations.get(user_id)
            messages.append({"role": "user", "content": text})

            # 创建发送中间消息的回调
            def send_interim(msg_text: str):
                self._feishu.send_text(chat_id, msg_text)

            # 执行 Agent 循环（在 messages 副本上操作）
            updated_messages, reply = self._agent.run(
                messages=messages,
                chat_id=chat_id,
                user_id=user_id,
                send_message_fn=send_interim,
            )

            # 发送最终回复
            if reply:
                self._feishu.send_text(chat_id, reply, reply_msg_id=msg_id)
            else:
                # 即使是空回复也给用户反馈
                self._feishu.send_text(chat_id, "🤔 我没有想到回复内容，请再试试~")

            # 成功后才保存对话历史
            self._conversations.save(user_id, updated_messages)

        except Exception as e:
            logger.error(f"Agent 异常: {e}", exc_info=True)
            self._feishu.send_text(chat_id, f"⚠️ AI 处理出错: {e}")
        finally:
            lock.release()

    # ════════════════════════════════════════════════════════════════
    #  卡片回调（兼容传统模式的下载/订阅按钮）
    # ════════════════════════════════════════════════════════════════

    def _handle_card_action(self, data: dict) -> dict:
        try:
            action = data.get("event", {}).get("action", {})
            value = action.get("value", {})
            act = value.get("action", "")
            operator = data.get("event", {}).get("operator", {})
            user_id = operator.get("open_id", "")
            ctx = data.get("event", {}).get("context", {})
            chat_id = ctx.get("open_chat_id", "") or self._chat_id

            if act == "download_resource":
                idx = int(value.get("index", 0))
                threading.Thread(
                    target=self._card_download,
                    args=(idx, user_id, chat_id),
                    daemon=True,
                ).start()
            elif act == "subscribe":
                idx = int(value.get("index", 0))
                threading.Thread(
                    target=self._card_subscribe,
                    args=(idx, user_id, chat_id),
                    daemon=True,
                ).start()
        except Exception as e:
            logger.error(f"卡片回调异常: {e}", exc_info=True)
        return {"code": 0}

    def _card_download(self, idx: int, user_id: str, chat_id: str):
        result = self._tools.download_resource(idx, confirmed=True, user_id=user_id)
        msg = result.get("message") or result.get("error", "操作失败")
        icon = "✅" if result.get("success") else "⚠️"
        self._feishu.send_text(chat_id, f"{icon} {msg}")

    def _card_subscribe(self, idx: int, user_id: str, chat_id: str):
        result = self._tools.subscribe_media(idx, None, user_id)
        msg = result.get("message") or result.get("error", "操作失败")
        icon = "✅" if result.get("success") else "⚠️"
        self._feishu.send_text(chat_id, f"{icon} {msg}")

    # ════════════════════════════════════════════════════════════════
    #  诊断命令
    # ════════════════════════════════════════════════════════════════

    def _cmd_status(self, chat_id: str, msg_id: str):
        model = self._openrouter_model or OpenRouterClient.DEFAULT_MODEL
        conv_count = self._conversations.active_users if self._conversations else 0
        self._feishu.send_text(
            chat_id,
            f"🔧 插件诊断\n"
            f"版本: {self.plugin_version}\n"
            f"启用: {self._enabled}\n"
            f"AI Agent: {'✅ 已激活' if self._agent else '❌ 未激活'}\n"
            f"  llm_enabled: {self._llm_enabled}\n"
            f"  api_key: {'已配置' if self._openrouter_key else '未配置'}\n"
            f"  model: {model}\n"
            f"对话缓存: {conv_count} 个用户\n"
            f"指令: /clear 清除对话 | /status 查看状态",
        )

    # ════════════════════════════════════════════════════════════════
    #  表单配置
    # ════════════════════════════════════════════════════════════════

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        MsgTypeOptions = [
            {"title": "入库", "value": "transfer"},
            {"title": "资源下载", "value": "download"},
            {"title": "订阅", "value": "subscribe"},
            {"title": "站点消息", "value": "site"},
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    # ── 基础配置行 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "app_id",
                                            "label": "App ID",
                                            "placeholder": "飞书应用 App ID",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "app_secret",
                                            "label": "App Secret",
                                            "placeholder": "飞书应用 App Secret",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "chat_id",
                                            "label": "群 Chat ID",
                                            "placeholder": "可选，不填则自动获取",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ── 消息类型 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "msgtypes",
                                            "label": "通知消息类型",
                                            "multiple": True,
                                            "chips": True,
                                            "items": MsgTypeOptions,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # ── 分割线 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{"component": "VDivider"}],
                            }
                        ],
                    },
                    # ── AI Agent 配置 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "llm_enabled",
                                            "label": "启用 AI Agent",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "openrouter_key",
                                            "label": "OpenRouter API Key",
                                            "placeholder": "sk-or-v1-...",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "openrouter_model",
                                            "label": "模型 (可选)",
                                            "placeholder": "默认: google/gemini-2.5-flash-preview:free",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ── 说明 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "开启 AI Agent 后，机器人将化身智能体：自动理解自然语言、"
                                                "按偏好筛选资源（4K/杜比/5.1 等）、多轮对话确认后下载。\n"
                                                "下载操作需用户明确确认后才会执行。\n"
                                                "API Key 获取: https://openrouter.ai/settings/keys"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "app_id": "",
            "app_secret": "",
            "chat_id": "",
            "msgtypes": ["transfer", "download"],
            "llm_enabled": False,
            "openrouter_key": "",
            "openrouter_model": "",
        }

    def get_page(self) -> List[dict]:
        pass

    # ════════════════════════════════════════════════════════════════
    #  事件通知
    # ════════════════════════════════════════════════════════════════

    @eventmanager.register(EventType.TransferComplete)
    def _on_transfer(self, event: Event):
        if not self._enabled or "transfer" not in self._msgtypes or not self._chat_id:
            return
        edata = event.event_data or {}
        mi = edata.get("mediainfo")
        if not mi:
            return
        title = getattr(mi, "title", "")
        year = getattr(mi, "year", "")
        text = f"🎬 入库完成: {title}" + (f" ({year})" if year else "")
        self._feishu.send_text(self._chat_id, text)

    @eventmanager.register(EventType.DownloadAdded)
    def _on_download(self, event: Event):
        if not self._enabled or "download" not in self._msgtypes or not self._chat_id:
            return
        edata = event.event_data or {}
        mi = edata.get("mediainfo")
        title = getattr(mi, "title", "未知") if mi else "未知"
        self._feishu.send_text(self._chat_id, f"⬇️ 开始下载: {title}")

    @eventmanager.register(EventType.SubscribeAdded)
    def _on_subscribe(self, event: Event):
        if not self._enabled or "subscribe" not in self._msgtypes or not self._chat_id:
            return
        edata = event.event_data or {}
        title = edata.get("title") or edata.get("name") or "未知"
        self._feishu.send_text(self._chat_id, f"📌 新增订阅: {title}")
