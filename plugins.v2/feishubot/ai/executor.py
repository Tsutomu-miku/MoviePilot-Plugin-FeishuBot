"""AI 对话系统 — 工具执行器（连接 Agent 工具与 MoviePilot 业务逻辑）"""

import json as _json
from typing import Optional
from app.log import logger

from .types import ToolResult, ChatState


class ToolExecutor:
    """
    工具执行器 — 接收工具名称+参数，调度到具体业务实现。

    设计：
    - 通过 bind() 注入 MoviePilot 的 Chain 依赖，不直接 import 业务模块
    - 内部管理 ChatState（搜索缓存、待确认下载等）
    - 每个工具是一个独立的私有方法，添加新工具只需加一个方法 + _DISPATCH 一行
    """

    def __init__(self, state: ChatState):
        self.state = state
        self._extract_tags = None  # 注入: utils._extract_tags

    def bind(self, *, extract_tags=None):
        """注入外部依赖"""
        if extract_tags:
            self._extract_tags = extract_tags

    def execute(self, fn_name: str, fn_args: dict) -> ToolResult:
        """执行工具调用，返回统一的 ToolResult"""
        dispatch = {
            "search_media": self._do_search_media,
            "search_resources": self._do_search_resources,
            "download_resource": self._do_download_resource,
            "subscribe_media": self._do_subscribe_media,
            "get_downloading": self._do_get_downloading,
        }
        handler = dispatch.get(fn_name)
        if not handler:
            return ToolResult(success=False, error=f"未知工具: {fn_name}")

        try:
            return handler(**fn_args)
        except Exception as e:
            logger.error(f"工具 {fn_name} 执行异常: {e}", exc_info=True)
            return ToolResult(success=False, error=str(e))

    # ════════════════════════════════════════════════════════════════
    #  各工具实现
    # ════════════════════════════════════════════════════════════════

    def _do_search_media(self, keyword: str = "", **_) -> ToolResult:
        """搜索影视信息"""
        if not keyword:
            return ToolResult(success=False, error="请提供搜索关键词")
        try:
            from app.chain.media import MediaChain
            from app.schemas.types import MediaType

            result = MediaChain().search(title=keyword)
            if isinstance(result, tuple) and len(result) == 2:
                meta, medias = result
            elif isinstance(result, list):
                meta, medias = None, result
            else:
                return ToolResult(success=True, data={"keyword": keyword, "results": [], "error": "搜索返回格式异常"})

            if not medias:
                name = getattr(meta, "name", keyword) if meta else keyword
                return ToolResult(success=True, data={"keyword": keyword, "results": [], "message": f"未找到「{name}」"})

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

            # 缓存到 state
            self.state.search_cache = medias[:8]
            return ToolResult(success=True, data={"keyword": keyword, "total_found": len(medias), "results": valid})

        except Exception as e:
            logger.error(f"search_media 异常: {e}", exc_info=True)
            return ToolResult(success=False, error=str(e))

    def _do_search_resources(self, keyword: str = "", **_) -> ToolResult:
        """搜索种子资源"""
        if not keyword:
            return ToolResult(success=False, error="请提供搜索关键词")
        try:
            from app.chain.media import MediaChain
            from app.chain.search import SearchChain

            # 先通过 MediaChain 规范化标题
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
                return ToolResult(success=True, data={
                    "keyword": keyword, "title": title, "results": [],
                    "message": f"未找到「{title}」的下载资源",
                })

            extract = self._extract_tags or (lambda x: {})
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
                    "tags": extract(tname),
                })

            # 缓存到 state
            self.state.resource_cache = contexts[:20]
            return ToolResult(success=True, data={
                "keyword": keyword, "title": title,
                "total_found": len(contexts), "showing": len(results),
                "results": results,
            })

        except Exception as e:
            logger.error(f"search_resources 异常: {e}", exc_info=True)
            return ToolResult(success=False, error=str(e))

    def _do_download_resource(self, index: int = 0, confirmed: bool = False, **_) -> ToolResult:
        """下载资源 — 两阶段确认"""
        cached = self.state.resource_cache

        if confirmed:
            # ── 阶段二：执行下载 ──
            pending = self.state.pending_download
            if pending and (index == -1 or index == pending["index"]):
                index = pending["index"]
            if not cached:
                return ToolResult(success=False, error="没有缓存的搜索结果，请先搜索资源。")
            if index < 0 or index >= len(cached):
                if pending:
                    index = pending["index"]
                else:
                    return ToolResult(success=False, error=f"序号 {index} 无效且无待确认下载，请先选择资源。")

            ctx = cached[index]
            t = getattr(ctx, "torrent_info", None)
            title = getattr(t, "title", "未知") if t else "未知"

            try:
                # 补充 media_info（SearchChain 返回的 Context 可能没有）
                if not getattr(ctx, "media_info", None):
                    try:
                        from app.chain.media import MediaChain
                        _meta = getattr(ctx, "meta_info", None)
                        _media = MediaChain().recognize_media(meta=_meta)
                        if _media:
                            ctx.media_info = _media
                    except Exception as me:
                        logger.warning(f"download: 媒体识别异常: {me}")

                from app.chain.download import DownloadChain
                result = DownloadChain().download_single(context=ctx, userid="feishu")
                self.state.clear_download()

                if result:
                    return ToolResult(success=True, data={"title": title, "message": f"✅ 已添加下载: {title}"})
                else:
                    return ToolResult(success=False, error=f"下载提交失败: {title}")

            except Exception as e:
                logger.error(f"download_resource 异常: {e}", exc_info=True)
                return ToolResult(success=False, error=str(e))
        else:
            # ── 阶段一：展示详情，缓存待确认 ──
            if not cached:
                return ToolResult(success=False, error="当前没有缓存的搜索结果。请先用 search_resources 搜索。")
            if index < 0 or index >= len(cached):
                return ToolResult(success=False, error=f"序号 {index} 无效，有效范围: 0-{len(cached)-1}")

            ctx = cached[index]
            t = getattr(ctx, "torrent_info", None)
            title = getattr(t, "title", "未知") if t else "未知"
            size = getattr(t, "size", "未知") if t else "未知"
            site = getattr(t, "site_name", "未知") if t else "未知"
            extract = self._extract_tags or (lambda x: {})

            self.state.pending_download = {"index": index, "title": title, "size": size, "site": site}

            return ToolResult(success=True, data={
                "status": "pending_confirmation",
                "index": index, "title": title, "size": size, "site": site,
                "tags": extract(title),
                "message": (
                    f"资源「{title}」（{site}, {size}）等待用户确认。"
                    "请向用户展示资源信息并明确询问是否确认下载。"
                    f"用户确认后调用 download_resource(index={index}, confirmed=true) 执行下载。"
                ),
            })

    def _do_subscribe_media(self, index: Optional[int] = None, keyword: Optional[str] = None, **_) -> ToolResult:
        """订阅影视"""
        mediainfo = None

        # 优先通过 index 从搜索缓存获取
        if index is not None:
            cached = self.state.search_cache
            if 0 <= index < len(cached):
                mediainfo = cached[index]

        # 否则通过关键词搜索
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
                return ToolResult(success=False, error=f"搜索失败: {e}")

        if not mediainfo:
            return ToolResult(success=False, error="未找到可订阅的作品，请提供更精确的名称")

        try:
            from app.chain.subscribe import SubscribeChain
            from app.schemas.types import MediaType

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
                return ToolResult(success=True, data={"title": title, "message": f"已订阅: {title}"})
            else:
                return ToolResult(success=False, error=err_msg or "订阅失败")

        except Exception as e:
            logger.error(f"subscribe_media 异常: {e}", exc_info=True)
            return ToolResult(success=False, error=str(e))

    def _do_get_downloading(self, **_) -> ToolResult:
        """获取下载列表"""
        try:
            from app.chain.download import DownloadChain
            torrents = DownloadChain().downloading_torrents()
            if not torrents:
                return ToolResult(success=True, data={"tasks": [], "message": "当前没有正在下载的任务"})
            tasks = []
            for t in torrents[:15]:
                tasks.append({
                    "title": getattr(t, "title", "") or getattr(t, "name", "未知"),
                    "progress": getattr(t, "progress", 0),
                })
            return ToolResult(success=True, data={"tasks": tasks, "total": len(torrents)})
        except Exception as e:
            return ToolResult(success=False, error=str(e))
