# MoviePilot-Plugin-FeishuBot

<p align="center">
  <strong>MoviePilot 飞书机器人双向通信插件</strong>
</p>

<p align="center">
  支持影视搜索 | 订阅管理 | 下载任务查看 | 交互式卡片 | 系统通知推送
</p>

---

## ✨ 功能特性

### 🔄 双向通信
- **接收消息**：通过飞书 Webhook 回调接收用户消息
- **发送消息**：自动推送 MoviePilot 系统通知到飞书
- **卡片交互**：支持交互式消息卡片按钮操作

### 🎬 影视搜索
- 发送关键词或 `/search <关键词>` 搜索影视
- 搜索结果以精美卡片展示，包含评分、简介
- 支持一键订阅和搜索资源

### 📥 订阅下载
- `/subscribe <关键词>` 一键订阅影视
- 搜索结果卡片中直接点击「订阅」按钮
- 资源列表中点击「下载」按钮直接下载

### ⬇️ 下载管理
- `/downloading` 查看当前下载任务进度

### 📢 系统通知
- 自动接收 MoviePilot 系统通知（转移完成、下载完成等）
- 支持选择推送的通知类型

---

## 📋 支持的命令

| 命令 | 别名 | 说明 |
|------|------|------|
| `/search <关键词>` | `/s`, `/搜索` | 搜索影视 |
| `/subscribe <关键词>` | `/sub`, `/订阅` | 订阅影视 |
| `/downloading` | `/dl`, `/下载中` | 查看下载任务 |
| `/help` | `/h`, `/帮助` | 显示帮助 |
| `<直接输入文字>` | — | 自动搜索 |

---

## 🛠️ 安装配置

### 1. 创建飞书应用

1. 登录 [飞书开放平台](https://open.feishu.cn/app)
2. 创建自建应用，获取 **App ID** 和 **App Secret**
3. 开启「机器人」能力
4. 添加以下权限：
   - `im:message` — 读取用户发给机器人的消息
   - `im:message:send_as_bot` — 以机器人身份发送消息

### 2. 配置事件订阅

1. 在飞书开放平台 → 事件订阅 → 配置请求地址：
   ```
   https://你的MoviePilot地址/api/plugin/feishu/webhook
   ```
2. 订阅以下事件：
   - `im.message.receive_v1` — 接收消息

### 3. 配置消息卡片回调

1. 在飞书开放平台 → 消息卡片 → 配置卡片回调地址：
   ```
   https://你的MoviePilot地址/api/plugin/feishu/webhook
   ```

### 4. 安装插件

在 MoviePilot 的 `PLUGIN_MARKET` 环境变量中添加本仓库地址：

```
https://github.com/你的用户名/MoviePilot-Plugin-FeishuBot
```

或在 MoviePilot 后台 → 插件市场 → 添加第三方仓库。

### 5. 配置插件

在 MoviePilot 后台 → 插件 → 飞书机器人，填入：

| 配置项 | 说明 |
|--------|------|
| App ID | 飞书应用的 App ID |
| App Secret | 飞书应用的 App Secret |
| Verification Token | 事件订阅的验证 Token |
| Encrypt Key | （可选）事件加密密钥 |
| 默认会话 ID | 用于主动推送通知的 chat_id |
| 消息类型 | 选择要推送的通知类型 |

---

## 🏗️ 架构说明

```
                    ┌─────────────────┐
                    │   飞书客户端      │
                    └────────┬────────┘
                             │ 消息/卡片回调
                             ▼
                    ┌─────────────────┐
                    │  飞书开放平台     │
                    └────────┬────────┘
                             │ Webhook POST
                             ▼
              ┌──────────────────────────────┐
              │  MoviePilot                   │
              │  ┌────────────────────────┐  │
              │  │  FeishuBot Plugin      │  │
              │  │  /api/plugin/feishu/   │  │
              │  │  webhook               │  │
              │  └───────────┬────────────┘  │
              │              │               │
              │  ┌───────────▼────────────┐  │
              │  │  MediaChain            │  │
              │  │  SearchChain           │  │
              │  │  SubscribeChain        │  │
              │  │  DownloadChain         │  │
              │  └────────────────────────┘  │
              └──────────────────────────────┘
```

### 核心流程

1. **消息接收**：飞书将用户消息通过 Webhook POST 到 `/api/plugin/feishu/webhook`
2. **命令解析**：插件解析消息文本，分发到对应的命令处理器
3. **业务执行**：调用 MoviePilot 内部 Chain（MediaChain、SearchChain 等）执行搜索/订阅/下载
4. **结果回复**：将结果构建为飞书消息卡片，通过飞书 API 发送回用户
5. **卡片交互**：用户点击卡片按钮时，飞书回调到同一 Webhook，插件处理后返回 Toast 或更新卡片
6. **通知推送**：监听 MoviePilot 的 `NoticeMessage` 事件，自动推送到飞书

---

## 📁 目录结构

```
MoviePilot-Plugin-FeishuBot/
├── plugins.v2/
│   └── feishubot/
│       └── __init__.py       # 插件主代码
├── icons/
│   └── feishu.png            # 插件图标
├── package.v2.json           # 插件元数据
├── LICENSE
└── README.md
```

---

## ⚠️ 注意事项

1. **MoviePilot 必须有公网访问地址**：飞书需要能 POST 到你的 Webhook 回调地址
2. **HTTPS 推荐**：飞书要求回调地址使用 HTTPS（可通过 Nginx/Caddy 反代实现）
3. **响应时间**：飞书要求 Webhook 在 3 秒内响应，插件已对耗时操作使用异步处理
4. **API 版本**：本插件适配 MoviePilot V2 版本

---

## 📝 更新日志

### v1.0.0
- 首个版本发布
- 支持飞书双向消息通信
- 支持影视搜索（关键词识别 + TMDB 搜索）
- 支持一键订阅和下载
- 支持交互式消息卡片
- 支持 MoviePilot 系统通知推送
- 支持消息类型过滤

---

## 📄 License

MIT License