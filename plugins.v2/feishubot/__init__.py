"""
飞书机器人双向通信插件（长连接版）
使用飞书官方 WebSocket 长连接，无需公网 IP / Webhook
支持: 消息接收、影视搜索、订阅下载、交互式卡片
"""
import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

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
    plugin_desc = "飞书机器人双向通信插件，基于长连接无需公网IP，支持影视搜索、订阅下载与消息交互"
    plugin_icon = "feishu.png"
    plugin_version = "2.0.0"
    plugin_author = "MoviePilot-Community"
    plugin_config_prefix = "feishubot_"
    plugin_order = 20
    auth_level = 1

    # ==================== 私有属性 ====================
    _enabled: bool = False
    _app_id: str = ""
    _app_secret: str = ""
    _default_chat_id: str = ""
    _use_lark: bool = False
    _msgtypes: list = []

    # 飞书 API Token
    _tenant_access_token: str = ""
    _token_expires_at: float = 0
    _token_lock = threading.Lock()

    # WebSocket 长连接
    _ws_client = None
    _ws_thread: Optional[threading.Thread] = None
    _ws_running: bool = False

    # API 基础地址
    _base_url: str = ""

    # 搜索结果缓存 {user_id: [MediaInfo, ...]}
    _search_cache: Dict[str, List[Any]] = {}

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        # 先停止旧连接
        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._app_id = config.get("app_id", "")
            self._app_secret = config.get("app_secret", "")
            self._default_chat_id = config.get("default_chat_id", "")
            self._use_lark = config.get("use_lark", False)
            self._msgtypes = config.get("msgtypes", [])

        if self._use_lark:
            self._base_url = "https://open.larksuite.com"
        else:
            self._base_url = "https://open.feishu.cn"

        self._search_cache = {}
        self._tenant_access_token = ""
        self._token_expires_at = 0

        # 启动长连接
        if self.get_state():
            self._start_ws_client()

    def get_state(self) -> bool:
        return self._enabled and bool(self._app_id) and bool(self._app_secret)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """保留一个状态查询 API"""
        return [
            {
                "path": "/feishu/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "summary": "飞书机器人连接状态",
            }
        ]

    def api_status(self, **kwargs) -> dict:
        """返回连接状态"""
        return {
            "enabled": self._enabled,
            "connected": self._ws_running and self._ws_thread and self._ws_thread.is_alive(),
            "app_id": self._app_id[:8] + "..." if self._app_id else "",
            "mode": "websocket_long_connection",
        }

    def get_service(self) -> List[Dict[str, Any]]:
        pass

    def stop_service(self):
        """停止服务"""
        self._ws_running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception:
                pass
            self._ws_client = None
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)
        self._ws_thread = None
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
                                            "model": "default_chat_id",
                                            "label": "默认会话 ID（可选）",
                                            "placeholder": "用于主动推送通知的 chat_id，可留空自动获取",
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
                                            "text": "本插件使用飞书 WebSocket 长连接模式，无需公网 IP 或配置 Webhook。\n\n"
                                                    "配置步骤：\n"
                                                    "1. 在飞书开放平台创建企业自建应用\n"
                                                    "2. 填入 App ID 和 App Secret\n"
                                                    "3. 在「事件与回调」中选择「使用长连接接收」模式\n"
                                                    "4. 订阅 im.message.receive_v1 事件\n"
                                                    "5. 在「权限管理」中开通 im:message、im:message:send_as_bot 等权限\n"
                                                    "6. 发布应用版本并启用本插件",
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
            "default_chat_id": "",
            "use_lark": False,
            "msgtypes": [],
        }

    def get_page(self) -> List[dict]:
        connected = self._ws_running and self._ws_thread and self._ws_thread.is_alive()
        status_text = "✅ WebSocket 长连接已建立" if connected else "❌ 未连接"
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
                                    "type": "success" if connected else "warning",
                                    "variant": "tonal",
                                    "text": f"{status_text}\n\n"
                                            "支持的命令:\n"
                                            "/search <关键词> — 搜索影视\n"
                                            "/subscribe <关键词> — 订阅影视\n"
                                            "/downloading — 查看下载中的任务\n"
                                            "/help — 帮助\n"
                                            "直接发送影视名称也可搜索",
                                },
                            }
                        ],
                    }
                ],
            }
        ]

    # ============================================================
    #  WebSocket 长连接管理
    # ============================================================
    def _start_ws_client(self):
        """启动飞书 WebSocket 长连接"""
        try:
            # 尝试使用官方 SDK
            self._start_with_sdk()
        except ImportError:
            logger.warning("未安装 lark-oapi SDK，使用内置 WebSocket 客户端")
            self._start_with_builtin()

    def _start_with_sdk(self):
        """使用官方 lark-oapi SDK 的 WebSocket 客户端"""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        # 注册消息事件处理器
        event_handler = (
            lark.EventDispatcherHandler
            .builder("", "")
            .register_p2_im_message_receive_v1(self._on_sdk_message)
            .build()
        )

        # 设置域名
        domain = lark.LARK_DOMAIN if self._use_lark else lark.FEISHU_DOMAIN

        # 创建 WebSocket 客户端
        self._ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=event_handler,
            domain=domain,
            log_level=lark.LogLevel.INFO,
        )

        # 在后台线程中启动
        self._ws_running = True
        self._ws_thread = threading.Thread(
            target=self._run_sdk_client,
            daemon=True,
            name="feishu-ws-sdk",
        )
        self._ws_thread.start()
        logger.info("飞书机器人 WebSocket 长连接已启动 (SDK 模式)")

    def _run_sdk_client(self):
        """运行 SDK 客户端（阻塞）"""
        try:
            self._ws_client.start()
        except Exception as e:
            logger.error(f"飞书 SDK WebSocket 异常退出: {e}", exc_info=True)
            self._ws_running = False

    def _on_sdk_message(self, data) -> None:
        """SDK 模式: 接收到消息的回调"""
        try:
            import lark_oapi as lark
            raw = lark.JSON.marshal(data)
            body = json.loads(raw) if isinstance(raw, str) else raw
            # 在子线程处理，避免阻塞 SDK 事件循环
            threading.Thread(
                target=self._handle_message_event,
                args=(body,),
                daemon=True,
            ).start()
        except Exception as e:
            logger.error(f"飞书 SDK 消息处理异常: {e}", exc_info=True)

    # ----------------------------------------------------------
    #  内置 WebSocket 客户端 (无需 lark-oapi)
    # ----------------------------------------------------------
    def _start_with_builtin(self):
        """使用内置 WebSocket 实现"""
        self._ws_running = True
        self._ws_thread = threading.Thread(
            target=self._builtin_ws_loop,
            daemon=True,
            name="feishu-ws-builtin",
        )
        self._ws_thread.start()
        logger.info("飞书机器人 WebSocket 长连接已启动 (内置模式)")

    def _builtin_ws_loop(self):
        """内置 WebSocket 主循环，带自动重连"""
        reconnect_interval = 120
        reconnect_nonce = 30

        while self._ws_running:
            try:
                self._builtin_ws_connect(reconnect_interval)
            except Exception as e:
                logger.error(f"飞书 WebSocket 连接异常: {e}", exc_info=True)

            if not self._ws_running:
                break

            # 重连等待
            import random
            wait = reconnect_interval + random.randint(0, reconnect_nonce)
            logger.info(f"飞书 WebSocket 将在 {wait} 秒后重连...")
            for _ in range(wait):
                if not self._ws_running:
                    return
                time.sleep(1)

    def _builtin_ws_connect(self, default_reconnect_interval: int):
        """建立单次 WebSocket 连接"""
        import websocket

        # Step 1: 获取 WebSocket 连接地址
        endpoint_url = f"{self._base_url}/callback/ws/endpoint"
        logger.info(f"飞书 WebSocket: 正在获取连接地址 ...")
        resp = requests.post(
            endpoint_url,
            headers={"locale": "zh"},
            json={
                "AppID": self._app_id,
                "AppSecret": self._app_secret,
            },
            timeout=30,
        )
        result = resp.json()
        if result.get("code") != 0:
            raise Exception(f"获取 WebSocket 地址失败: {result}")

        ws_url = result["data"]["URL"]
        client_config = result["data"].get("ClientConfig", {})
        ping_interval = client_config.get("PingInterval", 120)

        logger.info(f"飞书 WebSocket: 连接到 {ws_url[:60]}...")

        # Step 2: 建立 WebSocket 连接
        # 飞书使用 Protobuf 帧，但在无 protobuf 依赖时退化为 JSON 长轮询
        # 这里尝试导入 protobuf，如果不可用则走 JSON 兼容模式
        use_protobuf = False
        try:
            from google.protobuf.descriptor import FieldDescriptor
            use_protobuf = True
        except ImportError:
            pass

        ws = websocket.WebSocketApp(
            ws_url,
            on_message=lambda ws, msg: self._on_ws_message(ws, msg, use_protobuf),
            on_error=lambda ws, err: logger.error(f"飞书 WebSocket 错误: {err}"),
            on_close=lambda ws, code, reason: logger.warning(
                f"飞书 WebSocket 断开: code={code}, reason={reason}"
            ),
            on_open=lambda ws: logger.info("飞书 WebSocket 连接建立成功"),
        )

        # 启动 ping 线程
        ping_stop = threading.Event()

        def ping_loop():
            while not ping_stop.is_set() and self._ws_running:
                try:
                    if use_protobuf:
                        self._send_protobuf_ping(ws)
                    else:
                        ws.send("ping")
                except Exception:
                    break
                ping_stop.wait(ping_interval)

        ping_thread = threading.Thread(target=ping_loop, daemon=True)
        ping_thread.start()

        try:
            ws.run_forever(ping_interval=0)
        finally:
            ping_stop.set()

    def _send_protobuf_ping(self, ws):
        """发送 Protobuf 格式的 ping 帧"""
        try:
            from lark_oapi.ws.pb import pbbp2_pb2
            frame = pbbp2_pb2.Frame()
            frame.method = 0  # CONTROL
            frame.SeqID = 0
            frame.LogID = 0
            header = frame.headers.add()
            header.key = "type"
            header.value = "ping"
            ws.send(frame.SerializeToString(), opcode=0x2)
        except Exception:
            # 退化为简单 ping
            ws.send("ping")

    def _on_ws_message(self, ws, message, use_protobuf: bool):
        """处理 WebSocket 消息"""
        try:
            payload = None

            if use_protobuf and isinstance(message, bytes):
                try:
                    from lark_oapi.ws.pb import pbbp2_pb2
                    frame = pbbp2_pb2.Frame()
                    frame.ParseFromString(message)

                    # CONTROL 帧 (ping/pong) 忽略
                    if frame.method == 0:
                        return

                    # DATA 帧
                    if frame.payload:
                        payload = json.loads(frame.payload.decode("utf-8"))

                    # 发送 ACK
                    ack_payload = json.dumps({"code": 200}).encode("utf-8")
                    frame.payload = ack_payload
                    ws.send(frame.SerializeToString(), opcode=0x2)
                except Exception as e:
                    logger.debug(f"Protobuf 解析失败，尝试 JSON: {e}")
                    payload = None

            if payload is None:
                # JSON 模式
                if isinstance(message, bytes):
                    message = message.decode("utf-8")
                payload = json.loads(message)

            if not payload:
                return

            # 提取事件类型
            header = payload.get("header", {})
            event_type = header.get("event_type", "")

            if event_type == "im.message.receive_v1":
                threading.Thread(
                    target=self._handle_message_event,
                    args=(payload,),
                    daemon=True,
                ).start()

        except Exception as e:
            logger.error(f"飞书 WebSocket 消息处理异常: {e}", exc_info=True)

    # ============================================================
    #  Token 管理
    # ============================================================
    def _get_tenant_token(self) -> Optional[str]:
        """获取 tenant_access_token（发送消息用）"""
        with self._token_lock:
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
        """发送文本消息"""
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
            return result
        except Exception as e:
            logger.error(f"飞书发送消息异常: {e}")
            return None

    def _send_card(self, chat_id: str, card: dict, msg_id: str = None):
        """发送交互式卡片"""
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
            return result
        except Exception as e:
            logger.error(f"飞书发送卡片异常: {e}")
            return None

    def _update_card(self, message_id: str, card: dict):
        """更新已有卡片"""
        url = f"{self._base_url}/open-apis/im/v1/messages/{message_id}"
        payload = {
            "msg_type": "interactive",
            "content": json.dumps(card),
        }
        try:
            resp = requests.patch(url, headers=self._headers(), json=payload, timeout=10)
            result = resp.json()
            if result.get("code") != 0:
                logger.error(f"飞书更新卡片失败: {result}")
        except Exception as e:
            logger.error(f"飞书更新卡片异常: {e}")

    # ============================================================
    #  消息处理核心
    # ============================================================
    def _handle_message_event(self, body: dict):
        """处理接收到的消息事件"""
        try:
            event = body.get("event", {})
            message = event.get("message", {})
            sender = event.get("sender", {})

            chat_id = message.get("chat_id", "")
            msg_id = message.get("message_id", "")
            msg_type = message.get("message_type", "")
            chat_type = message.get("chat_type", "")
            user_id = sender.get("sender_id", {}).get("open_id", "")

            # 自动记录 chat_id，用于后续推送
            if chat_id and not self._default_chat_id:
                self._default_chat_id = chat_id
                logger.info(f"飞书: 自动记录会话 ID: {chat_id}")
                # 保存到配置
                self._save_chat_id(chat_id)

            # 只处理文本消息
            if msg_type != "text":
                self._send_text(chat_id, "暂只支持文本消息哦，请直接输入影视名称搜索 🎬", msg_id)
                return

            # 解析文本
            content = json.loads(message.get("content", "{}"))
            text = content.get("text", "").strip()

            # 群聊中去掉 @机器人
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
                # 直接当搜索
                self._cmd_search(text, chat_id, msg_id, user_id)

        except Exception as e:
            logger.error(f"飞书处理消息异常: {e}", exc_info=True)

    def _save_chat_id(self, chat_id: str):
        """保存 chat_id 到插件配置"""
        try:
            config = self.get_config() or {}
            config["default_chat_id"] = chat_id
            self.update_config(config)
        except Exception as e:
            logger.debug(f"保存 chat_id 失败: {e}")

    def _dispatch_command(self, text: str, chat_id: str, msg_id: str, user_id: str):
        """扩展命令分发，支持快捷交互"""
        import re

        # 快捷订阅：订阅1, 订阅2
        sub_match = re.match(r'^[/]?(订阅|subscribe|sub)\s*(\d+)$', text, re.IGNORECASE)
        if sub_match:
            idx = int(sub_match.group(2)) - 1
            cached = self._search_cache.get(user_id, [])
            if 0 <= idx < len(cached):
                self._subscribe_media(cached[idx], chat_id, msg_id)
            else:
                self._send_text(chat_id, f"⚠️ 序号 {idx + 1} 无效，请先搜索", msg_id)
            return

        # 快捷下载：下载1, 下载2
        dl_match = re.match(r'^[/]?(下载|download)\s*(\d+)$', text, re.IGNORECASE)
        if dl_match:
            idx = int(dl_match.group(2)) - 1
            cached = self._search_cache.get(user_id, [])
            if 0 <= idx < len(cached):
                self._do_search_and_download(cached[idx], chat_id, msg_id, user_id)
            else:
                self._send_text(chat_id, f"⚠️ 序号 {idx + 1} 无效，请先搜索", msg_id)
            return

        # 其他命令
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        cmd_map = {
            "/search": self._cmd_search_wrapper,
            "/s": self._cmd_search_wrapper,
            "/搜索": self._cmd_search_wrapper,
            "/subscribe": self._cmd_subscribe_wrapper,
            "/sub": self._cmd_subscribe_wrapper,
            "/订阅": self._cmd_subscribe_wrapper,
            "/downloading": lambda a, c, m, u: self._cmd_downloading(c, m),
            "/dl": lambda a, c, m, u: self._cmd_downloading(c, m),
            "/下载中": lambda a, c, m, u: self._cmd_downloading(c, m),
            "/help": lambda a, c, m, u: self._cmd_help(c, m),
            "/h": lambda a, c, m, u: self._cmd_help(c, m),
            "/帮助": lambda a, c, m, u: self._cmd_help(c, m),
        }

        handler = cmd_map.get(cmd)
        if handler:
            handler(args, chat_id, msg_id, user_id)
        else:
            self._send_text(chat_id, f"未知命令: {cmd}\n输入 /help 查看帮助", msg_id)

    def _cmd_search_wrapper(self, args: str, chat_id: str, msg_id: str, user_id: str):
        if not args:
            self._send_text(chat_id, "请输入搜索关键词，例如：/search 三体", msg_id)
            return
        self._cmd_search(args, chat_id, msg_id, user_id)

    def _cmd_subscribe_wrapper(self, args: str, chat_id: str, msg_id: str, user_id: str):
        if not args:
            self._send_text(chat_id, "请输入要订阅的内容，例如：/subscribe 三体", msg_id)
            return
        self._cmd_subscribe(args, chat_id, msg_id, user_id)

    # ============================================================
    #  命令实现
    # ============================================================
    def _cmd_help(self, chat_id: str, msg_id: str):
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
                            "💡 也可以直接发送影视名称进行搜索\n"
                            "📡 连接模式: WebSocket 长连接 (无需公网IP)"
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
            from app.chain.media import MediaChain
            media_chain = MediaChain()

            # 先尝试精确识别
            meta = media_chain.recognize_by_meta(keyword)
            if meta and meta.tmdb_info:
                medias = [meta]
            else:
                # 退化为模糊搜索
                medias = media_chain.search(title=keyword)

            if not medias:
                self._send_text(chat_id, f"😔 未找到与 \"{keyword}\" 相关的结果")
                return

            medias = medias[:6]
            self._search_cache[user_id] = medias

            card = self._build_search_result_card(medias, keyword, user_id)
            self._send_card(chat_id, card)

        except Exception as e:
            logger.error(f"飞书搜索异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 搜索出错: {str(e)}")

    def _build_search_result_card(self, medias: list, keyword: str, user_id: str) -> dict:
        """构建搜索结果卡片"""
        elements = []

        for i, media in enumerate(medias[:6]):
            title = getattr(media, "title", "") or getattr(media, "title_year", "未知")
            year = getattr(media, "year", "")
            rating = getattr(media, "vote_average", "")
            mtype = "电影" if getattr(media, "type", None) == MediaType.MOVIE else "电视剧"
            overview = getattr(media, "overview", "") or ""
            if len(overview) > 100:
                overview = overview[:100] + "..."
            tmdb_id = getattr(media, "tmdb_id", "")
            poster = getattr(media, "poster_path", "")

            # 标题行
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

            # 操作按钮 - 使用文本交互代替卡片回调（长连接模式下卡片回调需额外配置）
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"👉 回复 `订阅{i + 1}` 订阅此片 | 回复 `下载{i + 1}` 搜索资源",
                },
            })
            elements.append({"tag": "hr"})

        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "💡 回复「订阅+序号」订阅 | 回复「下载+序号」搜索资源下载"},
            ],
        })

        return {
            "header": {
                "title": {"tag": "plain_text", "content": f"🎬 搜索结果: {keyword}"},
                "template": "blue",
            },
            "elements": elements,
        }

    def _cmd_subscribe(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        """订阅影视"""
        # 检查是否是快捷订阅 (订阅1, 订阅2...)
        import re
        quick_match = re.match(r'^(\d+)$', keyword)
        if quick_match:
            idx = int(quick_match.group(1)) - 1
            cached = self._search_cache.get(user_id, [])
            if 0 <= idx < len(cached):
                media = cached[idx]
                return self._subscribe_media(media, chat_id, msg_id)
            else:
                self._send_text(chat_id, f"⚠️ 序号 {idx + 1} 无效，请先搜索", msg_id)
                return

        self._send_text(chat_id, f"📥 正在订阅: {keyword} ...", msg_id)
        try:
            from app.chain.media import MediaChain
            from app.chain.subscribe import SubscribeChain

            media_chain = MediaChain()
            meta = media_chain.recognize_by_meta(keyword)
            if not meta or not meta.tmdb_info:
                self._send_text(chat_id, f"❌ 未识别到: {keyword}")
                return

            self._subscribe_media(meta, chat_id, msg_id)

        except Exception as e:
            logger.error(f"飞书订阅异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 订阅出错: {str(e)}")

    def _subscribe_media(self, media, chat_id: str, msg_id: str):
        """执行订阅"""
        try:
            from app.chain.subscribe import SubscribeChain
            subscribe_chain = SubscribeChain()

            title = getattr(media, "title", "未知")
            year = getattr(media, "year", "")
            tmdb_id = getattr(media, "tmdb_id", None)
            mtype = getattr(media, "type", MediaType.MOVIE)

            sid, msg = subscribe_chain.add(
                title=title,
                year=year,
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
                                "content": (
                                    f"**{title}**"
                                    f"{f' ({year})' if year else ''}\n\n"
                                    f"类型: {'电影' if mtype == MediaType.MOVIE else '电视剧'}\n"
                                    f"TMDB ID: {tmdb_id}\n\n"
                                    "系统将自动搜索并下载资源 🎉"
                                ),
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
                    # 简易进度条
                    filled = int(progress / 10)
                    bar = "▓" * filled + "░" * (10 - filled)
                    line += f"{bar} {progress:.1f}%  "
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
                    "title": {"tag": "plain_text", "content": f"⬇️ 下载中 ({len(downloads)})"},
                    "template": "wathet",
                },
                "elements": elements,
            }
            self._send_card(chat_id, card, msg_id)

        except Exception as e:
            logger.error(f"飞书查看下载异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 查询失败: {str(e)}", msg_id)

    # ============================================================
    #  快捷交互处理（通过文本命令模拟按钮）
    # ============================================================
    def _do_search_and_download(self, media, chat_id: str, msg_id: str, user_id: str):
        """搜索资源并展示下载选项"""
        title = getattr(media, "title", "未知")
        mtype = getattr(media, "type", MediaType.MOVIE)

        self._send_text(chat_id, f"🔍 正在搜索 {title} 的资源...", msg_id)

        try:
            from app.chain.search import SearchChain
            search_chain = SearchChain()

            contexts = search_chain.search_by_title(
                title=title,
                mtype=mtype,
            )

            if not contexts:
                self._send_text(chat_id, f"😔 未找到 {title} 的下载资源")
                return

            # 缓存资源列表
            resource_key = f"{user_id}_resources"
            self._search_cache[resource_key] = contexts[:10]

            elements = []
            for i, ctx in enumerate(contexts[:10]):
                torrent = ctx.torrent_info
                t_title = getattr(torrent, "title", "未知")
                size = getattr(torrent, "size", "")
                seeders = getattr(torrent, "seeders", "")
                site = getattr(torrent, "site_name", "")

                line = f"**{i + 1}. {t_title}**\n"
                parts = []
                if site:
                    parts.append(f"站点: {site}")
                if size:
                    parts.append(f"大小: {size}")
                if seeders:
                    parts.append(f"做种: {seeders}")
                if parts:
                    line += "  |  ".join(parts)

                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": line},
                })
                elements.append({"tag": "hr"})

            elements.append({
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "💡 回复「选择+序号」下载，如：选择1"},
                ],
            })

            card = {
                "header": {
                    "title": {"tag": "plain_text", "content": f"📦 {title} 的资源列表"},
                    "template": "green",
                },
                "elements": elements,
            }
            self._send_card(chat_id, card)

        except Exception as e:
            logger.error(f"飞书搜索资源异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 搜索资源出错: {str(e)}")

    # ============================================================
    #  系统通知推送
    # ============================================================
    @eventmanager.register(EventType.NoticeMessage)
    def handle_notice(self, event: Event):
        """监听系统通知并推送到飞书"""
        if not self.get_state():
            return
        if not self._default_chat_id:
            return

        event_data = event.event_data or {}
        msg_type: NotificationType = event_data.get("type")
        title = event_data.get("title", "")
        text = event_data.get("text", "")

        # 过滤消息类型
        if self._msgtypes and msg_type and msg_type.name not in self._msgtypes:
            return

        # 卡片颜色
        color = "blue"
        if msg_type:
            val = msg_type.value or ""
            if "完成" in val or "成功" in val:
                color = "green"
            elif "失败" in val or "错误" in val:
                color = "red"
            elif "警告" in val:
                color = "orange"

        card = {
            "header": {
                "title": {"tag": "plain_text", "content": title or "MoviePilot 通知"},
                "template": color,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": text or "（无详细内容）"},
                }
            ],
        }

        try:
            self._send_card(self._default_chat_id, card)
        except Exception as e:
            logger.error(f"飞书通知推送异常: {e}", exc_info=True)
