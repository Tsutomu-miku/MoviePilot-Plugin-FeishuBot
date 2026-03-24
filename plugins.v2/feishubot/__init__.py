"""
飞书机器人双向通信插件（长连接版 v2.2.0）
使用飞书官方 WebSocket 长连接，无需公网 IP / Webhook
内置 Protobuf 解析器，无需额外安装 protobuf 依赖
修复 SDK 模式下 "event loop is already running" 问题
"""
import asyncio
import json
import re
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
# ==================================================================
class _MiniProtobuf:
    """
    极简 Protobuf 解析器，仅支持飞书 Frame 结构
    """

    @staticmethod
    def _read_varint(data: bytes, pos: int) -> Tuple[int, int]:
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
        length, pos = _MiniProtobuf._read_varint(data, pos)
        end = pos + length
        if end > len(data):
            raise ValueError(f"length-delimited 越界")
        return data[pos:end], end

    @staticmethod
    def parse_frame(data: bytes) -> dict:
        """解析飞书 Frame protobuf"""
        frame = {
            "SeqID": 0, "LogID": 0, "service": 0, "method": 0,
            "headers": [], "payload": b""
        }
        pos = 0
        while pos < len(data):
            tag, pos = _MiniProtobuf._read_varint(data, pos)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if wire_type == 0:  # varint
                value, pos = _MiniProtobuf._read_varint(data, pos)
                if field_number == 1: frame["SeqID"] = value
                elif field_number == 2: frame["LogID"] = value
                elif field_number == 3: frame["service"] = value
                elif field_number == 4: frame["method"] = value
            elif wire_type == 2:  # length-delimited
                raw, pos = _MiniProtobuf._read_length_delimited(data, pos)
                if field_number == 5:
                    frame["headers"].append(_MiniProtobuf._parse_header(raw))
                elif field_number == 8:
                    frame["payload"] = raw
                # 其他 length-delimited 字段直接跳过
            elif wire_type == 1: pos += 8  # 64-bit
            elif wire_type == 5: pos += 4  # 32-bit
            else: break
        return frame

    @staticmethod
    def _parse_header(data: bytes) -> dict:
        header = {"key": "", "value": ""}
        pos = 0
        while pos < len(data):
            tag, pos = _MiniProtobuf._read_varint(data, pos)
            fn = tag >> 3
            wt = tag & 0x07
            if wt == 2:
                raw, pos = _MiniProtobuf._read_length_delimited(data, pos)
                if fn == 1: header["key"] = raw.decode("utf-8", errors="replace")
                elif fn == 2: header["value"] = raw.decode("utf-8", errors="replace")
            elif wt == 0:
                _, pos = _MiniProtobuf._read_varint(data, pos)
            else: break
        return header

    @staticmethod
    def build_ping_frame(service_id: int) -> bytes:
        parts = [b'\x08\x00', b'\x10\x00', b'\x18']
        parts.append(_MiniProtobuf._encode_varint(service_id))
        parts.append(b'\x20\x00')
        hp = _MiniProtobuf._encode_header("type", "ping")
        parts.append(b'\x2a')
        parts.append(_MiniProtobuf._encode_varint(len(hp)))
        parts.append(hp)
        return b''.join(parts)

    @staticmethod
    def build_ack_frame(original_frame: dict, code: int = 200) -> bytes:
        parts = []
        parts.append(b'\x08'); parts.append(_MiniProtobuf._encode_varint(original_frame.get("SeqID", 0)))
        parts.append(b'\x10'); parts.append(_MiniProtobuf._encode_varint(original_frame.get("LogID", 0)))
        parts.append(b'\x18'); parts.append(_MiniProtobuf._encode_varint(original_frame.get("service", 0)))
        parts.append(b'\x20\x01')
        for h in original_frame.get("headers", []):
            hp = _MiniProtobuf._encode_header(h["key"], h["value"])
            parts.append(b'\x2a')
            parts.append(_MiniProtobuf._encode_varint(len(hp)))
            parts.append(hp)
        resp_json = json.dumps({"code": code}).encode("utf-8")
        parts.append(b'\x42')
        parts.append(_MiniProtobuf._encode_varint(len(resp_json)))
        parts.append(resp_json)
        return b''.join(parts)

    @staticmethod
    def _encode_varint(value: int) -> bytes:
        parts = []
        while value > 0x7F:
            parts.append((value & 0x7F) | 0x80)
            value >>= 7
        parts.append(value & 0x7F)
        return bytes(parts)

    @staticmethod
    def _encode_header(key: str, value: str) -> bytes:
        parts = []
        kb = key.encode("utf-8")
        parts.append(b'\x0a'); parts.append(_MiniProtobuf._encode_varint(len(kb))); parts.append(kb)
        vb = value.encode("utf-8")
        parts.append(b'\x12'); parts.append(_MiniProtobuf._encode_varint(len(vb))); parts.append(vb)
        return b''.join(parts)


