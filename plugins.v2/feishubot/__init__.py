"""
飞书机器人插件 v2.5.0 — MoviePilot Agent Mode
当启用 AI 后，插件变为完整 Agent：LLM 通过多轮 tool calling
自主编排搜索、筛选、推荐、下载、订阅等全部操作。
"""
import json as _json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType
from app.schemas.types import EventType
from apscheduler.schedulers.background import BackgroundScheduler


# ════════════════════════════════════════════════════════════════════════
#  OpenRouter LLM 客户端
# ════════════════════════════════════════════════════════════════════════
class _OpenRouterClient:
    """零依赖 OpenRouter Chat Completions 客户端"""

    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODEL = "google/gemini-2.5-flash-preview:free"

    def __init__(self, api_key: str, model: str = ""):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL

    def chat(
        self,
        messages: list,
        tools: list = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Tsutomu-miku/MoviePilot-Plugin-FeishuBot",
            "X-OpenRouter-Title": "MoviePilot-FeishuBot",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        resp = requests.post(self.BASE_URL, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()


# ════════════════════════════════════════════════════════════════════════
#  Agent Tools 定义 — 返回结构化数据供 LLM 推理
# ════════════════════════════════════════════════════════════════════════
_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_media",
            "description": (
                "搜索影视作品（电影/电视剧/动漫），返回媒体信息列表。"
                "当用户想查找、搜索、看某部影视作品时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "影视作品名称，如「流浪地球」「进击的巨人」",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_resources",
            "description": (
                "搜索指定影视作品的可下载种子资源，返回资源列表（含标题、站点、大小、"
                "做种数、分辨率/编码/音轨/来源等标签）。"
                "当用户想下载、想看资源列表、或指定了分辨率/声道/编码等偏好时使用。"
                "你可以根据返回的标签信息为用户筛选和推荐最合适的资源。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "影视作品名称",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_resource",
            "description": (
                "下载指定序号的种子资源。必须先调用 search_resources 获取资源列表后才能使用。"
                "在下载前应告知用户你选择了哪个资源及原因，并征得用户同意。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "资源在 search_resources 返回列表中的序号（从 0 开始）",
                    }
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subscribe_media",
            "description": (
                "订阅影视作品，订阅后系统会自动搜索并下载更新。"
                "可以传入 search_media 返回列表中的序号，或直接传入作品名称。"
                "当用户想订阅、追剧、自动下载时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "search_media 返回列表中的序号（从 0 开始），优先使用",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "如果没有先搜索过，可直接传入作品名称",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_downloading",
            "description": "获取当前正在下载的任务列表，返回每个任务的名称和进度。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "向用户发送一条中间状态消息（如搜索提示、处理中提示）。"
                "适合在执行耗时操作前告知用户正在处理。"
                "注意：Agent 最终回复会自动发送，不需要用这个工具发最终结果。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要发送的消息内容",
                    }
                },
                "required": ["text"],
            },
        },
    },
]

_AGENT_SYSTEM_PROMPT = """你是 MoviePilot 飞书机器人 AI 助手。你可以通过工具帮助用户搜索、下载、订阅影视资源。

## 你的工具
1. **search_media** — 搜索影视作品，获取媒体信息（标题、年份、类型、评分、简介）
2. **search_resources** — 搜索下载资源，获取种子列表（标题、站点、大小、做种数、标签：分辨率/编码/音轨/来源）
3. **download_resource** — 下载指定资源（需先 search_resources）
4. **subscribe_media** — 订阅影视（自动追更下载）
5. **get_downloading** — 查看当前下载进度
6. **send_message** — 发送中间状态提示给用户

## 工作规则

### 搜索流程
- 用户发来片名 → 调用 search_media → 向用户展示结果摘要
- 用户如果只是想了解作品信息，展示搜索结果即可

### 下载流程（核心能力）
- 用户想下载（含指定偏好如 4K/5.1/蓝光等）→ 先 send_message 告知正在搜索 → 调用 search_resources 获取资源列表
- 分析返回的 tags 字段，根据用户偏好智能筛选和排序
- 向用户推荐最匹配的 1-3 个资源，说明推荐理由（分辨率、音轨、做种数等）
- **重要：不要自行下载，展示推荐后等用户确认「下载第 X 个」**
- 用户确认后调用 download_resource

### 订阅流程
- 用户想订阅/追剧 → 调用 search_media 确认作品 → 调用 subscribe_media

### 偏好理解
自然语言映射示例：
- "4K" "超高清" → 2160p/4K/UHD
- "蓝光" "原盘" → BluRay/Remux
- "5.1环绕声" → 5.1/DD5.1/DDP5.1
- "全景声" → Atmos
- "杜比视界" "DV" → DolbyVision/DV
- "HDR" → HDR/HDR10/HDR10+
- "高码率" → Remux/BluRay + 大文件
- "体积小" "小文件" → WEB-DL + x265 + 较小 size

## 回复风格
- 简洁友好，使用中文
- 用 emoji 适当点缀，但不要过多
- 展示列表时用编号，突出关键信息（分辨率、大小、做种数）
- 如果用户只是打招呼或闲聊，直接友好回复，不需要调用工具"""


