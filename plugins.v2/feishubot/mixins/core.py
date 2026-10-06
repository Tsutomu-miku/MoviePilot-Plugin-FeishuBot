"""Feishu bot core state/session helpers."""

import json as _json
import re
import threading
import time as _time
from typing import Any, Callable, List, Optional, Tuple

from app.log import logger

from ..ai import ChatEngine
from ..ai.llm import normalize_model_name
from ..ai.types import ChatState
from ..card_builder import _CardBuilder
from ..state import bind_engine_state, sync_state_cache
from ..utils import _extract_tags


class FeishuCoreMixin:
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
        def _session_key(chat_id: str, user_id: str) -> str:
            # Replies and quoted messages stay in the same chat/user session.
            return _json.dumps([chat_id, user_id], ensure_ascii=False)

        def _get_or_create_engine(self, session_key: str) -> Optional[ChatEngine]:
            if not (self._llm_enabled and self._openrouter_key and self._openrouter_model):
                return None
            if self._engines is None:
                self._engines = {}
            if self._engine_pool_lock is None:
                self._engine_pool_lock = threading.RLock()
            with self._engine_pool_lock:
                engine = self._engines.get(session_key)
                if engine is None:
                    engine = self._create_chat_engine(session_key)
                    self._engines[session_key] = engine
                return engine

        def _create_chat_engine(self, session_key: str) -> ChatEngine:
            engine = ChatEngine(api_key=self._openrouter_key, model=self._openrouter_model)
            bind_engine_state(engine, self._get_session_state(session_key))
            engine.executor.bind(extract_tags=_extract_tags)
            return engine

        def _get_session_state(self, session_key: str = "") -> ChatState:
            if self._session_states is None:
                self._session_states = {}
            if self._engine_pool_lock is None:
                self._engine_pool_lock = threading.RLock()
            with self._engine_pool_lock:
                return self._session_states.setdefault(session_key, ChatState())

        def _get_ai_status_model(self) -> str:
            for engine in (self._engines or {}).values():
                return engine.resolved_model_name
            return self._openrouter_model or "未配置"

        @classmethod
        def _is_direct_confirm_text(cls, text: str) -> bool:
            normalized = str(text or "").strip().lower()
            return normalized in cls._DIRECT_CONFIRM_TEXTS

        @classmethod
        def _is_direct_cancel_text(cls, text: str) -> bool:
            normalized = str(text or "").strip().lower()
            return normalized in cls._DIRECT_CANCEL_TEXTS

        @classmethod
        def _parse_cached_index_command(cls, text: str) -> Tuple[Optional[str], Optional[int]]:
            normalized = str(text or "").strip()
            for pattern in cls._DOWNLOAD_INDEX_PATTERNS:
                match = pattern.match(normalized)
                if match:
                    index = int(match.group(1)) - 1
                    return "download", index if index >= 0 else None

            for pattern in cls._SUBSCRIBE_INDEX_PATTERNS:
                match = pattern.match(normalized)
                if match:
                    index = int(match.group(1)) - 1
                    return "subscribe", index if index >= 0 else None

            return None, None

        def _record_message_once(self, msg_id: str) -> bool:
            if not msg_id:
                return True

            if self._seen_msg_ids is None:
                self._seen_msg_ids = {}
            if self._seen_msg_ids_lock is None:
                self._seen_msg_ids_lock = threading.Lock()

            now = _time.monotonic()
            cutoff = now - self._SEEN_MESSAGE_TTL_SECONDS
            with self._seen_msg_ids_lock:
                if msg_id in self._seen_msg_ids:
                    return False

                self._seen_msg_ids[msg_id] = now
                expired = [key for key, ts in self._seen_msg_ids.items() if ts < cutoff]
                for key in expired:
                    self._seen_msg_ids.pop(key, None)
                return True

        @staticmethod
        def _append_history_pair(engine: Optional[ChatEngine], user_text: str, assistant_text: str):
            if not engine:
                return
            engine.history.append({"role": "user", "content": user_text})
            engine.history.append({"role": "assistant", "content": assistant_text})

        def _reply_card(self, chat_id: str, card: dict, msg_id: str = ""):
            if self._feishu:
                self._feishu.send_card(chat_id, card, reply_msg_id=msg_id or None)

        def _reply_error(self, chat_id: str, msg_id: str, message: str):
            self._reply_card(chat_id, _CardBuilder.error_card(message), msg_id)

        def _reply_notify(
            self,
            chat_id: str,
            msg_id: str,
            title: str,
            message: str,
            template: str = "blue",
        ):
            self._reply_card(chat_id, _CardBuilder.notify_card(title, message, template), msg_id)

        @staticmethod
        def _extract_message_text(msg: dict) -> str:
            try:
                text = _json.loads(msg.get("content", "{}")).get("text", "").strip()
            except Exception:
                text = ""

            mentions = msg.get("mentions")
            if mentions:
                for mention in mentions:
                    key = mention.get("key", "")
                    if key:
                        text = text.replace(key, "").strip()
            return text

        def _sync_engine_cache(
            self,
            session_key: str = "",
            *,
            search_cache: Optional[list] = None,
            resource_cache: Optional[list] = None,
        ) -> ChatState:
            return sync_state_cache(
                self._get_session_state(session_key),
                search_cache=search_cache,
                resource_cache=resource_cache,
            )

        def _handle_system_text_command(
            self,
            text: str,
            engine: Optional[ChatEngine],
            chat_id: str,
            msg_id: str,
            session_key: str = "",
        ) -> bool:
            if text.startswith("/status") or text.startswith("/状态"):
                self._cmd_status(chat_id, msg_id, state=engine.state if engine else self._get_session_state(session_key))
                return True

            if text in ("/clear", "/清除", "清除对话", "重新开始"):
                if engine:
                    engine.reset()
                else:
                    self._get_session_state(session_key).clear_all()
                self._reply_notify(chat_id, msg_id, "🗑️ 对话已清除", "历史会话已重置，可以开始新的对话。", "green")
                return True

            if text.startswith("/help") or text.startswith("/帮助"):
                self._cmd_help(chat_id, msg_id)
                return True

            return False

        def _handle_quick_text_action(
            self,
            text: str,
            engine: Optional[ChatEngine],
            chat_id: str,
            msg_id: str,
            session_key: str,
        ) -> bool:
            return (
                self._try_handle_direct_pending_action(text, engine, chat_id, msg_id, session_key)
                or self._try_handle_cached_index_action(text, engine, chat_id, msg_id, session_key)
            )

        def _dispatch_serial_task(
            self,
            session_key: str,
            token: str,
            task_name: str,
            func: Callable,
            *args,
        ) -> bool:
            return self._enqueue_task(func, *args)

        def _try_handle_cached_index_action(
            self,
            text: str,
            engine: Optional[ChatEngine],
            chat_id: str,
            msg_id: str,
            session_key: str = "",
        ) -> bool:
            action, index = self._parse_cached_index_command(text)
            if action is None:
                return False

            if index is None:
                message = "序号必须从 1 开始，请重新输入，例如“下载1号”。"
                self._append_history_pair(engine, text, message)
                self._reply_error(chat_id, msg_id, message)
                return True

            state = engine.state if engine is not None else self._get_session_state(session_key)
            if action == "download":
                if not state.resource_cache:
                    message = "当前没有可用的资源列表上下文，请先搜索资源后再发送“下载11号”。"
                    self._append_history_pair(engine, text, message)
                    self._reply_error(chat_id, msg_id, message)
                    logger.info(f"[Agent] 序号下载缺少资源缓存: session={session_key}, text={text}")
                    return True

                if engine is not None:
                    result = engine.executor.execute(
                        "download_resource",
                        {"index": index, "confirmed": False},
                    )
                    success = result.success
                    data = result.data if isinstance(result.data, dict) else {}
                    error = result.error
                else:
                    data = self._legacy_tool_download_resource(index, confirmed=False, session_key=session_key)
                    success = not bool(data.get("error"))
                    error = data.get("error")

                if not success or not isinstance(data, dict):
                    message = error or "下载预览失败"
                    self._append_history_pair(engine, text, message)
                    self._reply_error(chat_id, msg_id, message)
                    return True

                message = data.get("message") or f"已选中第 {index + 1} 项资源，等待确认下载。"
                self._append_history_pair(engine, text, message)
                self._reply_card(
                    chat_id,
                    _CardBuilder.download_confirm_card(
                        index,
                        data.get("title", "未知"),
                        data.get("site", "未知"),
                        data.get("size", "未知"),
                        data.get("tags", {}),
                        selection_id=data.get("selection_id", ""),
                    ),
                    msg_id,
                )
                logger.info(f"[Agent] 直接命中资源序号下载: session={session_key}, index={index}")
                return True

            if not state.search_cache:
                message = "当前没有可用的影视搜索结果，请先搜索作品后再发送“订阅2”。"
                self._append_history_pair(engine, text, message)
                self._reply_error(chat_id, msg_id, message)
                logger.info(f"[Agent] 序号订阅缺少搜索缓存: session={session_key}, text={text}")
                return True

            if engine is not None:
                result = engine.executor.execute("subscribe_media", {"index": index})
                success = result.success
                data = result.data if isinstance(result.data, dict) else {}
                error = result.error
            else:
                data = self._legacy_tool_subscribe_media(idx=index, session_key=session_key)
                success = bool(data.get("success"))
                error = data.get("error")

            message = ""
            if success and isinstance(data, dict):
                message = data.get("message") or "已添加订阅"
                card = _CardBuilder.notify_card("✅ 订阅成功", message, "green")
            else:
                message = error or "订阅失败"
                card = _CardBuilder.error_card(message)
            self._append_history_pair(engine, text, message)
            self._reply_card(chat_id, card, msg_id)
            logger.info(f"[Agent] 直接命中影视序号订阅: session={session_key}, index={index}")
            return True

        def _cleanup_stale_sessions(self):
            if not self._engines or self._engine_pool_lock is None:
                return

            stale_keys = []
            with self._engine_pool_lock:
                for session_key, engine in list(self._engines.items()):
                    if engine.history.is_stale(self._SESSION_TTL_SECONDS):
                        stale_keys.append(session_key)

                for session_key in stale_keys:
                    engine = self._engines.pop(session_key, None)
                    self._session_states.pop(session_key, None)
                    if engine is not None:
                        try:
                            engine.reset()
                        except Exception:
                            pass

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
            state = engine.state if engine is not None else self._get_session_state(session_key)
            if state is None:
                return False

            pending = state.pending_download
            if not pending:
                return False

            normalized = str(text or "").strip()

            if self._is_direct_confirm_text(normalized):
                title = pending.get("title", "未知资源")
                action_key = f"text_confirm::{session_key}::{pending.get('index', -1)}::{title}"
                if not self._mark_action_once(action_key):
                    logger.info(f"[Agent] 忽略重复确认: session={session_key}, title={title}")
                    self._reply_notify(chat_id, msg_id, "📝 已受理", f"下载确认已在处理中: {title}", "grey")
                    return True
                if engine is not None:
                    engine.history.append({"role": "user", "content": normalized})
                    result = engine.executor.execute(
                        "download_resource",
                        {"index": -1, "confirmed": True},
                    )
                else:
                    result_dict = self._legacy_tool_download_resource(-1, confirmed=True, session_key=session_key)
                    result = type("LegacyResult", (), {
                        "success": bool(result_dict.get("success")),
                        "data": result_dict,
                        "error": result_dict.get("error"),
                    })()
                message = "✅ 已确认下载"
                if result.success and isinstance(result.data, dict):
                    message = result.data.get("message") or message
                elif not result.success:
                    message = result.error or "下载失败"
                if engine is not None:
                    engine.history.append({"role": "assistant", "content": message})
                card = (
                    _CardBuilder.notify_card("✅ 下载已添加", message, "green")
                    if result.success
                    else _CardBuilder.error_card(message)
                )
                self._reply_card(chat_id, card, msg_id)
                logger.info(f"[Agent] 直接确认待下载资源: session={session_key}, title={title}")
                return True

            if self._is_direct_cancel_text(normalized):
                title = pending.get("title", "未知资源")
                if engine is not None:
                    engine.history.append({"role": "user", "content": normalized})
                state.clear_download()
                message = f"已取消下载确认: {title}"
                if engine is not None:
                    engine.history.append({"role": "assistant", "content": message})
                self._reply_notify(chat_id, msg_id, "🛑 已取消", message, "grey")
                logger.info(f"[Agent] 已取消待下载资源: session={session_key}, title={title}")
                return True

            return False
