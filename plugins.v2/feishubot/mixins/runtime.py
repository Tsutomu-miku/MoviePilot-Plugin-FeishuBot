"""Feishu bot runtime and websocket lifecycle helpers."""

import json as _json
import threading
import time as _time
from datetime import datetime
from typing import Any, Dict, List

from app.log import logger

from ..ai.llm import DEFAULT_MODEL
from ..ai.types import ChatState
from ..feishu_api import _FeishuAPI
from ..utils import _HAS_LARK_SDK, EventDispatcherHandler, LarkWSClient, lark


class FeishuRuntimeMixin:
        def init_plugin(self, config: dict = None):
            logger.info(
                f"飞书机器人插件初始化 v{self.plugin_version} "
                f"inst={id(self):#x}, keys={list(config.keys()) if config else 'None'}"
            )
            if config:
                self._enabled = config.get("enabled", False)
                self._app_id = config.get("app_id", "")
                self._app_secret = config.get("app_secret", "")
                self._chat_id = config.get("chat_id", "") or config.get("default_chat_id", "")
                self._msgtypes = config.get("msgtypes") or []

                # WebSocket 长连接开关（默认开启）
                ws_raw = config.get("use_ws")
                if ws_raw is None:
                    self._use_ws = True
                elif isinstance(ws_raw, bool):
                    self._use_ws = ws_raw
                elif isinstance(ws_raw, str):
                    self._use_ws = ws_raw.lower() in ("true", "1", "yes", "on")
                else:
                    self._use_ws = bool(ws_raw)

                self._llm_enabled = self._parse_bool_config(config.get("llm_enabled"), False)
                self._openrouter_key = str(config.get("openrouter_key", "") or "").strip()
                self._openrouter_free_model = str(
                    config.get("openrouter_free_model", "") or ""
                ).strip() or DEFAULT_MODEL
                self._openrouter_model = str(config.get("openrouter_model", "") or "").strip()
                self._openrouter_fallback_models = self._parse_str_list_config(
                    config.get("openrouter_fallback_models")
                )
                self._openrouter_auto_fallback = self._parse_bool_config(
                    config.get("openrouter_auto_fallback"), True
                )

            self._feishu = _FeishuAPI(self._app_id, self._app_secret)
            self._engines = {}
            self._shared_state = ChatState()
            self._engine_pool_lock = threading.Lock()
            self._recent_actions = {}
            self._recent_actions_lock = threading.Lock()
            self._seen_msg_ids = {}
            self._seen_msg_ids_lock = threading.Lock()
            self._global_processing_lock = threading.Lock()
            self._global_processing = False
            self._init_ts = datetime.now()
            self._feishu_ok = False
            self._msg_count = 0
            self._agent_count = 0
            self._legacy_count = 0
            self._recover_count = 0
            self._ws_connected = False

            # 验证飞书 Token 连通性
            if self._app_id and self._app_secret:
                try:
                    token = self._feishu._get_token()
                    if token:
                        self._feishu_ok = True
                        logger.info(f"飞书 Token 获取成功 ✓ (token={token[:8]}...)")
                    else:
                        logger.warning("飞书 Token 获取返回空值 ✗")
                except Exception as e:
                    logger.error(f"飞书 Token 获取失败 ✗: {e}")
            else:
                logger.warning("飞书 App ID / App Secret 未配置")

            logger.info(
                f"飞书配置: enabled={self._enabled}, llm_enabled={self._llm_enabled}, "
                f"use_ws={self._use_ws}, lark_sdk={'✓' if _HAS_LARK_SDK else '✗'}, "
                f"api_key={'已配置' if self._openrouter_key else '未配置'}, "
                f"ai_models={' -> '.join(self._get_ai_model_chain())}"
            )

            if self._llm_enabled and self._openrouter_key:
                try:
                    logger.info(
                        f"飞书 Agent 模式已启用 ✓ inst={id(self):#x}, "
                        f"模型链: {' -> '.join(self._get_ai_model_chain())}"
                    )
                except Exception as e:
                    logger.error(f"飞书 Agent 初始化失败: {e}", exc_info=True)
            elif self._llm_enabled:
                logger.warning("飞书 AI Agent 已启用但 API Key 未配置，回退到传统模式")
            else:
                logger.info(f"飞书传统模式（AI Agent 未启用）inst={id(self):#x}")

            # ── 启动 WebSocket 长连接 ──
            if self._enabled and self._use_ws and self._app_id and self._app_secret:
                self._start_ws_client()

        def get_state(self) -> bool:
            return self._enabled

        @staticmethod
        def get_command() -> List[Dict[str, Any]]:
            return []

        def stop_service(self):
            """清理运行时资源，防止插件重载时 '占用' 冲突"""
            logger.warning(
                f"飞书机器人 stop_service v{self.plugin_version} "
                f"inst={id(self):#x}, "
                f"engine={'有' if self._engines else '无'}, "
                f"feishu={'有' if self._feishu else '无'}, "
                f"ws_running={self._ws_running}, "
                f"msgs={getattr(self, '_msg_count', '?')}, "
                f"agents={getattr(self, '_agent_count', '?')}, "
                f"recovers={getattr(self, '_recover_count', '?')}"
            )

            self._stop_ws_client()

            if self._engines:
                for engine in self._engines.values():
                    engine.reset()
            self._engines = None
            self._shared_state = None
            self._engine_pool_lock = None
            self._recent_actions = None
            self._recent_actions_lock = None
            self._feishu = None
            self._seen_msg_ids = None
            self._seen_msg_ids_lock = None
            self._global_processing_lock = None
            self._global_processing = False

        def _start_ws_client(self):
            """启动飞书 WebSocket 长连接"""
            if not _HAS_LARK_SDK:
                logger.error(
                    "lark-oapi SDK 未安装，无法使用 WebSocket 长连接！"
                    "请在 MoviePilot 容器中执行: pip install lark-oapi 。"
                    "或者关闭 WebSocket 模式，使用 HTTP 回调方式（需公网 IP）。"
                )
                return

            if self._ws_running:
                logger.warning("WebSocket 长连接已在运行中，跳过重复启动")
                return

            try:
                event_handler = self._build_event_handler()
                self._ws_client = LarkWSClient(
                    self._app_id,
                    self._app_secret,
                    event_handler=event_handler,
                    log_level=lark.LogLevel.INFO,
                )

                self._ws_running = True
                self._ws_thread = threading.Thread(
                    target=self._ws_run_loop,
                    name="feishu-ws-client",
                    daemon=True,
                )
                self._ws_thread.start()

                logger.info(
                    f"飞书 WebSocket 长连接启动中... "
                    f"inst={id(self):#x}, lark-oapi SDK ✓"
                )
            except Exception as e:
                logger.error(f"飞书 WebSocket 长连接启动失败: {e}", exc_info=True)
                self._ws_running = False

        def _ws_run_loop(self):
            """在后台线程中运行 WebSocket 客户端（带自动重连）"""
            import asyncio

            while self._ws_running:
                new_loop = None
                try:
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)

                    import lark_oapi.ws.client as _ws_mod
                    _ws_mod.loop = new_loop

                    logger.info("飞书 WebSocket 长连接线程启动")
                    self._ws_connected = True
                    self._ws_client.start()
                except Exception as e:
                    logger.error(f"飞书 WebSocket 长连接异常退出: {e}", exc_info=True)
                finally:
                    self._ws_connected = False
                    if new_loop is not None:
                        try:
                            new_loop.close()
                        except Exception:
                            pass

                if self._ws_running:
                    logger.warning("飞书 WebSocket 长连接断开，10 秒后尝试重连...")
                    _time.sleep(10)

                    try:
                        event_handler = self._build_event_handler()
                        self._ws_client = LarkWSClient(
                            self._app_id,
                            self._app_secret,
                            event_handler=event_handler,
                            log_level=lark.LogLevel.INFO,
                        )
                    except Exception as e:
                        logger.error(f"WebSocket 客户端重建失败: {e}", exc_info=True)
                        _time.sleep(30)

            logger.info("飞书 WebSocket 长连接线程已退出")

        def _build_event_handler(self) -> "EventDispatcherHandler":
            """构建 lark-oapi 事件分发处理器"""
            plugin = self

            def on_message_receive(data):
                try:
                    logger.info(
                        f"[WS] 收到消息事件 v{plugin.plugin_version}, "
                        f"inst={id(plugin):#x}"
                    )
                    event_data = _json.loads(lark.JSON.marshal(data))
                    event = event_data.get("event", {})
                    if not event:
                        logger.warning("[WS] 消息事件缺少 event 字段")
                        return
                    plugin._ensure_runtime_ready()
                    threading.Thread(
                        target=plugin._handle_message,
                        args=(event,),
                        daemon=True,
                    ).start()
                except Exception as e:
                    logger.error(f"[WS] 消息事件处理异常: {e}", exc_info=True)

            handler = EventDispatcherHandler.builder("", "") \
                .register_p2_im_message_receive_v1(on_message_receive) \
                .build()

            return handler

        def _stop_ws_client(self):
            """停止 WebSocket 长连接"""
            self._ws_running = False
            self._ws_connected = False

            if self._ws_client is not None:
                try:
                    logger.info("正在停止飞书 WebSocket 长连接...")
                except Exception as e:
                    logger.error(f"停止 WebSocket 异常: {e}")
                finally:
                    self._ws_client = None

            if self._ws_thread is not None:
                try:
                    self._ws_thread.join(timeout=5)
                except Exception:
                    pass
                self._ws_thread = None

            logger.info("飞书 WebSocket 长连接已停止")

        def _ensure_runtime_ready(self):
            """惰性恢复运行时对象"""
            recovered = []

            if self._feishu is None and self._app_id:
                self._feishu = _FeishuAPI(self._app_id, self._app_secret)
                recovered.append("feishu")

            if self._seen_msg_ids is None:
                self._seen_msg_ids = {}
            if self._seen_msg_ids_lock is None:
                self._seen_msg_ids_lock = threading.Lock()
                recovered.append("seen_msg_ids_lock")

            if self._shared_state is None:
                self._shared_state = ChatState()
                recovered.append("shared_state")

            if self._recent_actions is None:
                self._recent_actions = {}
                recovered.append("recent_actions")
            if self._recent_actions_lock is None:
                self._recent_actions_lock = threading.Lock()
                recovered.append("recent_actions_lock")
            if self._global_processing_lock is None:
                self._global_processing_lock = threading.Lock()
                recovered.append("global_processing_lock")

            if self._llm_enabled and self._openrouter_key:
                if self._engines is None:
                    self._engines = {}
                    recovered.append("engines")
                if self._engine_pool_lock is None:
                    self._engine_pool_lock = threading.Lock()
                    recovered.append("engine_pool_lock")

            if recovered:
                try:
                    self._recover_count = getattr(self, "_recover_count", 0) + 1
                except Exception:
                    pass
                logger.warning(
                    f"飞书运行时对象已自动恢复 (第{self._recover_count}次): "
                    f"inst={id(self):#x}, {recovered}"
                )

        def _try_acquire_global_processing(self, session_key: str, msg_id: str) -> bool:
            if self._global_processing_lock is None:
                self._global_processing_lock = threading.Lock()

            with self._global_processing_lock:
                if self._global_processing:
                    logger.info(
                        f"[Busy] 丢弃新消息: session={session_key}, msg_id={msg_id or '-'}"
                    )
                    return False
                self._global_processing = True
                logger.info(
                    f"[Busy] 开始处理: session={session_key}, msg_id={msg_id or '-'}"
                )
                return True

        def _release_global_processing(self, session_key: str = "", msg_id: str = ""):
            if self._global_processing_lock is None:
                self._global_processing = False
                return

            with self._global_processing_lock:
                self._global_processing = False
                logger.info(
                    f"[Busy] 结束处理: session={session_key or '-'}, msg_id={msg_id or '-'}"
                )
