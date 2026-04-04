"""Feishu bot legacy commands, cards, and diagnostics helpers."""

import re
from datetime import datetime

from app.log import logger

from ..card_builder import _CardBuilder
from ..state import cache_counts
from ..utils import _HAS_LARK_SDK


class FeishuInteractionMixin:
        def _legacy_tool_search_media(self, keyword: str, session_key: str = "") -> dict:
            """传统模式: 搜索影视"""
            try:
                from app.chain.search import SearchChain
                results = SearchChain().search_medias(title=keyword)
                if not results:
                    return {"results": [], "message": f"未找到与 '{keyword}' 相关的内容"}

                self._sync_engine_cache(session_key, search_cache=results[:10])

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

        def _legacy_tool_search_resources(self, keyword: str, session_key: str = "") -> dict:
            """传统模式: 搜索资源"""
            try:
                from app.chain.search import SearchChain
                contexts = SearchChain().search_torrents(title=keyword)
                if not contexts:
                    return {"results": [], "message": f"未找到 '{keyword}' 的下载资源"}

                self._sync_engine_cache(session_key, resource_cache=contexts[:15])

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
                from app.chain.download import DownloadChain
                state = self._get_session_state()
                cache = state.resource_cache
                if not cache:
                    return {"error": "没有可用的资源缓存，请先搜索资源"}
                if index < 0 or index >= len(cache):
                    pending = state.pending_download
                    if confirmed and pending:
                        index = pending.get("index", index)
                    else:
                        return {"error": f"索引 {index} 超出范围 (0-{len(cache)-1})"}
                ctx = cache[index]
                t = ctx.torrent_info
                if not confirmed:
                    state.pending_download = {
                        "index": index,
                        "title": t.title or "未知",
                        "site": t.site_name or "未知",
                        "size": f"{t.size / (1024**3):.1f} GB" if t.size else "未知",
                    }
                    return {
                        "title": t.title or "未知",
                        "site": t.site_name or "未知",
                        "size": f"{t.size / (1024**3):.1f} GB" if t.size else "未知",
                        "tags": {"seeders": t.seeders or 0},
                    }
                DownloadChain().download_single(ctx)
                state.clear_download()
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
                state = self._get_session_state()
                if idx is not None and state.search_cache:
                    cache = state.search_cache
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
            self._feishu.send_card(chat_id, _CardBuilder.search_result_card(keyword, items))

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

        def _cmd_status(self, chat_id: str, msg_id: str):
            model = self._get_ai_status_model()
            cache_media, cache_res = cache_counts(self._get_session_state())

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
                    self._dispatch_serial_task(
                        session_key,
                        callback_token,
                        "card_download",
                        self._card_download,
                        idx,
                        user_id,
                        chat_id,
                    )
                elif act == "download_resource_confirm":
                    idx = int(value.get("index", 0))
                    action_key = f"card_confirm::{session_key}::{idx}"
                    if not self._mark_action_once(action_key):
                        logger.info(f"[Card] 忽略重复下载确认: trace={trace_id}, session={session_key}, index={idx}")
                        return {"code": 0}
                    self._dispatch_serial_task(
                        session_key,
                        callback_token,
                        "card_download_confirmed",
                        self._card_download_confirmed,
                        idx,
                        user_id,
                        chat_id,
                    )
                elif act == "subscribe":
                    idx = int(value.get("index", 0))
                    action_key = f"card_subscribe::{session_key}::{idx}"
                    if not self._mark_action_once(action_key):
                        logger.info(f"[Card] 忽略重复订阅: trace={trace_id}, session={session_key}, index={idx}")
                        return {"code": 0}
                    self._dispatch_serial_task(
                        session_key,
                        callback_token,
                        "card_subscribe",
                        self._card_subscribe,
                        idx,
                        user_id,
                        chat_id,
                    )
                elif act == "search_resources_by_title":
                    kw = value.get("keyword", "")
                    self._dispatch_serial_task(
                        session_key,
                        callback_token,
                        "card_search_resources",
                        self._card_search_resources,
                        kw,
                        user_id,
                        chat_id,
                    )
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
            session_key = self._session_key(chat_id, user_id)
            self._feishu.send_card(
                chat_id,
                _CardBuilder.notify_card("📦 搜索资源中...", f"正在搜索「{keyword}」的下载资源...", "indigo"),
            )
            result = self._legacy_tool_search_resources(keyword, session_key=session_key)
            if result.get("error"):
                self._feishu.send_card(chat_id, _CardBuilder.error_card(result["error"]))
                return
            items = result.get("results", [])
            title = result.get("title", keyword)
            self._feishu.send_card(chat_id, _CardBuilder.resource_result_card(keyword, title, items))