# ════════════════════════════════════════════════════════════════════════
#  主插件类
# ════════════════════════════════════════════════════════════════════════
class FeishuBot(_PluginBase):
    plugin_name = "飞书机器人"
    plugin_desc = "飞书群机器人消息通知与交互，支持 AI Agent 智能体模式"
    plugin_icon = "Feishu_A.png"
    plugin_version = "2.5.0"
    plugin_author = "Tsutomu-miku"
    author_url = "https://github.com/Tsutomu-miku"
    plugin_config_prefix = "feishubot_"
    plugin_order = 28
    auth_level = 1

    # ── 配置属性 ──
    _enabled = False
    _app_id = ""
    _app_secret = ""
    _chat_id = ""
    _msgtypes: list = []
    _token = ""
    _token_expire = datetime.min
    _scheduler: Optional[BackgroundScheduler] = None
    # ── LLM 配置 ──
    _llm_enabled = False
    _openrouter_key = ""
    _openrouter_model = ""

    # ── 运行时状态 ──
    _llm_client: Optional[_OpenRouterClient] = None
    _search_cache: dict = {}       # user_id -> List[MediaInfo]
    _resource_cache: dict = {}     # user_id -> List[Context]
    _conversations: dict = {}      # user_id -> List[message]
    _user_locks: dict = {}

    _MAX_AGENT_ITERATIONS = 8
    _MAX_CONVERSATION_MESSAGES = 30

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._app_id = config.get("app_id", "")
            self._app_secret = config.get("app_secret", "")
            self._chat_id = config.get("chat_id", "")
            self._msgtypes = config.get("msgtypes") or []
            self._llm_enabled = config.get("llm_enabled", False)
            self._openrouter_key = config.get("openrouter_key", "")
            self._openrouter_model = config.get("openrouter_model", "")

        self._search_cache = {}
        self._resource_cache = {}
        self._conversations = {}
        self._user_locks = {}
        self._llm_client = None

        if self._llm_enabled and self._openrouter_key:
            self._llm_client = _OpenRouterClient(
                api_key=self._openrouter_key,
                model=self._openrouter_model,
            )
            logger.info(
                f"飞书机器人 Agent 模式已启用，模型: "
                f"{self._openrouter_model or _OpenRouterClient.DEFAULT_MODEL}"
            )

    # ════════════════════════════════════════════════════════════════
    #  表单 / 页面
    # ════════════════════════════════════════════════════════════════
    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/feishu_event",
                "endpoint": self._feishu_event,
                "methods": ["POST"],
                "summary": "飞书事件回调",
            }
        ]

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
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
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
                                                "API Key 获取: https://openrouter.ai/settings/keys（注册即送免费额度）"
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
    #  飞书 Token / 消息发送
    # ════════════════════════════════════════════════════════════════
    def _get_token(self) -> str:
        if self._token and datetime.now() < self._token_expire:
            return self._token
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=10,
            )
            data = resp.json()
            self._token = data.get("tenant_access_token", "")
            self._token_expire = datetime.now() + timedelta(
                seconds=data.get("expire", 7200) - 60
            )
        except Exception as e:
            logger.error(f"获取飞书 Token 失败: {e}")
        return self._token

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _send_text(self, chat_id: str, text: str, reply_id: str = None):
        body: dict = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": _json.dumps({"text": text}, ensure_ascii=False),
        }
        if reply_id:
            body["reply_in_thread"] = True
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers=self._headers(), json=body, timeout=10,
            )
            if resp.json().get("code") != 0:
                logger.warning(f"飞书文本发送失败: {resp.text}")
        except Exception as e:
            logger.error(f"飞书发送文本异常: {e}")

    def _send_card(self, chat_id: str, card: dict):
        body: dict = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": _json.dumps(card, ensure_ascii=False),
        }
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers=self._headers(), json=body, timeout=10,
            )
            if resp.json().get("code") != 0:
                logger.warning(f"飞书卡片发送失败: {resp.text}")
        except Exception as e:
            logger.error(f"飞书发送卡片异常: {e}")

    # ════════════════════════════════════════════════════════════════
    #  事件回调入口
    # ════════════════════════════════════════════════════════════════
    def _feishu_event(self, request_data: dict = None, **kwargs) -> dict:
        data = request_data or {}
        # URL 验证
        if data.get("type") == "url_verification":
            return {"challenge": data.get("challenge", "")}
        # 卡片回调
        if data.get("type") == "card.action.trigger":
            return self._handle_card_action(data)

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
            self._send_text(chat_id, "暂时只支持文字消息哦~", msg_id)
            return

        try:
            text = _json.loads(msg.get("content", "{}")).get("text", "").strip()
        except Exception:
            text = ""
        if not text:
            return

        logger.info(f"飞书收到: user={user_id}, text={text}")

        # ── Agent 模式：一切交给 LLM ──
        if self._llm_client:
            self._agent_handle(text, chat_id, msg_id, user_id)
            return

        # ── 传统模式：指令解析 ──
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

    # ════════════════════════════════════════════════════════════════
    #  Agent 核心 — 多轮 Tool Calling 循环
    # ════════════════════════════════════════════════════════════════
    def _agent_handle(self, text: str, chat_id: str, msg_id: str, user_id: str):
        """Agent 入口：构建对话上下文，执行 agent loop"""
        lock = self._get_user_lock(user_id)
        if not lock.acquire(blocking=False):
            self._send_text(chat_id, "⏳ 上一个请求还在处理中，请稍候...")
            return
        try:
            # 构建对话历史
            history = self._get_conversation(user_id)
            history.append({"role": "user", "content": text})

            # 执行 agent loop
            final_reply = self._agent_loop(history, chat_id, user_id)

            # 发送最终回复
            if final_reply:
                self._send_text(chat_id, final_reply, msg_id)

            # 保存对话历史
            self._save_conversation(user_id, history)

        except Exception as e:
            logger.error(f"Agent 异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ AI 处理出错: {e}", msg_id)
        finally:
            lock.release()

    def _agent_loop(
        self, messages: list, chat_id: str, user_id: str
    ) -> str:
        """
        Agent 循环：
        1. 发送 messages + tools 给 LLM
        2. 如果 LLM 返回 tool_calls → 执行工具 → 结果反馈 → 回到 1
        3. 如果 LLM 返回纯文本 → 结束循环，返回文本
        """
        for iteration in range(self._MAX_AGENT_ITERATIONS):
            try:
                result = self._llm_client.chat(
                    messages=messages, tools=_AGENT_TOOLS
                )
            except Exception as e:
                logger.error(f"Agent LLM 调用失败 (第{iteration+1}轮): {e}")
                return f"⚠️ AI 调用失败: {e}"

            choice = result.get("choices", [{}])[0]
            message = choice.get("message", {})

            # 检查是否有 tool_calls
            tool_calls = message.get("tool_calls")

            if not tool_calls:
                # LLM 返回了最终文本回复
                reply = message.get("content", "")
                # 把 assistant 回复加入历史
                messages.append({"role": "assistant", "content": reply})
                return reply

            # 有 tool_calls → 执行工具
            # 先把 assistant 消息（含 tool_calls）加入历史
            messages.append(message)

            for tc in tool_calls:
                fn_name = tc.get("function", {}).get("name", "")
                fn_args_raw = tc.get("function", {}).get("arguments", "{}")
                tc_id = tc.get("id", "")

                try:
                    fn_args = _json.loads(fn_args_raw)
                except Exception:
                    fn_args = {}

                logger.info(
                    f"Agent tool call [{iteration+1}]: {fn_name}({fn_args})"
                )

                # 执行工具
                tool_result = self._execute_tool(
                    fn_name, fn_args, chat_id, user_id
                )

                # 把工具结果加入历史
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": _json.dumps(
                            tool_result, ensure_ascii=False, default=str
                        ),
                    }
                )

        # 超过最大轮次
        return "⚠️ 处理步骤过多，请尝试简化请求。"

    # ════════════════════════════════════════════════════════════════
    #  工具调度器
    # ════════════════════════════════════════════════════════════════
    def _execute_tool(
        self, fn_name: str, fn_args: dict, chat_id: str, user_id: str
    ) -> dict:
        """路由到具体的工具实现"""
        try:
            if fn_name == "search_media":
                return self._tool_search_media(
                    fn_args.get("keyword", ""), user_id
                )
            elif fn_name == "search_resources":
                return self._tool_search_resources(
                    fn_args.get("keyword", ""), user_id
                )
            elif fn_name == "download_resource":
                return self._tool_download_resource(
                    fn_args.get("index", 0), user_id, chat_id
                )
            elif fn_name == "subscribe_media":
                return self._tool_subscribe_media(
                    fn_args.get("index"), fn_args.get("keyword"), user_id
                )
            elif fn_name == "get_downloading":
                return self._tool_get_downloading()
            elif fn_name == "send_message":
                text = fn_args.get("text", "")
                if text:
                    self._send_text(chat_id, text)
                return {"sent": True}
            else:
                return {"error": f"未知工具: {fn_name}"}
        except Exception as e:
            logger.error(f"工具 {fn_name} 执行异常: {e}", exc_info=True)
            return {"error": str(e)}

    # ════════════════════════════════════════════════════════════════
    #  Agent 工具实现 — 返回结构化数据供 LLM 推理
    # ════════════════════════════════════════════════════════════════
    def _tool_search_media(self, keyword: str, user_id: str) -> dict:
        """搜索影视作品，返回媒体列表"""
        if not keyword:
            return {"error": "请提供搜索关键词"}
        try:
            from app.chain.media import MediaChain

            mc = MediaChain()
            result = mc.search(title=keyword)

            if not isinstance(result, tuple) or len(result) != 2:
                return {"error": "搜索返回格式异常", "results": []}

            meta, medias = result

            if not medias:
                name = getattr(meta, "name", keyword) if meta else keyword
                return {"keyword": keyword, "results": [], "message": f"未找到「{name}」的相关结果"}

            # 过滤有效结果并序列化
            valid = []
            for i, m in enumerate(medias[:8]):
                if not hasattr(m, "title"):
                    continue
                raw_type = getattr(m, "type", None)
                if hasattr(raw_type, "value"):
                    mtype_str = "电影" if raw_type == MediaType.MOVIE else "电视剧"
                else:
                    mtype_str = str(raw_type) if raw_type else "未知"

                valid.append(
                    {
                        "index": i,
                        "title": getattr(m, "title", ""),
                        "year": getattr(m, "year", ""),
                        "type": mtype_str,
                        "rating": getattr(m, "vote_average", ""),
                        "overview": (getattr(m, "overview", "") or "")[:120],
                        "tmdb_id": getattr(m, "tmdb_id", ""),
                    }
                )

            # 缓存
            self._search_cache[user_id] = medias[:8]

            return {
                "keyword": keyword,
                "total_found": len(medias),
                "results": valid,
            }
        except Exception as e:
            logger.error(f"tool search_media 异常: {e}", exc_info=True)
            return {"error": str(e)}

    def _tool_search_resources(self, keyword: str, user_id: str) -> dict:
        """搜索种子资源，返回资源列表（含标签）"""
        if not keyword:
            return {"error": "请提供搜索关键词"}
        try:
            from app.chain.media import MediaChain
            from app.chain.search import SearchChain

            # 先识别精确标题
            title = keyword
            try:
                mc = MediaChain()
                result = mc.search(title=keyword)
                if isinstance(result, tuple) and len(result) == 2:
                    meta, medias = result
                    if medias and hasattr(medias[0], "title"):
                        title = medias[0].title or keyword
            except Exception:
                pass

            # 搜索种子
            contexts = SearchChain().search_by_title(title=title)
            if not contexts:
                return {
                    "keyword": keyword,
                    "title": title,
                    "results": [],
                    "message": f"未找到「{title}」的下载资源",
                }

            # 序列化并提取标签
            results = []
            for i, ctx in enumerate(contexts[:20]):
                t = getattr(ctx, "torrent_info", None)
                if not t:
                    continue
                tname = getattr(t, "title", "") or getattr(t, "description", "") or ""
                tags = self._extract_tags(tname)
                results.append(
                    {
                        "index": i,
                        "title": tname,
                        "site": getattr(t, "site_name", ""),
                        "size": getattr(t, "size", ""),
                        "seeders": getattr(t, "seeders", ""),
                        "tags": tags,
                    }
                )

            # 缓存
            self._resource_cache[user_id] = contexts[:20]

            return {
                "keyword": keyword,
                "title": title,
                "total_found": len(contexts),
                "showing": len(results),
                "results": results,
            }
        except Exception as e:
            logger.error(f"tool search_resources 异常: {e}", exc_info=True)
            return {"error": str(e)}

    def _tool_download_resource(
        self, index: int, user_id: str, chat_id: str
    ) -> dict:
        """下载指定序号的资源"""
        cached = self._resource_cache.get(user_id, [])
        if not cached:
            return {"error": "没有缓存的资源列表，请先调用 search_resources"}
        if index < 0 or index >= len(cached):
            return {"error": f"序号 {index} 无效，有效范围: 0-{len(cached)-1}"}

        try:
            from app.chain.download import DownloadChain

            ctx = cached[index]
            t = getattr(ctx, "torrent_info", None)
            title = getattr(t, "title", "未知") if t else "未知"

            dc = DownloadChain()
            result = dc.download_single(context=ctx, userid="feishu")
            if result:
                return {"success": True, "title": title, "message": f"已添加下载: {title}"}
            else:
                return {"success": False, "title": title, "message": "下载提交失败"}
        except Exception as e:
            logger.error(f"tool download_resource 异常: {e}", exc_info=True)
            return {"error": str(e)}

    def _tool_subscribe_media(
        self, index: Optional[int], keyword: Optional[str], user_id: str
    ) -> dict:
        """订阅影视作品"""
        mediainfo = None

        # 优先从缓存取
        if index is not None:
            cached = self._search_cache.get(user_id, [])
            if 0 <= index < len(cached):
                mediainfo = cached[index]

        # 无缓存 → 用关键词搜索
        if not mediainfo and keyword:
            try:
                from app.chain.media import MediaChain

                mc = MediaChain()
                result = mc.search(title=keyword)
                if isinstance(result, tuple) and len(result) == 2:
                    _, medias = result
                    if medias:
                        for m in medias:
                            if hasattr(m, "title") and hasattr(m, "type"):
                                mediainfo = m
                                break
            except Exception as e:
                return {"error": f"搜索失败: {e}"}

        if not mediainfo:
            return {"error": "未找到可订阅的作品，请提供更精确的名称"}

        try:
            from app.chain.subscribe import SubscribeChain

            title = getattr(mediainfo, "title", "") or "未知"
            raw_type = getattr(mediainfo, "type", None)
            mtype = raw_type if (raw_type and hasattr(raw_type, "value")) else MediaType.MOVIE

            sid, err_msg = SubscribeChain().add(
                mtype=mtype,
                title=title,
                year=getattr(mediainfo, "year", ""),
                tmdbid=getattr(mediainfo, "tmdb_id", None),
                doubanid=getattr(mediainfo, "douban_id", None),
                exist_ok=True,
                username="飞书用户",
            )
            if sid:
                return {"success": True, "title": title, "message": f"已订阅: {title}"}
            else:
                return {"success": False, "title": title, "message": err_msg or "订阅失败"}
        except Exception as e:
            logger.error(f"tool subscribe_media 异常: {e}", exc_info=True)
            return {"error": str(e)}

    def _tool_get_downloading(self) -> dict:
        """获取当前下载列表"""
        try:
            from app.chain.download import DownloadChain

            torrents = DownloadChain().downloading_torrents()
            if not torrents:
                return {"tasks": [], "message": "当前没有正在下载的任务"}

            tasks = []
            for t in torrents[:15]:
                tasks.append(
                    {
                        "title": getattr(t, "title", "") or getattr(t, "name", "未知"),
                        "progress": getattr(t, "progress", 0),
                    }
                )
            return {"tasks": tasks, "total": len(torrents)}
        except Exception as e:
            return {"error": str(e)}

    # ════════════════════════════════════════════════════════════════
    #  标签提取
    # ════════════════════════════════════════════════════════════════
    @staticmethod
    def _extract_tags(title: str) -> dict:
        """从资源标题中提取结构化标签"""
        if not title:
            return {}
        tl = title.lower()
        tags = {}

        # 分辨率
        for kw, label in [
            ("2160p", "4K"), ("4k", "4K"), ("uhd", "4K"),
            ("1080p", "1080p"), ("1080i", "1080p"),
            ("720p", "720p"),
        ]:
            if kw in tl:
                tags["resolution"] = label
                break

        # 视频编码
        for kw, label in [
            ("hevc", "HEVC/x265"), ("x265", "HEVC/x265"), ("h.265", "HEVC/x265"), ("h265", "HEVC/x265"),
            ("x264", "x264"), ("h.264", "x264"), ("h264", "x264"), ("avc", "x264"),
            ("av1", "AV1"),
        ]:
            if kw in tl:
                tags["video_codec"] = label
                break

        # HDR
        for kw, label in [
            ("dolby.vision", "Dolby Vision"), ("dolbyvision", "Dolby Vision"),
            ("dovi", "Dolby Vision"), (".dv.", "Dolby Vision"),
            ("hdr10+", "HDR10+"), ("hdr10plus", "HDR10+"),
            ("hdr10", "HDR10"), ("hdr", "HDR"),
        ]:
            if kw in tl:
                tags["hdr"] = label
                break

        # 音轨
        for kw, label in [
            ("atmos", "Atmos"), ("truehd", "TrueHD"),
            ("dts-hd", "DTS-HD MA"), ("dts.hd", "DTS-HD MA"), ("dtshdma", "DTS-HD MA"),
            ("dts-x", "DTS:X"), ("dtsx", "DTS:X"),
            ("dts", "DTS"),
            ("ddp5.1", "DD+ 5.1"), ("dd+5.1", "DD+ 5.1"), ("ddp.5.1", "DD+ 5.1"),
            ("dd5.1", "DD 5.1"),
            ("7.1", "7.1ch"), ("5.1", "5.1ch"),
            ("aac", "AAC"), ("flac", "FLAC"),
        ]:
            if kw in tl:
                tags["audio"] = label
                break

        # 来源
        for kw, label in [
            ("remux", "Remux"), ("bdremux", "Remux"),
            ("bluray", "BluRay"), ("blu-ray", "BluRay"),
            ("web-dl", "WEB-DL"), ("webdl", "WEB-DL"),
            ("webrip", "WEBRip"), ("web-rip", "WEBRip"),
            ("hdtv", "HDTV"),
        ]:
            if kw in tl:
                tags["source"] = label
                break

        return tags

    # ════════════════════════════════════════════════════════════════
    #  对话历史管理
    # ════════════════════════════════════════════════════════════════
    def _get_conversation(self, user_id: str) -> list:
        """获取用户的对话历史（含 system prompt）"""
        if user_id not in self._conversations:
            self._conversations[user_id] = [
                {"role": "system", "content": _AGENT_SYSTEM_PROMPT}
            ]
        return self._conversations[user_id]

    def _save_conversation(self, user_id: str, messages: list):
        """保存对话历史，保留最近 N 条（system prompt 始终保留）"""
        if len(messages) > self._MAX_CONVERSATION_MESSAGES:
            # 保留 system prompt + 最近的消息
            system = messages[0]
            recent = messages[-(self._MAX_CONVERSATION_MESSAGES - 1):]
            messages = [system] + recent
        self._conversations[user_id] = messages

    def _clear_conversation(self, user_id: str):
        """清除用户对话历史"""
        self._conversations.pop(user_id, None)

    # ════════════════════════════════════════════════════════════════
    #  卡片回调（兼容 v2.4.0 卡片）
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
        result = self._tool_download_resource(idx, user_id, chat_id)
        msg = result.get("message") or result.get("error", "操作失败")
        icon = "✅" if result.get("success") else "⚠️"
        self._send_text(chat_id, f"{icon} {msg}")

    def _card_subscribe(self, idx: int, user_id: str, chat_id: str):
        result = self._tool_subscribe_media(idx, None, user_id)
        msg = result.get("message") or result.get("error", "操作失败")
        icon = "✅" if result.get("success") else "⚠️"
        self._send_text(chat_id, f"{icon} {msg}")

    # ════════════════════════════════════════════════════════════════
    #  传统模式指令（LLM 未启用时的回退）
    # ════════════════════════════════════════════════════════════════
    def _get_user_lock(self, user_id: str):
        if user_id not in self._user_locks:
            self._user_locks[user_id] = threading.Lock()
        return self._user_locks[user_id]

    def _cmd_search(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        if not keyword:
            return
        self._send_text(chat_id, f"🔍 正在搜索: {keyword} ...", msg_id)
        try:
            from app.chain.media import MediaChain

            mc = MediaChain()
            result = mc.search(title=keyword)
            if not isinstance(result, tuple) or len(result) != 2:
                self._send_text(chat_id, "⚠️ 搜索返回异常")
                return
            meta, medias = result
            if not medias:
                name = getattr(meta, "name", keyword) if meta else keyword
                self._send_text(chat_id, f"😔 未找到: {name}")
                return
            valid = [m for m in medias[:6] if hasattr(m, "title")]
            if not valid:
                self._send_text(chat_id, "😔 未找到有效结果")
                return
            self._search_cache[user_id] = valid
            lines = []
            for i, m in enumerate(valid):
                title = getattr(m, "title", "")
                year = getattr(m, "year", "")
                rt = getattr(m, "type", None)
                mt = "电影" if (rt and hasattr(rt, "value") and rt == MediaType.MOVIE) else "电视剧"
                vote = getattr(m, "vote_average", "")
                line = f"{i+1}. {title} ({year}) [{mt}]"
                if vote:
                    line += f" ⭐{vote}"
                lines.append(line)
            lines.append("\n回复「订阅+序号」如: 订阅1 | 回复「下载+序号」如: 下载1")
            self._send_text(chat_id, "\n".join(lines), msg_id)
        except Exception as e:
            logger.error(f"搜索异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 搜索出错: {e}")

    def _cmd_subscribe(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        if not keyword:
            return
        self._send_text(chat_id, f"📥 正在订阅: {keyword} ...", msg_id)
        result = self._tool_subscribe_media(None, keyword, user_id)
        msg = result.get("message") or result.get("error", "操作失败")
        icon = "✅" if result.get("success") else "⚠️"
        self._send_text(chat_id, f"{icon} {msg}")

    def _cmd_downloading(self, chat_id: str, msg_id: str):
        result = self._tool_get_downloading()
        tasks = result.get("tasks", [])
        if not tasks:
            self._send_text(chat_id, "当前没有正在下载的任务", msg_id)
            return
        lines = [f"{i+1}. {t['title']}  进度: {t['progress']}%" for i, t in enumerate(tasks)]
        self._send_text(chat_id, "\n".join(lines), msg_id)

    def _cmd_help(self, chat_id: str, msg_id: str):
        agent_on = "✅ 已启用" if self._llm_client else "❌ 未启用"
        model = self._openrouter_model or _OpenRouterClient.DEFAULT_MODEL
        self._send_text(
            chat_id,
            f"📖 飞书机器人帮助\n\n"
            f"AI Agent: {agent_on}\n"
            f"模型: {model}\n\n"
            f"开启 AI 后直接用自然语言对话：\n"
            f"• 「流浪地球2」→ 搜索\n"
            f"• 「找个4K杜比视界的星际穿越」→ 智能筛选资源\n"
            f"• 「订阅进击的巨人」→ 自动追更\n"
            f"• 「下载进度怎么样」→ 查看下载\n\n"
            f"传统指令（始终可用）：\n"
            f"/搜索 <片名>\n"
            f"/订阅 <片名>\n"
            f"/正在下载\n"
            f"/帮助",
            msg_id,
        )

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
        self._send_text(self._chat_id, text)

    @eventmanager.register(EventType.DownloadAdded)
    def _on_download(self, event: Event):
        if not self._enabled or "download" not in self._msgtypes or not self._chat_id:
            return
        edata = event.event_data or {}
        mi = edata.get("mediainfo")
        title = getattr(mi, "title", "未知") if mi else "未知"
        self._send_text(self._chat_id, f"⬇️ 开始下载: {title}")

    @eventmanager.register(EventType.SubscribeAdded)
    def _on_subscribe(self, event: Event):
        if not self._enabled or "subscribe" not in self._msgtypes or not self._chat_id:
            return
        edata = event.event_data or {}
        title = edata.get("title") or edata.get("name") or "未知"
        self._send_text(self._chat_id, f"📌 新增订阅: {title}")

    # ════════════════════════════════════════════════════════════════
    #  生命周期
    # ════════════════════════════════════════════════════════════════
    def stop_service(self):
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._scheduler.shutdown()
            self._scheduler = None
