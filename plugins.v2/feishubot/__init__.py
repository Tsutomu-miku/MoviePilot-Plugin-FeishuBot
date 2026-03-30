"""
飞书机器人插件 v5.3.0 — MoviePilot Agent Mode + WebSocket 长连接

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
from .llm_client import _OpenRouterClient
from .conversation import _ConversationManager, _sanitize_assistant_message
from .agent_tools import _AGENT_TOOLS, _AGENT_SYSTEM_PROMPT


class FeishuBot(_PluginBase):

    # ── 插件元信息 ──
    plugin_name = "飞书机器人"
    plugin_desc = "飞书群机器人消息通知与交互，支持 AI Agent 智能体模式（WebSocket 长连接）"
    plugin_icon = "Feishu_A.png"
    plugin_version = "5.3.0"
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
    _use_ws: bool = True

    # ── 运行时 ──
    _feishu: Optional[_FeishuAPI] = None
    _llm_client: Optional[_OpenRouterClient] = None
    _conversations: Optional[_ConversationManager] = None
    _search_cache: Optional[dict] = None
    _resource_cache: Optional[dict] = None

    # ── 用户并发控制 (v5.0.0 重构) ──
    _user_locks: Optional[dict] = None          # user_id -> Lock
    _user_pending_msg: Optional[dict] = None    # user_id -> latest pending text
    _user_interrupted: Optional[dict] = None    # user_id -> bool (是否被打断)
    _user_processing: Optional[dict] = None     # user_id -> bool (是否正在处理)
    _seen_msg_ids: Optional[dict] = None         # msg_id -> timestamp (消息去重)
    _dispatch_lock: threading.Lock = threading.Lock()  # Agent 调度原子锁

    # ── WebSocket 长连接运行时 ──
    _ws_client: Optional[Any] = None
    _ws_thread: Optional[threading.Thread] = None
    _ws_running: bool = False

    _MAX_AGENT_ITERATIONS = 10
    _MSG_MERGE_DELAY = 1.5  # 消息合并等待窗口（秒）

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

            llm_raw = config.get("llm_enabled")
            if isinstance(llm_raw, bool):
                self._llm_enabled = llm_raw
            elif isinstance(llm_raw, str):
                self._llm_enabled = llm_raw.lower() in ("true", "1", "yes", "on")
            else:
                self._llm_enabled = bool(llm_raw) if llm_raw is not None else False

            self._openrouter_key = str(config.get("openrouter_key", "") or "").strip()
            self._openrouter_model = str(config.get("openrouter_model", "") or "").strip()

        self._feishu = _FeishuAPI(self._app_id, self._app_secret)
        self._search_cache = {}
        self._resource_cache = {}
        self._pending_download = {}
        self._user_locks = {}
        self._user_pending_msg = {}
        self._user_interrupted = {}
        self._user_processing = {}
        self._seen_msg_ids = {}
        self._dispatch_lock = threading.Lock()  # 实例级锁，避免 reload 共享
        self._llm_client = None
        self._conversations = None
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
            f"model={self._openrouter_model or 'default'}"
        )

        if self._llm_enabled and self._openrouter_key:
            try:
                self._llm_client = _OpenRouterClient(
                    api_key=self._openrouter_key,
                    model=self._openrouter_model,
                )
                self._conversations = _ConversationManager(_AGENT_SYSTEM_PROMPT)
                logger.info(
                    f"飞书 Agent 模式已启用 ✓ inst={id(self):#x}, "
                    f"模型: {self._openrouter_model or _OpenRouterClient.DEFAULT_MODEL}"
                )
            except Exception as e:
                logger.error(f"飞书 Agent 初始化失败: {e}", exc_info=True)
                self._llm_client = None
                self._conversations = None
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
            f"llm_client={'有' if self._llm_client else '无'}, "
            f"feishu={'有' if self._feishu else '无'}, "
            f"ws_running={self._ws_running}, "
            f"msgs={getattr(self, '_msg_count', '?')}, "
            f"agents={getattr(self, '_agent_count', '?')}, "
            f"recovers={getattr(self, '_recover_count', '?')}"
        )

        self._stop_ws_client()

        self._llm_client = None
        self._conversations = None
        self._feishu = None
        self._search_cache = None
        self._resource_cache = None
        self._pending_download = None
        self._user_locks = None
        self._user_pending_msg = None
        self._user_interrupted = None
        self._user_processing = None
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

        if self._search_cache is None:
            self._search_cache = {}
        if self._resource_cache is None:
            self._resource_cache = {}
        if self._pending_download is None:
            self._pending_download = {}
        if self._user_locks is None:
            self._user_locks = {}
        if self._user_pending_msg is None:
            self._user_pending_msg = {}
        if self._user_interrupted is None:
            self._user_interrupted = {}
        if self._user_processing is None:
            self._user_processing = {}
        if self._seen_msg_ids is None:
            self._seen_msg_ids = {}

        if self._llm_enabled and self._openrouter_key:
            if self._llm_client is None:
                try:
                    self._llm_client = _OpenRouterClient(
                        api_key=self._openrouter_key,
                        model=self._openrouter_model,
                    )
                    recovered.append("llm_client")
                except Exception as e:
                    logger.error(f"Agent LLM 客户端恢复失败: {e}")

            if self._conversations is None:
                self._conversations = _ConversationManager(_AGENT_SYSTEM_PROMPT)
                recovered.append("conversations")

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

            is_agent = self._llm_client is not None and self._conversations is not None
            try:
                self._msg_count = getattr(self, "_msg_count", 0) + 1
            except Exception:
                pass
            logger.info(
                f"飞书收到: v{self.plugin_version}, inst={id(self):#x}, "
                f"msg#{self._msg_count}, user={user_id}, text={text[:80]}"
            )
            logger.info(
                f"飞书路由: agent={'ON' if is_agent else 'OFF'}, "
                f"llm_enabled={self._llm_enabled}, "
                f"llm_client={type(self._llm_client).__name__}, "
                f"conv={type(self._conversations).__name__}"
            )

            # ── 始终可用的指令 ──
            if text.startswith("/status") or text.startswith("/状态"):
                self._cmd_status(chat_id, msg_id)
                return

            if text in ("/clear", "/清除", "清除对话", "重新开始"):
                if self._conversations:
                    self._conversations.clear(user_id)
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

            # ── Agent 模式：消息队列 + 打断机制 ──
            if is_agent:
                try:
                    self._agent_count = getattr(self, "_agent_count", 0) + 1
                except Exception:
                    pass
                logger.info(f"[Agent] 路由到 Agent (#{self._agent_count}): {text[:80]}")
                self._agent_dispatch(text, chat_id, msg_id, user_id)
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
    #  Agent 并发控制 (v5.0.0 新增 — 消息队列 + 打断机制)
    # ══════════════════════════════════════════════════════════════════════

    def _get_user_lock(self, user_id: str) -> threading.Lock:
        """获取用户级别的锁（线程安全）"""
        with self._dispatch_lock:
            if self._user_locks is None:
                self._user_locks = {}
            if user_id not in self._user_locks:
                self._user_locks[user_id] = threading.Lock()
            return self._user_locks[user_id]

    def _agent_dispatch(self, text: str, chat_id: str, msg_id: str, user_id: str):
        """
        Agent 消息调度器 — 解决用户连续发消息导致多轮并行回复的问题。

        策略:
        1. 如果该用户当前没有 Agent 在处理 → 等待短暂合并窗口后开始处理
        2. 如果该用户当前有 Agent 在处理 → 标记"打断"，存储最新消息
           当前轮次完成后会自动使用最新消息重新开始
        """
        if self._user_pending_msg is None:
            self._user_pending_msg = {}
        if self._user_interrupted is None:
            self._user_interrupted = {}
        if self._user_processing is None:
            self._user_processing = {}
        if self._seen_msg_ids is None:
            self._seen_msg_ids = {}

        with self._dispatch_lock:
            is_processing = self._user_processing.get(user_id, False)

            if is_processing:
                # ── 用户在 Agent 处理期间又发了新消息 → 标记打断 ──
                self._user_pending_msg[user_id] = (text, chat_id, msg_id)
                self._user_interrupted[user_id] = True
                logger.info(f"[Agent] 用户 {user_id} 打断: 存储新消息 '{text[:40]}'")
                _need_interrupt_card = True
            else:
                _need_interrupt_card = False
                # ── 没有正在处理的任务 → 等待合并窗口后启动 ──
                self._user_pending_msg[user_id] = (text, chat_id, msg_id)
                self._user_interrupted[user_id] = False
                self._user_processing[user_id] = True

        if _need_interrupt_card:
            # 给用户即时反馈（锁外执行，避免长时间持锁）
            if self._feishu:
                self._feishu.send_card(
                    chat_id,
                    _CardBuilder.interrupted_card(text),
                    reply_msg_id=msg_id,
                )
            return

        threading.Thread(
            target=self._agent_merge_and_run,
            args=(user_id,),
            daemon=True,
        ).start()

    def _agent_merge_and_run(self, user_id: str):
        """等待合并窗口 → 取最新消息 → 执行 Agent → 检查是否有新打断"""
        # 短暂等待，让快速连续的消息可以合并
        _time.sleep(self._MSG_MERGE_DELAY)

        lock = self._get_user_lock(user_id)
        if not lock.acquire(blocking=True, timeout=120):
            logger.warning(f"[Agent] 用户 {user_id} 锁获取超时")
            with self._dispatch_lock:
                self._user_processing[user_id] = False
            return

        try:
            while True:
                # 取最新消息
                pending = (self._user_pending_msg or {}).pop(user_id, None)
                if not pending:
                    break

                text, chat_id, msg_id = pending
                self._user_interrupted[user_id] = False
                # _user_processing 已在 _agent_dispatch 中设为 True

                logger.info(f"[Agent] 开始处理: user={user_id}, text='{text[:60]}'")

                # 执行 Agent
                self._agent_handle(text, chat_id, msg_id, user_id)

                # 检查是否被打断（有新消息等待）
                if self._user_interrupted.get(user_id, False):
                    logger.info(f"[Agent] 用户 {user_id} 被打断，处理新消息...")
                    self._user_interrupted[user_id] = False
                    continue
                else:
                    break
        finally:
            with self._dispatch_lock:
                self._user_processing[user_id] = False
            lock.release()

    # ══════════════════════════════════════════════════════════════════════
    #  Agent 入口 + 循环
    # ══════════════════════════════════════════════════════════════════════

    def _agent_handle(self, text: str, chat_id: str, msg_id: str, user_id: str):
        """Agent 入口：即时反馈 → 构建上下文 → 循环 → 更新卡片 → 保存历史"""
        _t0 = _time.monotonic()

        # ── 即时反馈: 发送 "处理中" 卡片 ──
        processing_card = _CardBuilder.processing_card(text)
        send_result = self._feishu.send_card(chat_id, processing_card, reply_msg_id=msg_id)

        # 提取发送的卡片 message_id，用于后续更新
        status_msg_id = ""
        try:
            status_msg_id = send_result.get("data", {}).get("message_id", "")
        except Exception:
            pass

        try:
            # 获取对话历史副本并追加新消息
            messages = self._conversations.get(user_id)
            messages.append({"role": "user", "content": text})

            # 执行 Agent 循环（带进度回调）
            step_log = []

            def on_tool_start(tool_name: str, tool_args: dict):
                """工具开始执行时的回调 — 更新进度卡片"""
                if self._user_interrupted.get(user_id, False):
                    return  # 已被打断，不再更新
                friendly = self._tool_friendly_name(tool_name, tool_args)
                step_log.append(friendly)
                if status_msg_id and self._feishu:
                    try:
                        progress_card = _CardBuilder.agent_tool_progress_card(
                            text, step_log[:-1], step_log[-1]
                        )
                        self._feishu.update_card(status_msg_id, progress_card)
                    except Exception:
                        pass

            def on_tool_done(tool_name: str, tool_args: dict):
                """工具完成时的回调"""
                pass  # step_log 已在 on_tool_start 中更新

            updated, reply = self._agent_loop(
                messages, chat_id, user_id,
                on_tool_start=on_tool_start,
                on_tool_done=on_tool_done,
            )

            # ── 检查是否被打断 ──
            if self._user_interrupted.get(user_id, False):
                logger.info(f"[Agent] 用户 {user_id} 处理被打断，保存已完成对话上下文")
                self._conversations.save(user_id, updated)
                return

            # ── 发送最终回复 ──
            elapsed = _time.monotonic() - _t0
            if reply:
                final_card = _CardBuilder.agent_reply_card(reply, elapsed)
                if status_msg_id and self._feishu:
                    self._feishu.update_card(status_msg_id, final_card)
                else:
                    self._feishu.send_card(chat_id, final_card, reply_msg_id=msg_id)
            else:
                error_card = _CardBuilder.error_card("AI 没有生成回复，请再试试~")
                if status_msg_id and self._feishu:
                    self._feishu.update_card(status_msg_id, error_card)
                else:
                    self._feishu.send_card(chat_id, error_card)

            # 成功后才保存
            self._conversations.save(user_id, updated)

        except Exception as e:
            logger.error(f"Agent 异常: {e}", exc_info=True)
            error_card = _CardBuilder.error_card(f"AI 处理出错: {e}")
            if status_msg_id and self._feishu:
                self._feishu.update_card(status_msg_id, error_card)
            elif self._feishu:
                self._feishu.send_card(chat_id, error_card)
        finally:
            _elapsed = _time.monotonic() - _t0
            logger.info(f"[Agent] 处理完成: user={user_id}, elapsed={_elapsed:.1f}s")

    @staticmethod
    def _tool_friendly_name(tool_name: str, tool_args: dict) -> str:
        """将工具名+参数转为用户友好的描述"""
        names = {
            "search_media": "搜索影视「{keyword}」",
            "search_resources": "搜索资源「{keyword}」",
            "download_resource": "下载资源 #{index}",
            "subscribe_media": "订阅影视",
            "get_downloading": "查询下载进度",
        }
        template = names.get(tool_name, tool_name)
        try:
            return template.format(**tool_args)
        except (KeyError, IndexError):
            return template.split("「")[0].strip()

    def _agent_loop(
        self, messages: list, chat_id: str, user_id: str,
        on_tool_start: Callable = None,
        on_tool_done: Callable = None,
    ) -> Tuple[list, str]:
        """
        多轮 Tool Calling 循环。
        在消息副本上操作，返回 (更新后的消息列表, 最终回复文本)。
        支持 on_tool_start / on_tool_done 回调用于进度更新。
        """
        working = list(messages)

        for iteration in range(self._MAX_AGENT_ITERATIONS):
            # ── 打断检查 ──
            if self._user_interrupted.get(user_id, False):
                logger.info(f"[Agent] 循环被打断 (第{iteration+1}轮)")
                return working, ""

            # ── 调用 LLM ──
            try:
                result = self._llm_client.chat(
                    messages=working, tools=_AGENT_TOOLS
                )
            except Exception as e:
                logger.error(f"Agent LLM 调用失败 (第{iteration+1}轮): {e}")
                err = f"⚠️ AI 调用失败: {e}"
                working.append({"role": "assistant", "content": err})
                return working, err

            # ── 解析响应 ──
            choices = result.get("choices")
            if not choices:
                logger.error(f"Agent 无 choices: {_json.dumps(result, ensure_ascii=False)[:500]}")
                err = "⚠️ AI 返回异常，请稍后重试"
                working.append({"role": "assistant", "content": err})
                return working, err

            raw_message = choices[0].get("message", {})
            tool_calls = raw_message.get("tool_calls")

            logger.info(
                f"Agent 第{iteration+1}轮: "
                f"tool_calls={len(tool_calls) if tool_calls else 0}, "
                f"has_content={bool(raw_message.get('content'))}"
            )

            # ── 无 tool_calls → 最终回复 ──
            if not tool_calls:
                reply = raw_message.get("content", "") or ""
                working.append({"role": "assistant", "content": reply})
                return working, reply

            # ── 有 tool_calls → 清洗消息 + 执行工具 ──
            clean_msg = _sanitize_assistant_message(raw_message)
            working.append(clean_msg)

            for tc in tool_calls:
                fn_name = tc.get("function", {}).get("name", "")
                fn_args_raw = tc.get("function", {}).get("arguments", "{}")
                tc_id = tc.get("id", "")

                try:
                    fn_args = _json.loads(fn_args_raw) if fn_args_raw else {}
                except (_json.JSONDecodeError, TypeError):
                    fn_args = {}

                logger.info(f"Agent tool [{iteration+1}]: {fn_name}({fn_args})")

                # 进度回调
                if on_tool_start:
                    on_tool_start(fn_name, fn_args)

                tool_result = self._execute_tool(fn_name, fn_args, chat_id, user_id)

                if on_tool_done:
                    on_tool_done(fn_name, fn_args)

                working.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": _json.dumps(tool_result, ensure_ascii=False, default=str),
                })

        timeout_msg = "⚠️ 处理步骤过多，请尝试简化请求。"
        working.append({"role": "assistant", "content": timeout_msg})
        return working, timeout_msg

    # ══════════════════════════════════════════════════════════════════════
    #  工具路由 & 实现
    # ══════════════════════════════════════════════════════════════════════

    def _execute_tool(
        self, fn_name: str, fn_args: dict, chat_id: str, user_id: str
    ) -> dict:
        try:
            if fn_name == "search_media":
                return self._tool_search_media(fn_args.get("keyword", ""), user_id)
            elif fn_name == "search_resources":
                return self._tool_search_resources(fn_args.get("keyword", ""), user_id)
            elif fn_name == "download_resource":
                return self._tool_download_resource(
                    index=fn_args.get("index", 0),
                    confirmed=fn_args.get("confirmed", False),
                    user_id=user_id,
                )
            elif fn_name == "subscribe_media":
                return self._tool_subscribe_media(
                    index=fn_args.get("index"),
                    keyword=fn_args.get("keyword"),
                    user_id=user_id,
                )
            elif fn_name == "get_downloading":
                return self._tool_get_downloading()
            else:
                return {"error": f"未知工具: {fn_name}"}
        except Exception as e:
            logger.error(f"工具 {fn_name} 异常: {e}", exc_info=True)
            return {"error": str(e)}

    def _tool_search_media(self, keyword: str, user_id: str) -> dict:
        if not keyword:
            return {"error": "请提供搜索关键词"}
        try:
            from app.chain.media import MediaChain
            result = MediaChain().search(title=keyword)
            if isinstance(result, tuple) and len(result) == 2:
                meta, medias = result
            elif isinstance(result, list):
                meta, medias = None, result
            else:
                return {"error": "搜索返回格式异常", "results": []}
            if not medias:
                name = getattr(meta, "name", keyword) if meta else keyword
                return {"keyword": keyword, "results": [], "message": f"未找到「{name}」"}

            valid = []
            for i, m in enumerate(medias[:8]):
                if isinstance(m, str) or not hasattr(m, "tmdb_id"):
                    continue
                raw_type = getattr(m, "type", None)
                if hasattr(raw_type, "value"):
                    mtype_str = "电影" if raw_type == MediaType.MOVIE else "电视剧"
                else:
                    mtype_str = str(raw_type) if raw_type else "未知"
                valid.append({
                    "index": i,
                    "title": getattr(m, "title", ""),
                    "year": getattr(m, "year", ""),
                    "type": mtype_str,
                    "rating": getattr(m, "vote_average", ""),
                    "overview": (getattr(m, "overview", "") or "")[:120],
                    "tmdb_id": getattr(m, "tmdb_id", ""),
                })
            if self._search_cache is not None:
                self._search_cache[user_id] = medias[:8]
            return {"keyword": keyword, "total_found": len(medias), "results": valid}
        except Exception as e:
            logger.error(f"search_media 异常: {e}", exc_info=True)
            return {"error": str(e)}

    def _tool_search_resources(self, keyword: str, user_id: str) -> dict:
        if not keyword:
            return {"error": "请提供搜索关键词"}
        try:
            from app.chain.media import MediaChain
            from app.chain.search import SearchChain

            title = keyword
            try:
                result = MediaChain().search(title=keyword)
                if isinstance(result, tuple) and len(result) == 2:
                    _, medias = result
                    if medias and not isinstance(medias[0], str) and hasattr(medias[0], "tmdb_id"):
                        raw_title = getattr(medias[0], "title", None)
                        if isinstance(raw_title, str) and raw_title:
                            title = raw_title
            except Exception:
                pass

            contexts = SearchChain().search_by_title(title=title)
            if not contexts:
                return {
                    "keyword": keyword, "title": title, "results": [],
                    "message": f"未找到「{title}」的下载资源",
                }

            results = []
            for i, ctx in enumerate(contexts[:20]):
                t = getattr(ctx, "torrent_info", None)
                if not t:
                    continue
                tname = getattr(t, "title", "") or getattr(t, "description", "") or ""
                results.append({
                    "index": i,
                    "title": tname,
                    "site": getattr(t, "site_name", ""),
                    "size": getattr(t, "size", ""),
                    "seeders": getattr(t, "seeders", ""),
                    "tags": _extract_tags(tname),
                })
            if self._resource_cache is not None:
                self._resource_cache[user_id] = contexts[:20]
            return {
                "keyword": keyword, "title": title,
                "total_found": len(contexts), "showing": len(results),
                "results": results,
            }
        except Exception as e:
            logger.error(f"search_resources 异常: {e}", exc_info=True)
            return {"error": str(e)}

    def _tool_download_resource(self, index: int, confirmed: bool, user_id: str) -> dict:
        """下载资源 — confirmed=false 返回详情并缓存待确认状态，confirmed=true 执行下载"""
        cached = (self._resource_cache or {}).get(user_id, [])

        if confirmed:
            pending = (self._pending_download or {}).get(user_id)
            if pending and (index == -1 or index == pending["index"]):
                index = pending["index"]
            if not cached:
                return {"status": "error", "message": "没有缓存的搜索结果，请先搜索资源。"}
            if index < 0 or index >= len(cached):
                if pending:
                    index = pending["index"]
                else:
                    return {"error": f"序号 {index} 无效且无待确认下载，请先选择资源。"}

            ctx = cached[index]
            t = getattr(ctx, "torrent_info", None)
            title = getattr(t, "title", "未知") if t else "未知"

            try:
                if not getattr(ctx, "media_info", None):
                    try:
                        from app.chain.media import MediaChain
                        _meta = getattr(ctx, "meta_info", None)
                        _media = MediaChain().recognize_media(meta=_meta)
                        if _media:
                            ctx.media_info = _media
                            logger.info(f"download_resource: 补充媒体识别成功 title={title}")
                        else:
                            logger.warning(f"download_resource: 无法识别媒体信息 title={title}")
                    except Exception as me:
                        logger.warning(f"download_resource: 媒体识别异常: {me}")

                from app.chain.download import DownloadChain
                result = DownloadChain().download_single(context=ctx, userid="feishu")
                if self._pending_download is not None:
                    self._pending_download.pop(user_id, None)
                if result:
                    return {"success": True, "title": title, "message": f"✅ 已添加下载: {title}"}
                else:
                    return {"success": False, "title": title, "message": "下载提交失败"}
            except Exception as e:
                logger.error(f"download_resource 异常: {e}", exc_info=True)
                return {"error": str(e)}

        if not cached:
            return {"status": "error", "message": "当前没有缓存的搜索结果。请先用 search_resources 搜索。"}
        if index < 0 or index >= len(cached):
            return {"error": f"序号 {index} 无效，有效范围: 0-{len(cached)-1}"}

        ctx = cached[index]
        t = getattr(ctx, "torrent_info", None)
        title = getattr(t, "title", "未知") if t else "未知"
        size = getattr(t, "size", "未知") if t else "未知"
        site = getattr(t, "site_name", "未知") if t else "未知"

        if self._pending_download is not None:
            self._pending_download[user_id] = {"index": index, "title": title, "size": size, "site": site}

        return {
            "status": "pending_confirmation",
            "index": index, "title": title, "size": size, "site": site,
            "tags": _extract_tags(title),
            "message": (
                f"资源「{title}」（{site}, {size}）等待用户确认。"
                "请向用户展示资源信息并明确询问是否确认下载。"
                "用户确认后调用 download_resource(index={idx}, confirmed=true) 执行下载。".format(idx=index)
            ),
        }

    def _tool_subscribe_media(self, index: Optional[int], keyword: Optional[str], user_id: str) -> dict:
        mediainfo = None
        if index is not None:
            cached = (self._search_cache or {}).get(user_id, [])
            if 0 <= index < len(cached):
                mediainfo = cached[index]

        if not mediainfo and keyword:
            try:
                from app.chain.media import MediaChain
                result = MediaChain().search(title=keyword)
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
                mtype=mtype, title=title,
                year=getattr(mediainfo, "year", ""),
                tmdbid=getattr(mediainfo, "tmdb_id", None),
                doubanid=getattr(mediainfo, "douban_id", None),
                exist_ok=True, username="飞书用户",
            )
            if sid:
                return {"success": True, "title": title, "message": f"已订阅: {title}"}
            else:
                return {"success": False, "title": title, "message": err_msg or "订阅失败"}
        except Exception as e:
            logger.error(f"subscribe_media 异常: {e}", exc_info=True)
            return {"error": str(e)}

    def _tool_get_downloading(self) -> dict:
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

    # ══════════════════════════════════════════════════════════════════════
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
        result = self._tool_search_media(keyword, user_id)
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
        result = self._tool_subscribe_media(None, keyword, user_id)
        msg = result.get("message") or result.get("error", "操作失败")
        if result.get("success"):
            self._feishu.send_card(
                chat_id,
                _CardBuilder.notify_card("✅ 订阅成功", msg, "green"),
            )
        else:
            self._feishu.send_card(chat_id, _CardBuilder.error_card(msg))

    def _legacy_downloading(self, chat_id: str, msg_id: str):
        result = self._tool_get_downloading()
        tasks = result.get("tasks", [])
        total = result.get("total", len(tasks))
        self._feishu.send_card(chat_id, _CardBuilder.downloading_card(tasks, total))

    # ══════════════════════════════════════════════════════════════════════
    #  诊断 / 帮助 (v5.0.0: 卡片化)
    # ══════════════════════════════════════════════════════════════════════

    def _cmd_status(self, chat_id: str, msg_id: str):
        model = self._openrouter_model or _OpenRouterClient.DEFAULT_MODEL
        conv = self._conversations.active_users if self._conversations else 0
        cache_media = len(self._search_cache) if self._search_cache else 0
        cache_res = len(self._resource_cache) if self._resource_cache else 0

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
        if self._llm_client:
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
            "conversations": conv,
            "msg_count": getattr(self, "_msg_count", 0),
            "agent_count": getattr(self, "_agent_count", 0),
            "legacy_count": getattr(self, "_legacy_count", 0),
            "recover_count": getattr(self, "_recover_count", 0),
            "cache_media": cache_media,
            "cache_res": cache_res,
        }
        self._feishu.send_card(
            chat_id,
            _CardBuilder.status_card(info),
            reply_msg_id=msg_id,
        )

    def _cmd_help(self, chat_id: str, msg_id: str):
        agent_on = self._llm_client is not None
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
                    target=self._card_download, args=(idx, user_id, chat_id), daemon=True,
                ).start()
            elif act == "download_resource_confirm":
                idx = int(value.get("index", 0))
                threading.Thread(
                    target=self._card_download_confirmed, args=(idx, user_id, chat_id), daemon=True,
                ).start()
            elif act == "subscribe":
                idx = int(value.get("index", 0))
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
        result = self._tool_download_resource(idx, confirmed=False, user_id=user_id)
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
        result = self._tool_download_resource(idx, confirmed=True, user_id=user_id)
        msg = result.get("message") or result.get("error", "操作失败")
        if result.get("success"):
            self._feishu.send_card(
                chat_id,
                _CardBuilder.notify_card("✅ 下载已添加", msg, "green"),
            )
        else:
            self._feishu.send_card(chat_id, _CardBuilder.error_card(msg))

    def _card_subscribe(self, idx: int, user_id: str, chat_id: str):
        result = self._tool_subscribe_media(idx, None, user_id)
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
        result = self._tool_search_resources(keyword, user_id)
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
                                    "💡 WebSocket 长连接模式（推荐）：无需公网 IP、域名或 HTTPS，NAS/Docker 友好。\n"
                                    "需安装 lark-oapi：在容器中执行 pip install lark-oapi\n"
                                    "飞书应用后台 → 事件订阅 → 选择「使用长连接接收」\n\n"
                                    "关闭 WebSocket 后回退到 HTTP 回调模式（需公网可达地址）。"
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
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "openrouter_key", "label": "OpenRouter API Key", "placeholder": "sk-or-..."}},
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 5}, "content": [
                                {"component": "VTextField", "props": {"model": "openrouter_model", "label": "模型 (可选)", "placeholder": "默认: google/gemini-2.5-flash-preview:free"}},
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VAlert", "props": {
                                "type": "info", "variant": "tonal",
                                "text": (
                                    "开启 AI Agent 后，机器人将化身智能体：自动理解自然语言、"
                                    "按偏好筛选资源（4K/杜比/5.1 等）、多轮对话确认后下载。\n"
                                    "API Key: https://openrouter.ai/settings/keys"
                                ),
                            }},
                        ]}],
                    },
                ],
            }
        ], {
            "enabled": False, "use_ws": True, "app_id": "", "app_secret": "", "chat_id": "",
            "msgtypes": ["transfer", "download"],
            "llm_enabled": False, "openrouter_key": "", "openrouter_model": "",
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
            agent_active = self._llm_client is not None
            model = self._openrouter_model or "default"
            try:
                model = self._openrouter_model or _OpenRouterClient.DEFAULT_MODEL
            except Exception:
                pass

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
