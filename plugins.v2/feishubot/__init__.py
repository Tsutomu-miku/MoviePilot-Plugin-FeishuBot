"""
飞书机器人插件 v5.2.0 — MoviePilot Agent Mode + WebSocket 长连接

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


# ╔════════════════════════════════════════════════════════════════════╗
# ║  0. 飞书 SDK 长连接可用性检测                                      ║
# ╚════════════════════════════════════════════════════════════════════╝

_HAS_LARK_SDK = False
try:
    import lark_oapi as lark
    from lark_oapi.ws import Client as LarkWSClient
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    _HAS_LARK_SDK = True
except Exception:
    lark = None
    LarkWSClient = None
    EventDispatcherHandler = None


# ╔════════════════════════════════════════════════════════════════════╗
# ║  1. 种子标签提取                                                  ║
# ╚════════════════════════════════════════════════════════════════════╝

def _extract_tags(title: str) -> dict:
    """从种子标题中提取结构化标签"""
    if not title:
        return {}
    tl = title.lower()
    tags = {}

    for kw, label in [
        ("2160p", "4K"), ("4k", "4K"), ("uhd", "4K"),
        ("1080p", "1080p"), ("1080i", "1080p"), ("720p", "720p"),
    ]:
        if kw in tl:
            tags["resolution"] = label
            break

    for kw, label in [
        ("hevc", "HEVC/x265"), ("x265", "HEVC/x265"),
        ("h.265", "HEVC/x265"), ("h265", "HEVC/x265"),
        ("x264", "x264"), ("h.264", "x264"), ("h264", "x264"),
        ("avc", "x264"), ("av1", "AV1"),
    ]:
        if kw in tl:
            tags["video_codec"] = label
            break

    for kw, label in [
        ("dolby.vision", "Dolby Vision"), ("dolbyvision", "Dolby Vision"),
        ("dovi", "Dolby Vision"), (".dv.", "Dolby Vision"),
        ("hdr10+", "HDR10+"), ("hdr10plus", "HDR10+"),
        ("hdr10", "HDR10"), ("hdr", "HDR"),
    ]:
        if kw in tl:
            tags["hdr"] = label
            break

    for kw, label in [
        ("atmos", "Atmos"), ("truehd", "TrueHD"),
        ("dts-hd", "DTS-HD MA"), ("dts.hd", "DTS-HD MA"),
        ("dtshdma", "DTS-HD MA"), ("dts-x", "DTS:X"), ("dtsx", "DTS:X"),
        ("dts", "DTS"),
        ("ddp5.1", "DD+ 5.1"), ("dd+5.1", "DD+ 5.1"), ("ddp.5.1", "DD+ 5.1"),
        ("dd5.1", "DD 5.1"),
        ("7.1", "7.1ch"), ("5.1", "5.1ch"),
        ("aac", "AAC"), ("flac", "FLAC"),
    ]:
        if kw in tl:
            tags["audio"] = label
            break

    for kw, label in [
        ("remux", "Remux"), ("bdremux", "Remux"),
        ("bluray", "BluRay"), ("blu-ray", "BluRay"),
        ("web-dl", "WEB-DL"), ("webdl", "WEB-DL"),
        ("webrip", "WEBRip"), ("web-rip", "WEBRip"), ("hdtv", "HDTV"),
    ]:
        if kw in tl:
            tags["source"] = label
            break

    return tags


# ╔════════════════════════════════════════════════════════════════════╗
# ║  2. 飞书 API 客户端                                               ║
# ╚════════════════════════════════════════════════════════════════════╝

class _FeishuAPI:
    """飞书 Token 管理 & 消息发送"""

    def __init__(self, app_id, app_secret):
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str = ""
        self._token_expire: datetime = datetime.min

    def _get_token(self) -> str:
        if self._token and datetime.now() < self._token_expire:
            return self._token
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=10,
            )
            data = resp.json()
            self._token = data.get("tenant_access_token", "")
            self._token_expire = datetime.now() + timedelta(
                seconds=data.get("expire", 7200) - 60
            )
        except Exception as e:
            logger.error(f"获取飞书 Token 失败: {e}")
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def send_text(self, chat_id: str, text: str, reply_msg_id: str = None):
        """发送文本消息。提供 reply_msg_id 时使用回复 API。"""
        content = _json.dumps({"text": text}, ensure_ascii=False)

        if reply_msg_id:
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_msg_id}/reply"
            body = {"msg_type": "text", "content": content}
            params = {}
        else:
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            body = {"receive_id": chat_id, "msg_type": "text", "content": content}
            params = {"receive_id_type": "chat_id"}

        try:
            resp = requests.post(
                url, params=params, headers=self._headers(),
                json=body, timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.warning(f"飞书消息发送失败: {result}")
        except Exception as e:
            logger.error(f"飞书发送异常: {e}")

    def send_card(self, chat_id: str, card: dict, reply_msg_id: str = None):
        """发送卡片消息。支持 reply_msg_id 回复。"""
        content = _json.dumps(card, ensure_ascii=False)

        if reply_msg_id:
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_msg_id}/reply"
            body = {"msg_type": "interactive", "content": content}
            params = {}
        else:
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            body = {
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": content,
            }
            params = {"receive_id_type": "chat_id"}

        try:
            resp = requests.post(
                url, params=params, headers=self._headers(),
                json=body, timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.warning(f"飞书卡片发送失败: {result}")
            return result
        except Exception as e:
            logger.error(f"飞书发送卡片异常: {e}")
            return {}

    def update_card(self, message_id: str, card: dict):
        """更新已发送的卡片消息（用于状态更新）"""
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
        body = {
            "msg_type": "interactive",
            "content": _json.dumps(card, ensure_ascii=False),
        }
        try:
            resp = requests.patch(
                url, headers=self._headers(), json=body, timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.warning(f"飞书卡片更新失败: {result}")
        except Exception as e:
            logger.error(f"飞书更新卡片异常: {e}")


# ╔════════════════════════════════════════════════════════════════════╗
# ║  2.5 飞书卡片构建器                                                ║
# ╚════════════════════════════════════════════════════════════════════╝

class _CardBuilder:
    """飞书 Interactive Card 构建工具集"""

    @staticmethod
    def _md(text: str) -> dict:
        """构建 markdown 元素"""
        return {"tag": "markdown", "content": text}

    @staticmethod
    def _hr() -> dict:
        return {"tag": "hr"}

    @staticmethod
    def _header(title: str, template: str = "blue", icon: str = "") -> dict:
        """构建卡片 header"""
        h = {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        }
        if icon:
            h["icon"] = {"tag": "standard_icon", "token": icon}
        return h

    @staticmethod
    def _button(text: str, value: dict, btn_type: str = "primary") -> dict:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": text},
            "type": btn_type,
            "value": value,
        }

    @staticmethod
    def _action(buttons: list) -> dict:
        return {"tag": "action", "actions": buttons}

    @staticmethod
    def _column_set(columns: list, flex_mode: str = "none") -> dict:
        return {
            "tag": "column_set",
            "flex_mode": flex_mode,
            "background_style": "default",
            "columns": columns,
        }

    @staticmethod
    def _column(elements: list, weight: int = 1, vertical_align: str = "top") -> dict:
        return {
            "tag": "column",
            "width": "weighted",
            "weight": weight,
            "vertical_align": vertical_align,
            "elements": elements,
        }

    @staticmethod
    def _note(text: str) -> dict:
        """底部备注（用 div + notation 模拟）"""
        return {
            "tag": "div",
            "text": {"tag": "lark_md", "content": text},
        }

    @classmethod
    def wrap(cls, header_title: str, elements: list,
             template: str = "blue", icon: str = "") -> dict:
        """包装完整卡片"""
        return {
            "header": cls._header(header_title, template, icon),
            "elements": elements,
        }

    # ── 高级卡片工厂 ──

    @classmethod
    def processing_card(cls, user_text: str) -> dict:
        """'正在处理' 占位卡片"""
        truncated = user_text[:60] + ('...' if len(user_text) > 60 else '')
        elements = [
            cls._md(f"**收到指令** ➜ {truncated}"),
            cls._hr(),
            cls._md("⏳ **AI 正在理解你的需求...**\n\n> 即将开始调用工具处理"),
        ]
        return cls.wrap("🤖 处理中", elements, template="indigo")

    @classmethod
    def agent_reply_card(cls, reply_text: str, elapsed: float = 0) -> dict:
        """Agent 最终回复卡片"""
        elements = [
            cls._md(reply_text),
        ]
        if elapsed > 0:
            elements.append(cls._hr())
            elements.append(cls._note(f"⏱ 耗时 {elapsed:.1f}s"))
        # 底部快捷操作
        elements.append(cls._action([
            cls._button("🗑️ 清除对话", {"action": "clear_conversation"}, "default"),
        ]))
        return cls.wrap("🤖 MoviePilot AI", elements, template="blue")

    @classmethod
    def agent_tool_progress_card(
        cls, user_text: str, steps: list, current_step: str = ""
    ) -> dict:
        """Agent 工具调用进度卡片（实时更新）"""
        truncated = user_text[:60] + ('...' if len(user_text) > 60 else '')
        lines = [f"**指令** ➜ {truncated}\n"]
        for s in steps:
            lines.append(f"✅ {s}")
        if current_step:
            lines.append(f"🔄 **{current_step}**...")
        # 简易进度指示
        total_steps = len(steps) + (1 if current_step else 0)
        done_steps = len(steps)
        bar_len = 10
        filled = int(bar_len * done_steps / max(total_steps, 1))
        bar = "▓" * filled + "░" * (bar_len - filled)
        lines.append(f"\n`[{bar}]` {done_steps}/{total_steps} 步")
        elements = [cls._md("\n".join(lines))]
        return cls.wrap("🤖 处理进度", elements, template="turquoise")

    @classmethod
    def search_result_card(cls, keyword: str, results: list) -> dict:
        """搜索结果卡片 — 多列信息密度布局"""
        if not results:
            return cls.wrap("🔍 搜索结果", [cls._md(f"未找到「{keyword}」相关结果")], template="grey")

        elements = [cls._md(f"搜索「**{keyword}**」找到 {len(results)} 个结果：\n")]

        for item in results[:8]:
            idx = item.get("index", 0) + 1
            title = item.get("title", "未知")
            year = item.get("year", "")
            mtype = item.get("type", "")
            rating = item.get("rating", "")
            overview = item.get("overview", "")

            # 标题行
            rating_str = f"  ⭐ **{rating}**" if rating else ""
            type_str = f"`{mtype}`" if mtype else ""
            header_line = f"**{idx}. {title}** ({year}) {type_str}{rating_str}"

            # 简介（截断）
            desc = overview[:80] + "..." if len(overview) > 80 else overview
            if desc:
                header_line += f"\n> {desc}"

            elements.append(cls._md(header_line))

            # 操作按钮
            btns = [
                cls._button("📥 订阅", {"action": "subscribe", "index": item.get("index", 0)}, "primary"),
                cls._button("🔍 搜资源", {"action": "search_resources_by_title", "keyword": title}, "default"),
            ]
            elements.append(cls._action(btns))

            if idx < len(results):
                elements.append(cls._hr())

        return cls.wrap("🔍 搜索结果", elements, template="blue")

    @classmethod
    def resource_result_card(cls, keyword: str, title: str, results: list) -> dict:
        """资源列表卡片 — 标签化 + 高信息密度"""
        if not results:
            return cls.wrap("📦 资源搜索", [cls._md(f"未找到「{title}」的下载资源")], template="grey")

        elements = [
            cls._md(f"「**{title}**」共 {len(results)} 个资源：\n"),
        ]

        for item in results[:10]:
            idx = item.get("index", 0) + 1
            tname = item.get("title", "未知")
            site = item.get("site", "")
            size = item.get("size", "")
            seeders = item.get("seeders", "")
            tags = item.get("tags", {})

            # 用 text_tag 风格展示标签
            tag_parts = []
            if tags.get("resolution"):
                tag_parts.append(f"`{tags['resolution']}`")
            if tags.get("video_codec"):
                tag_parts.append(f"`{tags['video_codec']}`")
            if tags.get("hdr"):
                tag_parts.append(f"`{tags['hdr']}`")
            if tags.get("audio"):
                tag_parts.append(f"`{tags['audio']}`")
            if tags.get("source"):
                tag_parts.append(f"`{tags['source']}`")

            tag_line = " ".join(tag_parts) if tag_parts else ""

            # 主信息: 两列布局
            left_md = f"**{idx}. {tname[:50]}**{'...' if len(tname) > 50 else ''}\n{tag_line}"
            right_md = f"📡 {site}\n💾 {size}  |  🌱 {seeders}"

            col = cls._column_set([
                cls._column([cls._md(left_md)], weight=3),
                cls._column([cls._md(right_md)], weight=2),
            ])
            elements.append(col)

            # 下载按钮
            elements.append(cls._action([
                cls._button(f"⬇️ 下载 #{idx}", {"action": "download_resource", "index": item.get("index", 0)}, "primary"),
            ]))

            if idx < len(results):
                elements.append(cls._hr())

        return cls.wrap("📦 资源列表", elements, template="green")

    @classmethod
    def download_confirm_card(cls, index: int, title: str, site: str, size: str, tags: dict) -> dict:
        """下载确认卡片"""
        tag_parts = []
        for k in ("resolution", "video_codec", "hdr", "audio", "source"):
            if tags.get(k):
                tag_parts.append(f"`{tags[k]}`")
        tag_line = " ".join(tag_parts) if tag_parts else "无标签信息"

        elements = [
            cls._md(f"**{title}**\n\n📡 站点: {site}  |  💾 大小: {size}\n🏷 标签: {tag_line}"),
            cls._hr(),
            cls._md("⚠️ **确认下载此资源？**"),
            cls._action([
                cls._button("✅ 确认下载", {"action": "download_resource_confirm", "index": index}, "primary"),
                cls._button("❌ 取消", {"action": "noop"}, "danger"),
            ]),
        ]
        return cls.wrap("⬇️ 下载确认", elements, template="orange")

    @classmethod
    def downloading_card(cls, tasks: list, total: int = 0) -> dict:
        """下载进度卡片 — 进度条可视化"""
        if not tasks:
            return cls.wrap("⬇️ 下载任务", [cls._md("当前没有正在下载的任务")], template="grey")

        elements = [cls._md(f"共 **{total}** 个下载任务：\n")]

        for t in tasks:
            title = t.get("title", "未知")
            progress = t.get("progress", 0)
            bar_filled = int(progress / 10)
            bar_empty = 10 - bar_filled
            bar = "█" * bar_filled + "░" * bar_empty
            elements.append(cls._md(f"**{title}**\n`{bar}` {progress}%"))

        return cls.wrap("⬇️ 下载任务", elements, template="turquoise")

    @classmethod
    def status_card(cls, info: dict) -> dict:
        """诊断状态卡片 — 结构化仪表板"""
        elements = [
            cls._column_set([
                cls._column([cls._md(
                    f"**版本** {info.get('version', '?')}\n"
                    f"**实例** {info.get('instance', '?')}\n"
                    f"**运行** {info.get('uptime', '?')}"
                )], weight=1),
                cls._column([cls._md(
                    f"**飞书 Token** {info.get('feishu_token', '❌')}\n"
                    f"**WebSocket** {info.get('ws_status', '❌')}\n"
                    f"**lark-oapi** {info.get('lark_sdk', '❌')}"
                )], weight=1),
                cls._column([cls._md(
                    f"**Agent** {info.get('agent_status', '❌')}\n"
                    f"**模型** {info.get('model', '?')}\n"
                    f"**对话数** {info.get('conversations', 0)}"
                )], weight=1),
            ]),
            cls._hr(),
            cls._md(
                f"📊 消息 **{info.get('msg_count', 0)}**  |  "
                f"Agent **{info.get('agent_count', 0)}**  |  "
                f"传统 **{info.get('legacy_count', 0)}**  |  "
                f"恢复 **{info.get('recover_count', 0)}**  |  "
                f"缓存 🔍{info.get('cache_media', 0)} 📦{info.get('cache_res', 0)}"
            ),
            cls._hr(),
            cls._md("💡 `/clear` 清除对话  |  `/status` 查看状态  |  `/help` 帮助"),
        ]
        return cls.wrap("🔧 插件诊断", elements, template="grey")

    @classmethod
    def help_card(cls, agent_on: bool, ws_on: bool) -> dict:
        """帮助卡片"""
        agent_str = "✅ 已启用" if agent_on else "❌ 未启用"
        ws_str = "✅ WebSocket" if ws_on else "📡 HTTP 回调"

        elements = [
            cls._column_set([
                cls._column([cls._md(f"🤖 AI Agent: **{agent_str}**")], weight=1),
                cls._column([cls._md(f"📡 消息通道: **{ws_str}**")], weight=1),
            ]),
            cls._hr(),
        ]

        if agent_on:
            elements.append(cls._md(
                "**AI 模式** — 直接用自然语言对话即可\n\n"
                "💬 `帮我搜一下流浪地球` → 搜索影视\n"
                "💬 `下载第1个 4K版本` → 搜资源并下载\n"
                "💬 `订阅进击的巨人` → 自动追更\n"
                "💬 `下载进度` → 查看当前任务"
            ))
        else:
            elements.append(cls._md(
                "**传统指令模式**\n\n"
                "🔍 `/搜索 <片名>` — 搜索影视\n"
                "📥 `/订阅 <片名>` — 订阅追更\n"
                "⬇️ `/正在下载` — 下载进度\n"
                "❓ `/帮助` — 显示此帮助"
            ))

        elements.extend([
            cls._hr(),
            cls._md("🗑 `/clear` 清除对话  |  🔧 `/status` 运行状态"),
        ])
        return cls.wrap("📖 飞书机器人帮助", elements, template="violet")

    @classmethod
    def error_card(cls, error_msg: str) -> dict:
        """错误提示卡片"""
        elements = [cls._md(f"⚠️ {error_msg}")]
        return cls.wrap("⚠️ 出错了", elements, template="red")

    @classmethod
    def notify_card(cls, title: str, content: str, template: str = "blue") -> dict:
        """通用通知卡片 (入库/下载/订阅事件)"""
        elements = [cls._md(content)]
        return cls.wrap(title, elements, template=template)

    @classmethod
    def interrupted_card(cls, merged_text: str) -> dict:
        """打断提示卡片"""
        elements = [
            cls._md(f"**新消息已收到** ➜ {merged_text[:60]}{'...' if len(merged_text) > 60 else ''}"),
            cls._hr(),
            cls._md("🔄 **已打断上一轮处理，正在响应最新消息...**"),
        ]
        return cls.wrap("🤖 消息已更新", elements, template="orange")

    @classmethod
    def received_card(cls, user_text: str) -> dict:
        """消息已收到即时确认卡片 (v5.1.1) — dispatch 阶段立即发送"""
        truncated = user_text[:60] + ('...' if len(user_text) > 60 else '')
        elements = [
            cls._md(f"**已收到** ➜ {truncated}"),
            cls._md("⏳ 正在准备处理..."),
        ]
        return cls.wrap("🤖 MoviePilot AI", elements, template="turquoise")


# ╔════════════════════════════════════════════════════════════════════╗
# ║  3. OpenRouter LLM 客户端                                         ║
# ╚════════════════════════════════════════════════════════════════════╝

class _OpenRouterClient:
    """零依赖 OpenRouter Chat Completions 客户端"""

    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODEL = "google/gemini-2.5-flash-preview:free"

    def __init__(self, api_key, model: str = ""):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL

    def chat(
        self,
        messages: list,
        tools: list = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Tsutomu-miku/MoviePilot-Plugin-FeishuBot",
            "X-OpenRouter-Title": "MoviePilot-FeishuBot",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        resp = requests.post(self.BASE_URL, headers=headers, json=payload, timeout=90)

        if resp.status_code != 200:
            logger.error(f"OpenRouter API {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()

        data = resp.json()
        if "error" in data:
            error_msg = data["error"].get("message", str(data["error"]))
            raise RuntimeError(f"OpenRouter API 错误: {error_msg}")

        return data


# ╔════════════════════════════════════════════════════════════════════╗
# ║  4. 对话历史管理                                                   ║
# ╚════════════════════════════════════════════════════════════════════╝

_MAX_CONVERSATION_MESSAGES = 30


class _ConversationManager:
    """管理每个用户的多轮对话历史（安全截断，不破坏 tool_call 配对）"""

    def __init__(self, system_prompt: str, max_messages: int = _MAX_CONVERSATION_MESSAGES):
        self._system_prompt = system_prompt
        self._max_messages = max_messages
        self._store: Dict[str, list] = {}

    def get(self, user_id: str) -> list:
        """返回对话历史的副本（含 system prompt），防止意外修改"""
        if user_id not in self._store:
            self._store[user_id] = [{"role": "system", "content": self._system_prompt}]
        return list(self._store[user_id])

    def save(self, user_id: str, messages: list):
        """保存对话历史，智能截断 — 不在 tool_call 序列中间切断"""
        if len(messages) <= self._max_messages:
            self._store[user_id] = messages
            return

        system = messages[0]
        candidates = messages[1:]
        max_recent = self._max_messages - 1
        cut_start = len(candidates) - max_recent

        # 向后移动截断点直到找到安全位置
        while cut_start < len(candidates):
            msg = candidates[cut_start]
            role = msg.get("role", "")
            if role == "user":
                break
            if role == "assistant" and not msg.get("tool_calls"):
                break
            cut_start += 1

        self._store[user_id] = [system] + candidates[cut_start:]

    def clear(self, user_id: str):
        self._store.pop(user_id, None)

    @property
    def active_users(self) -> int:
        return len(self._store)


# ╔════════════════════════════════════════════════════════════════════╗
# ║  5. Agent 工具定义                                                 ║
# ╚════════════════════════════════════════════════════════════════════╝

_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_media",
            "description": (
                "搜索影视作品（电影/电视剧/动漫），返回媒体信息列表。"
                "当用户想查找、搜索、了解某部影视作品时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "影视作品名称，如「流浪地球」「进击的巨人」",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_resources",
            "description": (
                "搜索指定影视作品的可下载种子资源，返回资源列表。"
                "返回结果包含：标题、站点、大小、做种数、标签（分辨率/编码/音轨/来源）。"
                "当用户想下载某部作品、或想看资源列表、或指定了质量偏好（4K/杜比/蓝光等）时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "影视作品名称",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_resource",
            "description": (
                "下载指定序号的种子资源。必须先调用 search_resources 获取资源列表。\n\n"
                "**两步确认机制**：\n"
                "1. confirmed=false → 返回资源详情，系统会记住该选择\n"
                "2. 用户确认后 → confirmed=true 执行下载\n\n"
                "confirmed=true 时，如果不确定序号可传 index=-1，"
                "系统会自动使用上次 confirmed=false 时记住的资源。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "资源序号(从0开始)。confirmed=true 时可传 -1 表示使用上次待确认的资源",
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "false=预览详情，true=执行下载",
                    },
                },
                "required": ["index", "confirmed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subscribe_media",
            "description": (
                "订阅影视作品，系统会自动搜索并下载更新。"
                "可传入 search_media 返回的序号，或直接传入作品名称。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "search_media 返回列表中的序号（从 0 开始）",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "如果没有先搜索过，可直接传入作品名称",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_downloading",
            "description": "获取当前正在下载的任务列表，返回每个任务的名称和进度。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ╔════════════════════════════════════════════════════════════════════╗
# ║  6. Agent 系统提示词                                               ║
# ╚════════════════════════════════════════════════════════════════════╝

_AGENT_SYSTEM_PROMPT = """\
你是 MoviePilot 飞书机器人 AI 助手。你通过工具帮助用户搜索、下载、订阅影视资源。

