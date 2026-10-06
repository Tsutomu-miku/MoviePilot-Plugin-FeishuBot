"""Plugin lifecycle, FIFO dispatch and owned WebSocket transport."""

import copy
import json
import threading
from datetime import datetime
from typing import Any, Dict, List

from app.log import logger

from ..ai.types import ChatState
from ..dispatch import SerialDispatcher
from ..feishu_api import _FeishuAPI
from ..transport import WebSocketTransport
from ..utils import _HAS_LARK_SDK, EventDispatcherHandler, LarkWSClient, lark


class FeishuRuntimeMixin:
    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = dict(config or {})
        self._enabled = self._parse_bool_config(config.get("enabled"))
        self._app_id = str(config.get("app_id") or "").strip()
        self._app_secret = str(config.get("app_secret") or "").strip()
        self._chat_id = str(config.get("chat_id") or config.get("default_chat_id") or "").strip()
        self._msgtypes = config.get("msgtypes") or []
        self._use_ws = self._parse_bool_config(config.get("use_ws"), True)
        self._llm_enabled = self._parse_bool_config(config.get("llm_enabled"))
        self._openrouter_key = str(config.get("openrouter_key") or "").strip()
        # Preserve a previously chosen model once, as an ordinary model ID.
        self._openrouter_model = str(config.get("openrouter_model") or config.get("openrouter_free_model") or "").strip()
        if any(key in config for key in ("openrouter_free_model", "openrouter_fallback_models", "openrouter_auto_fallback")):
            migrated = {key: value for key, value in config.items() if key not in
                        ("openrouter_free_model", "openrouter_fallback_models", "openrouter_auto_fallback")}
            migrated["openrouter_model"] = self._openrouter_model
            if hasattr(self, "update_config"):
                self.update_config(migrated)

        self._generation = getattr(self, "_generation", 0) + 1
        self._stopped = False
        if getattr(self, "_task_context", None) is None:
            self._task_context = threading.local()
        self._engines = {}
        self._session_states = {}
        self._engine_pool_lock = threading.RLock()
        self._recent_actions = {}
        self._recent_actions_lock = threading.Lock()
        self._seen_msg_ids = {}
        self._seen_msg_ids_lock = threading.Lock()
        self._init_ts = datetime.now()
        self._msg_count = self._agent_count = self._legacy_count = self._recover_count = 0
        generation = self._generation
        self._feishu = _FeishuAPI(self._app_id, self._app_secret,
                                  is_active=lambda: self._is_dispatch_active(generation))
        self._feishu_ok = False
        self._dispatcher = SerialDispatcher()
        if self._enabled and self._app_id and self._app_secret:
            self._feishu_ok = bool(self._feishu._get_token())
        logger.info(f"飞书机器人初始化 v{self.plugin_version}, inst={id(self):#x}, model={self._openrouter_model or '未配置'}")
        if self._enabled and self._use_ws and self._app_id and self._app_secret:
            self._start_ws_client()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def _is_dispatch_active(self, generation=None):
        current = getattr(self, "_generation", 0)
        context = getattr(self, "_task_context", None)
        return (not getattr(self, "_stopped", False)
                and (generation is None or generation == current)
                and getattr(context, "generation", current) == current)

    def stop_service(self):
        self._stopped = True
        self._generation = getattr(self, "_generation", 0) + 1
        for engine in (self._engines or {}).values():
            engine.cancel()
        self._stop_ws_client()
        dispatcher = getattr(self, "_dispatcher", None)
        if dispatcher is not None:
            dispatcher.stop()
        self._dispatcher = None
        # Do not reset objects still owned by an in-flight cancelled turn.
        self._engines = {}
        self._session_states = {}
        self._feishu = None

    def _ensure_runtime_ready(self):
        # A callback from a stopped instance must never recreate its runtime.
        return self._is_dispatch_active() and self._dispatcher is not None

    def _enqueue_task(self, function, *args):
        if not self._ensure_runtime_ready():
            return False
        generation = self._generation
        context = self._task_context
        def run():
            context.generation = generation
            try:
                if self._is_dispatch_active(generation):
                    function(*args)
            finally:
                context.__dict__.pop("generation", None)
        return self._dispatcher.submit(run)

    def _enqueue_message(self, event):
        if not self._enabled or not self._ensure_runtime_ready():
            return False
        message = event.get("message", {})
        msg_id = message.get("message_id", "")
        if not self._record_message_once(msg_id):
            return False
        accepted = self._enqueue_task(self._handle_message, copy.deepcopy(event))
        if not accepted:
            with self._seen_msg_ids_lock:
                self._seen_msg_ids.pop(msg_id, None)
            if self._feishu:
                self._feishu.send_text(message.get("chat_id") or self._chat_id,
                                       "消息队列已满，请稍后重试。", reply_msg_id=msg_id)
        return accepted

    def _start_ws_client(self):
        if not _HAS_LARK_SDK:
            logger.error("请安装 lark-oapi 以使用飞书长连接")
            return
        try:
            import lark_oapi.ws.client as sdk_module
            handler = self._build_event_handler()
            transport = WebSocketTransport(
                lambda: LarkWSClient(self._app_id, self._app_secret,
                                     event_handler=handler, log_level=lark.LogLevel.INFO),
                sdk_module,
                on_retire=self.stop_service,
            )
            self._ws_transport = transport
            transport.start()
            self._ws_running = True
        except Exception as exc:
            self._ws_running = False
            logger.error(f"飞书长连接启动失败: {exc}", exc_info=True)

    def _build_event_handler(self):
        generation = self._generation
        def on_message_receive(data):
            if not self._is_dispatch_active(generation):
                return
            try:
                event = json.loads(lark.JSON.marshal(data)).get("event", {})
                if event:
                    self._enqueue_message(event)
            except Exception:
                logger.error("飞书消息回调异常", exc_info=True)
        def on_card_action(data):
            if not self._is_dispatch_active(generation):
                return None
            payload = json.loads(lark.JSON.marshal(data))
            self._handle_card_action(payload)
            # Return the SDK's native callback response, not a raw dict.
            from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
            return P2CardActionTriggerResponse({})
        builder = EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message_receive)
        if hasattr(builder, "register_p2_card_action_trigger"):
            builder = builder.register_p2_card_action_trigger(on_card_action)
        return builder.build()

    def _stop_ws_client(self):
        self._ws_running = False
        transport = getattr(self, "_ws_transport", None)
        if transport is not None:
            transport.stop()
        self._ws_transport = None
