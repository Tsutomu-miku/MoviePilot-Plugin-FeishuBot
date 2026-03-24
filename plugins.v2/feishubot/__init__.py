"""
飞书机器人双向通信插件
支持: 消息接收、影视搜索、订阅下载、交互式卡片
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Type

from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType, MediaType

import requests


class FeishuBot(_PluginBase):
    """飞书机器人双向通信插件"""

    # ==================== 插件元数据 ====================
    plugin_name = "飞书机器人"
    plugin_desc = "飞书机器人双向通信插件，支持影视搜索、订阅下载与消息交互"
    plugin_icon = "feishu.png"
    plugin_version = "1.0.0"
    plugin_author = "MoviePilot-Community"
    plugin_config_prefix = "feishubot_"
    plugin_order = 20
    auth_level = 1

    # ==================== 私有属性 ====================
    _enabled: bool = False
    _app_id: str = ""
    _app_secret: str = ""
    _verification_token: str = ""
    _encrypt_key: str = ""
    _default_chat_id: str = ""
    _use_lark: bool = False  # 使用国际版 Lark
    _msgtypes: list = []

    _tenant_access_token: str = ""
    _token_expires_at: float = 0
    _token_lock = threading.Lock()

    # API 基础地址
    _base_url: str = ""

    # 搜索结果缓存 {user_id: [MediaInfo, ...]}
    _search_cache: Dict[str, List[Any]] = {}

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        if config:
            self._enabled = config.get("enabled", False)
            self._app_id = config.get("app_id", "")
            self._app_secret = config.get("app_secret", "")
            self._verification_token = config.get("verification_token", "")
            self._encrypt_key = config.get("encrypt_key", "")
            self._default_chat_id = config.get("default_chat_id", "")
            self._use_lark = config.get("use_lark", False)
            self._msgtypes = config.get("msgtypes", [])

        # 设置 API 基础地址
        if self._use_lark:
            self._base_url = "https://open.larksuite.com"
        else:
            self._base_url = "https://open.feishu.cn"

        # 清空缓存
        self._search_cache = {}
        self._tenant_access_token = ""
        self._token_expires_at = 0

    def get_state(self) -> bool:
        return self._enabled and bool(self._app_id) and bool(self._app_secret)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令"""
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """对外暴露 API 端点"""
        return [
            {
                "path": "/feishu/webhook",
                "endpoint": self.webhook_handler,
                "methods": ["POST"],
                "summary": "飞书事件回调",
                "description": "接收飞书事件推送和卡片回调",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        pass

    def stop_service(self):
        """停止服务"""
        self._search_cache.clear()

    # ============================================================
    #  配置表单
    # ============================================================
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })
        return [
            {
                "component": "VForm",
                "content": [
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "use_lark",
                                            "label": "国际版 (Lark)",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "app_id",
                                            "label": "App ID",
                                            "placeholder": "飞书应用的 App ID",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "app_secret",
                                            "label": "App Secret",
                                            "type": "password",
                                            "placeholder": "飞书应用的 App Secret",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "verification_token",
                                            "label": "Verification Token",
                                            "placeholder": "事件订阅的验证 Token",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "encrypt_key",
                                            "label": "Encrypt Key（可选）",
                                            "type": "password",
                                            "placeholder": "事件加密密钥，不填则不加密",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "default_chat_id",
                                            "label": "默认会话 ID",
                                            "placeholder": "用于主动推送通知的会话 chat_id",
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
                                            "label": "消息类型",
                                            "items": MsgTypeOptions,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "placeholder": "选择要推送的通知类型，留空则全部推送",
                                        },
                                    }
                                ],
                            }
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "Webhook 回调地址：{HOST}/api/plugin/feishu/webhook\n"
                                                    "请在飞书开放平台 -> 事件订阅中配置此地址，"
                                                    "并订阅 im.message.receive_v1 事件。\n"
                                                    "同时在「消息卡片」中配置相同的回调地址。",
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
            "verification_token": "",
            "encrypt_key": "",
            "default_chat_id": "",
            "use_lark": False,
            "msgtypes": [],
        }

    def get_page(self) -> List[dict]:
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
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "飞书机器人已启用。\n"
                                            "支持的命令: /search 搜索影视 | /subscribe 订阅 | /downloading 下载中 | /help 帮助",
                                },
                            }
                        ],
                    }
                ],
            }
        ]

    # ============================================================
    #  Token 管理
    # ============================================================
    def _get_tenant_token(self) -> Optional[str]:
        """获取 tenant_access_token，自动缓存和刷新"""
        with self._token_lock:
            # 还有 5 分钟以上有效期就直接返回
            if self._tenant_access_token and time.time() < self._token_expires_at - 300:
                return self._tenant_access_token
            try:
                url = f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal"
                resp = requests.post(url, json={
                    "app_id": self._app_id,
                    "app_secret": self._app_secret,
                }, timeout=10)
                data = resp.json()
                if data.get("code") == 0:
                    self._tenant_access_token = data["tenant_access_token"]
                    self._token_expires_at = time.time() + data.get("expire", 7200)
                    return self._tenant_access_token
                else:
                    logger.error(f"飞书获取 token 失败: {data}")
                    return None
            except Exception as e:
                logger.error(f"飞书获取 token 异常: {e}")
                return None

    def _headers(self) -> dict:
        token = self._get_tenant_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ============================================================
    #  消息发送
    # ============================================================
    def _send_text(self, chat_id: str, text: str, msg_id: str = None):
        """发送文本消息，如果提供 msg_id 则回复"""
        if msg_id:
            url = f"{self._base_url}/open-apis/im/v1/messages/{msg_id}/reply"
            payload = {
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            }
        else:
            url = f"{self._base_url}/open-apis/im/v1/messages"
            payload = {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            }
        try:
            resp = requests.post(
                url,
                params={"receive_id_type": "chat_id"} if not msg_id else None,
                headers=self._headers(),
                json=payload,
                timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.error(f"飞书发送消息失败: {result}")
        except Exception as e:
            logger.error(f"飞书发送消息异常: {e}")

    def _send_card(self, chat_id: str, card: dict, msg_id: str = None):
        """发送卡片消息"""
        if msg_id:
            url = f"{self._base_url}/open-apis/im/v1/messages/{msg_id}/reply"
            payload = {
                "msg_type": "interactive",
                "content": json.dumps(card),
            }
        else:
            url = f"{self._base_url}/open-apis/im/v1/messages"
            payload = {
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card),
            }
        try:
            resp = requests.post(
                url,
                params={"receive_id_type": "chat_id"} if not msg_id else None,
                headers=self._headers(),
                json=payload,
                timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.error(f"飞书发送卡片失败: {result}")
        except Exception as e:
            logger.error(f"飞书发送卡片异常: {e}")

    def _update_card(self, message_id: str, card: dict):
        """更新已有卡片消息"""
        url = f"{self._base_url}/open-apis/im/v1/messages/{message_id}"
        payload = {
            "msg_type": "interactive",
            "content": json.dumps(card),
        }
        try:
            resp = requests.patch(
                url,
                headers=self._headers(),
                json=payload,
                timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.error(f"飞书更新卡片失败: {result}")
        except Exception as e:
            logger.error(f"飞书更新卡片异常: {e}")

    # ============================================================
    #  Webhook 事件处理
    # ============================================================
    def webhook_handler(self, **kwargs) -> Any:
        """
        处理飞书 Webhook 回调
        包括: URL 验证 / 消息接收 / 卡片回调
        """
        from fastapi import Request, Response
        req: Request = kwargs.get("request")
        if not req:
            return {"code": -1, "msg": "invalid request"}

        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    body = loop.run_in_executor(pool, lambda: asyncio.ensure_future(req.json()))
                    # 使用同步方式处理
                    body = json.loads(req._body.decode("utf-8")) if hasattr(req, '_body') else {}
            else:
                body = asyncio.run(req.json())
        except Exception:
            body = {}

        if not body:
            return {"code": -1}

        # --- 情况1: URL 验证 (challenge) ---
        if body.get("type") == "url_verification":
            logger.info("飞书 Webhook URL 验证请求")
            return {"challenge": body.get("challenge", "")}

        # --- 情况2: 卡片交互回调 ---
        header = body.get("header", {})
        event_type = header.get("event_type", "")

        if event_type == "card.action.trigger":
            return self._handle_card_action(body)

        # --- 情况3: 消息接收 ---
        if event_type == "im.message.receive_v1":
            # 异步处理，立即返回 200
            threading.Thread(
                target=self._handle_message_event,
                args=(body,),
                daemon=True,
            ).start()
            return {}

        # 其他事件不处理
        return {}

    def _handle_message_event(self, body: dict):
        """处理接收到的消息事件"""
        try:
            event = body.get("event", {})
            message = event.get("message", {})
            sender = event.get("sender", {})

            chat_id = message.get("chat_id", "")
            msg_id = message.get("message_id", "")
            msg_type = message.get("message_type", "")
            user_id = sender.get("sender_id", {}).get("open_id", "")

            # 只处理文本消息
            if msg_type != "text":
                self._send_text(chat_id, "暂时只支持文本消息哦 😊", msg_id)
                return

            # 解析文本
            content = json.loads(message.get("content", "{}"))
            text = content.get("text", "").strip()

            # 群聊中去掉 @机器人 的部分
            mentions = message.get("mentions", [])
            for m in mentions:
                key = m.get("key", "")
                if key:
                    text = text.replace(key, "").strip()

            if not text:
                return

            logger.info(f"飞书收到消息: user={user_id}, chat={chat_id}, text={text}")

            # 命令路由
            if text.startswith("/"):
                self._dispatch_command(text, chat_id, msg_id, user_id)
            else:
                # 默认当作搜索关键词
                self._cmd_search(text, chat_id, msg_id, user_id)

        except Exception as e:
            logger.error(f"飞书处理消息异常: {e}", exc_info=True)

    def _dispatch_command(self, text: str, chat_id: str, msg_id: str, user_id: str):
        """命令分发"""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/search", "/s", "/搜索"):
            if not args:
                self._send_text(chat_id, "请输入搜索关键词，例如：/search 三体", msg_id)
                return
            self._cmd_search(args, chat_id, msg_id, user_id)

        elif cmd in ("/subscribe", "/sub", "/订阅"):
            if not args:
                self._send_text(chat_id, "请输入要订阅的内容，例如：/subscribe 三体", msg_id)
                return
            self._cmd_subscribe(args, chat_id, msg_id, user_id)

        elif cmd in ("/downloading", "/dl", "/下载中"):
            self._cmd_downloading(chat_id, msg_id)

        elif cmd in ("/help", "/h", "/帮助"):
            self._cmd_help(chat_id, msg_id)

        else:
            self._send_text(
                chat_id,
                f"未知命令: {cmd}\n输入 /help 查看支持的命令",
                msg_id,
            )

    # ============================================================
    #  命令实现
    # ============================================================
    def _cmd_help(self, chat_id: str, msg_id: str):
        """帮助命令"""
        card = {
            "header": {
                "title": {"tag": "plain_text", "content": "🎬 MoviePilot 飞书机器人"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "**支持的命令：**\n\n"
                            "🔍 `/search <关键词>` — 搜索影视\n"
                            "📥 `/subscribe <关键词>` — 订阅影视\n"
                            "⬇️ `/downloading` — 查看下载中的任务\n"
                            "❓ `/help` — 显示此帮助\n\n"
                            "💡 也可以直接发送影视名称进行搜索"
                        ),
                    },
                },
            ],
        }
        self._send_card(chat_id, card, msg_id)

    def _cmd_search(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        """搜索影视"""
        self._send_text(chat_id, f"🔍 正在搜索: {keyword} ...", msg_id)

        try:
            # 调用 MoviePilot 的搜索接口
            from app.chain.media import MediaChain
            from app.chain.search import SearchChain

            media_chain = MediaChain()
            # 识别媒体信息
            meta = media_chain.recognize_by_meta(keyword)
            if not meta or not meta.tmdb_info:
                self._send_text(chat_id, f"❌ 未识别到: {keyword}，请尝试更精确的名称")
                return

            medias = [meta]

            # 缓存搜索结果
            self._search_cache[user_id] = medias

            # 构建结果卡片
            cards = self._build_search_result_card(medias, keyword)
            self._send_card(chat_id, cards)

        except Exception as e:
            logger.error(f"飞书搜索异常: {e}", exc_info=True)
            # Fallback: 尝试通过事件系统触发搜索
            self._search_via_event(keyword, chat_id, msg_id, user_id)

    def _search_via_event(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        """通过 MoviePilot 内部 API 搜索"""
        try:
            from app.chain.media import MediaChain
            media_chain = MediaChain()
            medias = media_chain.search(title=keyword)
            if not medias:
                self._send_text(chat_id, f"😔 未找到与 \"{keyword}\" 相关的结果")
                return

            # 只取前 8 个
            medias = medias[:8]
            self._search_cache[user_id] = medias

            card = self._build_search_result_card(medias, keyword)
            self._send_card(chat_id, card)
        except Exception as e:
            logger.error(f"飞书搜索异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 搜索出错: {str(e)}")

    def _build_search_result_card(self, medias: list, keyword: str) -> dict:
        """构建搜索结果卡片"""
        elements = []

        for i, media in enumerate(medias[:8]):
            title = getattr(media, "title", "") or getattr(media, "title_year", "未知")
            year = getattr(media, "year", "")
            rating = getattr(media, "vote_average", "")
            mtype = "电影" if getattr(media, "type", None) == MediaType.MOVIE else "电视剧"
            overview = getattr(media, "overview", "") or ""
            if len(overview) > 80:
                overview = overview[:80] + "..."
            tmdb_id = getattr(media, "tmdb_id", "")

            line = f"**{i + 1}. {title}**"
            if year:
                line += f" ({year})"
            line += f"  [{mtype}]"
            if rating:
                line += f"  ⭐ {rating}"
            if overview:
                line += f"\n{overview}"

            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": line},
            })

            # 操作按钮
            actions = []
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📥 订阅"},
                "type": "primary",
                "value": {
                    "action": "subscribe",
                    "index": str(i),
                    "title": title,
                    "year": str(year),
                    "tmdb_id": str(tmdb_id),
                    "mtype": mtype,
                },
            })
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔍 搜索资源"},
                "type": "default",
                "value": {
                    "action": "search_resource",
                    "index": str(i),
                    "title": title,
                    "year": str(year),
                    "tmdb_id": str(tmdb_id),
                    "mtype": mtype,
                },
            })

            elements.append({
                "tag": "action",
                "actions": actions,
            })
            elements.append({"tag": "hr"})

        card = {
            "header": {
                "title": {"tag": "plain_text", "content": f"🎬 搜索结果: {keyword}"},
                "template": "blue",
            },
            "elements": elements,
        }
        return card

    def _cmd_subscribe(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        """订阅影视"""
        self._send_text(chat_id, f"📥 正在订阅: {keyword} ...", msg_id)
        try:
            from app.chain.media import MediaChain
            from app.chain.subscribe import SubscribeChain

            media_chain = MediaChain()
            subscribe_chain = SubscribeChain()

            # 识别媒体
            meta = media_chain.recognize_by_meta(keyword)
            if not meta or not meta.tmdb_info:
                self._send_text(chat_id, f"❌ 未识别到: {keyword}")
                return

            title = meta.title or keyword
            tmdb_id = meta.tmdb_id
            mtype = meta.type

            # 添加订阅
            sid, msg = subscribe_chain.add(
                title=title,
                year=meta.year,
                mtype=mtype,
                tmdbid=tmdb_id,
                userid="feishu",
            )

            if sid:
                card = {
                    "header": {
                        "title": {"tag": "plain_text", "content": "✅ 订阅成功"},
                        "template": "green",
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "lark_md",
                                "content": f"**{title}** ({meta.year or ''})\n\n"
                                           f"类型: {'电影' if mtype == MediaType.MOVIE else '电视剧'}\n"
                                           f"TMDB ID: {tmdb_id}\n\n"
                                           "系统将自动搜索并下载资源。",
                            },
                        }
                    ],
                }
                self._send_card(chat_id, card)
            else:
                self._send_text(chat_id, f"⚠️ 订阅失败: {msg or '未知原因'}")

        except Exception as e:
            logger.error(f"飞书订阅异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 订阅出错: {str(e)}")

    def _cmd_downloading(self, chat_id: str, msg_id: str):
        """查看下载中的任务"""
        try:
            from app.chain.download import DownloadChain
            download_chain = DownloadChain()
            downloads = download_chain.downloading()

            if not downloads:
                self._send_text(chat_id, "📭 当前没有正在下载的任务", msg_id)
                return

            elements = []
            for dl in downloads[:10]:
                title = getattr(dl, "title", "未知")
                progress = getattr(dl, "progress", 0)
                speed = getattr(dl, "dlspeed", "")
                size = getattr(dl, "size", "")

                line = f"**{title}**\n"
                if progress is not None:
                    line += f"进度: {progress:.1f}%  "
                if speed:
                    line += f"速度: {speed}  "
                if size:
                    line += f"大小: {size}"

                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": line},
                })
                elements.append({"tag": "hr"})

            card = {
                "header": {
                    "title": {"tag": "plain_text", "content": "⬇️ 下载中的任务"},
                    "template": "wathet",
                },
                "elements": elements,
            }
            self._send_card(chat_id, card, msg_id)

        except Exception as e:
            logger.error(f"飞书查看下载异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 查询下载任务失败: {str(e)}", msg_id)

    # ============================================================
    #  卡片交互回调处理
    # ============================================================
    def _handle_card_action(self, body: dict) -> dict:
        """处理卡片按钮点击"""
        try:
            event = body.get("event", {})
            action = event.get("action", {})
            value = action.get("value", {})
            operator = event.get("operator", {})
            user_id = operator.get("open_id", "")

            action_type = value.get("action", "")

            if action_type == "subscribe":
                return self._action_subscribe(value, user_id)
            elif action_type == "search_resource":
                return self._action_search_resource(value, user_id)
            elif action_type == "download":
                return self._action_download(value, user_id)

        except Exception as e:
            logger.error(f"飞书卡片回调异常: {e}", exc_info=True)

        return {"toast": {"type": "info", "content": "操作已收到"}}

    def _action_subscribe(self, value: dict, user_id: str) -> dict:
        """卡片按钮: 订阅"""
        title = value.get("title", "")
        year = value.get("year", "")
        tmdb_id = value.get("tmdb_id", "")
        mtype_str = value.get("mtype", "电影")

        try:
            from app.chain.subscribe import SubscribeChain
            subscribe_chain = SubscribeChain()

            mtype = MediaType.MOVIE if mtype_str == "电影" else MediaType.TV

            sid, msg = subscribe_chain.add(
                title=title,
                year=year,
                mtype=mtype,
                tmdbid=int(tmdb_id) if tmdb_id else None,
                userid="feishu",
            )

            if sid:
                return {
                    "toast": {"type": "success", "content": f"✅ {title} 订阅成功！"}
                }
            else:
                return {
                    "toast": {"type": "warning", "content": f"订阅失败: {msg or '未知原因'}"}
                }

        except Exception as e:
            logger.error(f"飞书订阅操作异常: {e}", exc_info=True)
            return {"toast": {"type": "error", "content": f"订阅出错: {str(e)}"}}

    def _action_search_resource(self, value: dict, user_id: str) -> dict:
        """卡片按钮: 搜索资源"""
        title = value.get("title", "")
        tmdb_id = value.get("tmdb_id", "")
        mtype_str = value.get("mtype", "电影")

        # 异步搜索资源并发送结果
        threading.Thread(
            target=self._do_search_resource,
            args=(title, tmdb_id, mtype_str, user_id),
            daemon=True,
        ).start()

        return {"toast": {"type": "info", "content": f"🔍 正在搜索 {title} 的资源..."}}

    def _do_search_resource(self, title: str, tmdb_id: str, mtype_str: str, user_id: str):
        """异步搜索资源"""
        chat_id = self._default_chat_id
        try:
            from app.chain.search import SearchChain
            from app.chain.media import MediaChain

            search_chain = SearchChain()
            media_chain = MediaChain()

            mtype = MediaType.MOVIE if mtype_str == "电影" else MediaType.TV

            # 识别媒体
            meta = media_chain.recognize_by_meta(title)
            if not meta:
                self._send_text(chat_id, f"❌ 未识别到: {title}")
                return

            # 搜索资源
            contexts = search_chain.search_by_title(
                title=title,
                mtype=mtype,
            )

            if not contexts:
                self._send_text(chat_id, f"😔 未找到 {title} 的下载资源")
                return

            # 构建资源列表卡片
            elements = []
            for i, ctx in enumerate(contexts[:10]):
                torrent = ctx.torrent_info
                t_title = getattr(torrent, "title", "未知")
                size = getattr(torrent, "size", "")
                seeders = getattr(torrent, "seeders", "")
                site = getattr(torrent, "site_name", "")

                line = f"**{i + 1}. {t_title}**\n"
                if site:
                    line += f"站点: {site}  "
                if size:
                    line += f"大小: {size}  "
                if seeders:
                    line += f"做种: {seeders}"

                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": line},
                })

                elements.append({
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "⬇️ 下载"},
                            "type": "primary",
                            "value": {
                                "action": "download",
                                "index": str(i),
                                "title": title,
                                "enclosure": getattr(torrent, "enclosure", ""),
                            },
                        }
                    ],
                })
                elements.append({"tag": "hr"})

            card = {
                "header": {
                    "title": {"tag": "plain_text", "content": f"📦 {title} 的资源"},
                    "template": "green",
                },
                "elements": elements,
            }
            self._send_card(chat_id, card)

        except Exception as e:
            logger.error(f"飞书搜索资源异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 搜索资源出错: {str(e)}")

    def _action_download(self, value: dict, user_id: str) -> dict:
        """卡片按钮: 下载"""
        title = value.get("title", "")

        try:
            from app.chain.download import DownloadChain
            download_chain = DownloadChain()

            # 触发下载
            enclosure = value.get("enclosure", "")
            if enclosure:
                result = download_chain.download_single(
                    context=None,
                    torrent_url=enclosure,
                    userid="feishu",
                )
                if result:
                    return {"toast": {"type": "success", "content": f"✅ {title} 开始下载"}}

            return {"toast": {"type": "warning", "content": "下载失败，请重试"}}

        except Exception as e:
            logger.error(f"飞书下载操作异常: {e}", exc_info=True)
            return {"toast": {"type": "error", "content": f"下载出错: {str(e)}"}}

    # ============================================================
    #  系统通知推送（监听 MoviePilot 通知事件）
    # ============================================================
    @eventmanager.register(EventType.NoticeMessage)
    def handle_notice(self, event: Event):
        """处理系统通知消息"""
        if not self.get_state():
            return
        if not self._default_chat_id:
            return

        event_data = event.event_data or {}
        msg_type: NotificationType = event_data.get("type")
        title = event_data.get("title", "")
        text = event_data.get("text", "")
        image = event_data.get("image", "")

        # 过滤消息类型
        if self._msgtypes and msg_type and msg_type.name not in self._msgtypes:
            return

        # 构建通知卡片
        elements = []
        if text:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": text},
            })

        if image:
            elements.append({
                "tag": "img",
                "img_key": "",
                "alt": {"tag": "plain_text", "content": title},
            })

        # 确定卡片颜色
        color = "blue"
        if msg_type:
            if "完成" in (msg_type.value or ""):
                color = "green"
            elif "失败" in (msg_type.value or "") or "错误" in (msg_type.value or ""):
                color = "red"

        card = {
            "header": {
                "title": {"tag": "plain_text", "content": title or "MoviePilot 通知"},
                "template": color,
            },
            "elements": elements or [
                {"tag": "div", "text": {"tag": "plain_text", "content": "（无详细内容）"}}
            ],
        }

        try:
            self._send_card(self._default_chat_id, card)
        except Exception as e:
            logger.error(f"飞书通知推送异常: {e}", exc_info=True)
