"""
飞书机器人插件 v6.0.2 — ChatEngine 重构 + WebSocket 长连接

更新记录 (v6.0.2):
- **会话隔离修复**: ChatEngine 从全局单实例改为按 `chat_id + user_id` 维度隔离，避免确认消息串会话或上下文丢失
- **队列与并发修复**: 同一会话使用 FIFO 消息队列 + dispatch lock + running 状态，避免旧会话与新消息并发回复
- **确认流程短路**: 待确认下载存在时，"确认/下载吧/取消" 等短回复直接命中状态机，不再依赖 LLM 猜测上下文
- **动作幂等增强**: 为文本确认、卡片确认下载和订阅按钮增加短期幂等保护，降低重复触发导致的重复下载风险
- **运行时治理**: 新增会话 TTL 清理、结构化 trace 日志与状态页会话计数，便于长期运行排障
更新记录 (v5.1.1):
- **消息去重**: 基于 message_id 的幂等处理, 同一消息无论来自 WS 还是 HTTP 回调只处理一次
- **并发竞态修复**: _agent_dispatch 使用 _dispatch_lock 保护 check-then-act 操作
  - 解决 WebSocket + HTTP 双通道同时处理同一消息导致的"双线程回复"问题
  - 修复 _user_locks 非线程安全 dict 操作可能产生多把锁的问题
- **即时反馈增强**: 消息到达即发送"已收到"反馈, 不再等待合并窗口
  - Legacy 模式新增 processing 即时反馈卡片
  - Agent 模式在 dispatch 阶段即发送确认, 合并窗口期间用户可见
- **卡片样式优化**: 更丰富的颜色主题 + 底部快捷操作 + 进度可视化增强

更新记录 (v5.0.0):
- **Agent 并发控制重构**: 用户连续发消息不再触发多轮并行回复
  - 新增消息队列机制: 用户快速发送的多条消息自动合并为一次请求
  - 新增 "打断" 逻辑: 用户在 Agent 处理期间发新消息会标记打断, 当前轮次完成后
    立即使用最新消息重新开始, 而非排队等待
  - 移除旧的 lock.acquire(blocking=False) 拒绝策略
- **即时反馈**: 收到消息后立即发送 "处理中" 卡片, 让用户知道请求已收到
- **飞书卡片全面重构**: 所有输出改用 interactive card, 大幅提升信息密度
  - 搜索结果: 多列布局 + 评分标签 + 操作按钮
  - 资源列表: 标签化展示分辨率/编码/音轨/来源
  - 下载进度: 进度条可视化
  - 状态诊断: 结构化仪表板卡片
  - Agent 最终回复: 带 header 的 markdown 卡片
- **系统提示词优化**: 适配新的卡片输出, Agent 回复更结构化

修复记录 (v4.0.1):
- 修复 WebSocket 长连接因 asyncio event loop 冲突导致无法启动
- MoviePilot (FastAPI/Uvicorn) 主线程已有 event loop，为后台线程创建独立 loop
- 替换 lark-oapi SDK 模块级 event loop 变量解决 "This event loop is already running"

修复记录 (v4.0.0):
- **核心修复**: 新增 WebSocket 长连接收消息，替代已失效的 HTTP 回调方式
- 使用 lark-oapi SDK 的 ws.Client 建立长连接，无需公网 IP/域名
- 插件主动出站连接飞书服务器，NAS/Docker 友好
- 支持自动重连、Protobuf 解析、心跳保活
- 保留 HTTP 回调端点作为备用方案
- 新增 `use_ws` 配置开关（默认开启）

修复记录 (v3.5.0):
- 修复 get_page 使用不支持的 VCard 组件导致 MoviePilot 插件加载失败
- 修复 _handle_message daemon 线程无顶层异常捕获导致静默崩溃
- 所有计数器使用 getattr 安全访问防御旧实例属性缺失
- _feishu_event 入口添加顶层 try/except 防止端点崩溃

修复记录 (v3.4.0):
- 全面增强诊断日志: 所有生命周期方法打印版本号
- 新增插件详情页实时运行状态仪表板 (get_page)
- init_plugin 增加飞书 Token 连通性验证
- _feishu_event 入口添加请求日志
- _handle_message / _agent_handle 日志增加版本标识

修复记录 (v3.3.0):
- 增强诊断日志: 所有关键路径添加 instance id 追踪
- 拆分路由日志避免 MoviePilot UI 截断
- Agent 处理添加耗时统计
- stop_service 日志升级为 warning 级别

修复记录 (v3.1.1):
- 修复 Agent 模式因 stop_service 后运行时对象丢失导致回退传统模式
- 新增 _ensure_runtime_ready 惰性恢复机制防御生命周期异常
- 修复群聊 @提及标记未清理导致消息包含占位符
- stop_service 新增日志输出便于排查生命周期问题

修复记录 (v3.1.0):
- 修复 get_command/get_page 返回 None 导致 MoviePilot 加载异常（插件占用）
- 修复旧配置 default_chat_id 键名不兼容
- 修复搜索结果中字符串对象被误当 MediaInfo 使用（str.title 方法引用 Bug）
- 类级别可变对象改为 None，init_plugin 中实例化

修复记录 (v3.0.1):
- 合并为单文件结构，兼容 MoviePilot 插件动态加载机制
- 修复 Agent 消息格式污染（清洗 API 响应额外字段）
- 修复对话历史在循环中途异常时腐败（副本操作 + 成功后才保存）
- 修复对话截断破坏 tool_call 消息配对（智能寻找安全切点）
- 下载操作增加工具层面强制确认机制（confirmed 参数）
- 修复飞书回复 API 使用方式
- 修复空回复不发送问题
"""


