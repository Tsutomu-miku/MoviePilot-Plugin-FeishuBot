"""
飞书机器人双向通信插件（长连接版 v2.1.0）
使用飞书官方 WebSocket 长连接，无需公网 IP / Webhook
内置 Protobuf 解析器，无需额外安装 protobuf 依赖
"""
import json
import struct
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType, MediaType

import requests


# ==================================================================
#  内置 Protobuf 解析器 (零依赖)
#  解析飞书 WebSocket 使用的 pbbp2.Frame 帧
# ==================================================================
class _MiniProtobuf:
    """
    极简 Protobuf 解析器，仅支持飞书 Frame 结构
    飞书帧格式 (proto2):
        message Header { required string key=1; required string value=2; }
        message Frame {
            required uint64 SeqID=1; required uint64 LogID=2;
            required int32 service=3; required int32 method=4;
            repeated Header headers=5;
            optional string payload_encoding=6;
            optional string payload_type=7; optional bytes payload=8;
            optional string LogIDNew=9;
        }
    """

    @staticmethod
    def _read_varint(data: bytes, pos: int) -> Tuple[int, int]:
        """读取 varint，返回 (value, new_pos)"""
        result = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            result |= (b & 0x7F) << shift
            pos += 1
            if (b & 0x80) == 0:
                return result, pos
            shift += 7
        raise ValueError("varint 未正常终止")

    @staticmethod
    def _read_length_delimited(data: bytes, pos: int) -> Tuple[bytes, int]:
        """读取 length-delimited 字段"""
        length, pos = _MiniProtobuf._read_varint(data, pos)
        end = pos + length
        if end > len(data):
            raise ValueError(f"length-delimited 越界: need {length}, have {len(data) - pos}")
        return data[pos:end], end

    @staticmethod
    def parse_frame(data: bytes) -> dict:
        """
        解析飞书 Frame protobuf 帧
        返回: {
            "SeqID": int, "LogID": int, "service": int, "method": int,
            "headers": [{"key": str, "value": str}, ...],
            "payload_encoding": str, "payload_type": str,
            "payload": bytes, "LogIDNew": str
        }
        """
        frame = {
            "SeqID": 0, "LogID": 0, "service": 0, "method": 0,
            "headers": [], "payload_encoding": "", "payload_type": "",
            "payload": b"", "LogIDNew": ""
        }
        pos = 0
        while pos < len(data):
            # 读取 field tag
            tag, pos = _MiniProtobuf._read_varint(data, pos)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if wire_type == 0:  # varint
                value, pos = _MiniProtobuf._read_varint(data, pos)
                if field_number == 1:
                    frame["SeqID"] = value
                elif field_number == 2:
                    frame["LogID"] = value
                elif field_number == 3:
                    frame["service"] = value
                elif field_number == 4:
                    frame["method"] = value

            elif wire_type == 2:  # length-delimited
                raw, pos = _MiniProtobuf._read_length_delimited(data, pos)
                if field_number == 5:
                    # Header 子消息
                    header = _MiniProtobuf._parse_header(raw)
                    frame["headers"].append(header)
                elif field_number == 6:
                    frame["payload_encoding"] = raw.decode("utf-8", errors="replace")
                elif field_number == 7:
                    frame["payload_type"] = raw.decode("utf-8", errors="replace")
                elif field_number == 8:
                    frame["payload"] = raw
                elif field_number == 9:
                    frame["LogIDNew"] = raw.decode("utf-8", errors="replace")

            elif wire_type == 1:  # 64-bit
                pos += 8
            elif wire_type == 5:  # 32-bit
                pos += 4
            else:
                break  # 未知类型，停止

        return frame

    @staticmethod
    def _parse_header(data: bytes) -> dict:
        """解析 Header 子消息"""
        header = {"key": "", "value": ""}
        pos = 0
        while pos < len(data):
            tag, pos = _MiniProtobuf._read_varint(data, pos)
            field_number = tag >> 3
            wire_type = tag & 0x07
            if wire_type == 2:
                raw, pos = _MiniProtobuf._read_length_delimited(data, pos)
                if field_number == 1:
                    header["key"] = raw.decode("utf-8", errors="replace")
                elif field_number == 2:
                    header["value"] = raw.decode("utf-8", errors="replace")
            elif wire_type == 0:
                _, pos = _MiniProtobuf._read_varint(data, pos)
            else:
                break
        return header

    @staticmethod
    def build_ping_frame(service_id: int) -> bytes:
        """构建 Ping 帧 (CONTROL frame with type=ping)"""
        parts = []

        # SeqID = 0 (field 1, varint)
        parts.append(b'\x08\x00')
        # LogID = 0 (field 2, varint)
        parts.append(b'\x10\x00')
        # service = service_id (field 3, varint)
        parts.append(b'\x18')
        parts.append(_MiniProtobuf._encode_varint(service_id))
        # method = 0 / CONTROL (field 4, varint)
        parts.append(b'\x20\x00')
        # headers: Header { key="type", value="ping" }
        header_payload = _MiniProtobuf._encode_header("type", "ping")
        parts.append(b'\x2a')  # field 5, wire type 2
        parts.append(_MiniProtobuf._encode_varint(len(header_payload)))
        parts.append(header_payload)

        return b''.join(parts)

    @staticmethod
    def build_ack_frame(original_frame: dict, code: int = 200) -> bytes:
        """构建 ACK 响应帧"""
        parts = []

        # SeqID (field 1)
        parts.append(b'\x08')
        parts.append(_MiniProtobuf._encode_varint(original_frame.get("SeqID", 0)))
        # LogID (field 2)
        parts.append(b'\x10')
        parts.append(_MiniProtobuf._encode_varint(original_frame.get("LogID", 0)))
        # service (field 3)
        parts.append(b'\x18')
        parts.append(_MiniProtobuf._encode_varint(original_frame.get("service", 0)))
        # method (field 4) = 1 / DATA
        parts.append(b'\x20\x01')

        # 复制原始 headers
        for h in original_frame.get("headers", []):
            header_payload = _MiniProtobuf._encode_header(h["key"], h["value"])
            parts.append(b'\x2a')
            parts.append(_MiniProtobuf._encode_varint(len(header_payload)))
            parts.append(header_payload)

        # payload = JSON {"code": 200}
        resp_json = json.dumps({"code": code}).encode("utf-8")
        parts.append(b'\x42')  # field 8, wire type 2
        parts.append(_MiniProtobuf._encode_varint(len(resp_json)))
        parts.append(resp_json)

        return b''.join(parts)

    @staticmethod
    def _encode_varint(value: int) -> bytes:
        """编码 varint"""
        parts = []
        while value > 0x7F:
            parts.append((value & 0x7F) | 0x80)
            value >>= 7
        parts.append(value & 0x7F)
        return bytes(parts)

    @staticmethod
    def _encode_header(key: str, value: str) -> bytes:
        """编码 Header 子消息"""
        parts = []
        # key (field 1)
        key_bytes = key.encode("utf-8")
        parts.append(b'\x0a')
        parts.append(_MiniProtobuf._encode_varint(len(key_bytes)))
        parts.append(key_bytes)
        # value (field 2)
        val_bytes = value.encode("utf-8")
        parts.append(b'\x12')
        parts.append(_MiniProtobuf._encode_varint(len(val_bytes)))
        parts.append(val_bytes)
        return b''.join(parts)


