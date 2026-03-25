import json as _json
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Type

import pytz
import requests
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaInfo, MediaType
from app.schemas.types import EventType
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


# ========================================================================
#  OpenRouter LLM 客户端 —— 零额外依赖，纯 requests 实现
# ========================================================================
class _OpenRouterClient:
    """
    轻量 OpenRouter Chat Completions 客户端
    兼容 OpenAI /v1/chat/completions 格式，支持 function calling
    """

    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODEL = "openai/gpt-oss-120b:free"

    def __init__(self, api_key: str, model: str = ""):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL

    def chat(
        self,
        messages: list,
        tools: list = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> dict:
        """
        发送 chat completion 请求
        :return: OpenAI 格式的 response dict
        """
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

        resp = requests.post(
            self.BASE_URL, headers=headers, json=payload, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def quick_ask(self, prompt: str, system: str = "") -> str:
        """简单问答，返回纯文本"""
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        data = self.chat(msgs)
        return (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )


# ========================================================================
#  意图识别 & 工具定义
# ========================================================================

# Function calling 的 tools 定义
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_media",
            "description": "搜索影视作品（电影、电视剧、动漫）。当用户想查找、搜索、看某部影视作品时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "影视作品名称关键词，如「流浪地球」「进击的巨人」",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subscribe_media",
            "description": "订阅影视作品，系统会自动搜索并下载。当用户想订阅、追剧、自动下载某部作品时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "要订阅的影视作品名称",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_resources",
            "description": "搜索指定影视作品的下载资源并按偏好筛选。当用户想要下载某部作品并可能指定了分辨率、声道、编码等偏好时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "影视作品名称",
                    },
                    "filters": {
                        "type": "object",
                        "description": "资源筛选条件",
                        "properties": {
                            "resolution": {
                                "type": "string",
                                "description": "分辨率偏好，如 4K/2160p/1080p/720p",
                            },
                            "audio": {
                                "type": "string",
                                "description": "音频偏好，如 5.1/7.1/Atmos/DTS/DTS-HD/TrueHD",
                            },
                            "codec": {
                                "type": "string",
                                "description": "视频编码偏好，如 x265/HEVC/x264/AV1/HDR/DV(Dolby Vision)",
                            },
                            "source": {
                                "type": "string",
                                "description": "来源偏好，如 BluRay/WEB-DL/Remux/HDTV",
                            },
                            "subtitle": {
                                "type": "string",
                                "description": "字幕偏好，如 中字/简中/繁中/双语",
                            },
                        },
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_downloading",
            "description": "查看当前正在下载的任务。当用户想知道下载进度、当前在下什么时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_help",
            "description": "显示帮助信息。当用户问怎么用、有什么功能时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chat_reply",
            "description": "普通闲聊回复。当用户的消息不涉及影视搜索/订阅/下载，而是闲聊、问好、问问题时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reply": {
                        "type": "string",
                        "description": "回复给用户的内容",
                    },
                },
                "required": ["reply"],
            },
        },
    },
]

_SYSTEM_PROMPT = """你是 MoviePilot 飞书机器人助手，帮助用户搜索、订阅和下载影视资源。

你的能力：
1. **搜索影视**：根据片名搜索电影/电视剧/动漫
2. **订阅影视**：订阅后系统会自动搜索下载
3. **搜索资源并筛选**：搜索下载资源，支持按分辨率(4K/1080p)、声道(5.1/7.1/Atmos)、编码(x265/HDR/DV)、来源(BluRay/Remux)、字幕等筛选
4. **查看下载**：查看正在下载的任务
5. **闲聊**：回答用户的一般性问题

规则：
- 如果用户发了一个影视名称（不带任何其他说明），默认当作搜索处理
- 如果用户说"找4K的XX"、"要5.1声道"、"下载XX的蓝光版"，使用 search_resources 并提取筛选条件
- 如果用户说"订阅XX"、"追XX"、"自动下载XX"，使用 subscribe_media
- 如果用户只是打招呼或问问题，用 chat_reply 回复
- 筛选条件要从用户自然语言中智能提取，比如"杜比视界"→DV，"全景声"→Atmos
- 对用户的消息保持简洁友好的回复风格"""