## 可用工具
1. **search_media** — 搜索影视作品信息（标题、年份、类型、评分、简介）
2. **search_resources** — 搜索下载资源，获取种子列表（标题、站点、大小、做种数、标签）
3. **download_resource** — 下载指定资源（两步确认：先预览再下载）
4. **subscribe_media** — 订阅影视（自动追更下载）
5. **get_downloading** — 查看当前下载进度

## 核心工作流程

### 搜索
用户发来片名 → 调用 search_media → 用结构化格式展示：
- 编号 + 标题 + 年份 + 类型 + 评分
- 每个结果附简短简介

### 下载（最重要，必须严格遵守）
1. 用户想下载 → 调用 search_resources 获取资源列表
2. 分析返回的 tags，根据用户偏好筛选排序
3. 推荐 1-3 个最佳资源，用表格对比：
   - 序号 | 标题 | 站点 | 大小 | 标签
4. 调用 download_resource(index=X, confirmed=false) 获取待下载资源详情（系统会记住此选择）
5. **展示详情并明确询问用户是否确认下载**
6. 用户确认后 → 调用 download_resource(index=X, confirmed=true) 执行下载
   - 如果你不确定之前选的序号，可以传 index=-1，系统会自动使用上次待确认的资源
7. **绝对禁止**未经用户确认就设置 confirmed=true

