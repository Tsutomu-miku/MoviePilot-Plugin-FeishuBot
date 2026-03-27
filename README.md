# MoviePilot-Plugin-FeishuBot

<p align="center">
  <strong>MoviePilot 飞书机器人双向通信插件</strong>
</p>
<p align="center">
  基于飞书 WebSocket 长连接 · 无需公网 IP · 支持影视搜索与订阅下载
</p>

---

## ✨ 特性

- **WebSocket 长连接** — 参考 [OpenClaw](https://github.com/nicepkg/openclaw) 方案，使用飞书官方长连接协议，NAS 部署无需公网 IP、无需域名、无需反向代理
- **双向通信** — 在飞书中搜索影视、订阅、查看下载进度，系统通知自动推送
- **交互式卡片** — 搜索结果以富文本卡片展示，回复序号快捷操作
- **自动重连** — 断线自动重连，服务稳定运行
- **双引擎** — 优先使用官方 `lark-oapi` SDK，未安装时自动退化为内置 WebSocket 客户端

## 🏗️ 架构

```
┌────────────┐     WebSocket (长连接)      ┌─────────────────┐
│  飞书客户端  │ ◄═══════════════════════► │  飞书开放平台     │
│  (手机/PC)  │                            │  (事件推送服务)   │
└────────────┘                            └────────┬────────┘
                                                   │ WSS 长连接
                                                   ▼
                                          ┌─────────────────┐
                                          │  MoviePilot NAS  │
                                          │  (FeishuBot插件)  │
                                          │                   │
                                          │  ┌─────────────┐  │
                                          │  │ WS 客户端    │  │ ← 主动连接，无需公网
                                          │  └──────┬──────┘  │
                                          │         │         │
                                          │  ┌──────▼──────┐  │
                                          │  │ 命令处理器   │  │
                                          │  └──────┬──────┘  │
                                          │         │         │
                                          │  ┌──────▼──────┐  │
                                          │  │ MediaChain   │  │ ← 调用 MoviePilot 内部 API
                                          │  │ SearchChain  │  │
                                          │  │ SubChain     │  │
                                          │  └─────────────┘  │
                                          └───────────────────┘
```

**对比传统 Webhook 方案：**

| 对比项 | Webhook 方案 | WebSocket 长连接 (本插件) |
|--------|-------------|---------------------------|
| 公网 IP | ✅ 必须 | ❌ 不需要 |
| 域名 + HTTPS | ✅ 必须 | ❌ 不需要 |
| 反向代理 | ✅ 需要 Nginx 等 | ❌ 不需要 |
| NAT 穿透 | ✅ 需要 frp 等 | ❌ 不需要 |
| 连接方向 | 飞书 → 你的服务器 | 你的服务器 → 飞书 |
| NAS 友好度 | ⭐ | ⭐⭐⭐⭐⭐ |

## 📋 支持的命令

| 命令 | 别名 | 说明 |
|------|------|------|
| `/search <关键词>` | `/s`, `/搜索` | 搜索影视，返回结果卡片 |
| `/subscribe <关键词>` | `/sub`, `/订阅` | 订阅影视，自动搜索下载 |
| `/downloading` | `/dl`, `/下载中` | 查看下载中的任务和进度 |
| `/help` | `/h`, `/帮助` | 显示帮助信息 |
| 直接发送文字 | — | 自动作为关键词搜索 |
| `订阅1` / `订阅2` | — | 快捷订阅搜索结果 |
| `下载1` / `下载2` | — | 快捷搜索资源下载 |

## 🚀 安装配置

### 第一步：创建飞书应用

1. 登录 [飞书开放平台](https://open.feishu.cn/app/)，点击「创建企业自建应用」
2. 记录 **App ID** 和 **App Secret**

### 第二步：配置事件订阅（长连接模式）

1. 进入应用 → **事件与回调**
2. **加密策略**：选择任意策略（长连接模式下不需要 Verification Token）
3. **事件推送方式**：选择 **「使用长连接接收」**
4. 点击「添加事件」，搜索并订阅：
   - `im.message.receive_v1` — 接收消息

### 第三步：配置权限

进入 **权限管理**，开通以下权限：

| 权限 | 说明 |
|------|------|
| `im:message` | 获取与发送单聊、群组消息 |
| `im:message:send_as_bot` | 以应用身份发送消息 |
| `im:chat` | 获取群信息（可选） |

### 第四步：发布应用

1. 进入 **版本管理与发布** → 创建版本 → 申请发布
2. 管理员审批通过后，应用即可使用

### 第五步：安装插件

**方式一：添加自定义插件仓库**

在 MoviePilot 设置中添加插件仓库地址：

```
https://github.com/Tsutomu-miku/MoviePilot-Plugin-FeishuBot
```

**方式二：环境变量**

```env
PLUGIN_MARKET=https://github.com/Tsutomu-miku/MoviePilot-Plugin-FeishuBot
```

### 第六步：配置插件

在 MoviePilot 插件市场安装「飞书机器人」，填入：

- **App ID** — 飞书应用的 App ID
- **App Secret** — 飞书应用的 App Secret
- **默认会话 ID** — 可留空，首次收到消息时自动记录

启用插件后，WebSocket 长连接会自动建立。

## 🔧 工作原理

```
1. 插件启动 → POST /callback/ws/endpoint (app_id + app_secret)
      ↓
2. 获取 WSS 地址 → wss://xxx?device_id=yyy&service_id=zzz
      ↓
3. 建立 WebSocket 连接 ← 插件主动连接，可穿越 NAT
      ↓
4. 收到 Protobuf 帧 → 解析事件 → 处理消息 → 回复 ACK
      ↓
5. 通过飞书 HTTP API 发送消息/卡片给用户
```

**双引擎模式：**
- **SDK 模式**：如果已安装 `lark-oapi` 包，自动使用官方 SDK 的 WebSocket 客户端
- **内置模式**：未安装 SDK 时，使用内置 `websocket-client` 实现连接

推荐在 MoviePilot 的 Docker 中预装 SDK：
```bash
pip install lark-oapi
```

## 🤖 Agent 模式技术方案

### 多轮下载确认流程

Agent 模式下，用户通过自然语言完成「搜索 → 选择 → 确认下载」三步交互。每一步都是独立的用户消息，触发独立的 Agent 循环。核心挑战是**跨消息保持上下文**。

#### 数据流

```
用户消息 1: "下载电影 xxx"
│
├─ Agent Loop ─► search_resources("xxx")
│                  └─► _resource_cache[user] = [ctx0, ctx1, ...]
│                  └─► LLM 回复推荐列表
├─ 保存对话历史 ✓
│
用户消息 2: "选择第3个"
│
├─ Agent Loop ─► download_resource(index=3, confirmed=false)
│                  └─► _pending_download[user] = {index:3, title:...}
│                  └─► LLM 回复资源详情，询问确认
├─ 保存对话历史 ✓
│
用户消息 3: "确认下载"
│
├─ Agent Loop ─► download_resource(index=-1, confirmed=true)
│                  └─► 从 _pending_download[user] 取回 index=3
│                  └─► 补充 media_info (如缺失)
│                  └─► DownloadChain.download_single(ctx)
│                  └─► 清除 _pending_download[user]
├─ 保存对话历史 ✓
```

#### 解决的问题

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 确认下载时 Agent 不知道要下载哪个 | 用户消息 3 触发新 Agent 循环，LLM 可能丢失上下文 | `_pending_download` 缓存：confirmed=false 时记住用户选择，confirmed=true 时 index=-1 自动回溯 |
| 打断导致对话历史丢失 | 用户连续发消息触发打断机制，被打断的消息不保存历史 | 打断时仍保存已完成的对话上下文，后续消息可获得完整历史 |
| download_resource 报错 NoneType | `SearchChain.search_by_title` 返回的 Context 不含 `media_info`，但 `DownloadChain.download_single` 访问 `context.media_info.category` | confirmed=true 执行前检测并调用 `MediaChain.recognize_media` 补充 |

#### 关键状态存储

| 存储 | 类型 | 生命周期 | 作用 |
|------|------|----------|------|
| `_resource_cache[user_id]` | `List[Context]` | search_resources 时写入 | 保存搜索到的种子资源列表 |
| `_pending_download[user_id]` | `dict{index,title,...}` | confirmed=false 时写入，confirmed=true 后清除 | 记住用户待确认的下载选择 |
| `_conversations` | `ConversationManager` | 每次 agent_handle 完成后保存 | 多轮对话历史（含 tool_calls） |
| `_search_cache[user_id]` | `List[MediaInfo]` | search_media 时写入 | 保存影视搜索结果 |

### 并发控制（打断 + 合并）

```
用户快速发送多条消息:

msg_1 ──┐
msg_2 ──┤  合并窗口 (1.5s)
msg_3 ──┘
         ↓
    取最新消息 msg_3 开始处理
         ↓
    处理中... ← msg_4 到达 → 标记打断 + 存储 msg_4
         ↓
    当前轮次完成 → 保存已完成对话历史
         ↓
    检测到打断 → 取 msg_4 继续处理
```

## 📁 目录结构

```
MoviePilot-Plugin-FeishuBot/
├── plugins.v2/
│   └── feishubot/
│       └── __init__.py       # 插件主代码
├── icons/
│   └── feishu.png            # 插件图标
├── package.v2.json           # 插件元数据
├── LICENSE                   # MIT License
└── README.md                 # 本文件
```

## 📋 更新日志

| 版本 | 更新内容 |
|------|----------|
| v5.2.0 | 修复 Agent 多轮下载确认上下文丢失：新增 `_pending_download` 状态缓存 + 打断时保存对话历史 + `media_info` 补充识别 |
| v5.1.3 | 修复执行错误导致的异常关闭 |
| v5.1.1 | Agent 并发修复 + 下载容错增强 + System Prompt 上下文理解指令 |
| v5.1.0 | 消息去重(msg_id幂等) + 并发竞态修复 + 即时反馈增强 + 卡片样式优化 |
| v5.0.0 | Agent 并发控制重构 + 即时反馈卡片 + 飞书交互式卡片全面升级 |
| v4.0.0 | 新增 WebSocket 长连接收消息，替代已失效的 HTTP 回调方式 |
| v3.0.0 | 新增 Agent 智能对话模式 |
| v2.0.0 | 新增搜索、订阅、下载等交互指令 |
| v1.0.0 | 初始版本：飞书机器人消息通知 |

## 📝 许可证

[MIT License](LICENSE)