# ========================================================================
#  主插件类
# ========================================================================
class FeishuBot(_PluginBase):
    # 插件名称
    plugin_name = "飞书机器人"
    # 插件描述
    plugin_desc = "飞书群机器人消息通知与交互，支持 AI 智能意图识别"
    # 插件图标
    plugin_icon = "Feishu_A.png"
    # 插件版本
    plugin_version = "2.4.0"
    # 插件作者
    plugin_author = "Tsutomu-miku"
    # 作者主页
    author_url = "https://github.com/Tsutomu-miku"
    # 插件配置项ID前缀
    plugin_config_prefix = "feishubot_"
    # 加载顺序
    plugin_order = 28
    # 可使用的用户级别
    auth_level = 1

    # ---- 配置属性 ----
    _enabled = False
    _app_id = ""
    _app_secret = ""
    _chat_id = ""
    _msgtypes: list = []
    _token = ""
    _token_expire = datetime.min
    _scheduler: Optional[BackgroundScheduler] = None

    # ---- LLM 配置 ----
    _llm_enabled = False
    _openrouter_key = ""
    _openrouter_model = ""

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._app_id = config.get("app_id", "")
            self._app_secret = config.get("app_secret", "")
            self._chat_id = config.get("chat_id", "")
            self._msgtypes = config.get("msgtypes") or []
            # LLM 配置
            self._llm_enabled = config.get("llm_enabled", False)
            self._openrouter_key = config.get("openrouter_key", "")
            self._openrouter_model = config.get("openrouter_model", "")
        self._search_cache: dict = {}
        self._user_locks: dict = {}
        self._llm_client: Optional[_OpenRouterClient] = None
        if self._llm_enabled and self._openrouter_key:
            self._llm_client = _OpenRouterClient(
                api_key=self._openrouter_key,
                model=self._openrouter_model,
            )

    # region ========= 表单/页面 =========
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
        """
        拼装插件配置页面
        """
        MsgTypeOptions = [
            {"title": "入库", "value": "transfer"},
            {"title": "资源下载", "value": "download"},
            {"title": "订阅", "value": "subscribe"},
            {"title": "站点消息", "value": "site"},
            {"title": "手动处理", "value": "manual"},
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    # ---- 基础配置行 ----
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
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
                                "props": {"cols": 12, "md": 4},
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
                                "props": {"cols": 12, "md": 4},
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
                        ],
                    },
                    # ---- 群ID + 消息类型 ----
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "chat_id",
                                            "label": "群 Chat ID (可选)",
                                            "placeholder": "不填则回复到消息来源",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "msgtypes",
                                            "label": "消息类型",
                                            "multiple": True,
                                            "chips": True,
                                            "items": MsgTypeOptions,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ---- 分割线：AI 能力 ----
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                    }
                                ],
                            }
                        ],
                    },
                    # ---- AI / LLM 配置 ----
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "llm_enabled",
                                            "label": "启用 AI 智能识别",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "openrouter_model",
                                            "label": "模型 (可选)",
                                            "placeholder": "默认: openai/gpt-oss-120b:free",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ---- 提示信息 ----
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
                                            "text": "基础用法：在飞书开放平台创建自建应用并配置事件回调地址: "
                                            "http(s)://你的域名/api/v1/plugin/feishu_event\n"
                                            "AI 增强：启用后支持自然语言交互，自动识别意图并筛选资源。"
                                            "OpenRouter 提供免费模型，注册即可获取 API Key: https://openrouter.ai/settings/keys",
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

    # endregion

    # region ========= 飞书 Token / 发送 =========
    def _get_token(self) -> str:
        """获取 tenant_access_token（带缓存）"""
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
            logger.error(f"获取飞书Token失败: {e}")
        return self._token

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _send_text(self, chat_id: str, text: str, reply_id: str = None):
        """发送文本消息"""
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
                headers=self._headers(),
                json=body,
                timeout=10,
            )
            if resp.json().get("code") != 0:
                logger.warning(f"飞书文本消息发送失败: {resp.text}")
        except Exception as e:
            logger.error(f"飞书发送文本异常: {e}")

    def _send_card(self, chat_id: str, card: dict, reply_id: str = None):
        """发送卡片消息"""
        body: dict = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": _json.dumps(card, ensure_ascii=False),
        }
        if reply_id:
            body["reply_in_thread"] = True
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers=self._headers(),
                json=body,
                timeout=10,
            )
            if resp.json().get("code") != 0:
                logger.warning(f"飞书卡片消息发送失败: {resp.text}")
        except Exception as e:
            logger.error(f"飞书发送卡片异常: {e}")

    def _send_image(self, chat_id: str, image_url: str):
        """上传并发送图片（image_key 方式）"""
        try:
            img_resp = requests.get(image_url, timeout=15)
            if img_resp.status_code != 200:
                return
            upload = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {self._get_token()}"},
                data={"image_type": "message"},
                files={"image": ("poster.jpg", img_resp.content, "image/jpeg")},
                timeout=15,
            )
            image_key = upload.json().get("data", {}).get("image_key")
            if not image_key:
                return
            body = {
                "receive_id": chat_id,
                "msg_type": "image",
                "content": _json.dumps({"image_key": image_key}),
            }
            requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers=self._headers(),
                json=body,
                timeout=10,
            )
        except Exception as e:
            logger.error(f"飞书发送图片异常: {e}")

    # endregion

    # region ========= 事件回调入口 =========
    def _feishu_event(self, request_data: dict = None, **kwargs) -> dict:
        """
        飞书事件回调统一入口
        """
        data = request_data or {}

        # ---- 1. URL 验证挑战 ----
        if data.get("type") == "url_verification":
            return {"challenge": data.get("challenge", "")}

        # ---- 2. 卡片回调 (card.action.trigger) ----
        if data.get("type") == "card.action.trigger":
            return self._handle_card_action(data)

        header = data.get("header", {})
        event = data.get("event", {})
        event_type = header.get("event_type", "")

        # ---- 3. 普通消息 ----
        if event_type == "im.message.receive_v1":
            threading.Thread(
                target=self._handle_message, args=(event,), daemon=True
            ).start()
        return {"code": 0}

    # endregion

    # region ========= 消息处理 — 含 LLM 意图识别 =========
    def _handle_message(self, event: dict):
        """处理 im.message.receive_v1 事件"""
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

        # ---- 优先检查显式指令（始终生效，不依赖 LLM）----
        if text.startswith("/search") or text.startswith("/搜索"):
            keyword = re.sub(r"^/(search|搜索)\s*", "", text).strip()
            self._cmd_search(keyword, chat_id, msg_id, user_id)
            return
        if text.startswith("/subscribe") or text.startswith("/订阅"):
            keyword = re.sub(r"^/(subscribe|订阅)\s*", "", text).strip()
            self._cmd_subscribe(keyword, chat_id, msg_id, user_id)
            return
        if text.startswith("/downloading") or text.startswith("/正在下载"):
            self._cmd_downloading(chat_id, msg_id)
            return
        if text.startswith("/help") or text.startswith("/帮助"):
            self._cmd_help(chat_id, msg_id)
            return

        # ---- LLM 意图识别模式 ----
        if self._llm_client:
            self._handle_with_llm(text, chat_id, msg_id, user_id)
        else:
            # 无 LLM：回退到默认搜索
            self._cmd_search(text, chat_id, msg_id, user_id)

    def _handle_with_llm(self, text: str, chat_id: str, msg_id: str, user_id: str):
        """使用 LLM function calling 识别意图并路由"""
        try:
            messages = [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ]
            result = self._llm_client.chat(messages=messages, tools=_TOOLS)

            choice = result.get("choices", [{}])[0]
            message = choice.get("message", {})

            # 检查是否有 tool_calls
            tool_calls = message.get("tool_calls")
            if tool_calls:
                tc = tool_calls[0]  # 取第一个工具调用
                fn_name = tc.get("function", {}).get("name", "")
                fn_args_str = tc.get("function", {}).get("arguments", "{}")
                try:
                    fn_args = _json.loads(fn_args_str)
                except Exception:
                    fn_args = {}

                logger.info(f"LLM 意图: {fn_name}, args={fn_args}")
                self._dispatch_tool(fn_name, fn_args, chat_id, msg_id, user_id)
            else:
                # LLM 没有调用工具，直接返回文本回复
                reply = message.get("content", "")
                if reply:
                    self._send_text(chat_id, reply, msg_id)
                else:
                    # 回退到搜索
                    self._cmd_search(text, chat_id, msg_id, user_id)

        except Exception as e:
            logger.warning(f"LLM 意图识别异常，回退到关键词搜索: {e}")
            # LLM 失败时回退到默认搜索
            self._cmd_search(text, chat_id, msg_id, user_id)

    def _dispatch_tool(
        self, fn_name: str, fn_args: dict,
        chat_id: str, msg_id: str, user_id: str,
    ):
        """根据 LLM 返回的 function name 路由到对应处理方法"""
        if fn_name == "search_media":
            self._cmd_search(fn_args.get("keyword", ""), chat_id, msg_id, user_id)
        elif fn_name == "subscribe_media":
            self._cmd_subscribe(fn_args.get("keyword", ""), chat_id, msg_id, user_id)
        elif fn_name == "search_resources":
            keyword = fn_args.get("keyword", "")
            filters = fn_args.get("filters", {})
            self._cmd_search_resources(keyword, filters, chat_id, msg_id, user_id)
        elif fn_name == "show_downloading":
            self._cmd_downloading(chat_id, msg_id)
        elif fn_name == "show_help":
            self._cmd_help(chat_id, msg_id)
        elif fn_name == "chat_reply":
            reply = fn_args.get("reply", "🤔 我不太理解你的意思")
            self._send_text(chat_id, reply, msg_id)
        else:
            logger.warning(f"未知的 LLM 工具调用: {fn_name}")
            self._cmd_search(fn_args.get("keyword", ""), chat_id, msg_id, user_id)

    # endregion

    # region ========= 指令实现 =========

    def _get_user_lock(self, user_id: str):
        """获取用户级别的锁，防止同一用户并发操作冲突"""
        if user_id not in self._user_locks:
            self._user_locks[user_id] = threading.Lock()
        return self._user_locks[user_id]

    def _cmd_search(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        """
        搜索影视
        使用 MediaChain().search(title) -> (MetaBase, List[MediaInfo])
        """
        if not keyword:
            return
        lock = self._get_user_lock(user_id)
        if not lock.acquire(blocking=False):
            self._send_text(chat_id, "⏳ 上一个请求还在处理中，请稍候...", msg_id)
            return
        try:
            self._send_text(chat_id, f"🔍 正在搜索: {keyword} ...", msg_id)
            from app.chain.media import MediaChain

            mc = MediaChain()
            result = mc.search(title=keyword)

            if not isinstance(result, tuple) or len(result) != 2:
                logger.warning(f"search() 返回了非预期类型: {type(result)}")
                self._send_text(chat_id, "⚠️ 搜索返回异常，请重试")
                return

            meta, medias = result

            if not meta or not getattr(meta, "name", None):
                self._send_text(chat_id, f"😔 无法识别: {keyword}")
                return
            if not medias:
                self._send_text(
                    chat_id,
                    f"😔 未找到 {getattr(meta, 'name', keyword)} 的相关结果",
                )
                return

            valid_medias = [
                m for m in medias[:6]
                if hasattr(m, "title") and hasattr(m, "type")
            ]
            if not valid_medias:
                self._send_text(
                    chat_id,
                    f"😔 未找到 {getattr(meta, 'name', keyword)} 的有效结果",
                )
                return

            self._search_cache[user_id] = valid_medias
            self._send_card(chat_id, self._build_search_card(valid_medias, keyword))
        except Exception as e:
            logger.error(f"飞书搜索异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 搜索出错: {e}")
        finally:
            lock.release()

    def _cmd_subscribe(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        """
        订阅影视
        优先 search() 识别，后备 recognize_by_meta()
        """
        if not keyword:
            return
        lock = self._get_user_lock(user_id)
        if not lock.acquire(blocking=False):
            self._send_text(chat_id, "⏳ 上一个请求还在处理中，请稍候...", msg_id)
            return
        try:
            self._send_text(chat_id, f"📥 正在订阅: {keyword} ...", msg_id)
            from app.chain.media import MediaChain

            mc = MediaChain()
            mediainfo = None

            try:
                result = mc.search(title=keyword)
                if isinstance(result, tuple) and len(result) == 2:
                    meta, medias = result
                    if medias:
                        for m in medias:
                            if hasattr(m, "title") and hasattr(m, "type"):
                                mediainfo = m
                                break
            except Exception as e:
                logger.warning(f"订阅搜索阶段异常: {e}")

            if not mediainfo:
                try:
                    from app.core.metainfo import MetaInfo as MetaInfoFunc

                    metainfo = MetaInfoFunc(title=keyword)
                    mediainfo = mc.recognize_by_meta(metainfo)
                    if mediainfo and not hasattr(mediainfo, "type"):
                        logger.warning(
                            f"recognize_by_meta 返回了非 MediaInfo 对象: {type(mediainfo)}"
                        )
                        mediainfo = None
                except Exception as e:
                    logger.warning(f"订阅识别阶段异常: {e}")

            if not mediainfo:
                self._send_text(chat_id, f"❌ 未识别到: {keyword}")
                return

            self._subscribe_media(mediainfo, chat_id, msg_id)
        except Exception as e:
            logger.error(f"飞书订阅异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 订阅出错: {e}")
        finally:
            lock.release()

    def _cmd_search_resources(
        self, keyword: str, filters: dict,
        chat_id: str, msg_id: str, user_id: str,
    ):
        """
        搜索下载资源并按偏好筛选
        LLM 提取的 filters: resolution, audio, codec, source, subtitle
        """
        if not keyword:
            return
        lock = self._get_user_lock(user_id)
        if not lock.acquire(blocking=False):
            self._send_text(chat_id, "⏳ 上一个请求还在处理中，请稍候...", msg_id)
            return
        try:
            # 构造筛选条件描述
            filter_desc = self._format_filters(filters)
            hint = f"🔍 正在搜索 {keyword} 的资源"
            if filter_desc:
                hint += f"（{filter_desc}）"
            hint += " ..."
            self._send_text(chat_id, hint, msg_id)

            # 先搜索媒体获取标题
            from app.chain.media import MediaChain
            from app.chain.search import SearchChain

            mc = MediaChain()
            title = keyword
            try:
                result = mc.search(title=keyword)
                if isinstance(result, tuple) and len(result) == 2:
                    meta, medias = result
                    if medias and hasattr(medias[0], "title"):
                        title = medias[0].title or keyword
                        self._search_cache[user_id] = [
                            m for m in medias[:6]
                            if hasattr(m, "title") and hasattr(m, "type")
                        ]
            except Exception as e:
                logger.warning(f"资源搜索-媒体识别异常: {e}")

            # 搜索种子资源
            contexts = SearchChain().search_by_title(title=title)
            if not contexts:
                self._send_text(chat_id, f"😔 未找到 {title} 的下载资源")
                return

            # 应用筛选
            filtered = self._apply_filters(contexts, filters)
            if not filtered and filters:
                # 筛选后无结果，提示并展示全部
                self._send_text(
                    chat_id,
                    f"⚠️ 未找到完全匹配筛选条件的资源，为你展示所有结果：",
                )
                filtered = contexts

            # 缓存并展示
            show_list = filtered[:10]
            self._search_cache[f"{user_id}_res"] = show_list
            card = self._build_resource_card(title, show_list, filters)
            self._send_card(chat_id, card)
        except Exception as e:
            logger.error(f"飞书资源搜索异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 搜索资源出错: {e}")
        finally:
            lock.release()

    def _cmd_downloading(self, chat_id: str, msg_id: str):
        """正在下载"""
        from app.chain.download import DownloadChain

        dc = DownloadChain()
        torrents = dc.downloading_torrents()
        if not torrents:
            self._send_text(chat_id, "当前没有正在下载的任务")
            return
        lines = []
        for i, t in enumerate(torrents[:10], 1):
            name = getattr(t, "title", "") or getattr(t, "name", "未知")
            progress = getattr(t, "progress", 0)
            lines.append(f"{i}. {name}  进度: {progress}%")
        self._send_text(chat_id, "\n".join(lines), msg_id)

    def _cmd_help(self, chat_id: str, msg_id: str):
        ai_status = "✅ 已启用" if self._llm_client else "❌ 未启用"
        model_name = self._openrouter_model or _OpenRouterClient.DEFAULT_MODEL
        help_text = (
            "📖 飞书机器人指令帮助\n\n"
            "**基础指令：**\n"
            "/搜索 <片名>  —— 搜索影视\n"
            "/订阅 <片名>  —— 直接订阅\n"
            "/正在下载      —— 查看下载进度\n"
            "/帮助          —— 显示本帮助\n\n"
            "**AI 智能模式：**\n"
            f"状态: {ai_status}\n"
            f"模型: {model_name}\n\n"
            "开启 AI 后可直接用自然语言交互：\n"
            '• "找一部4K的流浪地球"\n'
            '• "下载5.1声道蓝光版的星际穿越"\n'
            '• "订阅进击的巨人最终季"\n'
            '• "有杜比视界的奥本海默吗"\n\n'
            "直接发送片名也会触发搜索"
        )
        self._send_text(chat_id, help_text, msg_id)

    # endregion

    # region ========= 资源筛选逻辑 =========

    # 筛选关键词映射 — 用于匹配 torrent title
    _RESOLUTION_KEYWORDS = {
        "4k": ["4k", "2160p", "uhd"],
        "2160p": ["4k", "2160p", "uhd"],
        "1080p": ["1080p", "1080i"],
        "720p": ["720p"],
    }
    _AUDIO_KEYWORDS = {
        "5.1": ["5.1", "dd5.1", "ddp5.1", "dd+5.1", "ac3.5.1"],
        "7.1": ["7.1", "ddp7.1", "dd+7.1"],
        "atmos": ["atmos"],
        "dts": ["dts"],
        "dts-hd": ["dts-hd", "dts-hdma", "dts-hd.ma"],
        "truehd": ["truehd", "true-hd"],
    }
    _CODEC_KEYWORDS = {
        "x265": ["x265", "hevc", "h265", "h.265"],
        "hevc": ["x265", "hevc", "h265", "h.265"],
        "x264": ["x264", "h264", "h.264", "avc"],
        "av1": ["av1"],
        "hdr": ["hdr", "hdr10", "hdr10+"],
        "dv": ["dv", "dolby.vision", "dolbyvision", "dovi"],
    }
    _SOURCE_KEYWORDS = {
        "bluray": ["bluray", "blu-ray", "bdremux", "bdrip"],
        "remux": ["remux"],
        "web-dl": ["web-dl", "webdl"],
        "webrip": ["webrip", "web-rip"],
        "hdtv": ["hdtv"],
    }

    def _apply_filters(self, contexts: list, filters: dict) -> list:
        """
        对搜索到的 Context 列表按照筛选条件进行过滤
        匹配策略：宽松匹配（OR），只要 torrent title 中包含任一关键词即算匹配
        """
        if not filters:
            return contexts

        def _match(text: str, category: dict, value: str) -> bool:
            """检查 text 中是否包含 value 对应的关键词"""
            if not value:
                return True  # 无此筛选条件
            value_lower = value.lower().replace(" ", "").replace("-", "")
            # 从映射中查找关键词列表
            keywords = category.get(value_lower)
            if not keywords:
                # 未在映射中找到，尝试直接匹配原始值
                keywords = [value_lower]
            text_lower = text.lower().replace(" ", "")
            return any(kw in text_lower for kw in keywords)

        result = []
        for ctx in contexts:
            t = getattr(ctx, "torrent_info", None)
            if not t:
                continue
            torrent_title = (
                getattr(t, "title", "") or getattr(t, "description", "") or ""
            )
            if not torrent_title:
                continue

            match = True
            if filters.get("resolution"):
                match = match and _match(
                    torrent_title, self._RESOLUTION_KEYWORDS, filters["resolution"]
                )
            if filters.get("audio"):
                match = match and _match(
                    torrent_title, self._AUDIO_KEYWORDS, filters["audio"]
                )
            if filters.get("codec"):
                match = match and _match(
                    torrent_title, self._CODEC_KEYWORDS, filters["codec"]
                )
            if filters.get("source"):
                match = match and _match(
                    torrent_title, self._SOURCE_KEYWORDS, filters["source"]
                )
            if filters.get("subtitle"):
                sub_kw = filters["subtitle"].lower()
                match = match and (sub_kw in torrent_title.lower())

            if match:
                result.append(ctx)
        return result

    @staticmethod
    def _format_filters(filters: dict) -> str:
        """将筛选条件格式化为可读字符串"""
        if not filters:
            return ""
        parts = []
        label_map = {
            "resolution": "分辨率",
            "audio": "声道",
            "codec": "编码",
            "source": "来源",
            "subtitle": "字幕",
        }
        for key, label in label_map.items():
            val = filters.get(key)
            if val:
                parts.append(f"{label}: {val}")
        return " / ".join(parts)

    # endregion

    # region ========= 卡片构造 =========

    def _build_search_card(self, medias: list, keyword: str) -> dict:
        """构造搜索结果卡片"""
        elements = [
            {
                "tag": "markdown",
                "content": f"**🔍 \u201c{keyword}\u201d 的搜索结果 (前{len(medias)}条)**",
            },
            {"tag": "hr"},
        ]
        for idx, media in enumerate(medias):
            title = getattr(media, "title", "") or getattr(
                media, "title_year", "未知"
            )
            year = getattr(media, "year", "")
            raw_type = getattr(media, "type", None)
            if hasattr(raw_type, "value"):
                mtype = "电影" if raw_type == MediaType.MOVIE else "电视剧"
            else:
                mtype = (
                    "电影"
                    if str(raw_type).lower() in ("movie", "电影")
                    else "电视剧"
                )
            vote = getattr(media, "vote_average", "")
            overview = (getattr(media, "overview", "") or "")[:80]
            poster = getattr(media, "poster_url", "") or ""

            md_lines = [f"**{idx + 1}. {title}**"]
            if year:
                md_lines.append(f"年份: {year}")
            md_lines.append(f"类型: {mtype}")
            if vote:
                md_lines.append(f"评分: {vote}")
            if overview:
                md_lines.append(f"简介: {overview}...")

            col_text = {
                "tag": "column",
                "width": "weighted",
                "weight": 3,
                "vertical_align": "top",
                "elements": [
                    {"tag": "markdown", "content": "\n".join(md_lines)}
                ],
            }
            col_img = {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [],
            }
            if poster:
                col_img["elements"].append(
                    {
                        "tag": "img",
                        "img_key": poster,
                        "alt": {"tag": "plain_text", "content": title},
                        "preview": True,
                    }
                )

            elements.append(
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "background_style": "default",
                    "columns": [col_text, col_img],
                }
            )
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": f"📥 订阅 {title}",
                            },
                            "type": "primary",
                            "value": {
                                "action": "subscribe",
                                "index": str(idx),
                            },
                        },
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": f"📦 搜索资源",
                            },
                            "type": "default",
                            "value": {
                                "action": "search_resource",
                                "index": str(idx),
                            },
                        },
                    ],
                }
            )
            elements.append({"tag": "hr"})

        return {
            "config": {"wide_screen_mode": True},
            "elements": elements,
            "header": {
                "title": {"tag": "plain_text", "content": "搜索结果"},
                "template": "blue",
            },
        }

    def _build_resource_card(
        self, title: str, contexts: list, filters: dict = None
    ) -> dict:
        """构造资源列表卡片"""
        filter_desc = self._format_filters(filters) if filters else ""
        header_text = f"📦 {title} 的资源"
        if filter_desc:
            header_text += f"（{filter_desc}）"

        elements = []
        for i, ctx in enumerate(contexts):
            t = getattr(ctx, "torrent_info", None)
            if not t:
                continue
            tname = getattr(t, "title", "") or "未知"
            line = f"**{i + 1}. {tname}**\n"
            parts = []
            if getattr(t, "site_name", ""):
                parts.append(f"站点: {t.site_name}")
            if getattr(t, "size", ""):
                parts.append(f"大小: {t.size}")
            if getattr(t, "seeders", ""):
                parts.append(f"做种: {t.seeders}")
            if parts:
                line += " | ".join(parts)

            # 高亮匹配到的筛选关键词
            if filters:
                tags = self._extract_resource_tags(tname)
                if tags:
                    line += f"\n🏷️ {' '.join(tags)}"

            elements.append(
                {"tag": "div", "text": {"tag": "lark_md", "content": line}}
            )
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": f"⬇️ 下载",
                            },
                            "type": "primary",
                            "value": {
                                "action": "download_resource",
                                "index": str(i),
                            },
                        }
                    ],
                }
            )
            elements.append({"tag": "hr"})

        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "💡 点击「下载」按钮开始下载对应资源",
                    }
                ],
            }
        )

        return {
            "config": {"wide_screen_mode": True},
            "elements": elements,
            "header": {
                "title": {"tag": "plain_text", "content": header_text},
                "template": "green",
            },
        }

    @staticmethod
    def _extract_resource_tags(title: str) -> list:
        """从资源标题中提取可读标签"""
        tags = []
        title_lower = title.lower()
        # 分辨率
        for kw in ["4K", "2160p", "1080p", "720p"]:
            if kw.lower() in title_lower:
                tags.append(kw)
                break
        # 编码
        for kw in ["HEVC", "x265", "x264", "AV1", "H.264", "H.265"]:
            if kw.lower() in title_lower:
                tags.append(kw)
                break
        # HDR
        for kw in ["DV", "Dolby Vision", "HDR10+", "HDR10", "HDR"]:
            if kw.lower() in title_lower:
                tags.append(kw)
                break
        # 音频
        for kw in ["Atmos", "TrueHD", "DTS-HD", "DTS", "7.1", "5.1", "AAC"]:
            if kw.lower() in title_lower:
                tags.append(kw)
                break
        # 来源
        for kw in ["Remux", "BluRay", "WEB-DL", "WEBRip", "HDTV"]:
            if kw.lower() in title_lower:
                tags.append(kw)
                break
        return tags

    # endregion

    # region ========= 卡片回调处理 =========

    def _handle_card_action(self, data: dict) -> dict:
        """处理卡片按钮回调（card.action.trigger）"""
        try:
            action = data.get("event", {}).get("action", {})
            value = action.get("value", {})
            act = value.get("action", "")
            operator = data.get("event", {}).get("operator", {})
            user_id = operator.get("open_id", "")
            ctx = data.get("event", {}).get("context", {})
            chat_id = ctx.get("open_chat_id", "") or self._chat_id

            if act == "subscribe":
                idx = int(value.get("index", 0))
                cached = self._search_cache.get(user_id, [])
                if idx < len(cached):
                    media = cached[idx]
                    threading.Thread(
                        target=self._subscribe_media,
                        args=(media, chat_id, ""),
                        daemon=True,
                    ).start()

            elif act == "search_resource":
                idx = int(value.get("index", 0))
                cached = self._search_cache.get(user_id, [])
                if idx < len(cached):
                    media = cached[idx]
                    title = getattr(media, "title", "") or "未知"
                    threading.Thread(
                        target=self._cmd_search_resources,
                        args=(title, {}, chat_id, "", user_id),
                        daemon=True,
                    ).start()

            elif act == "download_resource":
                idx = int(value.get("index", 0))
                cached_res = self._search_cache.get(f"{user_id}_res", [])
                if idx < len(cached_res):
                    context = cached_res[idx]
                    threading.Thread(
                        target=self._do_download,
                        args=(context, chat_id),
                        daemon=True,
                    ).start()

        except Exception as e:
            logger.error(f"飞书卡片回调异常: {e}", exc_info=True)
        return {"code": 0}

    def _do_download(self, context, chat_id: str):
        """执行下载动作"""
        try:
            from app.chain.download import DownloadChain

            dc = DownloadChain()
            t = getattr(context, "torrent_info", None)
            title = getattr(t, "title", "未知") if t else "未知"
            self._send_text(chat_id, f"⬇️ 正在提交下载: {title}")

            result = dc.download_single(context=context, userid="feishu")
            if result:
                self._send_text(chat_id, f"✅ 已添加下载: {title}")
            else:
                self._send_text(chat_id, f"⚠️ 下载提交失败: {title}")
        except Exception as e:
            logger.error(f"飞书下载异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 下载出错: {e}")

    # endregion

    # region ========= 订阅 / 通知逻辑 =========

    def _subscribe_media(self, media, chat_id: str, msg_id: str = None):
        """真正的订阅动作"""
        try:
            from app.chain.subscribe import SubscribeChain

            sc = SubscribeChain()
            title = getattr(media, "title", "") or "未知"
            raw_type = getattr(media, "type", None)
            if raw_type and hasattr(raw_type, "value"):
                mtype = raw_type
            else:
                mtype = MediaType.MOVIE

            sid, msg = sc.add(
                mtype=mtype,
                title=title,
                year=getattr(media, "year", ""),
                tmdbid=getattr(media, "tmdb_id", None),
                doubanid=getattr(media, "douban_id", None),
                exist_ok=True,
                username="飞书用户",
            )
            if sid:
                self._send_text(chat_id, f"✅ 已订阅: {title}", msg_id)
            else:
                self._send_text(chat_id, f"ℹ️ {msg or '订阅失败'}", msg_id)
        except Exception as e:
            logger.error(f"飞书订阅动作异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 订阅失败: {e}", msg_id)

    @eventmanager.register(EventType.TransferComplete)
    def _on_transfer(self, event: Event):
        """入库完成"""
        if not self._enabled or "transfer" not in self._msgtypes:
            return
        edata = event.event_data or {}
        mi: Optional[MediaInfo] = edata.get("mediainfo")
        if not mi:
            return
        title = getattr(mi, "title", "")
        year = getattr(mi, "year", "")
        text = f"🎬 入库完成: {title} ({year})" if year else f"🎬 入库完成: {title}"
        self._send_text(self._chat_id, text)
        poster = getattr(mi, "poster_url", "")
        if poster:
            self._send_image(self._chat_id, poster)

    @eventmanager.register(EventType.DownloadAdded)
    def _on_download(self, event: Event):
        """资源下载"""
        if not self._enabled or "download" not in self._msgtypes:
            return
        edata = event.event_data or {}
        mi = edata.get("mediainfo")
        context = edata.get("context")
        title = (
            getattr(mi, "title", "")
            if mi
            else (getattr(context, "title", "") if context else "未知")
        )
        text = f"⬇️ 开始下载: {title}"
        self._send_text(self._chat_id, text)

    @eventmanager.register(EventType.SubscribeAdded)
    def _on_subscribe(self, event: Event):
        """订阅添加"""
        if not self._enabled or "subscribe" not in self._msgtypes:
            return
        edata = event.event_data or {}
        title = edata.get("title") or edata.get("name") or "未知"
        text = f"📌 新增订阅: {title}"
        self._send_text(self._chat_id, text)

    # endregion

    def stop_service(self):
        """退出插件"""
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._scheduler.shutdown()
            self._scheduler = None