# ==================================================================
#  辅助函数
# ==================================================================
def _get_header(headers: list, key: str) -> str:
    """从 headers 列表中获取指定 key 的 value"""
    for h in headers:
        if h.get("key") == key:
            return h.get("value", "")
    return ""


# ==================================================================
#  主插件类
# ==================================================================
class FeishuBot(_PluginBase):
    """飞书机器人双向通信插件"""

    # ==================== 插件元数据 ====================
    plugin_name = "飞书机器人"
    plugin_desc = "飞书机器人双向通信插件，基于长连接无需公网IP，支持影视搜索、订阅下载与消息交互"
    plugin_icon = "feishu.png"
    plugin_version = "2.1.0"
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
    _ws_service_id: int = 0  # 从连接 URL 中解析

    # 消息分片缓存 {msg_id: {total: int, parts: {seq: bytes}}}
    _msg_cache: Dict[str, dict] = {}
    _msg_cache_lock = threading.Lock()

    # API 基础地址
    _base_url: str = ""

    # 搜索结果缓存 {user_id: [MediaInfo, ...]}
    _search_cache: Dict[str, List[Any]] = {}

    def init_plugin(self, config: dict = None):
        """初始化插件"""
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
        self._msg_cache = {}
        self._tenant_access_token = ""
        self._token_expires_at = 0

        if self.get_state():
            self._start_ws_client()

    def get_state(self) -> bool:
        return self._enabled and bool(self._app_id) and bool(self._app_secret)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/feishu/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "summary": "飞书机器人连接状态",
            }
        ]

    def api_status(self, **kwargs) -> dict:
        return {
            "enabled": self._enabled,
            "connected": self._ws_running and self._ws_thread and self._ws_thread.is_alive(),
            "app_id": self._app_id[:8] + "..." if self._app_id else "",
            "mode": "websocket_long_connection",
        }

    def get_service(self) -> List[Dict[str, Any]]:
        pass

    def stop_service(self):
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
        self._msg_cache.clear()

    # ============================================================
    #  配置表单
    # ============================================================
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({"title": item.value, "value": item.name})
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
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "use_lark", "label": "国际版 (Lark)"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {"model": "app_id", "label": "App ID",
                                              "placeholder": "飞书应用的 App ID"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {"model": "app_secret", "label": "App Secret",
                                              "type": "password",
                                              "placeholder": "飞书应用的 App Secret"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VTextField",
                                "props": {"model": "default_chat_id",
                                          "label": "默认会话 ID（可选）",
                                          "placeholder": "留空则自动获取首次对话的 chat_id"},
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VSelect",
                                "props": {
                                    "model": "msgtypes", "label": "消息类型",
                                    "items": MsgTypeOptions, "multiple": True,
                                    "chips": True, "clearable": True,
                                    "placeholder": "选择推送的通知类型，留空全部推送",
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "本插件使用飞书 WebSocket 长连接，无需公网 IP。\n\n"
                                            "配置步骤：\n"
                                            "1. 飞书开放平台创建企业自建应用，获取 App ID / Secret\n"
                                            "2. 事件与回调 → 选择「使用长连接接收」\n"
                                            "3. 订阅 im.message.receive_v1 事件\n"
                                            "4. 权限管理 → 开通 im:message 和 im:message:send_as_bot\n"
                                            "5. 发布应用版本 → 启用本插件\n\n"
                                            "如需更稳定的连接，可在 Docker 中安装 lark-oapi SDK：\n"
                                            "docker exec -it moviepilot pip install lark-oapi",
                                },
                            }],
                        }],
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
        return [{
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VAlert",
                    "props": {
                        "type": "success" if connected else "warning",
                        "variant": "tonal",
                        "text": f"{status_text}\n\n"
                                "命令: /search 搜索 | /subscribe 订阅 | "
                                "/downloading 下载中 | /help 帮助\n"
                                "直接发送影视名称也可搜索",
                    },
                }],
            }],
        }]

    # ============================================================
    #  WebSocket 长连接管理
    # ============================================================
    def _start_ws_client(self):
        """启动 WebSocket 长连接"""
        try:
            import lark_oapi
            self._start_with_sdk()
        except ImportError:
            logger.info("未安装 lark-oapi，使用内置 WebSocket + Protobuf 解析器")
            self._start_with_builtin()

    # ---------- SDK 模式 ----------
    def _start_with_sdk(self):
        import lark_oapi as lark
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_sdk_message)
            .build()
        )
        domain = lark.LARK_DOMAIN if self._use_lark else lark.FEISHU_DOMAIN
        self._ws_client = lark.ws.Client(
            self._app_id, self._app_secret,
            event_handler=event_handler,
            domain=domain, log_level=lark.LogLevel.INFO,
        )
        self._ws_running = True
        self._ws_thread = threading.Thread(
            target=self._run_sdk_client, daemon=True, name="feishu-ws-sdk",
        )
        self._ws_thread.start()
        logger.info("飞书机器人长连接已启动 (lark-oapi SDK 模式)")

    def _run_sdk_client(self):
        try:
            self._ws_client.start()
        except Exception as e:
            logger.error(f"飞书 SDK WebSocket 异常: {e}", exc_info=True)
            self._ws_running = False

    def _on_sdk_message(self, data) -> None:
        try:
            import lark_oapi as lark
            raw = lark.JSON.marshal(data)
            body = json.loads(raw) if isinstance(raw, str) else raw
            threading.Thread(
                target=self._handle_message_event, args=(body,), daemon=True
            ).start()
        except Exception as e:
            logger.error(f"飞书 SDK 消息处理异常: {e}", exc_info=True)

    # ---------- 内置 WebSocket 模式 (带 Protobuf 解析) ----------
    def _start_with_builtin(self):
        self._ws_running = True
        self._ws_thread = threading.Thread(
            target=self._builtin_ws_loop, daemon=True, name="feishu-ws-builtin",
        )
        self._ws_thread.start()
        logger.info("飞书机器人长连接已启动 (内置 WebSocket 模式)")

    def _builtin_ws_loop(self):
        """主循环，带自动重连"""
        import random
        reconnect_interval = 120
        reconnect_nonce = 30

        while self._ws_running:
            try:
                self._builtin_ws_connect()
            except Exception as e:
                logger.error(f"飞书 WebSocket 连接异常: {e}")

            if not self._ws_running:
                break
            wait = reconnect_interval + random.randint(0, reconnect_nonce)
            logger.info(f"飞书 WebSocket 将在 {wait}s 后重连...")
            for _ in range(wait):
                if not self._ws_running:
                    return
                time.sleep(1)

    def _builtin_ws_connect(self):
        """建立单次 WebSocket 连接"""
        import websocket
        from urllib.parse import urlparse, parse_qs

        # 1) 获取 WebSocket URL
        endpoint_url = f"{self._base_url}/callback/ws/endpoint"
        logger.info("飞书: 正在获取 WebSocket 连接地址...")
        resp = requests.post(
            endpoint_url,
            headers={"locale": "zh"},
            json={"AppID": self._app_id, "AppSecret": self._app_secret},
            timeout=30,
        )
        result = resp.json()
        if result.get("code") != 0:
            raise Exception(f"获取 WebSocket 地址失败: code={result.get('code')}, msg={result.get('msg')}")

        data = result["data"]
        ws_url = data["URL"]
        client_config = data.get("ClientConfig", {})
        ping_interval = client_config.get("PingInterval", 120)

        # 解析 service_id
        parsed = urlparse(ws_url)
        qs = parse_qs(parsed.query)
        self._ws_service_id = int(qs.get("service_id", [0])[0])
        conn_id = qs.get("device_id", [""])[0]

        logger.info(f"飞书: WebSocket 连接建立中 (conn_id={conn_id[:16]}...)")

        # 2) 连接 WebSocket
        ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_builtin_ws_message,
            on_error=lambda ws, err: logger.error(f"飞书 WebSocket 错误: {err}"),
            on_close=lambda ws, code, reason: logger.warning(
                f"飞书 WebSocket 断开: code={code}, reason={reason}"
            ),
            on_open=lambda ws: logger.info("飞书 WebSocket 连接成功 ✓"),
        )
        self._ws_obj = ws

        # 3) Ping 线程
        ping_stop = threading.Event()

        def ping_loop():
            while not ping_stop.is_set() and self._ws_running:
                try:
                    ping_data = _MiniProtobuf.build_ping_frame(self._ws_service_id)
                    ws.send(ping_data, opcode=0x2)  # binary frame
                    logger.debug("飞书: Ping sent")
                except Exception as e:
                    logger.debug(f"飞书: Ping 失败: {e}")
                    break
                ping_stop.wait(ping_interval)

        ping_thread = threading.Thread(target=ping_loop, daemon=True)
        ping_thread.start()

        try:
            ws.run_forever(ping_interval=0)
        finally:
            ping_stop.set()
            self._ws_obj = None

    def _on_builtin_ws_message(self, ws, message):
        """处理 WebSocket 收到的二进制消息（Protobuf 帧）"""
        try:
            if not isinstance(message, bytes):
                message = message.encode("utf-8") if isinstance(message, str) else message

            # 使用内置解析器解析 Protobuf 帧
            frame = _MiniProtobuf.parse_frame(message)
            method = frame.get("method", 0)
            headers = frame.get("headers", [])
            msg_type = _get_header(headers, "type")

            # CONTROL 帧 (ping/pong)
            if method == 0:
                if msg_type == "pong":
                    logger.debug("飞书: Pong received")
                    # 尝试从 payload 更新配置
                    if frame.get("payload"):
                        try:
                            conf = json.loads(frame["payload"].decode("utf-8"))
                            logger.debug(f"飞书: 配置更新: {conf}")
                        except Exception:
                            pass
                return

            # DATA 帧
            if method != 1:
                return

            # 处理消息分片
            msg_id = _get_header(headers, "message_id")
            total = int(_get_header(headers, "sum") or "1")
            seq = int(_get_header(headers, "seq") or "0")
            payload = frame.get("payload", b"")

            if total > 1:
                payload = self._combine_chunks(msg_id, total, seq, payload)
                if payload is None:
                    return  # 还有分片未到

            # 发送 ACK
            try:
                ack_data = _MiniProtobuf.build_ack_frame(frame, code=200)
                ws.send(ack_data, opcode=0x2)
            except Exception as e:
                logger.debug(f"飞书: ACK 发送失败: {e}")

            # 解析 JSON payload
            if not payload:
                return

            try:
                event_body = json.loads(payload.decode("utf-8"))
            except Exception as e:
                logger.error(f"飞书: payload JSON 解析失败: {e}")
                return

            event_type = event_body.get("header", {}).get("event_type", "")

            if event_type == "im.message.receive_v1":
                threading.Thread(
                    target=self._handle_message_event,
                    args=(event_body,),
                    daemon=True,
                ).start()
            else:
                logger.debug(f"飞书: 忽略事件类型: {event_type or msg_type}")

        except Exception as e:
            logger.error(f"飞书 WebSocket 消息处理异常: {e}", exc_info=True)

    def _combine_chunks(self, msg_id: str, total: int, seq: int, data: bytes) -> Optional[bytes]:
        """组合分片消息"""
        with self._msg_cache_lock:
            if msg_id not in self._msg_cache:
                self._msg_cache[msg_id] = {
                    "total": total, "parts": {}, "time": time.time()
                }
            cache = self._msg_cache[msg_id]
            cache["parts"][seq] = data

            # 检查是否所有分片已到
            if len(cache["parts"]) < total:
                return None

            # 按序号组合
            combined = b""
            for i in range(total):
                part = cache["parts"].get(i, b"")
                combined += part

            # 清理缓存
            del self._msg_cache[msg_id]

            # 清理过期缓存 (>30s)
            now = time.time()
            expired = [k for k, v in self._msg_cache.items() if now - v["time"] > 30]
            for k in expired:
                del self._msg_cache[k]

            return combined

    # ============================================================
    #  Token 管理
    # ============================================================
    def _get_tenant_token(self) -> Optional[str]:
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
        return {
            "Authorization": f"Bearer {self._get_tenant_token()}",
            "Content-Type": "application/json",
        }

    # ============================================================
    #  消息发送
    # ============================================================
    def _send_text(self, chat_id: str, text: str, msg_id: str = None):
        if msg_id:
            url = f"{self._base_url}/open-apis/im/v1/messages/{msg_id}/reply"
            payload = {"msg_type": "text", "content": json.dumps({"text": text})}
            params = None
        else:
            url = f"{self._base_url}/open-apis/im/v1/messages"
            payload = {"receive_id": chat_id, "msg_type": "text",
                       "content": json.dumps({"text": text})}
            params = {"receive_id_type": "chat_id"}
        try:
            resp = requests.post(url, params=params, headers=self._headers(),
                                 json=payload, timeout=10)
            result = resp.json()
            if result.get("code") != 0:
                logger.error(f"飞书发送消息失败: {result}")
            return result
        except Exception as e:
            logger.error(f"飞书发送消息异常: {e}")
            return None

    def _send_card(self, chat_id: str, card: dict, msg_id: str = None):
        if msg_id:
            url = f"{self._base_url}/open-apis/im/v1/messages/{msg_id}/reply"
            payload = {"msg_type": "interactive", "content": json.dumps(card)}
            params = None
        else:
            url = f"{self._base_url}/open-apis/im/v1/messages"
            payload = {"receive_id": chat_id, "msg_type": "interactive",
                       "content": json.dumps(card)}
            params = {"receive_id_type": "chat_id"}
        try:
            resp = requests.post(url, params=params, headers=self._headers(),
                                 json=payload, timeout=10)
            result = resp.json()
            if result.get("code") != 0:
                logger.error(f"飞书发送卡片失败: {result}")
            return result
        except Exception as e:
            logger.error(f"飞书发送卡片异常: {e}")
            return None

    # ============================================================
    #  消息处理核心
    # ============================================================
    def _handle_message_event(self, body: dict):
        try:
            event = body.get("event", {})
            message = event.get("message", {})
            sender = event.get("sender", {})

            chat_id = message.get("chat_id", "")
            msg_id = message.get("message_id", "")
            msg_type = message.get("message_type", "")
            user_id = sender.get("sender_id", {}).get("open_id", "")

            # 自动记录 chat_id
            if chat_id and not self._default_chat_id:
                self._default_chat_id = chat_id
                logger.info(f"飞书: 自动记录会话 ID: {chat_id}")
                self._save_chat_id(chat_id)

            if msg_type != "text":
                self._send_text(chat_id, "暂只支持文本消息，请直接输入影视名称搜索 🎬", msg_id)
                return

            content = json.loads(message.get("content", "{}"))
            text = content.get("text", "").strip()

            # 去掉 @机器人
            for m in message.get("mentions", []):
                key = m.get("key", "")
                if key:
                    text = text.replace(key, "").strip()

            if not text:
                return

            logger.info(f"飞书收到: user={user_id}, text={text}")

            if text.startswith("/"):
                self._dispatch_command(text, chat_id, msg_id, user_id)
            else:
                # 检查快捷指令
                import re
                sub_m = re.match(r'^订阅\s*(\d+)$', text)
                dl_m = re.match(r'^下载\s*(\d+)$', text)
                sel_m = re.match(r'^选择\s*(\d+)$', text)
                if sub_m:
                    self._quick_subscribe(int(sub_m.group(1)), chat_id, msg_id, user_id)
                elif dl_m:
                    self._quick_download(int(dl_m.group(1)), chat_id, msg_id, user_id)
                elif sel_m:
                    self._quick_select(int(sel_m.group(1)), chat_id, msg_id, user_id)
                else:
                    self._cmd_search(text, chat_id, msg_id, user_id)

        except Exception as e:
            logger.error(f"飞书处理消息异常: {e}", exc_info=True)

    def _save_chat_id(self, chat_id: str):
        try:
            config = self.get_config() or {}
            config["default_chat_id"] = chat_id
            self.update_config(config)
        except Exception as e:
            logger.debug(f"保存 chat_id 失败: {e}")

    def _dispatch_command(self, text: str, chat_id: str, msg_id: str, user_id: str):
        import re
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/search", "/s", "/搜索"):
            if not args:
                self._send_text(chat_id, "请输入关键词，如: /search 三体", msg_id)
            else:
                self._cmd_search(args, chat_id, msg_id, user_id)
        elif cmd in ("/subscribe", "/sub", "/订阅"):
            if not args:
                self._send_text(chat_id, "请输入内容，如: /subscribe 三体", msg_id)
            else:
                self._cmd_subscribe(args, chat_id, msg_id, user_id)
        elif cmd in ("/downloading", "/dl", "/下载中"):
            self._cmd_downloading(chat_id, msg_id)
        elif cmd in ("/help", "/h", "/帮助"):
            self._cmd_help(chat_id, msg_id)
        else:
            self._send_text(chat_id, f"未知命令: {cmd}\n输入 /help 查看帮助", msg_id)

    # ============================================================
    #  命令实现
    # ============================================================
    def _cmd_help(self, chat_id: str, msg_id: str):
        card = {
            "header": {
                "title": {"tag": "plain_text", "content": "🎬 MoviePilot 飞书机器人"},
                "template": "blue",
            },
            "elements": [{
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "**支持的命令：**\n\n"
                        "🔍 `/search <关键词>` — 搜索影视\n"
                        "📥 `/subscribe <关键词>` — 订阅影视\n"
                        "⬇️ `/downloading` — 查看下载中的任务\n"
                        "❓ `/help` — 显示此帮助\n\n"
                        "**快捷操作：**\n"
                        "💡 直接发送影视名称即可搜索\n"
                        "💡 搜索后回复 `订阅1` 快捷订阅\n"
                        "💡 搜索后回复 `下载1` 搜索资源\n"
                        "💡 资源列表中回复 `选择1` 下载\n\n"
                        "📡 连接模式: WebSocket 长连接"
                    ),
                },
            }],
        }
        self._send_card(chat_id, card, msg_id)

    def _cmd_search(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        self._send_text(chat_id, f"🔍 正在搜索: {keyword} ...", msg_id)
        try:
            from app.chain.media import MediaChain
            media_chain = MediaChain()
            meta = media_chain.recognize_by_meta(keyword)
            if meta and meta.tmdb_info:
                medias = [meta]
            else:
                medias = media_chain.search(title=keyword)

            if not medias:
                self._send_text(chat_id, f"😔 未找到: {keyword}")
                return

            medias = medias[:6]
            self._search_cache[user_id] = medias

            card = self._build_search_result_card(medias, keyword)
            self._send_card(chat_id, card)
        except Exception as e:
            logger.error(f"飞书搜索异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 搜索出错: {e}")

    def _build_search_result_card(self, medias: list, keyword: str) -> dict:
        elements = []
        for i, media in enumerate(medias[:6]):
            title = getattr(media, "title", "") or getattr(media, "title_year", "未知")
            year = getattr(media, "year", "")
            rating = getattr(media, "vote_average", "")
            mtype = "电影" if getattr(media, "type", None) == MediaType.MOVIE else "电视剧"
            overview = getattr(media, "overview", "") or ""
            if len(overview) > 100:
                overview = overview[:100] + "..."

            line = f"**{i + 1}. {title}**"
            if year:
                line += f" ({year})"
            line += f"  [{mtype}]"
            if rating:
                line += f"  ⭐ {rating}"
            if overview:
                line += f"\n{overview}"

            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"👉 回复 `订阅{i + 1}` 订阅 | 回复 `下载{i + 1}` 搜索资源"},
            })
            elements.append({"tag": "hr"})

        return {
            "header": {
                "title": {"tag": "plain_text", "content": f"🎬 搜索结果: {keyword}"},
                "template": "blue",
            },
            "elements": elements,
        }

    def _cmd_subscribe(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        self._send_text(chat_id, f"📥 正在订阅: {keyword} ...", msg_id)
        try:
            from app.chain.media import MediaChain
            media_chain = MediaChain()
            meta = media_chain.recognize_by_meta(keyword)
            if not meta or not meta.tmdb_info:
                self._send_text(chat_id, f"❌ 未识别到: {keyword}")
                return
            self._subscribe_media(meta, chat_id, msg_id)
        except Exception as e:
            logger.error(f"飞书订阅异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 订阅出错: {e}")

    def _subscribe_media(self, media, chat_id: str, msg_id: str = None):
        try:
            from app.chain.subscribe import SubscribeChain
            subscribe_chain = SubscribeChain()
            title = getattr(media, "title", "未知")
            year = getattr(media, "year", "")
            tmdb_id = getattr(media, "tmdb_id", None)
            mtype = getattr(media, "type", MediaType.MOVIE)

            sid, msg = subscribe_chain.add(
                title=title, year=year, mtype=mtype,
                tmdbid=tmdb_id, userid="feishu",
            )
            if sid:
                mtype_str = "电影" if mtype == MediaType.MOVIE else "电视剧"
                card = {
                    "header": {"title": {"tag": "plain_text", "content": "✅ 订阅成功"},
                               "template": "green"},
                    "elements": [{"tag": "div", "text": {"tag": "lark_md",
                        "content": f"**{title}** ({year})\n类型: {mtype_str} | TMDB: {tmdb_id}\n系统将自动搜索下载 🎉"}}],
                }
                self._send_card(chat_id, card)
            else:
                self._send_text(chat_id, f"⚠️ 订阅失败: {msg or '未知原因'}")
        except Exception as e:
            logger.error(f"飞书订阅异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 订阅出错: {e}")

    def _cmd_downloading(self, chat_id: str, msg_id: str):
        try:
            from app.chain.download import DownloadChain
            downloads = DownloadChain().downloading()
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
                    filled = int(progress / 10)
                    line += f"{'▓' * filled}{'░' * (10 - filled)} {progress:.1f}%  "
                if speed:
                    line += f"速度: {speed}  "
                if size:
                    line += f"大小: {size}"
                elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
                elements.append({"tag": "hr"})
            card = {
                "header": {"title": {"tag": "plain_text", "content": f"⬇️ 下载中 ({len(downloads)})"},
                           "template": "wathet"},
                "elements": elements,
            }
            self._send_card(chat_id, card, msg_id)
        except Exception as e:
            logger.error(f"飞书查看下载异常: {e}", exc_info=True)
            self._send_text(chat_id, f"⚠️ 查询失败: {e}", msg_id)

    # ============================================================
    #  快捷交互
    # ============================================================
    def _quick_subscribe(self, idx: int, chat_id: str, msg_id: str, user_id: str):
        """快捷订阅: 订阅1"""
        cached = self._search_cache.get(user_id, [])
        if 0 < idx <= len(cached):
            self._subscribe_media(cached[idx - 1], chat_id, msg_id)
        else:
            self._send_text(chat_id, f"⚠️ 序号 {idx} 无效，请先搜索影视", msg_id)

    def _quick_download(self, idx: int, chat_id: str, msg_id: str, user_id: str):
        """快捷搜索资源: 下载1"""
        cached = self._search_cache.get(user_id, [])
        if 0 < idx <= len(cached):
            media = cached[idx - 1]
            title = getattr(media, "title", "未知")
            mtype = getattr(media, "type", MediaType.MOVIE)
            self._send_text(chat_id, f"🔍 正在搜索 {title} 的资源...", msg_id)
            try:
                from app.chain.search import SearchChain
                contexts = SearchChain().search_by_title(title=title, mtype=mtype)
                if not contexts:
                    self._send_text(chat_id, f"😔 未找到 {title} 的资源")
                    return
                # 缓存资源
                self._search_cache[f"{user_id}_res"] = contexts[:10]
                elements = []
                for i, ctx in enumerate(contexts[:10]):
                    torrent = ctx.torrent_info
                    t_title = getattr(torrent, "title", "未知")
                    size = getattr(torrent, "size", "")
                    seeders = getattr(torrent, "seeders", "")
                    site = getattr(torrent, "site_name", "")
                    line = f"**{i + 1}. {t_title}**\n"
                    parts = []
                    if site: parts.append(f"站点: {site}")
                    if size: parts.append(f"大小: {size}")
                    if seeders: parts.append(f"做种: {seeders}")
                    if parts: line += "  |  ".join(parts)
                    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
                    elements.append({"tag": "hr"})
                elements.append({"tag": "note", "elements": [
                    {"tag": "plain_text", "content": "💡 回复「选择+序号」下载，如: 选择1"}]})
                card = {
                    "header": {"title": {"tag": "plain_text", "content": f"📦 {title} 的资源"},
                               "template": "green"},
                    "elements": elements,
                }
                self._send_card(chat_id, card)
            except Exception as e:
                logger.error(f"飞书搜索资源异常: {e}", exc_info=True)
                self._send_text(chat_id, f"⚠️ 搜索资源出错: {e}")
        else:
            self._send_text(chat_id, f"⚠️ 序号 {idx} 无效，请先搜索影视", msg_id)

    def _quick_select(self, idx: int, chat_id: str, msg_id: str, user_id: str):
        """快捷下载资源: 选择1"""
        cached = self._search_cache.get(f"{user_id}_res", [])
        if 0 < idx <= len(cached):
            ctx = cached[idx - 1]
            try:
                from app.chain.download import DownloadChain
                title = getattr(ctx.torrent_info, "title", "")
                result = DownloadChain().download_single(context=ctx, userid="feishu")
                if result:
                    self._send_text(chat_id, f"✅ 已开始下载: {title}")
                else:
                    self._send_text(chat_id, f"⚠️ 下载失败，请重试")
            except Exception as e:
                logger.error(f"飞书下载异常: {e}", exc_info=True)
                self._send_text(chat_id, f"⚠️ 下载出错: {e}")
        else:
            self._send_text(chat_id, f"⚠️ 序号 {idx} 无效，请先搜索资源", msg_id)

    # ============================================================
    #  系统通知推送
    # ============================================================
    @eventmanager.register(EventType.NoticeMessage)
    def handle_notice(self, event: Event):
        if not self.get_state() or not self._default_chat_id:
            return
        event_data = event.event_data or {}
        msg_type: NotificationType = event_data.get("type")
        title = event_data.get("title", "")
        text = event_data.get("text", "")

        if self._msgtypes and msg_type and msg_type.name not in self._msgtypes:
            return

        color = "blue"
        if msg_type:
            val = msg_type.value or ""
            if "完成" in val or "成功" in val:
                color = "green"
            elif "失败" in val or "错误" in val:
                color = "red"

        card = {
            "header": {"title": {"tag": "plain_text", "content": title or "MoviePilot 通知"},
                       "template": color},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text or "（无详情）"}}],
        }
        try:
            self._send_card(self._default_chat_id, card)
        except Exception as e:
            logger.error(f"飞书通知推送异常: {e}", exc_info=True)
