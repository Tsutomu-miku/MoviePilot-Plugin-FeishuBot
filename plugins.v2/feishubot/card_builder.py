"""飞书机器人插件 — 飞书卡片构建器"""


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
