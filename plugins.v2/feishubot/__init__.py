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


class FeishuBot(_PluginBase):
    # 插件名称
    plugin_name = "飞书机器人"
    # 插件描述
    plugin_desc = "飞书群机器人消息通知与交互"
    # 插件图标
    plugin_icon = "Feishu_A.png"
    # 插件版本
    plugin_version = "2.3.1"
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

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._app_id = config.get("app_id", "")
            self._app_secret = config.get("app_secret", "")
            self._chat_id = config.get("chat_id", "")
            self._msgtypes = config.get("msgtypes") or []
        self._search_cache: dict = {}
        self._user_locks: dict = {}

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
                                            "text": "需要在飞书开放平台创建自建应用并配置事件回调地址: "
                                            "http(s)://你的域名/api/v1/plugin/feishu_event",
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
            "content": f'{{"text":"{text}"}}',
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
        import json as _json

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
                "content": f'{{"image_key":"{image_key}"}}',
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
        同时处理 url_verification 和 im.message.receive_v1 消息事件
        以及 card.action.trigger 卡片按钮回调
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

    # region ========= 消息处理 =========
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

        import json as _json

        try:
            text = _json.loads(msg.get("content", "{}")).get("text", "").strip()
        except Exception:
            text = ""
        if not text:
            return

        # 指令路由
        if text.startswith("/search") or text.startswith("/搜索"):
            keyword = re.sub(r"^/(search|搜索)\s*", "", text).strip()
            self._cmd_search(keyword, chat_id, msg_id, user_id)

        elif text.startswith("/subscribe") or text.startswith("/订阅"):
            keyword = re.sub(r"^/(subscribe|订阅)\s*", "", text).strip()
            self._cmd_subscribe(keyword, chat_id, msg_id, user_id)

        elif text.startswith("/downloading") or text.startswith("/正在下载"):
            self._cmd_downloading(chat_id, msg_id)

        elif text.startswith("/help") or text.startswith("/帮助"):
            self._cmd_help(chat_id, msg_id)

        else:
            # 默认当搜索处理
            self._cmd_search(text, chat_id, msg_id, user_id)

    # endregion

    # region ========= 指令实现 =========

    def _get_user_lock(self, user_id: str):
        """获取用户级别的锁，防止同一用户并发操作冲突"""
        if user_id not in self._user_locks:
            import threading
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
            # search() 返回 Tuple[Optional[MetaBase], List[MediaInfo]]
            result = mc.search(title=keyword)

            # 防御性检查返回值
            if not isinstance(result, tuple) or len(result) != 2:
                logger.warning(f"search() 返回了非预期类型: {type(result)}")
                self._send_text(chat_id, f"⚠️ 搜索返回异常，请重试")
                return

            meta, medias = result

            if not meta or not getattr(meta, 'name', None):
                self._send_text(chat_id, f"😔 无法识别: {keyword}")
                return
            if not medias:
                self._send_text(chat_id, f"😔 未找到 {getattr(meta, 'name', keyword)} 的相关结果")
                return

            # 验证 medias 列表中的元素类型
            valid_medias = []
            for m in medias[:6]:
                if hasattr(m, 'title') and hasattr(m, 'type'):
                    valid_medias.append(m)
                else:
                    logger.warning(f"过滤掉无效的媒体对象: type={type(m)}, value={str(m)[:100]}")

            if not valid_medias:
                self._send_text(chat_id, f"😔 未找到 {getattr(meta, 'name', keyword)} 的有效结果")
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
        先通过 search 识别，再取第一个结果调 SubscribeChain.add()
        """
        if not keyword:
            return
        lock = self._get_user_lock(user_id)
        if not lock.acquire(blocking=False):
            self._send_text(chat_id, "⏳ 上一个请求还在处理中，请稍候...", msg_id)
            return
        try:
            self._send_text(chat_id, f"📥 正在订阅: {keyword} ...", msg_id)

            # 优先使用 search() 而非 recognize_by_meta()，更稳定
            from app.chain.media import MediaChain
            mc = MediaChain()
            mediainfo = None

            try:
                result = mc.search(title=keyword)
                if isinstance(result, tuple) and len(result) == 2:
                    meta, medias = result
                    if medias:
                        # 取第一个有效结果
                        for m in medias:
                            if hasattr(m, 'title') and hasattr(m, 'type'):
                                mediainfo = m
                                break
            except Exception as e:
                logger.warning(f"订阅搜索阶段异常: {e}")

            if not mediainfo:
                # 搜索失败，尝试 recognize_by_meta 作为后备
                try:
                    from app.core.metainfo import MetaInfo as MetaInfoFunc
                    metainfo = MetaInfoFunc(title=keyword)
                    mediainfo = mc.recognize_by_meta(metainfo)
                    # 验证返回类型
                    if mediainfo and not hasattr(mediainfo, 'type'):
                        logger.warning(f"recognize_by_meta 返回了非 MediaInfo 对象: {type(mediainfo)}")
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
        help_text = (
            "📖 飞书机器人指令帮助\n\n"
            "/搜索 <片名>  —— 搜索影视\n"
            "/订阅 <片名>  —— 直接订阅\n"
            "/正在下载      —— 查看下载进度\n"
            "/帮助          —— 显示本帮助\n\n"
            "直接发送片名也会触发搜索"
        )
        self._send_text(chat_id, help_text, msg_id)

    # endregion

    # region ========= 搜索结果卡片 & 卡片回调 =========

    def _build_search_card(self, medias: list, keyword: str) -> dict:
        """
        构造飞书卡片 JSON — 展示搜索结果列表（最多6条）
        每条带「订阅」按钮
        """
        elements = [
            {
                "tag": "markdown",
                "content": f"**🔍 \u201c{keyword}\u201d 的搜索结果 (前{len(medias)}条)**",
            },
            {"tag": "hr"},
        ]
        for idx, media in enumerate(medias):
            title = getattr(media, "title", "") or getattr(media, "title_year", "未知")
            year = getattr(media, "year", "")
            raw_type = getattr(media, "type", None)
            if hasattr(raw_type, 'value'):
                mtype = "电影" if raw_type == MediaType.MOVIE else "电视剧"
            else:
                mtype = "电影" if str(raw_type).lower() in ("movie", "电影") else "电视剧"
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
                "elements": [{"tag": "markdown", "content": "\n".join(md_lines)}],
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
                            "text": {"tag": "plain_text", "content": f"📥 订阅 {title}"},
                            "type": "primary",
                            "value": {"action": "subscribe", "index": str(idx)},
                        }
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

    def _handle_card_action(self, data: dict) -> dict:
        """处理卡片按钮回调（card.action.trigger）"""
        try:
            action = data.get("event", {}).get("action", {})
            value = action.get("value", {})
            act = value.get("action", "")
            operator = data.get("event", {}).get("operator", {})
            user_id = operator.get("open_id", "")

            if act == "subscribe":
                idx = int(value.get("index", 0))
                cached = self._search_cache.get(user_id, [])
                if idx < len(cached):
                    media = cached[idx]
                    chat_id = self._chat_id or ""
                    # 尝试从 context 拿到 chat_id
                    ctx = data.get("event", {}).get("context", {})
                    chat_id = ctx.get("open_chat_id", "") or chat_id
                    threading.Thread(
                        target=self._subscribe_media,
                        args=(media, chat_id, ""),
                        daemon=True,
                    ).start()
        except Exception as e:
            logger.error(f"飞书卡片回调异常: {e}", exc_info=True)
        return {"code": 0}

    # endregion

    # region ========= 订阅 / 通知逻辑 =========

    def _subscribe_media(self, media, chat_id: str, msg_id: str):
        """
        真正的订阅动作：调用 SubscribeChain().add()
        """
        try:
            from app.chain.subscribe import SubscribeChain

            sc = SubscribeChain()
            title = getattr(media, "title", "") or "未知"
            raw_type = getattr(media, "type", None)
            if raw_type and hasattr(raw_type, 'value'):
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
        title = getattr(mi, "title", "") if mi else (getattr(context, "title", "") if context else "未知")
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
