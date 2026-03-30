"""飞书机器人插件 — Agent 工具定义与系统提示词"""

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