### 订阅
用户想追剧/订阅 → 调用 search_media 确认 → 调用 subscribe_media

### 偏好理解
- "4K" "超高清" → 2160p/4K/UHD
- "蓝光" "原盘" → BluRay/Remux
- "5.1环绕声" → 5.1/DD5.1/DDP5.1
- "全景声" → Atmos
- "杜比视界" "DV" → DolbyVision/DV
- "HDR" → HDR/HDR10/HDR10+

## 回复风格
- 简洁友好，使用中文
- 使用 markdown 格式组织信息（**加粗**标题，`代码框`标签，> 引用说明）
- 展示列表时用编号，突出关键信息
- 搜索结果按"编号. 标题 (年份) [类型] ⭐评分"格式
- 资源对比时用简洁表格
- 闲聊直接回复，不调用工具
- 操作成功/失败时用对应 emoji 明确标识

## 上下文理解
- 当用户说"下载第X个""选第X个""要第X个"时，必须参考**最近一次搜索结果**，直接调用 download_resource
- 如果上下文中已有 search_resources 的结果，不要重复搜索，直接用 download_resource(index=X, confirmed=false)
- "这个""那个"等指代词通常指用户最近讨论的影视作品
- 当用户说"确认""确认下载""好的下载吧"等肯定回复时，说明用户在确认之前待确认的资源，直接调用 download_resource(index=-1, confirmed=true)"""


# ╔════════════════════════════════════════════════════════════════════╗
# ║  7. Agent 消息清洗                                                 ║
# ╚════════════════════════════════════════════════════════════════════╝

def _sanitize_assistant_message(raw_msg: dict) -> dict:
    """
    清洗 LLM 返回的 assistant 消息，只保留标准字段。
    防止 API 响应的额外字段（refusal, annotations 等）污染对话历史。
    """
    clean: dict = {"role": "assistant"}

    tool_calls = raw_msg.get("tool_calls")
    if tool_calls:
        clean["tool_calls"] = [
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", "{}"),
                },
            }
            for tc in tool_calls
        ]

    content = raw_msg.get("content")
    clean["content"] = content if isinstance(content, str) else ""
    return clean


# ╔════════════════════════════════════════════════════════════════════╗
# ║  8. 主插件类                                                       ║
# ╚════════════════════════════════════════════════════════════════════╝

class FeishuBot(_PluginBase):

    # ── 插件元信息 ──
    plugin_name = "飞书机器人"
    plugin_desc = "飞书群机器人消息通知与交互，支持 AI Agent 智能体模式（WebSocket 长连接）"
    plugin_icon = "Feishu_A.png"
    plugin_version = "5.2.0"
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
                _CardBuilder.notify_card("✅ 订阅成功", msg, "green"),
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
