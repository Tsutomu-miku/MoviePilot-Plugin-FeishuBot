"""
Agent 核心 — 工具定义、系统提示词、多轮 Tool Calling 循环

关键设计：
- 工具定义遵循 OpenAI Function Calling 格式
- download_resource 强制 confirmed 参数，工具层面保证用户确认
- Agent 循环在消息副本上操作，失败不会污染对话历史
- 消息格式严格清洗，防止 API 响应中的额外字段干扰后续调用
"""

import json
from typing import Callable, Optional, Tuple

from app.log import logger

from .llm_client import OpenRouterClient
from .tool_impl import ToolExecutor


# ════════════════════════════════════════════════════════════════════════
#  工具定义（OpenAI Function Calling 格式）
# ════════════════════════════════════════════════════════════════════════

AGENT_TOOLS = [
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
                "1. 第一次调用：confirmed=false → 返回资源详情，不会下载\n"
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
                        "description": (
                            "是否已获得用户确认。"
                            "首次推荐资源时必须设为 false，"
                            "用户明确确认后设为 true"
                        ),
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
                "订阅影视作品，订阅后系统会自动搜索并下载更新。"
                "可以传入 search_media 返回列表中的序号，或直接传入作品名称。"
                "当用户想订阅、追剧、自动下载时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "search_media 返回列表中的序号（从 0 开始），优先使用",
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
                "向用户发送一条中间状态消息（如「正在搜索...」「处理中...」）。"
                "适合在执行耗时操作前告知用户。"
                "注意：Agent 最终回复会自动发送，不需要用这个工具发最终结果。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要发送的消息内容",
                    }
                },
                "required": ["text"],
            },
        },
    },
]


# ════════════════════════════════════════════════════════════════════════
#  系统提示词
# ════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """\
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

### 下载（最重要的流程，必须严格遵守）
1. 用户想下载 → 调用 search_resources 获取资源列表
2. 分析返回的 tags，根据用户偏好筛选排序
3. 推荐 1-3 个最佳资源，说明推荐理由
4. 调用 download_resource(index=X, confirmed=false) 获取待下载资源详情
5. **展示详情并明确询问用户是否确认下载**
6. 用户确认后 → 调用 download_resource(index=X, confirmed=true) 执行下载
7. **绝对禁止**未经用户确认就设置 confirmed=true

### 订阅
用户想追剧/订阅 → 调用 search_media 确认作品 → 调用 subscribe_media

### 偏好理解
- "4K" "超高清" → 2160p/4K/UHD
- "蓝光" "原盘" → BluRay/Remux
- "5.1环绕声" → 5.1/DD5.1/DDP5.1
- "全景声" → Atmos
- "杜比视界" "DV" → DolbyVision/DV
- "HDR" → HDR/HDR10/HDR10+
- "高码率" → Remux/BluRay + 大文件
- "体积小" → WEB-DL + x265 + 小 size

## 回复风格
- 简洁友好，使用中文
- 用 emoji 适当点缀
- 展示列表时用编号，突出关键信息（分辨率、大小、做种数）
- 闲聊直接回复，不调用工具
- 用户说「清除对话」「重新开始」时告知已清除历史"""


# ════════════════════════════════════════════════════════════════════════
#  消息清洗工具
# ════════════════════════════════════════════════════════════════════════

def _sanitize_assistant_message(raw_msg: dict) -> dict:
    """
    清洗 LLM 返回的 assistant 消息，只保留标准字段。

    防止 API 返回的额外字段（refusal, annotations 等）
    污染对话历史导致后续调用失败。
    """
    clean = {"role": "assistant"}

    tool_calls = raw_msg.get("tool_calls")
    if tool_calls:
        # 只保留标准的 tool_call 结构
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

    # content: 确保是字符串（部分模型在 tool_calls 时返回 null）
    content = raw_msg.get("content")
    clean["content"] = content if isinstance(content, str) else ""

    return clean


# ════════════════════════════════════════════════════════════════════════
#  Agent 循环
# ════════════════════════════════════════════════════════════════════════

MAX_AGENT_ITERATIONS = 10


class AgentRunner:
    """
    Agent 执行引擎：多轮 Tool Calling 循环。

    设计原则：
    - 在消息副本上操作，异常不会污染原始对话历史
    - 所有 LLM 返回的消息经过清洗后才加入历史
    - 工具执行结果始终为 JSON 字符串
    """

    def __init__(
        self,
        llm_client: OpenRouterClient,
        tool_executor: ToolExecutor,
        max_iterations: int = MAX_AGENT_ITERATIONS,
    ):
        self.llm_client = llm_client
        self.tool_executor = tool_executor
        self.max_iterations = max_iterations

    def run(
        self,
        messages: list,
        chat_id: str,
        user_id: str,
        send_message_fn: Optional[Callable] = None,
    ) -> Tuple[list, str]:
        """
        执行 Agent 循环。

        Args:
            messages: 对话历史（含 system prompt 和新用户消息）
            chat_id: 飞书 chat_id
            user_id: 用户 open_id
            send_message_fn: 发送中间消息的回调

        Returns:
            (updated_messages, final_reply) — 更新后的消息列表和最终回复文本
        """
        # 在副本上操作，保护调用方的原始列表
        working = list(messages)

        for iteration in range(self.max_iterations):
            # ── 调用 LLM ──
            try:
                result = self.llm_client.chat(
                    messages=working, tools=AGENT_TOOLS
                )
            except Exception as e:
                logger.error(f"Agent LLM 调用失败 (第{iteration + 1}轮): {e}")
                error_reply = f"⚠️ AI 调用失败: {e}"
                working.append({"role": "assistant", "content": error_reply})
                return working, error_reply

            # ── 解析响应 ──
            choices = result.get("choices")
            if not choices:
                logger.error(f"Agent LLM 返回无 choices: {json.dumps(result, ensure_ascii=False)[:500]}")
                error_reply = "⚠️ AI 返回异常，请稍后重试"
                working.append({"role": "assistant", "content": error_reply})
                return working, error_reply

            raw_message = choices[0].get("message", {})
            tool_calls = raw_message.get("tool_calls")

            logger.info(
                f"Agent 第{iteration + 1}轮: "
                f"tool_calls={len(tool_calls) if tool_calls else 0}, "
                f"has_content={bool(raw_message.get('content'))}"
            )

            # ── 无 tool_calls → 最终回复 ──
            if not tool_calls:
                reply = raw_message.get("content", "") or ""
                working.append({"role": "assistant", "content": reply})
                return working, reply

            # ── 有 tool_calls → 执行工具 ──
            clean_msg = _sanitize_assistant_message(raw_message)
            working.append(clean_msg)

            for tc in (tool_calls or []):
                fn_name = tc.get("function", {}).get("name", "")
                fn_args_raw = tc.get("function", {}).get("arguments", "{}")
                tc_id = tc.get("id", "")

                try:
                    fn_args = json.loads(fn_args_raw) if fn_args_raw else {}
                except (json.JSONDecodeError, TypeError):
                    fn_args = {}

                logger.info(f"Agent tool [{iteration + 1}]: {fn_name}({fn_args})")

                tool_result = self.tool_executor.execute(
                    fn_name=fn_name,
                    fn_args=fn_args,
                    chat_id=chat_id,
                    user_id=user_id,
                    send_message_fn=send_message_fn,
                )

                working.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(
                        tool_result, ensure_ascii=False, default=str
                    ),
                })

        # 超过最大轮次
        timeout_reply = "⚠️ 处理步骤过多，请尝试简化请求。"
        working.append({"role": "assistant", "content": timeout_reply})
        return working, timeout_reply
