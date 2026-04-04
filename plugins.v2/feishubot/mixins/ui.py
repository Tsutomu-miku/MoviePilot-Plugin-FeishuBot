"""Feishu bot form/page/event presentation helpers."""

from datetime import datetime
from typing import Any, Dict, List, Tuple

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.schemas.types import EventType

from ..ai.llm import FREE_MODEL_CHOICES
from ..card_builder import _CardBuilder
from ..utils import _HAS_LARK_SDK


class FeishuUIMixin:
        def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
            MsgTypeOptions = [
                {"title": "入库", "value": "transfer"},
                {"title": "资源下载", "value": "download"},
                {"title": "订阅", "value": "subscribe"},
                {"title": "站点消息", "value": "site"},
            ]
            FreeModelOptions = [
                {"title": item["title"], "value": item["value"]}
                for item in FREE_MODEL_CHOICES
            ]
            return [
                {
                    "component": "VForm",
                    "content": [
                        {
                            "component": "VRow",
                            "content": [
                                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
                                ]},
                                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                    {"component": "VSwitch", "props": {"model": "use_ws", "label": "WebSocket 长连接"}},
                                ]},
                                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                    {"component": "VTextField", "props": {"model": "app_id", "label": "App ID", "placeholder": "飞书应用 App ID"}},
                                ]},
                                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                    {"component": "VTextField", "props": {"model": "app_secret", "label": "App Secret", "placeholder": "飞书应用 App Secret"}},
                                ]},
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                    {"component": "VTextField", "props": {"model": "chat_id", "label": "群 Chat ID", "placeholder": "可选，不填则自动获取"}},
                                ]},
                                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                    {"component": "VSelect", "props": {"model": "msgtypes", "label": "通知消息类型", "multiple": True, "chips": True, "items": MsgTypeOptions}},
                                ]},
                            ],
                        },
                        {"component": "VRow", "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VAlert", "props": {
                                    "type": "info", "variant": "tonal",
                                    "text": (
                                        "WebSocket 长连接模式（推荐）：无需公网 IP、域名或 HTTPS，NAS/Docker 友好。\n"
                                        "需安装 lark-oapi：在容器中执行 pip install lark-oapi\n"
                                        "飞书应用后台 -> 事件订阅 -> 选择“使用长连接接收”。"
                                    ),
                                }},
                            ]},
                        ]},
                        {"component": "VRow", "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VDivider"}]},
                        ]},
                        {
                            "component": "VRow",
                            "content": [
                                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                    {"component": "VSwitch", "props": {"model": "llm_enabled", "label": "启用 AI Agent"}},
                                ]},
                                {"component": "VCol", "props": {"cols": 12, "md": 5}, "content": [
                                    {"component": "VTextField", "props": {"model": "openrouter_key", "label": "OpenRouter API Key", "placeholder": "sk-or-v1-..."}},
                                ]},
                                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                    {"component": "VSwitch", "props": {"model": "openrouter_auto_fallback", "label": "自动切换后备免费模型"}},
                                ]},
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                    {"component": "VSelect", "props": {"model": "openrouter_free_model", "label": "免费主模型", "items": FreeModelOptions}},
                                ]},
                                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                    {"component": "VSelect", "props": {"model": "openrouter_fallback_models", "label": "免费后备模型", "multiple": True, "chips": True, "items": FreeModelOptions}},
                                ]},
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                {"component": "VCol", "props": {"cols": 12}, "content": [
                                    {"component": "VTextField", "props": {"model": "openrouter_model", "label": "自定义模型（可选，优先于免费主模型）", "placeholder": "例如：openai/gpt-4o-mini；留空则走免费主模型"}},
                                ]},
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VAlert", "props": {
                                    "type": "info", "variant": "tonal",
                                    "text": (
                                        "AI Agent 的实际能力是：理解自然语言、搜索影视、按偏好筛选资源、二次确认下载、订阅和查询下载进度。\n"
                                        "默认推荐 OpenRouter 免费路由；当具体免费模型限流、下线或无可用 provider 时，会自动切换到后备模型。\n"
                                        "API Key: https://openrouter.ai/settings/keys"
                                    ),
                                }},
                            ]}],
                        },
                    ],
                }
            ], {
                "enabled": False,
                "use_ws": True,
                "app_id": "",
                "app_secret": "",
                "chat_id": "",
                "msgtypes": ["transfer", "download"],
                "llm_enabled": False,
                "openrouter_key": "",
                "openrouter_model": "",
                "openrouter_free_model": DEFAULT_MODEL,
                "openrouter_fallback_models": list(DEFAULT_FALLBACK_MODELS),
                "openrouter_auto_fallback": True,
            }

        def get_page(self) -> List[dict]:
            """插件详情页 — 运行时状态（仅使用 MoviePilot 已知支持的组件）"""
            try:
                uptime = "未知"
                if hasattr(self, "_init_ts") and self._init_ts:
                    delta = datetime.now() - self._init_ts
                    hours, remainder = divmod(int(delta.total_seconds()), 3600)
                    mins, secs = divmod(remainder, 60)
                    uptime = f"{hours}h {mins}m {secs}s"

                feishu_ok = getattr(self, "_feishu_ok", False)
                agent_active = bool(self._llm_enabled and self._openrouter_key)
                model = self._get_ai_status_model()

                ws_status = "未启用"
                if self._use_ws:
                    if not _HAS_LARK_SDK:
                        ws_status = "SDK 未安装"
                    elif getattr(self, "_ws_connected", False):
                        ws_status = "✅ 已连接"
                    elif self._ws_running:
                        ws_status = "🔄 连接中"
                    else:
                        ws_status = "❌ 未运行"

                lines = [
                    f"📌 插件 v{self.plugin_version}  |  实例 {id(self):#x}  |  运行 {uptime}",
                    "",
                    f"📡 飞书: Token {'✅ 正常' if feishu_ok else '❌ 异常'}  |  "
                    f"API {'✅' if self._feishu else '❌'}  |  "
                    f"App ID {'✅' if self._app_id else '❌'}  |  "
                    f"WS {ws_status}",
                    "",
                    f"🤖 Agent: {'✅ 运行中' if agent_active else ('⚠️ 已启用未激活' if self._llm_enabled else '❌ 未启用')}  |  "
                    f"模型 {model}",
                    "",
                    f"📊 消息 {getattr(self, '_msg_count', 0)}  |  "
                    f"Agent {getattr(self, '_agent_count', 0)}  |  "
                    f"传统 {getattr(self, '_legacy_count', 0)}  |  "
                    f"恢复 {getattr(self, '_recover_count', 0)}",
                ]
                status_text = "\n".join(lines)
                alert_type = "success" if (feishu_ok and self._enabled) else "warning"

                return [
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
                                            "type": alert_type,
                                            "variant": "tonal",
                                            "text": status_text,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ]
            except Exception as e:
                logger.error(f"get_page 渲染异常: {e}", exc_info=True)
                return []

        @eventmanager.register(EventType.TransferComplete)
        def _on_transfer(self, event: Event):
            if not self._enabled or "transfer" not in self._msgtypes or not self._chat_id:
                return
            mi = (event.event_data or {}).get("mediainfo")
            if not mi:
                return
            title = getattr(mi, "title", "")
            year = getattr(mi, "year", "")
            text = f"**{title}**" + (f" ({year})" if year else "") + " 已入库完成"
            self._feishu.send_card(
                self._chat_id,
                _CardBuilder.notify_card("🎬 入库完成", text, "green"),
            )

        @eventmanager.register(EventType.DownloadAdded)
        def _on_download(self, event: Event):
            if not self._enabled or "download" not in self._msgtypes or not self._chat_id:
                return
            mi = (event.event_data or {}).get("mediainfo")
            title = getattr(mi, "title", "未知") if mi else "未知"
            self._feishu.send_card(
                self._chat_id,
                _CardBuilder.notify_card("⬇️ 开始下载", f"**{title}** 已添加到下载队列", "blue"),
            )

        @eventmanager.register(EventType.SubscribeAdded)
        def _on_subscribe(self, event: Event):
            if not self._enabled or "subscribe" not in self._msgtypes or not self._chat_id:
                return
            title = (event.event_data or {}).get("title") or (event.event_data or {}).get("name") or "未知"
            self._feishu.send_card(
                self._chat_id,
                _CardBuilder.notify_card("📌 新增订阅", f"**{title}** 已加入订阅列表", "violet"),
            )
