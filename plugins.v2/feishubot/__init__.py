"""Feishu bot plugin entrypoint."""

import re
import threading
from typing import Any, Optional

from app.plugins import _PluginBase

from .ai.llm import DEFAULT_MODEL
from .ai.types import ChatState
from .feishu_api import _FeishuAPI
from .mixins import (
    FeishuCoreMixin,
    FeishuInteractionMixin,
    FeishuRoutingMixin,
    FeishuRuntimeMixin,
    FeishuUIMixin,
)


class FeishuBot(
    FeishuCoreMixin,
    FeishuRuntimeMixin,
    FeishuRoutingMixin,
    FeishuInteractionMixin,
    FeishuUIMixin,
    _PluginBase,
):
    _SESSION_TTL_SECONDS = 3600
    _ACTION_DEDUPE_TTL_SECONDS = 15
    _SEEN_MESSAGE_TTL_SECONDS = 300
    _SINGLE_SESSION_KEY = "single_user"
    _DIRECT_CONFIRM_TEXTS = {
        "确认", "确认下载", "确定", "确定下载", "好的", "好", "下载吧", "好下载吧",
        "行", "行吧", "可以", "可以下载", "开始下载", "继续下载", "是", "是的",
    }
    _DIRECT_CANCEL_TEXTS = {
        "取消", "取消下载", "不用了", "先不要", "不要下载", "算了",
    }
    _DOWNLOAD_INDEX_PATTERNS = (
        re.compile(r"^\s*(?:下载|下)\s*第?\s*(\d+)\s*(?:号|个|条|项|部)?\s*$"),
        re.compile(r"^\s*第\s*(\d+)\s*(?:号|个|条|项)\s*(?:下载)?\s*$"),
    )
    _SUBSCRIBE_INDEX_PATTERNS = (
        re.compile(r"^\s*(?:订阅)\s*第?\s*(\d+)\s*(?:号|个|条|项|部)?\s*$"),
    )

    plugin_name = "飞书机器人"
    plugin_desc = "飞书群机器人消息通知与交互，支持 AI Agent 智能体模式（WebSocket 长连接）"
    plugin_icon = "Feishu_A.png"
    plugin_version = "6.0.5"
    plugin_author = "Tsutomu-miku"
    author_url = "https://github.com/Tsutomu-miku"
    plugin_config_prefix = "feishubot_"
    plugin_order = 28
    auth_level = 1

    _enabled: bool = False
    _app_id: str = ""
    _app_secret: str = ""
    _chat_id: str = ""
    _msgtypes: list = []
    _llm_enabled: bool = False
    _openrouter_key: str = ""
    _openrouter_model: str = ""
    _openrouter_free_model: str = DEFAULT_MODEL
    _openrouter_fallback_models: list = []
    _openrouter_auto_fallback: bool = True
    _use_ws: bool = True

    _feishu: Optional[_FeishuAPI] = None
    _engines: Optional[dict] = None
    _shared_state: Optional[ChatState] = None
    _engine_pool_lock: Optional[threading.Lock] = None
    _recent_actions: Optional[dict] = None
    _recent_actions_lock: Optional[threading.Lock] = None
    _seen_msg_ids: Optional[dict] = None
    _seen_msg_ids_lock: Optional[threading.Lock] = None
    _global_processing_lock: Optional[threading.Lock] = None
    _global_processing: bool = False

    _ws_client: Optional[Any] = None
    _ws_thread: Optional[threading.Thread] = None
    _ws_running: bool = False