import json as _json
import re
import threading
import time as _time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType
from app.schemas.types import EventType

from .utils import _HAS_LARK_SDK, _extract_tags, lark, LarkWSClient, EventDispatcherHandler
from .feishu_api import _FeishuAPI
from .card_builder import _CardBuilder
from .ai import ChatEngine
from .ai.llm import DEFAULT_FALLBACK_MODELS, DEFAULT_MODEL, FREE_MODEL_CHOICES


class FeishuBot(_PluginBase):
    _SESSION_TTL_SECONDS = 3600
    _ACTION_DEDUPE_TTL_SECONDS = 15
    _DIRECT_CONFIRM_TEXTS = {
        "确认", "确认下载", "确定", "确定下载", "好的", "好", "下载吧", "好下载吧",
        "行", "行吧", "可以", "可以下载", "开始下载", "继续下载", "是", "是的",
    }
    _DIRECT_CANCEL_TEXTS = {
        "取消", "取消下载", "不用了", "先不要", "不要下载", "算了",
    }

    # ── 插件元信息 ──
    plugin_name = "飞书机器人"
    plugin_desc = "飞书群机器人消息通知与交互，支持 AI Agent 智能体模式（WebSocket 长连接）"
    plugin_icon = "Feishu_A.png"
    plugin_version = "6.0.2"
    plugin_author = "Tsutomu-miku"
    author_url = "https://github.com/Tsutomu-miku"
    plugin_config_prefix = "feishubot_"
    plugin_order = 28
    auth_level = 1

    # ── 配置 ──
    _enabled: bool = False
    _app_id: str = ""
    _app_secret: str = ""
    _chat_id: str = ""
    _msgtypes: list = []
    _llm_enabled: bool = False
    _openrouter_key: str = ""
    _openrouter_model: str = ""
    _openrouter_free_model: str = DEFAULT_MODEL
    _openrouter_fallback_models: list = []
    _openrouter_auto_fallback: bool = True
    _use_ws: bool = True

    # ── 运行时 ──
    _feishu: Optional[_FeishuAPI] = None
    _engines: Optional[dict] = None
    _session_running: Optional[dict] = None
    _session_dispatch_locks: Optional[dict] = None
    _engine_pool_lock: Optional[threading.Lock] = None
    _recent_actions: Optional[dict] = None
    _recent_actions_lock: Optional[threading.Lock] = None
    _seen_msg_ids: Optional[dict] = None         # msg_id -> timestamp (消息去重)

    # ── WebSocket 长连接运行时 ──
    _ws_client: Optional[Any] = None
    _ws_thread: Optional[threading.Thread] = None
    _ws_running: bool = False

    @staticmethod
    def _parse_bool_config(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)

    @staticmethod
    def _parse_str_list_config(value: Any) -> List[str]:
        if isinstance(value, list):
            items = value
        elif isinstance(value, str):
            items = re.split(r"[,;\n]", value)
        elif value is None:
            items = []
        else:
            items = [value]

        result = []
        seen = set()
        for item in items:
            normalized = str(item or "").strip()
            if not normalized or normalized in seen:
                continue
            result.append(normalized)
            seen.add(normalized)
        return result

    def _get_ai_model_chain(self) -> List[str]:
        primary = str(
            self._openrouter_model or self._openrouter_free_model or DEFAULT_MODEL
        ).strip() or DEFAULT_MODEL
        fallback_models = self._parse_str_list_config(self._openrouter_fallback_models)
        if not fallback_models and self._openrouter_auto_fallback:
            fallback_models = list(DEFAULT_FALLBACK_MODELS)

        chain = []
        for model in [primary] + fallback_models:
            model = str(model or "").strip()
            if model and model not in chain:
                chain.append(model)
        return chain or [DEFAULT_MODEL]

    @staticmethod
    def _session_key(chat_id: str, user_id: str) -> str:
        chat = str(chat_id or "").strip() or "default_chat"
        user = str(user_id or "").strip() or "default_user"
        return f"{chat}::{user}"

    def _get_or_create_engine(self, session_key: str) -> Optional[ChatEngine]:
        if not (self._llm_enabled and self._openrouter_key):
            return None

        if self._engines is None:
            self._engines = {}
        if self._engine_pool_lock is None:
            self._engine_pool_lock = threading.Lock()

        with self._engine_pool_lock:
            engine = self._engines.get(session_key)
            if engine is not None:
                return engine
            engine = self._create_chat_engine()
            self._engines[session_key] = engine
            logger.info(f"[Agent] 创建会话引擎: session={session_key}")
            return engine

    def _get_session_dispatch_lock(self, session_key: str) -> threading.Lock:
        if self._session_dispatch_locks is None:
            self._session_dispatch_locks = {}
        if self._engine_pool_lock is None:
            self._engine_pool_lock = threading.Lock()

        with self._engine_pool_lock:
            lock = self._session_dispatch_locks.get(session_key)
            if lock is None:
                lock = threading.Lock()
                self._session_dispatch_locks[session_key] = lock
            return lock

    def _create_chat_engine(self) -> ChatEngine:
        model_chain = self._get_ai_model_chain()
        engine = ChatEngine(
            api_key=self._openrouter_key,
            model=model_chain[0],
            fallback_models=model_chain[1:],
            auto_fallback=self._openrouter_auto_fallback,
        )
        engine.executor.bind(extract_tags=_extract_tags)
        return engine

    def _get_ai_status_model(self) -> str:
        if self._engines:
            for engine in self._engines.values():
                return engine.resolved_model_name
        return self._get_ai_model_chain()[0]

    @classmethod
    def _is_direct_confirm_text(cls, text: str) -> bool:
        normalized = str(text or "").strip().lower()
        return normalized in cls._DIRECT_CONFIRM_TEXTS

    @classmethod
    def _is_direct_cancel_text(cls, text: str) -> bool:
        normalized = str(text or "").strip().lower()
        return normalized in cls._DIRECT_CANCEL_TEXTS

    def _cleanup_stale_sessions(self):
        if not self._engines or self._engine_pool_lock is None:
            return

        stale_keys = []
        with self._engine_pool_lock:
            for session_key, engine in list(self._engines.items()):
                running = bool(self._session_running.get(session_key)) if self._session_running else False
                if running:
                    continue
                if engine.history.is_stale(self._SESSION_TTL_SECONDS):
                    stale_keys.append(session_key)

            for session_key in stale_keys:
                engine = self._engines.pop(session_key, None)
                if engine is not None:
                    try:
                        engine.reset()
                    except Exception:
                        pass
                if self._session_running is not None:
                    self._session_running.pop(session_key, None)
                if self._session_dispatch_locks is not None:
                    self._session_dispatch_locks.pop(session_key, None)

        if stale_keys:
            logger.info(f"[Agent] 已清理过期会话: {len(stale_keys)}")

    def _cleanup_recent_actions(self):
        if self._recent_actions is None or self._recent_actions_lock is None:
            return

        cutoff = _time.monotonic() - self._ACTION_DEDUPE_TTL_SECONDS
        with self._recent_actions_lock:
            expired = [key for key, ts in self._recent_actions.items() if ts < cutoff]
            for key in expired:
                self._recent_actions.pop(key, None)

    def _mark_action_once(self, action_key: str) -> bool:
        if not action_key:
            return False
        if self._recent_actions is None:
            self._recent_actions = {}
        if self._recent_actions_lock is None:
            self._recent_actions_lock = threading.Lock()

        now = _time.monotonic()
        cutoff = now - self._ACTION_DEDUPE_TTL_SECONDS
        with self._recent_actions_lock:
            expired = [key for key, ts in self._recent_actions.items() if ts < cutoff]
            for key in expired:
                self._recent_actions.pop(key, None)
            if action_key in self._recent_actions:
                return False
            self._recent_actions[action_key] = now
            return True

    @staticmethod
    def _build_trace_id(kind: str, session_key: str = "", message_id: str = "") -> str:
        suffix = message_id or str(_time.time_ns())[-8:]
        if session_key:
            return f"{kind}:{session_key}:{suffix}"
        return f"{kind}:{suffix}"

    def _try_handle_direct_pending_action(
        self,
        text: str,
        engine: Optional[ChatEngine],
        chat_id: str,
        msg_id: str,
        session_key: str = "",
    ) -> bool:
        if not engine:
            return False

        pending = engine.state.pending_download
        if not pending:
            return False

        normalized = str(text or "").strip()

        if self._is_direct_confirm_text(normalized):
            title = pending.get("title", "未知资源")
            action_key = f"text_confirm::{session_key}::{pending.get('index', -1)}::{title}"
            if not self._mark_action_once(action_key):
                logger.info(f"[Agent] 忽略重复确认: session={session_key}, title={title}")
                if self._feishu:
                    self._feishu.send_card(
                        chat_id,
                        _CardBuilder.notify_card("📝 已受理", f"下载确认已在处理中: {title}", "grey"),
                        reply_msg_id=msg_id,
                    )
                return True
            engine.history.append({"role": "user", "content": normalized})
            result = engine.executor.execute(
                "download_resource",
                {"index": -1, "confirmed": True},
            )
            message = "✅ 已确认下载"
            if result.success and isinstance(result.data, dict):
                message = result.data.get("message") or message
            elif not result.success:
                message = result.error or "下载失败"
            engine.history.append({"role": "assistant", "content": message})
            if self._feishu:
                card = (
                    _CardBuilder.notify_card("✅ 下载已添加", message, "green")
                    if result.success
                    else _CardBuilder.error_card(message)
                )
                self._feishu.send_card(chat_id, card, reply_msg_id=msg_id)
            logger.info(f"[Agent] 直接确认待下载资源: session={session_key}, title={title}")
            return True

        if self._is_direct_cancel_text(normalized):
            title = pending.get("title", "未知资源")
            engine.history.append({"role": "user", "content": normalized})
            engine.state.clear_download()
            message = f"已取消下载确认: {title}"
            engine.history.append({"role": "assistant", "content": message})
            if self._feishu:
                self._feishu.send_card(
                    chat_id,
                    _CardBuilder.notify_card("🛑 已取消", message, "grey"),
                    reply_msg_id=msg_id,
                )
            logger.info(f"[Agent] 已取消待下载资源: session={session_key}, title={title}")
            return True

        return False



    # ══════════════════════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════════════════════

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
        self._session_running = {}
        self._session_dispatch_locks = {}
        self._engine_pool_lock = threading.Lock()
        self._recent_actions = {}
        self._recent_actions_lock = threading.Lock()
        self._seen_msg_ids = {}
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
        self._session_running = None
        self._session_dispatch_locks = None
        self._engine_pool_lock = None
        self._recent_actions = None
        self._recent_actions_lock = None
        self._feishu = None
        self._seen_msg_ids = None

    # ══════════════════════════════════════════════════════════════════════
    #  WebSocket 长连接管理
    # ══════════════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════════════

    def _ensure_runtime_ready(self):
        """惰性恢复运行时对象"""
        recovered = []

        if self._feishu is None and self._app_id:
            self._feishu = _FeishuAPI(self._app_id, self._app_secret)
            recovered.append("feishu")

        if self._seen_msg_ids is None:
            self._seen_msg_ids = {}

        if self._recent_actions is None:
            self._recent_actions = {}
            recovered.append("recent_actions")
        if self._recent_actions_lock is None:
            self._recent_actions_lock = threading.Lock()
            recovered.append("recent_actions_lock")

        if self._llm_enabled and self._openrouter_key:
            if self._engines is None:
                self._engines = {}
                recovered.append("engines")
            if self._session_running is None:
                self._session_running = {}
                recovered.append("session_running")
            if self._session_dispatch_locks is None:
                self._session_dispatch_locks = {}
                recovered.append("session_dispatch_locks")
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

    # ══════════════════════════════════════════════════════════════════════
    #  API 端点（HTTP 回调备用）
    # ══════════════════════════════════════════════════════════════════════

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/feishu_event",
                "endpoint": self._feishu_event,
                "methods": ["POST"],
                "summary": "飞书事件回调（HTTP 备用，推荐使用 WebSocket 长连接）",
            }
        ]

    def _feishu_event(self, request_data: dict = None, **kwargs) -> dict:
        try:
            data = request_data or {}
            evt_type = data.get("type") or data.get("header", {}).get("event_type", "unknown")
            logger.info(
                f"飞书回调到达: v{self.plugin_version}, inst={id(self):#x}, "
                f"type={evt_type}, enabled={self._enabled}"
            )
            if data.get("type") == "url_verification":
                logger.info("飞书 URL 验证请求")
                return {"challenge": data.get("challenge", "")}

            self._ensure_runtime_ready()

            if data.get("type") == "card.action.trigger":
                return self._handle_card_action(data)

            header = data.get("header", {})
            event = data.get("event", {})
            if header.get("event_type") == "im.message.receive_v1":
                threading.Thread(
                    target=self._handle_message, args=(event,), daemon=True
                ).start()
            return {"code": 0}
        except Exception as e:
            logger.error(f"飞书回调处理异常: {e}", exc_info=True)
            return {"code": -1, "msg": str(e)}

    # ══════════════════════════════════════════════════════════════════════
    #  消息路由 (v5.0.0 重构)
    # ══════════════════════════════════════════════════════════════════════

    def _handle_message(self, event: dict):
        try:
            self._cleanup_stale_sessions()
            self._cleanup_recent_actions()

            msg = event.get("message", {})
            chat_id = msg.get("chat_id", "") or self._chat_id
            msg_id = msg.get("message_id", "")
            msg_type = msg.get("message_type", "")
            sender = event.get("sender", {}).get("sender_id", {})
            user_id = sender.get("open_id", "")

            self._ensure_runtime_ready()

            # ── 消息去重: 同一条消息只处理一次 (防止 WS + HTTP 双通道重复) ──
            if msg_id and self._seen_msg_ids is not None:
                now = _time.monotonic()
                if msg_id in self._seen_msg_ids:
                    logger.info(f"[去重] 跳过重复消息 {msg_id}")
                    return
                self._seen_msg_ids[msg_id] = now
                # 清理超过 5 分钟的旧记录防止内存泄漏
                if len(self._seen_msg_ids) > 200:
                    cutoff = now - 300
                    self._seen_msg_ids = {
                        k: v for k, v in self._seen_msg_ids.items() if v > cutoff
                    }

            if msg_type != "text":
                if self._feishu:
                    self._feishu.send_card(
                        chat_id,
                        _CardBuilder.error_card("暂时只支持文字消息哦~")
                    )
                return

            try:
                text = _json.loads(msg.get("content", "{}")).get("text", "").strip()
            except Exception:
                text = ""

            # ── 清理飞书 @提及标记 ──
            mentions = msg.get("mentions")
            if mentions:
                for m in mentions:
                    key = m.get("key", "")
                    if key:
                        text = text.replace(key, "").strip()

            if not text:
                return

            session_key = self._session_key(chat_id, user_id)
            trace_id = self._build_trace_id("msg", session_key, msg_id)
            engine = self._get_or_create_engine(session_key)
            is_agent = engine is not None
            try:
                self._msg_count = getattr(self, "_msg_count", 0) + 1
            except Exception:
                pass
            logger.info(
                f"飞书收到: v{self.plugin_version}, inst={id(self):#x}, "
                f"msg#{self._msg_count}, trace={trace_id}, session={session_key}, "
                f"user={user_id}, text={text[:80]}"
            )
            logger.info(
                f"飞书路由: agent={'ON' if is_agent else 'OFF'}, "
                f"llm_enabled={self._llm_enabled}, "
                f"engine={type(engine).__name__ if engine else 'None'}"
            )

            # ── 始终可用的指令 ──
            if text.startswith("/status") or text.startswith("/状态"):
                self._cmd_status(chat_id, msg_id)
                return

            if text in ("/clear", "/清除", "清除对话", "重新开始"):
                if engine:
                    engine.reset()
                if self._feishu:
                    self._feishu.send_card(
                        chat_id,
                        _CardBuilder.notify_card("🗑️ 对话已清除", "历史会话已重置，可以开始新的对话。", "green"),
                        reply_msg_id=msg_id,
                    )
                return

            if text.startswith("/help") or text.startswith("/帮助"):
                self._cmd_help(chat_id, msg_id)
                return

            if self._try_handle_direct_pending_action(text, engine, chat_id, msg_id, session_key):
                return

            # ── Agent 模式：并发安全的消息路由 ──
            if is_agent:
                try:
                    self._agent_count = getattr(self, "_agent_count", 0) + 1
                except Exception:
                    pass
                logger.info(
                    f"[Agent] 收到消息 (#{self._agent_count}, trace={trace_id}, session={session_key}): {text[:80]}"
                )

                dispatch_lock = self._get_session_dispatch_lock(session_key)
                with dispatch_lock:
                    running = bool(self._session_running.get(session_key)) if self._session_running else False
                    if running:
                        engine.enqueue({
                            "text": text,
                            "chat_id": chat_id,
                            "msg_id": msg_id,
                        })
                        logger.info(f"[Agent] 会话忙碌，消息已排队: session={session_key}, text='{text[:40]}'")
                        if self._feishu:
                            self._feishu.send_card(
                                chat_id,
                                _CardBuilder.notify_card(
                                    "📝 已收到",
                                    f"正在处理上一条消息，你的新消息「{text[:30]}」排队中，稍后自动处理。",
                                    "blue",
                                ),
                                reply_msg_id=msg_id,
                            )
                        return

                    if self._session_running is not None:
                        self._session_running[session_key] = True

                threading.Thread(
                    target=self._agent_handle_v2,
                    args=(session_key, text, chat_id, msg_id),
                    daemon=True,
                ).start()
                return

            # ── 传统模式 ──
            try:
                self._legacy_count = getattr(self, "_legacy_count", 0) + 1
            except Exception:
                pass
            logger.info(f"[Legacy] 路由到传统指令 (#{self._legacy_count}): {text[:80]}")
            self._legacy_handle(text, chat_id, msg_id, user_id)
        except Exception as _exc:
            logger.error(f"_handle_message 顶层异常: {_exc}", exc_info=True)

    # ══════════════════════════════════════════════════════════════════════
    #  Agent 处理 (v6.0.0 — ChatEngine 集成)
    # ══════════════════════════════════════════════════════════════════════

    def _agent_handle_v2(self, session_key: str, text: str, chat_id: str, msg_id: str):
        """
        Agent 处理方法 — 使用 ChatEngine，内置排队消费循环。

        设计:
        1. engine.chat_with_progress() 内部有锁，保证同一时间只有一个在运行
        2. 处理完成后检查排队消息，在同一个线程中继续处理（不创建新线程）
        3. 连续多条消息不会产生并发冲突
        """
        engine = self._get_or_create_engine(session_key)
        trace_id = self._build_trace_id("agent", session_key, msg_id)
        if engine is None:
            logger.error(f"[Agent] 会话引擎不可用: trace={trace_id}, session={session_key}")
            dispatch_lock = self._get_session_dispatch_lock(session_key)
            with dispatch_lock:
                if self._session_running is not None:
                    self._session_running[session_key] = False
            return

        while True:  # ← 排队消费循环
            _t0 = _time.monotonic()

            # ── 即时反馈 ──
            processing_card = _CardBuilder.processing_card(text)
            send_result = self._feishu.send_card(
                chat_id, processing_card, reply_msg_id=msg_id
            )
            status_msg_id = ""
            try:
                status_msg_id = send_result.get("data", {}).get("message_id", "")
            except Exception:
                pass

            try:
                # ── 进度回调 ──
                step_log_display = []

                def on_tool_start(tool_name: str, tool_args: dict):
                    from .ai.tools import friendly_tool_name
                    friendly = friendly_tool_name(tool_name, tool_args)
                    step_log_display.append(friendly)
                    if status_msg_id and self._feishu:
                        try:
                            progress_card = _CardBuilder.agent_tool_progress_card(
                                text, step_log_display[:-1], step_log_display[-1]
                            )
                            self._feishu.update_card(status_msg_id, progress_card)
                        except Exception:
                            pass

                # ══ 核心: engine 内部有锁，线程安全 ══
                reply, steps = engine.chat_with_progress(
                    text,
                    on_tool_start=on_tool_start,
                )

                # ── 发送最终回复 ──
                elapsed = _time.monotonic() - _t0
                if reply:
                    final_card = _CardBuilder.agent_reply_card(reply, elapsed)
                    if status_msg_id:
                        self._feishu.update_card(status_msg_id, final_card)
                    else:
                        self._feishu.send_card(chat_id, final_card, reply_msg_id=msg_id)
                else:
                    error_card = _CardBuilder.error_card("AI 没有生成回复，请再试试~")
                    if status_msg_id:
                        self._feishu.update_card(status_msg_id, error_card)
                    else:
                        self._feishu.send_card(chat_id, error_card)

            except Exception as e:
                logger.error(f"Agent 异常: {e}", exc_info=True)
                error_card = _CardBuilder.error_card(f"AI 处理出错: {e}")
                if status_msg_id:
                    self._feishu.update_card(status_msg_id, error_card)
                elif self._feishu:
                    self._feishu.send_card(chat_id, error_card)
            finally:
                _elapsed = _time.monotonic() - _t0
                logger.info(f"[Agent] 完成: trace={trace_id}, session={session_key}, elapsed={_elapsed:.1f}s")

            # ════════════════════════════════════════════════════════
            #  排队消费：检查是否有新消息在处理期间到达
            # ════════════════════════════════════════════════════════
            dispatch_lock = self._get_session_dispatch_lock(session_key)
            with dispatch_lock:
                pending = engine.drain_pending()
                if pending is None:
                    if self._session_running is not None:
                        self._session_running[session_key] = False
                    break

            text = pending.get("text", "") or ""
            chat_id = pending.get("chat_id", "") or chat_id
            msg_id = pending.get("msg_id", "") or ""
            trace_id = self._build_trace_id("agent", session_key, msg_id)
            logger.info(f"[Agent] 消费排队消息: trace={trace_id}, session={session_key}, text='{text[:50]}'")

    # ══════════════════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════════════════
    #  传统模式工具桥接（卡片按钮 + 传统指令共用）
    # ══════════════════════════════════════════════════════════════════════

    def _legacy_tool_search_media(self, keyword: str) -> dict:
        """传统模式: 搜索影视"""
        try:
            from app.chain.search import SearchChain
            results = SearchChain().search_medias(title=keyword)
            if not results:
                return {"results": [], "message": f"未找到与 '{keyword}' 相关的内容"}
            items = []
            for i, m in enumerate(results[:10]):
                items.append({
                    "index": i,
                    "title": m.title or "未知",
                    "year": m.year or "",
                    "type": m.type.value if m.type else "未知",
                    "rating": m.vote_average or 0,
                    "overview": (m.overview or "")[:100],
                })
            return {"results": items, "total": len(results), "keyword": keyword}
        except Exception as e:
            return {"error": str(e)}

    def _legacy_tool_search_resources(self, keyword: str) -> dict:
        """传统模式: 搜索资源"""
        try:
            from app.chain.search import SearchChain
            contexts = SearchChain().search_torrents(title=keyword)
            if not contexts:
                return {"results": [], "message": f"未找到 '{keyword}' 的下载资源"}
            items = []
            for i, ctx in enumerate(contexts[:15]):
                t = ctx.torrent_info
                items.append({
                    "index": i,
                    "title": t.title or "未知",
                    "site": t.site_name or "未知",
                    "size": f"{t.size / (1024**3):.1f} GB" if t.size else "未知",
                    "seeders": t.seeders or 0,
                })
            return {"results": items, "total": len(contexts), "title": keyword}
        except Exception as e:
            return {"error": str(e)}

    def _legacy_tool_download_resource(
        self,
        index: int,
        confirmed: bool = False,
        session_key: str = "",
    ) -> dict:
        """传统模式: 下载资源"""
        try:
            from app.chain.search import SearchChain
            from app.chain.download import DownloadChain
            # 需要 engine 的 state 来获取 resource_cache
            engine = self._get_or_create_engine(session_key) if session_key else None
            if engine and engine.state.resource_cache:
                cache = engine.state.resource_cache
            else:
                return {"error": "没有可用的资源缓存，请先搜索资源"}
            if index < 0 or index >= len(cache):
                return {"error": f"索引 {index} 超出范围 (0-{len(cache)-1})"}
            ctx = cache[index]
            t = ctx.torrent_info
            if not confirmed:
                return {
                    "title": t.title or "未知",
                    "site": t.site_name or "未知",
                    "size": f"{t.size / (1024**3):.1f} GB" if t.size else "未知",
                    "tags": {"seeders": t.seeders or 0},
                }
            DownloadChain().download_single(ctx)
            return {"success": True, "message": f"已添加下载: {t.title}"}
        except Exception as e:
            return {"error": str(e)}

    def _legacy_tool_subscribe_media(
        self,
        keyword: str = None,
        idx: int = None,
        session_key: str = "",
    ) -> dict:
        """传统模式: 订阅"""
        try:
            from app.chain.search import SearchChain
            from app.chain.subscribe import SubscribeChain
            from app.chain.media import MediaChain
            engine = self._get_or_create_engine(session_key) if session_key else None
            if idx is not None and engine and engine.state.search_cache:
                cache = engine.state.search_cache
                if 0 <= idx < len(cache):
                    item = cache[idx]
                    info = MediaChain().recognize_media(meta=item)
                    if info and info.tmdb_info:
                        SubscribeChain().add(title=info.title, year=info.year,
                                             mtype=info.type, tmdbid=info.tmdb_id)
                        return {"success": True, "message": f"已订阅: {info.title}"}
                return {"error": f"索引 {idx} 对应的内容未找到"}
            if keyword:
                results = SearchChain().search_medias(title=keyword)
                if results:
                    m = results[0]
                    info = MediaChain().recognize_media(meta=m)
                    if info and info.tmdb_info:
                        SubscribeChain().add(title=info.title, year=info.year,
                                             mtype=info.type, tmdbid=info.tmdb_id)
                        return {"success": True, "message": f"已订阅: {info.title}"}
                return {"error": f"未找到可订阅的内容: {keyword}"}
            return {"error": "请提供关键词或索引"}
        except Exception as e:
            return {"error": str(e)}

    def _legacy_tool_get_downloading(self) -> dict:
        """传统模式: 查看下载"""
        try:
            from app.chain.download import DownloadChain
            torrents = DownloadChain().downloading_torrents()
            if not torrents:
                return {"tasks": [], "message": "当前没有正在下载的任务"}
            tasks = []
            for t in torrents[:15]:
                tasks.append({
                    "title": getattr(t, "title", "") or getattr(t, "name", "未知"),
                    "progress": getattr(t, "progress", 0),
                })
            return {"tasks": tasks, "total": len(torrents)}
        except Exception as e:
            return {"error": str(e)}

    #  传统模式指令 (v5.0.0: 使用卡片输出)
    # ══════════════════════════════════════════════════════════════════════

    def _legacy_handle(self, text: str, chat_id: str, msg_id: str, user_id: str):
        # ── v5.1.1: 即时反馈 ──
        if self._feishu:
            try:
                self._feishu.send_card(
                    chat_id,
                    _CardBuilder.processing_card(text),
                    reply_msg_id=msg_id,
                )
            except Exception:
                pass

        if text.startswith("/帮助") or text.startswith("/help"):
            self._cmd_help(chat_id, msg_id)
        elif text.startswith("/搜索") or text.startswith("/search"):
            kw = re.sub(r"^/(搜索|search)\s*", "", text).strip()
            self._legacy_search(kw, chat_id, msg_id, user_id)
        elif text.startswith("/订阅") or text.startswith("/subscribe"):
            kw = re.sub(r"^/(订阅|subscribe)\s*", "", text).strip()
            self._legacy_subscribe(kw, chat_id, msg_id, user_id)
        elif text.startswith("/正在下载") or text.startswith("/downloading"):
            self._legacy_downloading(chat_id, msg_id)
        else:
            self._legacy_search(text, chat_id, msg_id, user_id)

    def _legacy_search(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        if not keyword:
            return
        # 即时反馈
        self._feishu.send_card(
            chat_id,
            _CardBuilder.notify_card("🔍 搜索中...", f"正在搜索「{keyword}」，请稍候...", "indigo"),
        )
        result = self._legacy_tool_search_media(keyword)
        if result.get("error"):
            self._feishu.send_card(chat_id, _CardBuilder.error_card(result['error']))
            return
        items = result.get("results", [])
        if not items:
            self._feishu.send_card(
                chat_id,
                _CardBuilder.notify_card("🔍 搜索结果", result.get("message", f"未找到: {keyword}"), "grey"),
            )
            return
        # 使用搜索结果卡片
        self._feishu.send_card(chat_id, _CardBuilder.search_result_card(keyword, items))

    def _legacy_subscribe(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        if not keyword:
            return
        self._feishu.send_card(
            chat_id,
            _CardBuilder.notify_card("📥 订阅中...", f"正在订阅「{keyword}」...", "indigo"),
        )
        result = self._legacy_tool_subscribe_media(keyword)
        msg = result.get("message") or result.get("error", "操作失败")
        if result.get("success"):
            self._feishu.send_card(
                chat_id,
                _CardBuilder.notify_card("✅ 订阅成功", msg, "green"),
            )
        else:
            self._feishu.send_card(chat_id, _CardBuilder.error_card(msg))

    def _legacy_downloading(self, chat_id: str, msg_id: str):
        result = self._legacy_tool_get_downloading()
        tasks = result.get("tasks", [])
        total = result.get("total", len(tasks))
        self._feishu.send_card(chat_id, _CardBuilder.downloading_card(tasks, total))

    # ══════════════════════════════════════════════════════════════════════
    #  诊断 / 帮助 (v5.0.0: 卡片化)
    # ══════════════════════════════════════════════════════════════════════

    def _cmd_status(self, chat_id: str, msg_id: str):
        model = self._get_ai_status_model()
        cache_media = 0
        cache_res = 0

        uptime = "未知"
        if hasattr(self, "_init_ts") and self._init_ts:
            delta = datetime.now() - self._init_ts
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            mins, secs = divmod(remainder, 60)
            uptime = f"{hours}h {mins}m {secs}s"

        ws_status = "❌ 未启用"
        if self._use_ws:
            if not _HAS_LARK_SDK:
                ws_status = "⚠️ SDK 未安装"
            elif self._ws_connected:
                ws_status = "✅ 已连接"
            elif self._ws_running:
                ws_status = "🔄 连接中"
            else:
                ws_status = "❌ 未运行"

        agent_status = "❌ 未激活"
        if self._engines:
            agent_status = "✅ 运行中"
        elif self._llm_enabled:
            agent_status = "⚠️ 已启用未激活"

        info = {
            "version": self.plugin_version,
            "instance": f"{id(self):#x}",
            "uptime": uptime,
            "feishu_token": "✅ 正常" if getattr(self, "_feishu_ok", False) else "❌ 异常",
            "ws_status": ws_status,
            "lark_sdk": "✅ 已安装" if _HAS_LARK_SDK else "❌ 未安装",
            "agent_status": agent_status,
            "model": model,
            "msg_count": getattr(self, "_msg_count", 0),
            "agent_count": getattr(self, "_agent_count", 0),
            "legacy_count": getattr(self, "_legacy_count", 0),
            "recover_count": getattr(self, "_recover_count", 0),
            "conversations": len(self._engines or {}),
            "cache_media": cache_media,
            "cache_res": cache_res,
        }
        self._feishu.send_card(
            chat_id,
            _CardBuilder.status_card(info),
            reply_msg_id=msg_id,
        )

    def _cmd_help(self, chat_id: str, msg_id: str):
        agent_on = bool(self._llm_enabled and self._openrouter_key)
        ws_on = self._use_ws and self._ws_running
        self._feishu.send_card(
            chat_id,
            _CardBuilder.help_card(agent_on, ws_on),
            reply_msg_id=msg_id,
        )

    # ══════════════════════════════════════════════════════════════════════
    #  卡片回调 (v5.0.0: 支持新卡片按钮)
    # ══════════════════════════════════════════════════════════════════════

    def _handle_card_action(self, data: dict) -> dict:
        try:
            self._cleanup_recent_actions()
            action = data.get("event", {}).get("action", {})
            value = action.get("value", {})
            act = value.get("action", "")
            operator = data.get("event", {}).get("operator", {})
            user_id = operator.get("open_id", "")
            ctx = data.get("event", {}).get("context", {})
            chat_id = ctx.get("open_chat_id", "") or self._chat_id
            session_key = self._session_key(chat_id, user_id)
            callback_token = (
                data.get("header", {}).get("event_id", "")
                or data.get("event", {}).get("context", {}).get("open_message_id", "")
            )
            trace_id = self._build_trace_id("card", session_key, callback_token)
            logger.info(f"[Card] 收到回调: trace={trace_id}, session={session_key}, action={act}")

            if act == "download_resource":
                idx = int(value.get("index", 0))
                threading.Thread(
                    target=self._card_download, args=(idx, user_id, chat_id), daemon=True,
                ).start()
            elif act == "download_resource_confirm":
                idx = int(value.get("index", 0))
                action_key = f"card_confirm::{session_key}::{idx}"
                if not self._mark_action_once(action_key):
                    logger.info(f"[Card] 忽略重复下载确认: trace={trace_id}, session={session_key}, index={idx}")
                    return {"code": 0}
                threading.Thread(
                    target=self._card_download_confirmed, args=(idx, user_id, chat_id), daemon=True,
                ).start()
            elif act == "subscribe":
                idx = int(value.get("index", 0))
                action_key = f"card_subscribe::{session_key}::{idx}"
                if not self._mark_action_once(action_key):
                    logger.info(f"[Card] 忽略重复订阅: trace={trace_id}, session={session_key}, index={idx}")
                    return {"code": 0}
                threading.Thread(
                    target=self._card_subscribe, args=(idx, user_id, chat_id), daemon=True,
                ).start()
            elif act == "search_resources_by_title":
                kw = value.get("keyword", "")
                threading.Thread(
                    target=self._card_search_resources, args=(kw, user_id, chat_id), daemon=True,
                ).start()
            elif act == "noop":
                pass
        except Exception as e:
            logger.error(f"卡片回调异常: {e}", exc_info=True)
        return {"code": 0}

    def _card_download(self, idx: int, user_id: str, chat_id: str):
        """卡片按钮: 展示下载确认"""
        session_key = self._session_key(chat_id, user_id)
        result = self._legacy_tool_download_resource(idx, confirmed=False, session_key=session_key)
        if result.get("error"):
            self._feishu.send_card(chat_id, _CardBuilder.error_card(result["error"]))
            return
        self._feishu.send_card(
            chat_id,
            _CardBuilder.download_confirm_card(
                idx,
                result.get("title", "未知"),
                result.get("site", "未知"),
                result.get("size", "未知"),
                result.get("tags", {}),
            ),
        )

    def _card_download_confirmed(self, idx: int, user_id: str, chat_id: str):
        """卡片按钮: 确认下载"""
        session_key = self._session_key(chat_id, user_id)
        result = self._legacy_tool_download_resource(idx, confirmed=True, session_key=session_key)
        msg = result.get("message") or result.get("error", "操作失败")
        if result.get("success"):
            self._feishu.send_card(
                chat_id,
                _CardBuilder.notify_card("✅ 下载已添加", msg, "green"),
            )
        else:
            self._feishu.send_card(chat_id, _CardBuilder.error_card(msg))

    def _card_subscribe(self, idx: int, user_id: str, chat_id: str):
        session_key = self._session_key(chat_id, user_id)
        result = self._legacy_tool_subscribe_media(None, idx=idx, session_key=session_key)
        msg = result.get("message") or result.get("error", "操作失败")
        if result.get("success"):
            self._feishu.send_card(
                chat_id,
                _CardBuilder.notify_card("✅ 订阅成功",msg, "green"),
            )
        else:
            self._feishu.send_card(chat_id, _CardBuilder.error_card(msg))

    def _card_search_resources(self, keyword: str, user_id: str, chat_id: str):
        """卡片按钮: 搜索资源"""
        self._feishu.send_card(
            chat_id,
            _CardBuilder.notify_card("📦 搜索资源中...", f"正在搜索「{keyword}」的下载资源...", "indigo"),
        )
        result = self._legacy_tool_search_resources(keyword)
        if result.get("error"):
            self._feishu.send_card(chat_id, _CardBuilder.error_card(result["error"]))
            return
        items = result.get("results", [])
        title = result.get("title", keyword)
        self._feishu.send_card(chat_id, _CardBuilder.resource_result_card(keyword, title, items))

    # ══════════════════════════════════════════════════════════════════════
    #  表单配置
    # ══════════════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════════════
    #  事件通知 (v5.0.0: 卡片化)
    # ══════════════════════════════════════════════════════════════════════

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
