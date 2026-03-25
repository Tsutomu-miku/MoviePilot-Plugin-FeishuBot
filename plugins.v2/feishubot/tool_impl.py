"""Agent 工具实现 — 返回结构化数据供 LLM 推理"""

import json
from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas import MediaType

from .tag_parser import extract_tags


class ToolExecutor:
    """
    管理所有 Agent 工具的实际执行逻辑和运行时缓存。

    每个工具方法返回 dict，由 Agent 循环序列化为 JSON 传回 LLM。
    """

    def __init__(self):
        # 用户级缓存
        self._search_cache: Dict[str, list] = {}     # user_id -> List[MediaInfo]
        self._resource_cache: Dict[str, list] = {}    # user_id -> List[Context]

    def execute(
        self, fn_name: str, fn_args: dict, chat_id: str, user_id: str,
        send_message_fn=None,
    ) -> dict:
        """
        工具路由器 — 根据函数名分发到具体实现。

        Args:
            fn_name: 工具名称
            fn_args: 工具参数
            chat_id: 飞书 chat_id
            user_id: 用户 open_id
            send_message_fn: 发送消息的回调函数 (text) -> None
        """
        try:
            if fn_name == "search_media":
                return self.search_media(fn_args.get("keyword", ""), user_id)

            elif fn_name == "search_resources":
                return self.search_resources(fn_args.get("keyword", ""), user_id)

            elif fn_name == "download_resource":
                return self.download_resource(
                    index=fn_args.get("index", 0),
                    confirmed=fn_args.get("confirmed", False),
                    user_id=user_id,
                )

            elif fn_name == "subscribe_media":
                return self.subscribe_media(
                    index=fn_args.get("index"),
                    keyword=fn_args.get("keyword"),
                    user_id=user_id,
                )

            elif fn_name == "get_downloading":
                return self.get_downloading()

            elif fn_name == "send_message":
                text = fn_args.get("text", "")
                if text and send_message_fn:
                    send_message_fn(text)
                return {"sent": True}

            else:
                return {"error": f"未知工具: {fn_name}"}

        except Exception as e:
            logger.error(f"工具 {fn_name} 执行异常: {e}", exc_info=True)
            return {"error": str(e)}

    # ════════════════════════════════════════════════════════════════
    #  搜索影视作品
    # ════════════════════════════════════════════════════════════════

    def search_media(self, keyword: str, user_id: str) -> dict:
        """搜索影视作品，返回结构化媒体列表"""
        if not keyword:
            return {"error": "请提供搜索关键词"}

        try:
            from app.chain.media import MediaChain

            result = MediaChain().search(title=keyword)

            if not isinstance(result, tuple) or len(result) != 2:
                return {"error": "搜索返回格式异常", "results": []}

            meta, medias = result
            if not medias:
                name = getattr(meta, "name", keyword) if meta else keyword
                return {
                    "keyword": keyword, "results": [],
                    "message": f"未找到「{name}」的相关结果",
                }

            valid = []
            for i, m in enumerate(medias[:8]):
                if not hasattr(m, "title"):
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

            self._search_cache[user_id] = medias[:8]
            return {
                "keyword": keyword,
                "total_found": len(medias),
                "results": valid,
            }
        except Exception as e:
            logger.error(f"search_media 异常: {e}", exc_info=True)
            return {"error": str(e)}

    # ════════════════════════════════════════════════════════════════
    #  搜索种子资源
    # ════════════════════════════════════════════════════════════════

    def search_resources(self, keyword: str, user_id: str) -> dict:
        """搜索种子资源，返回资源列表（含结构化标签）"""
        if not keyword:
            return {"error": "请提供搜索关键词"}

        try:
            from app.chain.media import MediaChain
            from app.chain.search import SearchChain

            # 尝试用 MediaChain 获取精确标题
            title = keyword
            try:
                result = MediaChain().search(title=keyword)
                if isinstance(result, tuple) and len(result) == 2:
                    _, medias = result
                    if medias and hasattr(medias[0], "title"):
                        title = medias[0].title or keyword
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
                tname = (
                    getattr(t, "title", "")
                    or getattr(t, "description", "")
                    or ""
                )
                results.append({
                    "index": i,
                    "title": tname,
                    "site": getattr(t, "site_name", ""),
                    "size": getattr(t, "size", ""),
                    "seeders": getattr(t, "seeders", ""),
                    "tags": extract_tags(tname),
                })

            self._resource_cache[user_id] = contexts[:20]
            return {
                "keyword": keyword,
                "title": title,
                "total_found": len(contexts),
                "showing": len(results),
                "results": results,
            }
        except Exception as e:
            logger.error(f"search_resources 异常: {e}", exc_info=True)
            return {"error": str(e)}

    # ════════════════════════════════════════════════════════════════
    #  下载资源（带强制确认机制）
    # ════════════════════════════════════════════════════════════════

    def download_resource(
        self, index: int, confirmed: bool, user_id: str
    ) -> dict:
        """
        下载指定序号的资源。

        **强制确认**：confirmed=false 时仅返回资源详情，不执行下载。
        只有 confirmed=true 时才会真正提交下载任务。
        """
        cached = self._resource_cache.get(user_id, [])
        if not cached:
            return {"error": "没有缓存的资源列表，请先调用 search_resources"}
        if index < 0 or index >= len(cached):
            return {"error": f"序号 {index} 无效，有效范围: 0-{len(cached) - 1}"}

        ctx = cached[index]
        t = getattr(ctx, "torrent_info", None)
        title = getattr(t, "title", "未知") if t else "未知"
        size = getattr(t, "size", "未知") if t else "未知"
        site = getattr(t, "site_name", "未知") if t else "未知"

        if not confirmed:
            # 仅返回资源详情，提示 LLM 向用户确认
            return {
                "status": "pending_confirmation",
                "index": index,
                "title": title,
                "size": size,
                "site": site,
                "tags": extract_tags(title),
                "message": (
                    f"资源「{title}」（{site}, {size}）等待用户确认。"
                    "请向用户展示资源信息并明确询问是否确认下载。"
                    "用户确认后再次调用 download_resource 并设置 confirmed=true。"
                ),
            }

        # 用户已确认 → 真正下载
        try:
            from app.chain.download import DownloadChain

            result = DownloadChain().download_single(context=ctx, userid="feishu")
            if result:
                return {
                    "success": True, "title": title,
                    "message": f"✅ 已添加下载: {title}",
                }
            else:
                return {
                    "success": False, "title": title,
                    "message": "下载提交失败，请检查下载器状态",
                }
        except Exception as e:
            logger.error(f"download_resource 异常: {e}", exc_info=True)
            return {"error": str(e)}

    # ════════════════════════════════════════════════════════════════
    #  订阅影视
    # ════════════════════════════════════════════════════════════════

    def subscribe_media(
        self, index: Optional[int], keyword: Optional[str], user_id: str
    ) -> dict:
        """订阅影视作品。优先从搜索缓存取，否则用关键词现搜。"""
        mediainfo = None

        if index is not None:
            cached = self._search_cache.get(user_id, [])
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
            mtype = (
                raw_type
                if (raw_type and hasattr(raw_type, "value"))
                else MediaType.MOVIE
            )

            sid, err_msg = SubscribeChain().add(
                mtype=mtype,
                title=title,
                year=getattr(mediainfo, "year", ""),
                tmdbid=getattr(mediainfo, "tmdb_id", None),
                doubanid=getattr(mediainfo, "douban_id", None),
                exist_ok=True,
                username="飞书用户",
            )
            if sid:
                return {"success": True, "title": title, "message": f"已订阅: {title}"}
            else:
                return {"success": False, "title": title, "message": err_msg or "订阅失败"}
        except Exception as e:
            logger.error(f"subscribe_media 异常: {e}", exc_info=True)
            return {"error": str(e)}

    # ════════════════════════════════════════════════════════════════
    #  查看下载进度
    # ════════════════════════════════════════════════════════════════

    def get_downloading(self) -> dict:
        """获取当前正在下载的任务列表"""
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

    # ── 缓存清理 ──

    def clear_user_cache(self, user_id: str):
        """清除指定用户的搜索/资源缓存"""
        self._search_cache.pop(user_id, None)
        self._resource_cache.pop(user_id, None)
