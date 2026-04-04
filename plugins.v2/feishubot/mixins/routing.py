"""Feishu bot inbound routing helpers."""

import threading
import time as _time
from typing import Any, Dict, List

from app.log import logger

from ..card_builder import _CardBuilder


class FeishuRoutingMixin:
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
                if not self._record_message_once(msg_id):
                    logger.info(f"[去重] 跳过重复消息 {msg_id}")
                    return

                if msg_type != "text":
                    if self._feishu:
                        self._feishu.send_card(
                            chat_id,
                            _CardBuilder.error_card("暂时只支持文字消息哦~")
                        )
                    return

                text = self._extract_message_text(msg)

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

                if not self._try_acquire_global_processing(session_key, msg_id):
                    if self._feishu:
                        self._feishu.send_text(chat_id, "⏳", reply_msg_id=msg_id)
                    return

                release_after_handle = True

                if self._handle_system_text_command(text, engine, chat_id, msg_id):
                    return

                if self._handle_quick_text_action(text, engine, chat_id, msg_id, session_key):
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

                    release_after_handle = False
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
            finally:
                if locals().get("release_after_handle"):
                    self._release_global_processing(
                        locals().get("session_key", ""),
                        locals().get("msg_id", ""),
                    )

        def _agent_handle_v2(self, session_key: str, text: str, chat_id: str, msg_id: str):
            """
            Agent 处理方法 — 全局单线程模式。
            """
            engine = self._get_or_create_engine(session_key)
            trace_id = self._build_trace_id("agent", session_key, msg_id)
            if engine is None:
                logger.error(f"[Agent] 会话引擎不可用: trace={trace_id}, session={session_key}")
                self._release_global_processing(session_key, msg_id)
                return

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

                reply, steps = engine.chat_with_progress(
                    text,
                    on_tool_start=on_tool_start,
                )

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
                self._release_global_processing(session_key, msg_id)
