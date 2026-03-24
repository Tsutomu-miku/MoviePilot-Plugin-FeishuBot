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

## 📝 许可证

[MIT License](LICENSE)
