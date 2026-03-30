"""AI 对话系统 — 工具 Schema 定义"""

# OpenAI function calling 格式的工具列表
# 每个工具的 Schema 声明在这里，实际执行逻辑在 executor.py

TOOL_SCHEMAS = [
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


# ── 工具名称 → 用户友好描述（用于进度卡片） ──

TOOL_FRIENDLY_NAMES = {
    "search_media": "搜索影视「{keyword}」",
    "search_resources": "搜索资源「{keyword}」",
    "download_resource": "下载资源 #{index}",
    "subscribe_media": "订阅影视",
    "get_downloading": "查询下载进度",
}


def friendly_tool_name(tool_name: str, tool_args: dict) -> str:
    """将工具名+参数转为用户友好的描述"""
    template = TOOL_FRIENDLY_NAMES.get(tool_name, tool_name)
    try:
        return template.format(**tool_args)
    except (KeyError, IndexError):
        return template.split("「")[0].strip()
