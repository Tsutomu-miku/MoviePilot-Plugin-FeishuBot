"""
飞书机器人插件 v4.0.0 — MoviePilot Agent Mode + WebSocket 长连接

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
except ImportError:
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

    def send_card(self, chat_id: str, card: dict):
        body = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": _json.dumps(card, ensure_ascii=False),
        }
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers=self._headers(), json=body, timeout=10,
            )
            result = resp.json()
            if result.get("code") != 0:
                logger.warning(f"飞书卡片发送失败: {result}")
        except Exception as e:
            logger.error(f"飞书发送卡片异常: {e}")


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
                "**重要**：此工具有两步确认机制：\n"
                "1. 第一次调用：confirmed=false → 仅返回资源详情，不会下载\n"
                "2. 用户明确确认后：confirmed=true → 执行实际下载\n\n"
                "你必须先用 confirmed=false 获取详情并展示给用户，"
                "等用户明确说「确认」「下载」「好的」等肯定回复后，"
                "再用 confirmed=true 执行下载。绝对不要跳过确认步骤。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "资源在 search_resources 返回列表中的序号（从 0 开始）",
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "是否已获得用户确认。首次推荐时必须设为 false，用户确认后设为 true",
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
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "向用户发送一条中间状态消息（如「正在搜索...」）。"
                "Agent 最终回复会自动发送，不需要用这个工具发最终结果。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要发送的消息内容"}
                },
                "required": ["text"],
            },
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
6. **send_message** — 发送中间状态提示

## 核心工作流程

### 搜索
用户发来片名 → 调用 search_media → 展示结果摘要（编号+标题+年份+类型+评分）

### 下载（最重要，必须严格遵守）
1. 用户想下载 → 调用 search_resources 获取资源列表
2. 分析返回的 tags，根据用户偏好筛选排序
3. 推荐 1-3 个最佳资源，说明推荐理由
4. 调用 download_resource(index=X, confirmed=false) 获取待下载资源详情
5. **展示详情并明确询问用户是否确认下载**
6. 用户确认后 → 调用 download_resource(index=X, confirmed=true) 执行下载
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
- 用 emoji 适当点缀
- 展示列表时用编号，突出关键信息
- 闲聊直接回复，不调用工具"""


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
    plugin_version = "4.0.0"
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
    _use_ws: bool = True  # 新增: 是否使用 WebSocket 长连接

    # ── 运行时 ──
    _feishu: Optional[_FeishuAPI] = None
    _llm_client: Optional[_OpenRouterClient] = None
    _conversations: Optional[_ConversationManager] = None
    _search_cache: Optional[dict] = None
    _resource_cache: Optional[dict] = None
    _user_locks: Optional[dict] = None

    # ── WebSocket 长连接运行时 ──
    _ws_client: Optional[Any] = None        # lark_oapi.ws.Client 实例
    _ws_thread: Optional[threading.Thread] = None
    _ws_running: bool = False

    _MAX_AGENT_ITERATIONS = 10

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
                self._use_ws = True  # 默认开启
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
        self._user_locks = {}
        self._llm_client = None
        self._conversations = None
        self._init_ts = datetime.now()  # 插件初始化时间戳
        self._feishu_ok = False  # 飞书连通状态
        self._msg_count = 0  # 消息计数
        self._agent_count = 0  # Agent 调用计数
        self._legacy_count = 0  # 传统模式调用计数
        self._recover_count = 0  # 运行时恢复次数
        self._ws_connected = False  # WebSocket 连接状态

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

        # ── 停止 WebSocket 长连接 ──
        self._stop_ws_client()

        self._llm_client = None
        self._conversations = None
        self._feishu = None
        self._search_cache = None
        self._resource_cache = None
        self._user_locks = None

    # ══════════════════════════════════════════════════════════════════════
    #  WebSocket 长连接管理 (v4.0.0 新增)
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
            # 构建事件处理器
            event_handler = self._build_event_handler()

            # 创建 lark-oapi WebSocket 客户端
            self._ws_client = LarkWSClient(
                self._app_id,
                self._app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO,
            )

            # 在后台线程中启动（ws.Client.start() 是阻塞的）
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
                # ── 关键修复: 为当前线程创建全新的 event loop ──
                # MoviePilot (FastAPI/Uvicorn) 主线程已有 event loop，
                # lark-oapi SDK 内部 start() 调用 loop.run_until_complete()，
                # 如果复用主线程 loop 会报 "This event loop is already running"。
                # 解决方案: 在后台线程创建独立 loop 并替换 SDK 模块级变量。
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)

                # 替换 lark-oapi SDK 内部使用的模块级 event loop
                import lark_oapi.ws.client as _ws_mod
                _ws_mod.loop = new_loop

                logger.info("飞书 WebSocket 长连接线程启动")
                self._ws_connected = True
                self._ws_client.start()  # 阻塞，直到连接断开
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
                import time
                logger.warning("飞书 WebSocket 长连接断开，10 秒后尝试重连...")
                time.sleep(10)

                # 重新创建客户端实例
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
                    import time
                    time.sleep(30)

        logger.info("飞书 WebSocket 长连接线程已退出")

    def _build_event_handler(self) -> "EventDispatcherHandler":
        """构建 lark-oapi 事件分发处理器"""
        # 创建闭包引用 self
        plugin = self

        def on_message_receive(data):
            """处理接收到的消息事件 (im.message.receive_v1)"""
            try:
                logger.info(
                    f"[WS] 收到消息事件 v{plugin.plugin_version}, "
                    f"inst={id(plugin):#x}"
                )

                # 从 lark-oapi 事件对象中提取数据
                event_data = _json.loads(lark.JSON.marshal(data))
                event = event_data.get("event", {})

                if not event:
                    logger.warning("[WS] 消息事件缺少 event 字段")
                    return

                # 确保运行时对象可用
                plugin._ensure_runtime_ready()

                # 启动后台线程处理消息
                threading.Thread(
                    target=plugin._handle_message,
                    args=(event,),
                    daemon=True,
                ).start()
            except Exception as e:
                logger.error(f"[WS] 消息事件处理异常: {e}", exc_info=True)

        # 使用 EventDispatcherHandler 构建器注册事件
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
                # lark-oapi ws.Client 没有公开的 stop 方法，
                # 设置 _ws_running=False 后线程退出循环即可
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
        """
        惰性恢复运行时对象。

        防御场景:
        - get_api() 绑定了旧实例，而新实例的 stop_service 又被调用
        - MoviePilot 生命周期管理导致运行时对象意外丢失
        - 插件重载时 stop_service 被调用但 API 端点仍指向旧实例
        """
        recovered = []

        if self._feishu is None and self._app_id:
            self._feishu = _FeishuAPI(self._app_id, self._app_secret)
            recovered.append("feishu")

        if self._search_cache is None:
            self._search_cache = {}
        if self._resource_cache is None:
            self._resource_cache = {}
        if self._user_locks is None:
            self._user_locks = {}

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
    #  API 端点（HTTP 回调 — 保留作为备用 / 向下兼容）
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

            # 确保运行时对象可用（防御 stop_service 后仍有请求到达）
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
    #  消息路由
    # ══════════════════════════════════════════════════════════════════════

    def _handle_message(self, event: dict):
      try:
        msg = event.get("message", {})
        chat_id = msg.get("chat_id", "") or self._chat_id
        msg_id = msg.get("message_id", "")
        msg_type = msg.get("message_type", "")
        sender = event.get("sender", {}).get("sender_id", {})
        user_id = sender.get("open_id", "")

        # 确保运行时对象可用（后台线程可能在 stop_service 后执行）
        self._ensure_runtime_ready()

        if msg_type != "text":
            if self._feishu:
                self._feishu.send_text(chat_id, "暂时只支持文字消息哦~")
            return

        try:
            text = _json.loads(msg.get("content", "{}")).get("text", "").strip()
        except Exception:
            text = ""

        # ── 清理飞书 @提及标记（群聊中会包含 @_user_1 等占位符）──
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
                self._feishu.send_text(chat_id, "🗑️ 对话已清除")
            return

        # ── Agent 模式：一切交给 LLM ──
        if is_agent:
            try:
                self._agent_count = getattr(self, "_agent_count", 0) + 1
            except Exception:
                pass
            logger.info(f"[Agent] 路由到 Agent (#{self._agent_count}): {text[:80]}")
            self._agent_handle(text, chat_id, msg_id, user_id)
            return

        # ── 传统模式：指令解析 ──
        try:
            self._legacy_count = getattr(self, "_legacy_count", 0) + 1
        except Exception:
            pass
        logger.info(f"[Legacy] 路由到传统指令 (#{self._legacy_count}): {text[:80]}")
        self._legacy_handle(text, chat_id, msg_id, user_id)
      except Exception as _exc:
        logger.error(f"_handle_message 顶层异常: {_exc}", exc_info=True)

    # ══════════════════════════════════════════════════════════════════════
    #  Agent 入口 + 循环
    # ══════════════════════════════════════════════════════════════════════

    def _get_user_lock(self, user_id: str) -> threading.Lock:
        if self._user_locks is None:
            self._user_locks = {}
        if user_id not in self._user_locks:
            self._user_locks[user_id] = threading.Lock()
        return self._user_locks[user_id]

    def _agent_handle(self, text: str, chat_id: str, msg_id: str, user_id: str):
        """Agent 入口：构建上下文 → 循环 → 发送回复 → 保存历史"""
        import time as _time
        _t0 = _time.monotonic()
        lock = self._get_user_lock(user_id)
        if not lock.acquire(blocking=False):
            self._feishu.send_text(chat_id, "⏳ 上一个请求还在处理中，请稍候...")
            return

        try:
            # 获取对话历史副本并追加新消息
            messages = self._conversations.get(user_id)
            messages.append({"role": "user", "content": text})

            # 执行 Agent 循环
            updated, reply = self._agent_loop(messages, chat_id, user_id)

            # 发送回复
            if reply:
                self._feishu.send_text(chat_id, reply, reply_msg_id=msg_id)
            else:
                self._feishu.send_text(chat_id, "🔔 我没有想到回复，请再试试~")

            # 成功后才保存
            self._conversations.save(user_id, updated)

        except Exception as e:
            logger.error(f"Agent 异常: {e}", exc_info=True)
            self._feishu.send_text(chat_id, f"⚠️ AI 处理出错: {e}")
        finally:
            _elapsed = _time.monotonic() - _t0
            logger.info(f"[Agent] 处理完成: user={user_id}, elapsed={_elapsed:.1f}s")
            lock.release()

    def _agent_loop(
        self, messages: list, chat_id: str, user_id: str
    ) -> Tuple[list, str]:
        """
        多轮 Tool Calling 循环。

        在消息副本上操作，返回 (更新后的消息列表, 最终回复文本)。
        """
        working = list(messages)

        for iteration in range(self._MAX_AGENT_ITERATIONS):
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

                tool_result = self._execute_tool(fn_name, fn_args, chat_id, user_id)

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
            elif fn_name == "send_message":
                text = fn_args.get("text", "")
                if text:
                    self._feishu.send_text(chat_id, text)
                return {"sent": True}
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
            # 兼容不同 MoviePilot 版本的返回格式
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
                # 跳过字符串（str.title 是方法，会被误判为有 title 属性）
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
                        # 安全取 title 属性（避免 str.title 方法引用）
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
        """下载资源 — confirmed=false 仅返回详情，confirmed=true 才执行下载"""
        cached = (self._resource_cache or {}).get(user_id, [])
        if not cached:
            return {"error": "没有缓存的资源列表，请先调用 search_resources"}
        if index < 0 or index >= len(cached):
            return {"error": f"序号 {index} 无效，有效范围: 0-{len(cached)-1}"}

        ctx = cached[index]
        t = getattr(ctx, "torrent_info", None)
        title = getattr(t, "title", "未知") if t else "未知"
        size = getattr(t, "size", "未知") if t else "未知"
        site = getattr(t, "site_name", "未知") if t else "未知"

        if not confirmed:
            return {
                "status": "pending_confirmation",
                "index": index, "title": title, "size": size, "site": site,
                "tags": _extract_tags(title),
                "message": (
                    f"资源「{title}」（{site}, {size}）等待用户确认。"
                    "请向用户展示资源信息并明确询问是否确认下载。"
                    "用户确认后再次调用 download_resource 并设置 confirmed=true。"
                ),
            }

        try:
            from app.chain.download import DownloadChain
            result = DownloadChain().download_single(context=ctx, userid="feishu")
            if result:
                return {"success": True, "title": title, "message": f"✅ 已添加下载: {title}"}
            else:
                return {"success": False, "title": title, "message": "下载提交失败"}
        except Exception as e:
            logger.error(f"download_resource 异常: {e}", exc_info=True)
            return {"error": str(e)}

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
    #  传统模式指令
    # ══════════════════════════════════════════════════════════════════════

    def _legacy_handle(self, text: str, chat_id: str, msg_id: str, user_id: str):
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
        self._feishu.send_text(chat_id, f"🔍 正在搜索: {keyword} ...")
        result = self._tool_search_media(keyword, user_id)
        if result.get("error"):
            self._feishu.send_text(chat_id, f"⚠️ {result['error']}")
            return
        items = result.get("results", [])
        if not items:
            self._feishu.send_text(chat_id, result.get("message", f"😔 未找到: {keyword}"))
            return
        lines = []
        for item in items:
            line = f"{item['index']+1}. {item['title']} ({item['year']}) [{item['type']}]"
            if item.get("rating"):
                line += f" ⭐{item['rating']}"
            lines.append(line)
        lines.append("\n回复「/订阅 片名」订阅 | 回复片名搜索资源")
        self._feishu.send_text(chat_id, "\n".join(lines))

    def _legacy_subscribe(self, keyword: str, chat_id: str, msg_id: str, user_id: str):
        if not keyword:
            return
        self._feishu.send_text(chat_id, f"📥 正在订阅: {keyword} ...")
        result = self._tool_subscribe_media(None, keyword, user_id)
        msg = result.get("message") or result.get("error", "操作失败")
        icon = "✅" if result.get("success") else "⚠️"
        self._feishu.send_text(chat_id, f"{icon} {msg}")

    def _legacy_downloading(self, chat_id: str, msg_id: str):
        result = self._tool_get_downloading()
        tasks = result.get("tasks", [])
        if not tasks:
            self._feishu.send_text(chat_id, "当前没有正在下载的任务")
            return
        lines = [f"{i+1}. {t['title']}  进度: {t['progress']}%" for i, t in enumerate(tasks)]
        self._feishu.send_text(chat_id, "\n".join(lines))

    # ══════════════════════════════════════════════════════════════════════
    #  诊断 / 帮助
    # ══════════════════════════════════════════════════════════════════════

    def _cmd_status(self, chat_id: str, msg_id: str):
        model = self._openrouter_model or _OpenRouterClient.DEFAULT_MODEL
        conv = self._conversations.active_users if self._conversations else 0
        cache_media = len(self._search_cache) if self._search_cache else 0
        cache_res = len(self._resource_cache) if self._resource_cache else 0
        uptime = ""
        if hasattr(self, "_init_ts") and self._init_ts:
            delta = datetime.now() - self._init_ts
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            mins, secs = divmod(remainder, 60)
            uptime = f"{hours}h{mins}m{secs}s"

        ws_status = "❌ 未启用"
        if self._use_ws:
            if not _HAS_LARK_SDK:
                ws_status = "⚠️ SDK 未安装"
            elif self._ws_connected:
                ws_status = "✅ 已连接"
            elif self._ws_running:
                ws_status = "🔄 连接中..."
            else:
                ws_status = "❌ 未运行"

        self._feishu.send_text(
            chat_id,
            f"🔧 插件诊断 v{self.plugin_version}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 基础状态\n"
            f"  实例: {id(self):#x}\n"
            f"  启用: {self._enabled}\n"
            f"  运行时间: {uptime}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 飞书连接\n"
            f"  Token: {'✅ 正常' if getattr(self, '_feishu_ok', False) else '❌ 异常'}\n"
            f"  API 对象: {'✅' if self._feishu else '❌'}\n"
            f"  WebSocket: {ws_status}\n"
            f"  lark-oapi: {'✅ 已安装' if _HAS_LARK_SDK else '❌ 未安装'}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 AI Agent\n"
            f"  状态: {'✅ 已激活' if self._llm_client else '❌ 未激活'}\n"
            f"  llm_enabled: {self._llm_enabled}\n"
            f"  api_key: {'已配置' if self._openrouter_key else '未配置'}\n"
            f"  模型: {model}\n"
            f"  对话数: {conv}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 统计\n"
            f"  消息总数: {getattr(self, '_msg_count', 0)}\n"
            f"  Agent 调用: {getattr(self, '_agent_count', 0)}\n"
            f"  传统指令: {getattr(self, '_legacy_count', 0)}\n"
            f"  运行时恢复: {getattr(self, '_recover_count', 0)}\n"
            f"  搜索缓存: {cache_media} | 资源缓存: {cache_res}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"指令: /clear 清除对话 | /status 查看状态",
        )

    def _cmd_help(self, chat_id: str, msg_id: str):
        agent_on = "✅ 已启用" if self._llm_client else "❌ 未启用"
        ws_on = "✅ WebSocket" if (self._use_ws and self._ws_running) else "📡 HTTP 回调"
        self._feishu.send_text(
            chat_id,
            f"📖 飞书机器人帮助\n\n"
            f"AI Agent: {agent_on}\n"
            f"消息通道: {ws_on}\n\n"
            f"开启 AI 后直接用自然语言对话即可。\n"
            f"传统指令：\n"
            f"/搜索 <片名> | /订阅 <片名> | /正在下载 | /帮助",
        )

    # ══════════════════════════════════════════════════════════════════════
    #  卡片回调
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
            elif act == "subscribe":
                idx = int(value.get("index", 0))
                threading.Thread(
                    target=self._card_subscribe, args=(idx, user_id, chat_id), daemon=True,
                ).start()
        except Exception as e:
            logger.error(f"卡片回调异常: {e}", exc_info=True)
        return {"code": 0}

    def _card_download(self, idx: int, user_id: str, chat_id: str):
        result = self._tool_download_resource(idx, confirmed=True, user_id=user_id)
        msg = result.get("message") or result.get("error", "操作失败")
        icon = "✅" if result.get("success") else "⚠️"
        self._feishu.send_text(chat_id, f"{icon} {msg}")

    def _card_subscribe(self, idx: int, user_id: str, chat_id: str):
        result = self._tool_subscribe_media(idx, None, user_id)
        msg = result.get("message") or result.get("error", "操作失败")
        icon = "✅" if result.get("success") else "⚠️"
        self._feishu.send_text(chat_id, f"{icon} {msg}")

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
            # 运行时间
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

            # WebSocket 状态
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

            # 构建状态文本
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
    #  事件通知
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
        self._feishu.send_text(self._chat_id, f"🎬 入库完成: {title}" + (f" ({year})" if year else ""))

    @eventmanager.register(EventType.DownloadAdded)
    def _on_download(self, event: Event):
        if not self._enabled or "download" not in self._msgtypes or not self._chat_id:
            return
        mi = (event.event_data or {}).get("mediainfo")
        title = getattr(mi, "title", "未知") if mi else "未知"
        self._feishu.send_text(self._chat_id, f"⬇️ 开始下载: {title}")

    @eventmanager.register(EventType.SubscribeAdded)
    def _on_subscribe(self, event: Event):
        if not self._enabled or "subscribe" not in self._msgtypes or not self._chat_id:
            return
        title = (event.event_data or {}).get("title") or (event.event_data or {}).get("name") or "未知"
        self._feishu.send_text(self._chat_id, f"📌 新增订阅: {title}")
