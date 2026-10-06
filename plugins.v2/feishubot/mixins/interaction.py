"""Feishu bot legacy commands, cards, and diagnostics helpers."""

import re
import json

from ..ai.executor import ToolExecutor
from ..utils import _extract_tags
from datetime import datetime

from app.log import logger

from ..card_builder import _CardBuilder
from ..state import cache_counts
from ..utils import _HAS_LARK_SDK


class FeishuInteractionMixin:
        def _run_legacy_tool(self, name, args, session_key):
            executor = ToolExecutor(self._get_session_state(session_key))
            executor.bind(extract_tags=_extract_tags)
            result = executor.execute(name, args)
            if not result.success:
                return {"error": result.error}
            return dict(result.data or {}, success=True)

        def _legacy_tool_search_media(self, keyword: str, session_key: str = "") -> dict:
            result = self._run_legacy_tool("search_media", {"keyword": keyword}, session_key)
            result["search_id"] = self._get_session_state(session_key).media_search_id
            return result

        def _legacy_tool_search_resources(self, keyword: str, session_key: str = "") -> dict:
            result = self._run_legacy_tool("search_resources", {"keyword": keyword}, session_key)
            result["search_id"] = self._get_session_state(session_key).resource_search_id
            return result

        def _legacy_tool_download_resource(self, index: int, confirmed: bool = False,
                                           session_key: str = "", search_id: str = "",
                                           selection_id: str = "") -> dict:
            return self._run_legacy_tool("download_resource", {
                "index": index, "confirmed": confirmed,
                "search_id": search_id, "selection_id": selection_id,
            }, session_key)

        def _legacy_tool_subscribe_media(self, keyword=None, idx=None, session_key=""):
            return self._run_legacy_tool("subscribe_media", {"keyword": keyword, "index": idx}, session_key)

        def _legacy_tool_get_downloading(self):
            return self._run_legacy_tool("get_downloading", {}, "")

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
            session_key = self._session_key(chat_id, user_id)
            # 即时反馈
            self._feishu.send_card(
                chat_id,
                _CardBuilder.notify_card("🔍 搜索中...", f"正在搜索「{keyword}」，请稍候...", "indigo"),
            )
            result = self._legacy_tool_search_media(keyword, session_key=session_key)
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
            self._feishu.send_card(chat_id, _CardBuilder.search_result_card(keyword, items, search_id=result.get("search_id", "")))

        def _legacy_subscribe(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
            if not keyword:
                return
            session_key = self._session_key(chat_id, user_id)
            self._feishu.send_card(
                chat_id,
                _CardBuilder.notify_card("📥 订阅中...", f"正在订阅「{keyword}」...", "indigo"),
            )
            result = self._legacy_tool_subscribe_media(keyword, session_key=session_key)
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

        def _cmd_status(self, chat_id: str, msg_id: str, state=None):
            model = self._get_ai_status_model()
            cache_media, cache_res = cache_counts(state) if state is not None else (0, 0)

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
                elif self._ws_transport is not None and self._ws_transport.connected:
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

        def _handle_card_action(self, data: dict) -> dict:
            if not self._enabled or not self._ensure_runtime_ready():
                return {"code": 0}
            event = data.get("event", {})
            value = event.get("action", {}).get("value", {})
            act = value.get("action", "")
            user_id = event.get("operator", {}).get("open_id", "")
            ctx = event.get("context", {})
            chat_id = ctx.get("open_chat_id", "") or self._chat_id
            session_key = self._session_key(chat_id, user_id)
            event_id = data.get("header", {}).get("event_id", "") or event.get("token", "")
            if event_id and not self._record_message_once("card:" + event_id):
                return {"code": 0}
            actions = {
                "download_resource": (self._card_download, (int(value.get("index", 0)), user_id, chat_id, value.get("search_id", ""))),
                "download_resource_confirm": (self._card_download_confirmed, (int(value.get("index", 0)), user_id, chat_id, value.get("selection_id", ""))),
                "cancel_download": (self._card_cancel, (user_id, chat_id, value.get("selection_id", ""))),
                "subscribe": (self._card_subscribe, (int(value.get("index", 0)), user_id, chat_id, value.get("search_id", ""))),
                "search_resources_by_title": (self._card_search_resources, (value.get("keyword", ""), user_id, chat_id)),
            }
            task = actions.get(act)
            if task and not self._enqueue_task(task[0], *task[1]):
                self._reply_error(chat_id, "", "消息队列已满，请稍后重试。")
                if event_id:
                    with self._seen_msg_ids_lock:
                        self._seen_msg_ids.pop("card:" + event_id, None)
            return {"code": 0}

        def _card_download(self, idx: int, user_id: str, chat_id: str, search_id: str = ""):
            """卡片按钮: 展示下载确认"""
            session_key = self._session_key(chat_id, user_id)
            if not search_id:
                self._reply_error(chat_id, "", "这张旧资源卡片已过期，请重新搜索。")
                return
            result = self._legacy_tool_download_resource(idx, confirmed=False, session_key=session_key, search_id=search_id)
            if result.get("error"):
                self._feishu.send_card(chat_id, _CardBuilder.error_card(result["error"]))
                return
            self._record_card_history(session_key, f"选择第 {idx + 1} 项资源", result)
            self._feishu.send_card(
                chat_id,
                _CardBuilder.download_confirm_card(
                    idx,
                    result.get("title", "未知"),
                    result.get("site", "未知"),
                    result.get("size", "未知"),
                    result.get("tags", {}),
                    selection_id=result.get("selection_id", ""),
                ),
            )

        def _card_download_confirmed(self, idx: int, user_id: str, chat_id: str, selection_id: str = ""):
            """卡片按钮: 确认下载"""
            session_key = self._session_key(chat_id, user_id)
            if not selection_id:
                self._reply_error(chat_id, "", "这张旧确认卡片已过期，请重新选择资源。")
                return
            result = self._legacy_tool_download_resource(idx, confirmed=True, session_key=session_key, selection_id=selection_id)
            self._record_card_history(session_key, "确认下载选中的资源", result)
            msg = result.get("message") or result.get("error", "操作失败")
            if result.get("success"):
                self._feishu.send_card(
                    chat_id,
                    _CardBuilder.notify_card("✅ 下载已添加", msg, "green"),
                )
            else:
                self._feishu.send_card(chat_id, _CardBuilder.error_card(msg))

        def _card_subscribe(self, idx: int, user_id: str, chat_id: str, search_id: str = ""):
            session_key = self._session_key(chat_id, user_id)
            if not search_id or search_id != self._get_session_state(session_key).media_search_id:
                self._reply_error(chat_id, "", "这张影视卡片已过期，请重新搜索。")
                return
            result = self._legacy_tool_subscribe_media(None, idx=idx, session_key=session_key)
            self._record_card_history(session_key, f"订阅第 {idx + 1} 部作品", result)
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
            session_key = self._session_key(chat_id, user_id)
            self._feishu.send_card(
                chat_id,
                _CardBuilder.notify_card("📦 搜索资源中...", f"正在搜索「{keyword}」的下载资源...", "indigo"),
            )
            result = self._legacy_tool_search_resources(keyword, session_key=session_key)
            if result.get("error"):
                self._feishu.send_card(chat_id, _CardBuilder.error_card(result["error"]))
                return
            self._record_card_history(session_key, f"搜索「{keyword}」的资源", result)
            items = result.get("results", [])
            title = result.get("title", keyword)
            self._feishu.send_card(chat_id, _CardBuilder.resource_result_card(keyword, title, items, search_id=result.get("search_id", "")))

        def _record_card_history(self, session_key, user_text, result):
            engine = self._get_or_create_engine(session_key)
            # Result contains only the public view, never Context credentials/URLs.
            self._append_history_pair(engine, user_text, json.dumps(result, ensure_ascii=False, default=str))

        def _card_cancel(self, user_id, chat_id, selection_id):
            session_key = self._session_key(chat_id, user_id)
            state = self._get_session_state(session_key)
            pending = state.pending_download
            if not pending or not selection_id or pending.get("selection_id") != selection_id:
                self._reply_error(chat_id, "", "这张确认卡片已过期。")
                return
            title = pending.get("title", "未知资源")
            state.clear_download()
            result = {"message": f"已取消下载: {title}"}
            self._record_card_history(session_key, "取消下载", result)
            self._reply_notify(chat_id, "", "已取消", result["message"], "grey")