def _get_header(headers: list, key: str) -> str:
    for h in headers:
        if h.get("key") == key:
            return h.get("value", "")
    return ""


# ==================================================================
#  主插件类
# ==================================================================
class FeishuBot(_PluginBase):
    """飞书机器人双向通信插件"""

    plugin_name = "飞书机器人"
    plugin_desc = "飞书机器人双向通信插件，基于长连接无需公网IP，支持影视搜索、订阅下载与消息交互"
    plugin_icon = "feishu.png"
    plugin_version = "2.2.0"
    plugin_author = "MoviePilot-Community"
    plugin_config_prefix = "feishubot_"
    plugin_order = 20
    auth_level = 1

    _enabled: bool = False
    _app_id: str = ""
    _app_secret: str = ""
    _default_chat_id: str = ""
    _use_lark: bool = False
    _msgtypes: list = []

    _tenant_access_token: str = ""
    _token_expires_at: float = 0
    _token_lock = threading.Lock()

    _ws_thread: Optional[threading.Thread] = None
    _ws_running: bool = False
    _ws_service_id: int = 0

    _msg_cache: Dict[str, dict] = {}
    _msg_cache_lock = threading.Lock()

    _base_url: str = ""
    _search_cache: Dict[str, List[Any]] = {}

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._app_id = config.get("app_id", "")
            self._app_secret = config.get("app_secret", "")
            self._default_chat_id = config.get("default_chat_id", "")
            self._use_lark = config.get("use_lark", False)
            self._msgtypes = config.get("msgtypes", [])

        self._base_url = "https://open.larksuite.com" if self._use_lark else "https://open.feishu.cn"
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
        return [{
            "path": "/feishu/status", "endpoint": self.api_status,
            "methods": ["GET"], "summary": "飞书机器人连接状态",
        }]

    def api_status(self, **kwargs) -> dict:
        return {
            "enabled": self._enabled,
            "connected": self._ws_running and self._ws_thread and self._ws_thread.is_alive(),
            "mode": "websocket",
        }

    def get_service(self) -> List[Dict[str, Any]]:
        pass

    def stop_service(self):
        self._ws_running = False
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)
        self._ws_thread = None
        self._search_cache.clear()
        self._msg_cache.clear()

    # ============================================================
    #  配置表单
    # ============================================================
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        MsgTypeOptions = [{"title": item.value, "value": item.name} for item in NotificationType]
        return [
            {
                "component": "VForm",
                "content": [
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VSwitch", "props": {"model": "use_lark", "label": "国际版 (Lark)"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "app_id", "label": "App ID",
                                "placeholder": "飞书应用的 App ID"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "app_secret", "label": "App Secret",
                                "type": "password", "placeholder": "飞书应用的 App Secret"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "default_chat_id",
                                "label": "默认会话 ID（可选）",
                                "placeholder": "留空自动获取首次对话的 chat_id"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VSelect", "props": {"model": "msgtypes", "label": "消息类型",
                                "items": MsgTypeOptions, "multiple": True, "chips": True,
                                "clearable": True, "placeholder": "选择推送的通知类型，留空全部推送"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                "text": "本插件使用飞书 WebSocket 长连接，无需公网 IP。\n\n"
                                        "配置步骤：\n"
                                        "1. 飞书开放平台创建企业自建应用，获取 App ID / Secret\n"
                                        "2. 事件与回调 → 选择「使用长连接接收」\n"
                                        "3. 订阅 im.message.receive_v1 事件\n"
                                        "4. 权限管理 → 开通 im:message 和 im:message:send_as_bot\n"
                                        "5. 发布应用版本 → 启用本插件\n\n"
                                        "可选：docker exec -it moviepilot pip install lark-oapi（安装官方SDK）"}}]},
                    ]},
                ],
            }
        ], {
            "enabled": False, "app_id": "", "app_secret": "",
            "default_chat_id": "", "use_lark": False, "msgtypes": [],
        }

    def get_page(self) -> List[dict]:
        connected = self._ws_running and self._ws_thread and self._ws_thread.is_alive()
        return [{"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
            {"component": "VAlert", "props": {
                "type": "success" if connected else "warning", "variant": "tonal",
                "text": f"{'\u2705 \u957f\u8fde\u63a5\u5df2\u5efa\u7acb' if connected else '\u274c \u672a\u8fde\u63a5'}\n\n"
                        "命令: /search 搜索 | /subscribe 订阅 | /downloading 下载中 | /help 帮助\n"
                        "直接发送影视名称也可搜索"}}]}]}]

    # ============================================================
    #  WebSocket 长连接管理
    # ============================================================
    def _start_ws_client(self):
        """启动 WebSocket 长连接"""
        self._ws_running = True
        self._ws_thread = threading.Thread(
            target=self._ws_entry_point, daemon=True, name="feishu-ws",
        )
        self._ws_thread.start()

    def _ws_entry_point(self):
        """
        WebSocket 线程入口 —— 在全新的事件循环中运行
        解决 "This event loop is already running" 问题:
        lark-oapi SDK 使用模块级 loop = asyncio.get_event_loop()
        在 import 时会抓到 FastAPI(uvicorn) 的主事件循环，
        然后调 loop.run_until_complete() 就会冲突。
        所以必须在 import SDK 之前就为当前线程设置好新的事件循环。
        """
        # ★ 关键: 先创建全新事件循环，再 import SDK
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)

        try:
            self._try_sdk_mode(new_loop)
        except ImportError:
            logger.info("未安装 lark-oapi，使用内置 WebSocket 模式")
            self._builtin_ws_loop()
        except Exception as e:
            logger.warning(f"SDK 模式启动失败 ({e})，回退到内置模式")
            self._builtin_ws_loop()
        finally:
            try:
                new_loop.close()
            except Exception:
                pass

    def _try_sdk_mode(self, loop: asyncio.AbstractEventLoop):
        """
        在当前线程的新事件循环中启动 SDK
        """
        # ★ 在新事件循环已设置的上下文中 import SDK
        # 这样 SDK 模块级的 asyncio.get_event_loop() 拿到的是新 loop
        import importlib

        # 如果 lark_oapi.ws.client 之前被 import 过，
        # 它的模块级 loop 变量已经指向了旧的 FastAPI 事件循环。
        # 需要 reload 让它重新执行 loop = asyncio.get_event_loop()
        try:
            import lark_oapi.ws.client as ws_client_module
            # 强制模块重新获取当前线程的事件循环
            ws_client_module.loop = loop
            logger.debug("飞书: 已将 SDK 的事件循环替换为新 loop")
        except (ImportError, AttributeError):
            pass

        import lark_oapi as lark

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_sdk_message)
            .build()
        )
        domain = lark.LARK_DOMAIN if self._use_lark else lark.FEISHU_DOMAIN
        client = lark.ws.Client(
            self._app_id, self._app_secret,
            event_handler=event_handler,
            domain=domain, log_level=lark.LogLevel.INFO,
        )
        logger.info("飞书机器人长连接已启动 (lark-oapi SDK 模式)")
        client.start()  # 阻塞直到断开

    def _on_sdk_message(self, data) -> None:
        """SDK 回调 (在 SDK 的事件循环线程中执行)"""
        try:
            import lark_oapi as lark
            raw = lark.JSON.marshal(data)
            body = json.loads(raw) if isinstance(raw, str) else raw
            threading.Thread(
                target=self._handle_message_event, args=(body,), daemon=True
            ).start()
        except Exception as e:
            logger.error(f"飞书 SDK 消息处理异常: {e}", exc_info=True)

    # ---------- 内置 WebSocket 模式 ----------
    def _builtin_ws_loop(self):
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
        import websocket
        from urllib.parse import urlparse, parse_qs

        endpoint_url = f"{self._base_url}/callback/ws/endpoint"
        logger.info("飞书: 获取 WebSocket 地址...")
        resp = requests.post(
            endpoint_url, headers={"locale": "zh"},
            json={"AppID": self._app_id, "AppSecret": self._app_secret},
            timeout=30,
        )
        result = resp.json()
        if result.get("code") != 0:
            raise Exception(f"获取 WebSocket 地址失败: code={result.get('code')}, msg={result.get('msg')}")

        ws_url = result["data"]["URL"]
        client_config = result["data"].get("ClientConfig", {})
        ping_interval = client_config.get("PingInterval", 120)

        parsed = urlparse(ws_url)
        qs = parse_qs(parsed.query)
        self._ws_service_id = int(qs.get("service_id", [0])[0])
        conn_id = qs.get("device_id", [""])[0]
        logger.info(f"飞书: 连接中 (conn={conn_id[:16]}...)")

        ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_builtin_ws_message,
            on_error=lambda ws, err: logger.error(f"飞书 WS 错误: {err}"),
            on_close=lambda ws, code, reason: logger.warning(f"飞书 WS 断开: {code}, {reason}"),
            on_open=lambda ws: logger.info("飞书 WebSocket 连接成功 ✓"),
        )
        self._ws_obj = ws

        ping_stop = threading.Event()
        def ping_loop():
            while not ping_stop.is_set() and self._ws_running:
                try:
                    ws.send(_MiniProtobuf.build_ping_frame(self._ws_service_id), opcode=0x2)
                except Exception:
                    break
                ping_stop.wait(ping_interval)

        threading.Thread(target=ping_loop, daemon=True).start()
        try:
            ws.run_forever(ping_interval=0)
        finally:
            ping_stop.set()
            self._ws_obj = None

    def _on_builtin_ws_message(self, ws, message):
        try:
            if not isinstance(message, bytes):
                message = message.encode("utf-8") if isinstance(message, str) else message

            frame = _MiniProtobuf.parse_frame(message)
            method = frame.get("method", 0)
            headers = frame.get("headers", [])
            msg_type = _get_header(headers, "type")

            if method == 0:  # CONTROL (ping/pong)
                return

            if method != 1:  # 只处理 DATA 帧
                return

            msg_id = _get_header(headers, "message_id")
            total = int(_get_header(headers, "sum") or "1")
            seq = int(_get_header(headers, "seq") or "0")
            payload = frame.get("payload", b"")

            if total > 1:
                payload = self._combine_chunks(msg_id, total, seq, payload)
                if payload is None:
                    return

            # ACK
            try:
                ws.send(_MiniProtobuf.build_ack_frame(frame, 200), opcode=0x2)
            except Exception:
                pass

            if not payload:
                return

            event_body = json.loads(payload.decode("utf-8"))
            event_type = event_body.get("header", {}).get("event_type", "")

            if event_type == "im.message.receive_v1":
                threading.Thread(
                    target=self._handle_message_event, args=(event_body,), daemon=True
                ).start()

        except Exception as e:
            logger.error(f"飞书 WS 消息处理异常: {e}", exc_info=True)

    def _combine_chunks(self, msg_id: str, total: int, seq: int, data: bytes) -> Optional[bytes]:
        with self._msg_cache_lock:
            if msg_id not in self._msg_cache:
                self._msg_cache[msg_id] = {"total": total, "parts": {}, "time": time.time()}
            cache = self._msg_cache[msg_id]
            cache["parts"][seq] = data
            if len(cache["parts"]) < total:
                return None
            combined = b"".join(cache["parts"].get(i, b"") for i in range(total))
            del self._msg_cache[msg_id]
            now = time.time()
            for k in [k for k, v in self._msg_cache.items() if now - v["time"] > 30]:
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
                resp = requests.post(
                    f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": self._app_id, "app_secret": self._app_secret}, timeout=10)
                data = resp.json()
                if data.get("code") == 0:
                    self._tenant_access_token = data["tenant_access_token"]
                    self._token_expires_at = time.time() + data.get("expire", 7200)
                    return self._tenant_access_token
                logger.error(f"飞书 token 失败: {data}")
            except Exception as e:
                logger.error(f"飞书 token 异常: {e}")
            return None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_tenant_token()}", "Content-Type": "application/json"}

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
            payload = {"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text})}
            params = {"receive_id_type": "chat_id"}
        try:
            resp = requests.post(url, params=params, headers=self._headers(), json=payload, timeout=10)
            result = resp.json()
            if result.get("code") != 0:
                logger.error(f"飞书发消息失败: {result}")
        except Exception as e:
            logger.error(f"飞书发消息异常: {e}")

    def _send_card(self, chat_id: str, card: dict, msg_id: str = None):
        if msg_id:
            url = f"{self._base_url}/open-apis/im/v1/messages/{msg_id}/reply"
            payload = {"msg_type": "interactive", "content": json.dumps(card)}
            params = None
        else:
            url = f"{self._base_url}/open-apis/im/v1/messages"
            payload = {"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card)}
            params = {"receive_id_type": "chat_id"}
        try:
            resp = requests.post(url, params=params, headers=self._headers(), json=payload, timeout=10)
            result = resp.json()
            if result.get("code") != 0:
                logger.error(f"飞书发卡片失败: {result}")
        except Exception as e:
            logger.error(f"飞书发卡片异常: {e}")

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

            if chat_id and not self._default_chat_id:
                self._default_chat_id = chat_id
                logger.info(f"飞书: 自动记录会话 ID: {chat_id}")
                self._save_chat_id(chat_id)

            if msg_type != "text":
                self._send_text(chat_id, "暂只支持文本消息，请直接输入影视名称搜索 🎬", msg_id)
                return

            content = json.loads(message.get("content", "{}"))
            text = content.get("text", "").strip()
            for m in message.get("mentions", []):
                key = m.get("key", "")
                if key: text = text.replace(key, "").strip()
            if not text:
                return

            logger.info(f"飞书收到: user={user_id}, text={text}")

            if text.startswith("/"):
                self._dispatch_command(text, chat_id, msg_id, user_id)
            else:
                # 快捷指令
                sub_m = re.match(r'^\u8ba2\u9605\s*(\d+)$', text)
                dl_m = re.match(r'^\u4e0b\u8f7d\s*(\d+)$', text)
                sel_m = re.match(r'^\u9009\u62e9\s*(\d+)$', text)
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
        except Exception:
            pass

    def _dispatch_command(self, text: str, chat_id: str, msg_id: str, user_id: str):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/search", "/s", "/\u641c\u7d22"):
            self._cmd_search(args, chat_id, msg_id, user_id) if args else \
                self._send_text(chat_id, "请输入关键词，如: /search 三体", msg_id)
        elif cmd in ("/subscribe", "/sub", "/\u8ba2\u9605"):
            self._cmd_subscribe(args, chat_id, msg_id, user_id) if args else \
                self._send_text(chat_id, "请输入内容，如: /subscribe 三体", msg_id)
        elif cmd in ("/downloading", "/dl", "/\u4e0b\u8f7d\u4e2d"):
            self._cmd_downloading(chat_id, msg_id)
        elif cmd in ("/help", "/h", "/\u5e2e\u52a9"):
            self._cmd_help(chat_id, msg_id)
        else:
            self._send_text(chat_id, f"未知命令: {cmd}\n输入 /help 查看帮助", msg_id)

    # ============================================================
    #  命令实现
    # ============================================================
    def _cmd_help(self, chat_id: str, msg_id: str):
        card = {
            "header": {"title": {"tag": "plain_text", "content": "🎬 MoviePilot 飞书机器人"}, "template": "blue"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content":
                "**支持的命令：**\n\n"
                "🔍 `/search <\u5173\u952e\u8bcd>` \u2014 搜索影视\n"
                "📥 `/subscribe <\u5173\u952e\u8bcd>` \u2014 订阅影视\n"
                "\u2b07\ufe0f `/downloading` \u2014 查看下载中\n"
                "\u2753 `/help` \u2014 帮助\n\n"
                "**快捷操作：**\n"
                "直接发送影视名称 \u2192 搜索\n"
                "回复 `\u8ba2\u96051` \u2192 订阅搜索结果第1项\n"
                "回复 `\u4e0b\u8f7d1` \u2192 搜索第1项的资源\n"
                "回复 `\u9009\u62e91` \u2192 下载资源列表第1项\n\n"
                "📡 WebSocket 长连接 \u00b7 无需公网IP"
            }}],
        }
        self._send_card(chat_id, card, msg_id)

    def _cmd_search(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        self._send_text(chat_id, f"🔍 正在搜索: {keyword} ...", msg_id)
        try:
            from app.chain.media import MediaChain
            mc = MediaChain()
            meta = mc.recognize_by_meta(keyword)
            medias = [meta] if meta and meta.tmdb_info else mc.search(title=keyword)
            if not medias:
                self._send_text(chat_id, f"😔 未找到: {keyword}"); return
            medias = medias[:6]
            self._search_cache[user_id] = medias
            self._send_card(chat_id, self._build_search_card(medias, keyword))
        except Exception as e:
            logger.error(f"飞书搜索异常: {e}", exc_info=True)
            self._send_text(chat_id, f"\u26a0\ufe0f 搜索出错: {e}")

    def _build_search_card(self, medias: list, keyword: str) -> dict:
        elements = []
        for i, media in enumerate(medias[:6]):
            title = getattr(media, "title", "") or "未知"
            year = getattr(media, "year", "")
            rating = getattr(media, "vote_average", "")
            mtype = "电影" if getattr(media, "type", None) == MediaType.MOVIE else "电视剧"
            overview = (getattr(media, "overview", "") or "")[:100]
            if len(overview) == 100: overview += "..."

            line = f"**{i+1}. {title}**"
            if year: line += f" ({year})"
            line += f"  [{mtype}]"
            if rating: line += f"  \u2b50 {rating}"
            if overview: line += f"\n{overview}"

            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": f"\ud83d\udc49 回复 `\u8ba2\u9605{i+1}` 订阅 | 回复 `\u4e0b\u8f7d{i+1}` 搜索资源"}})
            elements.append({"tag": "hr"})
        return {
            "header": {"title": {"tag": "plain_text", "content": f"🎬 搜索结果: {keyword}"}, "template": "blue"},
            "elements": elements,
        }

    def _cmd_subscribe(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        self._send_text(chat_id, f"📥 正在订阅: {keyword} ...", msg_id)
        try:
            from app.chain.media import MediaChain
            meta = MediaChain().recognize_by_meta(keyword)
            if not meta or not meta.tmdb_info:
                self._send_text(chat_id, f"\u274c 未识别到: {keyword}"); return
            self._subscribe_media(meta, chat_id, msg_id)
        except Exception as e:
            logger.error(f"飞书订阅异常: {e}", exc_info=True)
            self._send_text(chat_id, f"\u26a0\ufe0f 订阅出错: {e}")

    def _subscribe_media(self, media, chat_id: str, msg_id: str = None):
        try:
            from app.chain.subscribe import SubscribeChain
            title = getattr(media, "title", "未知")
            year = getattr(media, "year", "")
            tmdb_id = getattr(media, "tmdb_id", None)
            mtype = getattr(media, "type", MediaType.MOVIE)
            sid, msg = SubscribeChain().add(title=title, year=year, mtype=mtype, tmdbid=tmdb_id, userid="feishu")
            if sid:
                mt = "电影" if mtype == MediaType.MOVIE else "电视剧"
                card = {
                    "header": {"title": {"tag": "plain_text", "content": "\u2705 订阅成功"}, "template": "green"},
                    "elements": [{"tag": "div", "text": {"tag": "lark_md",
                        "content": f"**{title}** ({year})\n类型: {mt} | TMDB: {tmdb_id}\n系统将自动搜索下载 🎉"}}],
                }
                self._send_card(chat_id, card)
            else:
                self._send_text(chat_id, f"\u26a0\ufe0f 订阅失败: {msg or '未知原因'}")
        except Exception as e:
            self._send_text(chat_id, f"\u26a0\ufe0f 订阅出错: {e}")

    def _cmd_downloading(self, chat_id: str, msg_id: str):
        try:
            from app.chain.download import DownloadChain
            downloads = DownloadChain().downloading()
            if not downloads:
                self._send_text(chat_id, "📭 当前没有正在下载的任务", msg_id); return
            elements = []
            for dl in downloads[:10]:
                title = getattr(dl, "title", "未知")
                progress = getattr(dl, "progress", 0)
                speed = getattr(dl, "dlspeed", "")
                size = getattr(dl, "size", "")
                line = f"**{title}**\n"
                if progress is not None:
                    f = int(progress / 10)
                    line += f"{'\u2593'*f}{'\u2591'*(10-f)} {progress:.1f}%  "
                if speed: line += f"速度: {speed}  "
                if size: line += f"大小: {size}"
                elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
                elements.append({"tag": "hr"})
            self._send_card(chat_id, {
                "header": {"title": {"tag": "plain_text", "content": f"\u2b07\ufe0f 下载中 ({len(downloads)})"}, "template": "wathet"},
                "elements": elements}, msg_id)
        except Exception as e:
            self._send_text(chat_id, f"\u26a0\ufe0f 查询失败: {e}", msg_id)

    # ============================================================
    #  快捷交互
    # ============================================================
    def _quick_subscribe(self, idx: int, chat_id: str, msg_id: str, user_id: str):
        cached = self._search_cache.get(user_id, [])
        if 0 < idx <= len(cached):
            self._subscribe_media(cached[idx-1], chat_id, msg_id)
        else:
            self._send_text(chat_id, f"\u26a0\ufe0f 序号 {idx} 无效，请先搜索", msg_id)

    def _quick_download(self, idx: int, chat_id: str, msg_id: str, user_id: str):
        cached = self._search_cache.get(user_id, [])
        if 0 < idx <= len(cached):
            media = cached[idx-1]
            title = getattr(media, "title", "未知")
            mtype = getattr(media, "type", MediaType.MOVIE)
            self._send_text(chat_id, f"🔍 搜索 {title} 的资源...", msg_id)
            try:
                from app.chain.search import SearchChain
                contexts = SearchChain().search_by_title(title=title, mtype=mtype)
                if not contexts:
                    self._send_text(chat_id, f"😔 未找到 {title} 的资源"); return
                self._search_cache[f"{user_id}_res"] = contexts[:10]
                elements = []
                for i, ctx in enumerate(contexts[:10]):
                    t = ctx.torrent_info
                    line = f"**{i+1}. {getattr(t, 'title', '未知')}**\n"
                    parts = []
                    if getattr(t, 'site_name', ''): parts.append(f"站点: {t.site_name}")
                    if getattr(t, 'size', ''): parts.append(f"大小: {t.size}")
                    if getattr(t, 'seeders', ''): parts.append(f"做种: {t.seeders}")
                    if parts: line += " | ".join(parts)
                    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
                    elements.append({"tag": "hr"})
                elements.append({"tag": "note", "elements": [
                    {"tag": "plain_text", "content": "💡 回复「选择+序号」下载，如: 选择1"}]})
                self._send_card(chat_id, {
                    "header": {"title": {"tag": "plain_text", "content": f"📦 {title} 的资源"}, "template": "green"},
                    "elements": elements})
            except Exception as e:
                self._send_text(chat_id, f"\u26a0\ufe0f 搜索资源出错: {e}")
        else:
            self._send_text(chat_id, f"\u26a0\ufe0f 序号 {idx} 无效，请先搜索", msg_id)

    def _quick_select(self, idx: int, chat_id: str, msg_id: str, user_id: str):
        cached = self._search_cache.get(f"{user_id}_res", [])
        if 0 < idx <= len(cached):
            ctx = cached[idx-1]
            try:
                from app.chain.download import DownloadChain
                title = getattr(ctx.torrent_info, "title", "")
                if DownloadChain().download_single(context=ctx, userid="feishu"):
                    self._send_text(chat_id, f"\u2705 开始下载: {title}")
                else:
                    self._send_text(chat_id, "\u26a0\ufe0f 下载失败，请重试")
            except Exception as e:
                self._send_text(chat_id, f"\u26a0\ufe0f 下载出错: {e}")
        else:
            self._send_text(chat_id, f"\u26a0\ufe0f 序号 {idx} 无效，请先搜索资源", msg_id)

    # ============================================================
    #  系统通知推送
    # ============================================================
    @eventmanager.register(EventType.NoticeMessage)
    def handle_notice(self, event: Event):
        if not self.get_state() or not self._default_chat_id:
            return
        ed = event.event_data or {}
        mt: NotificationType = ed.get("type")
        title = ed.get("title", "")
        text = ed.get("text", "")
        if self._msgtypes and mt and mt.name not in self._msgtypes:
            return
        color = "blue"
        if mt:
            v = mt.value or ""
            if "\u5b8c\u6210" in v or "\u6210\u529f" in v: color = "green"
            elif "\u5931\u8d25" in v or "\u9519\u8bef" in v: color = "red"
        try:
            self._send_card(self._default_chat_id, {
                "header": {"title": {"tag": "plain_text", "content": title or "MoviePilot 通知"}, "template": color},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text or "（无详情）"}}]})
        except Exception as e:
            logger.error(f"飞书通知异常: {e}", exc_info=True)
