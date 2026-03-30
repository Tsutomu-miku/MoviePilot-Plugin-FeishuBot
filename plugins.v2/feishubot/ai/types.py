"""AI 对话系统 — 数据类型定义"""

from dataclasses import dataclass, field
from typing import Any, Optional, List, Dict


@dataclass
class ToolResult:
    """工具执行结果的统一封装"""

    success: bool
    data: Any = None
    error: Optional[str] = None

    @property
    def text(self) -> str:
        """返回可嵌入对话的 JSON 文本"""
        import json as _json
        if self.success:
            return _json.dumps(self.data, ensure_ascii=False, default=str) if self.data is not None else '{"ok": true}'
        return _json.dumps({"error": self.error or "未知错误"}, ensure_ascii=False)


@dataclass
class ChatState:
    """
    对话状态 — 单用户，全局唯一实例。

    集中管理原先散落在主类中的:
      _search_cache, _resource_cache, _pending_download, _user_processing
    """

    search_cache: list = field(default_factory=list)        # search_media 结果 (MediaInfo 列表)
    resource_cache: list = field(default_factory=list)       # search_resources 结果 (Context 列表)
    pending_download: Optional[dict] = None                  # 待确认下载 {index, title, ...}
    is_processing: bool = False                              # 是否正在处理中

    def clear_download(self):
        self.pending_download = None

    def clear_all(self):
        """重置全部状态"""
        self.search_cache.clear()
        self.resource_cache.clear()
        self.pending_download = None
        self.is_processing = False
