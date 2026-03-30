"""
主插件类 __init__.py 中 AI 对话部分的改造示例。

本文件不是可运行的代码，而是展示「如何用 ChatEngine 替换旧的 6 个 AI 方法」。
实际操作时，只需替换 __init__.py 中对应的部分即可。
"""


# ══════════════════════════════════════════════════════════════════════
#  改造点 1: 导入（替换旧导入）
# ══════════════════════════════════════════════════════════════════════

# ── 删除这些旧导入 ──
# from .llm_client import _OpenRouterClient
# from .conversation import _ConversationManager, _sanitize_assistant_message
# from .agent_tools import _AGENT_TOOLS, _AGENT_SYSTEM_PROMPT

# ── 新增这一行 ──
# from .ai import ChatEngine


# ══════════════════════════════════════════════════════════════════════
#  改造点 2: 类属性（删除旧的并发控制属性）
# ══════════════════════════════════════════════════════════════════════

# ── 删除这些属性 ──
# _llm_client: Optional[_OpenRouterClient] = None
# _conversations: Optional[_ConversationManager] = None
# _user_locks: Optional[dict] = None
# _user_pending_msg: Optional[dict] = None
# _user_interrupted: Optional[dict] = None
# _user_processing: Optional[dict] = None
# _dispatch_lock: threading.Lock = threading.Lock()
# _MAX_AGENT_ITERATIONS = 10
# _MSG_MERGE_DELAY = 1.5

# ── 替换为 ──
# _engine: Optional[ChatEngine] = None


# ══════════════════════════════════════════════════════════════════════
#  改造点 3: init_plugin（初始化 ChatEngine）
# ══════════════════════════════════════════════════════════════════════

def init_plugin_ai_section(self, openrouter_key, openrouter_model):
    """init_plugin 中 AI 初始化部分的替换代码"""
    from .ai import ChatEngine
    from .utils import _extract_tags

    self._engine = None  # 先清空

    if self._llm_enabled and openrouter_key:
        try:
            self._engine = ChatEngine(
                api_key=openrouter_key,
                model=openrouter_model,
            )
            # 注入外部依赖
            self._engine.executor.bind(extract_tags=_extract_tags)

            logger.info(
                f"飞书 Agent 模式已启用 ✓ 模型: {self._engine.model_name}"
            )
        except Exception as e:
            logger.error(f"飞书 Agent 初始化失败: {e}", exc_info=True)
            self._engine = None
    elif self._llm_enabled:
        logger.warning("飞书 AI Agent 已启用但 API Key 未配置，回退到传统模式")


# ══════════════════════════════════════════════════════════════════════
#  改造点 4: _handle_message 中的路由判断
# ══════════════════════════════════════════════════════════════════════

def handle_message_routing_section(self, text, chat_id, msg_id, user_id):
    """_handle_message 中 Agent 路由部分的替换代码"""

    # ── /clear 指令 ──
    if text in ("/clear", "/清除", "清除对话", "重新开始"):
        if self._engine:
            self._engine.reset()
        if self._feishu:
            self._feishu.send_card(
                chat_id,
                _CardBuilder.notify_card("🗑️ 对话已清除", "历史会话已重置，可以开始新的对话。", "green"),
                reply_msg_id=msg_id,
            )
        return

    # ── Agent 模式（替换原 _agent_dispatch → _agent_merge_and_run → _agent_handle） ──
    if self._engine is not None:
        logger.info(f"[Agent] 路由到 ChatEngine: {text[:80]}")
        # 直接在 daemon 线程中调用，不再需要消息合并和打断机制
        import threading
        threading.Thread(
            target=self._agent_handle_v2,
            args=(text, chat_id, msg_id),
            daemon=True,
        ).start()
        return

    # ── 传统模式 ──
    self._legacy_handle(text, chat_id, msg_id, user_id)


# ══════════════════════════════════════════════════════════════════════
#  改造点 5: 新的 _agent_handle_v2（替换旧的 6 个方法）
# ══════════════════════════════════════════════════════════════════════

def _agent_handle_v2(self, text: str, chat_id: str, msg_id: str):
    """
    全新 Agent 处理方法 — 替换旧的 _agent_dispatch / _agent_merge_and_run /
    _agent_handle / _agent_loop / _execute_tool 共 5 个方法。

    极度简化：
    1. 发送「处理中」卡片
    2. engine.chat_with_progress(text) — 一行搞定
    3. 更新卡片为最终回复
    """
    import time as _time
    _t0 = _time.monotonic()

    # ── 即时反馈 ──
    processing_card = _CardBuilder.processing_card(text)
    send_result = self._feishu.send_card(chat_id, processing_card, reply_msg_id=msg_id)
    status_msg_id = ""
    try:
        status_msg_id = send_result.get("data", {}).get("message_id", "")
    except Exception:
        pass

    try:
        # ── 进度回调: 更新卡片 ──
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

        # ══ 核心: 一行调用 ══
        reply, steps = self._engine.chat_with_progress(
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
        logger.info(f"[Agent] 完成: elapsed={_elapsed:.1f}s")


# ══════════════════════════════════════════════════════════════════════
#  改造点 6: 删除旧方法
# ══════════════════════════════════════════════════════════════════════
#
# 以下方法全部删除:
#   - _get_user_lock()
#   - _agent_dispatch()
#   - _agent_merge_and_run()
#   - _agent_handle()
#   - _agent_loop()
#   - _execute_tool()
#   - _tool_search_media()
#   - _tool_search_resources()
#   - _tool_download_resource()
#   - _tool_subscribe_media()
#   - _tool_get_downloading()
#   - _tool_friendly_name()
#
# 共计删除约 450 行代码。


# ══════════════════════════════════════════════════════════════════════
#  改造点 7: 删除旧文件
# ══════════════════════════════════════════════════════════════════════
#
# 以下文件不再需要，可以删除:
#   - plugins.v2/feishubot/llm_client.py    → 被 ai/llm.py 替代
#   - plugins.v2/feishubot/conversation.py  → 被 ai/history.py 替代
#   - plugins.v2/feishubot/agent_tools.py   → 被 ai/tools.py + ai/prompts.py 替代
