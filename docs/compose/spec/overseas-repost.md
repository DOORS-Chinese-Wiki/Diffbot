---
feature: overseas-repost
status: delivered
updated: 2026-02-14
branch: in-place
commits: n/a
---

# 海外平台消息转载（Discord / Twitter → QQ）

## Report

**What was built** — DiffBot 新增海外平台 → QQ 单向消息转载，支持两类源：

1. **Discord**：Bot Token + REST 轮询频道消息。
2. **Twitter**：无需 X API，经 **RSSHub** 拉取账号时间线（`/twitter/user/:id`）。

群管理员用 `/bridge add` 绑定（Discord：频道 ID/URL；Twitter：`@handle` 或 x.com 链接）。`RepostEngine` 按平台轮询，经关键词/作者/bot 过滤，格式化为本地化文本（可带首张图片）推入现有 `Pusher`。存储为 schema v3（`repost_*`），tombstone/审计/runtime sync 与 wiki 一致。

指令面 `/bridge list|add|remove|enable|disable|filter|delete`。配置：`DIFFBOT_DISCORD_TOKEN`、`DIFFBOT_RSSHUB_URL`（默认 `https://rsshub.app`）。首次绑定不回放；失败 `available=0`，re-add/enable 可恢复；转载强制 `visual=False`。

**Verification** — `pytest` → **116 passed**。含 Discord 200/429/403、RSS 解析、Twitter 首次不回放、`@handle`/x.com 解析、格式化链接等。

**Journey log** —
1. 无 git，原地实现。
2. Discord 空频道游标用 `snowflake_now()`，避免误回放。
3. `get_channel` 的 `type=0` 曾被 `or -1` 误杀。
4. `/bridge filter` 写操作在 handler 强制 GROUP_ADMIN。
5. Discord 需服主邀请 Bot；用户无管理权限后改走 Twitter+RSSHub。

## [S1] Problem

DiffBot 目前只做 MediaWiki 变更监控并推送到 QQ 群。海外社区（尤其 Discord）的公告、讨论与补丁说明无法进入同一通知链路，群管理员只能人工复制粘贴。需要一个可运行时配置的转载通道：把指定 Discord 频道的新消息，经过滤后推送到已绑定的 QQ 群。

## [S2] Design

### 已定决策

| 轴 | 选择 |
|----|------|
| 方向 | 海外平台 → QQ（单向转载） |
| 平台 | Discord REST + Twitter（RSSHub），共用 `platform` 字段与转载管线 |
| Discord 接入 | Bot Token + REST 轮询 |
| Twitter 接入 | RSSHub `/twitter/user/:id`，无需 X API |
| 绑定方式 | 群管理员指令绑定 + 每绑定过滤器 |
| 内容保真 | 正文 + 首图；嵌入降级文本 |
| 工作区 | 无 git，原地实现 |

### 架构

与现有分层一致：核心逻辑进 `diffbot_core/`，NcatBot 只做薄适配。

```
plugins/diffbot/main.py     ← 注册 /bridge 指令 + 装配 RepostEngine
diffbot_core/
  ├── discord_api.py        ← Discord REST 客户端（频道校验、拉消息、下载附件）
  ├── repost.py             ← 转载引擎（每源轮询协程、过滤、格式化、入队）
  ├── repost_format.py      ← Discord 消息 → QQ 文本（zh/en）
  ├── store.py              ← schema v3：repost_* 表
  ├── manage.py             ← BridgeService（绑定/过滤/审计，唯一写入口）
  └── pusher.py             ← 复用现有令牌桶队列（text + image_path）
```

数据流：

```
Discord REST ──poll──► RepostEngine
                          │ filter(binding)
                          │ format
                          ▼
                       Pusher ──► QQ group
```

### Discord REST 契约

- 鉴权：`Authorization: Bot <token>`
- Token 来源：环境变量 `DIFFBOT_DISCORD_TOKEN` 优先，其次 YAML `discord.token`
- 校验频道：`GET /channels/{channel_id}`，要求 `type` ∈ {0 文本, 5 公告}
- 拉消息：`GET /channels/{channel_id}/messages?limit=50&after={last_msg_id}`
  - Discord 返回**从旧到新**；引擎按 snowflake 升序处理
  - `after=0` 或空游标：视为首次，只把最新消息 id 写入游标，**不回放历史**（对齐 wiki 首次行为）
- 附件下载：对 `content_type` 为 `image/*` 且 ≤ 8MB 的附件，下载到 `{data_dir}/repost/{source_id}/{message_id}/`
- 错误：429 读 `Retry-After` 退避；5xx/网络错误指数退避；403/404 标记源不可用并暂停该源轮询

**运维前提**：Discord 应用需开启 **Message Content Intent**，且 Bot 已加入目标服务器并具备目标频道读权限。

### 过滤器（每绑定独立）

| 字段 | 语义 |
|------|------|
| `keywords` | 非空时：正文（或作者名）命中任一关键词才转发；空 = 全量 |
| `ignore_authors` | 作者 id/用户名命中则丢弃 |
| `ignore_bots` | 默认 true，丢弃 bot/webhook 消息 |

空消息且无图片附件 → 直接丢弃（避免纯表情/系统消息轰炸）。

### 格式化（zh 站示意）

```
🔁[Discord] #general
👤 Alice
🕐 2026-02-14 12:00:00 UTC
📝 正文……（截断至 max_message_chars）
📎 图 1
🔗 https://discord.com/channels/{guild}/{channel}/{message}
```

英文站点用 en 模板。复用 `pusher.split_message` 分段；首张图片走 `image_path`，其余附件只保留链接行。**不走** wiki 的 PIL 图文卡片（`visual` 对转载消息强制关闭）。

### 存储（schema v3）

在 `_SCHEMA` 追加：

```sql
CREATE TABLE IF NOT EXISTS repost_sources (
    id TEXT PRIMARY KEY,              -- discord_{channel_id}
    platform TEXT NOT NULL DEFAULT 'discord',
    channel_id TEXT NOT NULL,
    guild_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    available INTEGER NOT NULL DEFAULT 1,  -- 0=403/404 暂停
    added_by TEXT NOT NULL DEFAULT '',
    added_at INTEGER,
    UNIQUE(platform, channel_id)
);
CREATE TABLE IF NOT EXISTS repost_bindings (
    group_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    keywords TEXT,                    -- JSON list[str] | null
    ignore_authors TEXT,              -- JSON list[str]
    ignore_bots INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL DEFAULT 'command',
    PRIMARY KEY (group_id, source_id)
);
CREATE TABLE IF NOT EXISTS repost_tombstone (
    group_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (group_id, source_id)
);
CREATE TABLE IF NOT EXISTS repost_cursor (
    source_id TEXT PRIMARY KEY,
    last_msg_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS repost_seen (
    source_id TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    PRIMARY KEY (source_id, msg_id)
);
```

- `meta.schema_version` 提升为 `3`；v2 → v3 为**纯增量**（新表），无旧数据迁移
- `repost_seen` 保留上限复用 `SEEN_KEEP` 模式
- 写操作全部经 `BridgeService`，并写入现有 `audit` 表

### 指令（权限对齐现有）

| 指令 | 说明 | 权限 |
|------|------|------|
| `/bridge list` | 列出本群绑定的 Discord 频道 | 所有人 |
| `/bridge add <频道ID或URL> [--name 名称]` | 校验频道后创建源并绑定本群，立即开始轮询 | 群管理员 |
| `/bridge remove <源>` | 本群退订（tombstone，防 YAML 复活） | 群管理员 |
| `/bridge enable\|disable <源>` | 暂停/恢复本群接收 | 群管理员 |
| `/bridge filter show` | 显示当前过滤规则 | 所有人 |
| `/bridge filter keyword <词>` | 添加关键词；`-词` 移除；`all` 清空 | 群管理员 |
| `/bridge filter ignore <作者>` | 忽略作者；`-作者` 移除 | 群管理员 |
| `/bridge filter bots on\|off` | 是否忽略 bot 消息 | 群管理员 |
| `/bridge delete <源>` | 删除源及其游标/绑定/下载缓存 | 超级管理员 |

`add` 接受纯数字 channel_id 或 `https://discord.com/channels/{guild}/{channel}`。同一频道可被多个群绑定（共享游标与源表）。

### 配置

YAML 追加可选段（仅种子/全局默认，运行期以 DB 为准）：

```yaml
discord:
  token: ""          # 建议用 DIFFBOT_DISCORD_TOKEN
  poll_interval: 20  # 秒
  jitter: 3
  batch_limit: 50
  timeout: 15
  max_image_bytes: 8388608
```

### 错误与生命周期

- `add`：token 缺失 → CommandError；频道不可读 → CommandError
- 轮询失败：与 wiki 相同的 backoff；连续失败将 `available=0` 并告警日志
- 恢复：重新 `/bridge add` 同一频道或 `/bridge enable` 会将 `available` 置回 1 并 sync 任务
- 优雅退出：`RepostEngine.stop()` 取消全部源任务，与 `MonitorEngine` 同序
- `/bot status` 追加：绑定源数、Discord token 是否已配置
- 转载消息 `visual` 强制为 False（不套 PIL 图文卡片）

### 测试边界

- `discord_api`：脚本化假 aiohttp 客户端（不访问外网）
- 过滤器、格式化、游标/去重、首次不回放：纯单测
- 指令解析与权限：沿用现有 plugin handler 测试模式
- 不做真实 Discord 登录 / 真实 QQ 发送的 E2E

## [S3] Out of Scope

- Telegram / X / Mastodon 等其它平台（仅预留 `platform` 与客户端接口形状）
- Gateway 实时、历史回填、编辑/删除消息同步
- QQ → Discord 反向转发
- Discord 嵌入卡片、贴纸、语音、视频完整还原
- 多图片逐张发送与 PIL 转载卡片
- YAML 静态 channel→群 映射作为主路径（指令绑定是唯一主路径）

## Tasks

- [x] T1: 实现 `discord_api.DiscordClient`（频道元数据、拉消息、附件下载、错误分类）— acceptance: 单测用假 HTTP 覆盖 200/429/403/附件过大分支 (covers: S2)
- [x] T2: store schema v3 + repost CRUD + seen/游标辅助 — acceptance: 新建 DB 自动含 repost_* 表且 `schema_version=3`；v2 库可增量升级 (covers: S2)
- [x] T3: `BridgeService`（add/remove/toggle/filter/audit + 频道校验） — acceptance: 管理动作可单测断言 DB 与 audit 行 (covers: S2)
- [x] T4: `repost_format` + 过滤器逻辑 — acceptance: zh/en 文本、关键词/作者/bot/空消息用例通过 (covers: S2)
- [x] T5: `RepostEngine` 轮询、首次游标、去重、入队 Pusher、优雅停止 — acceptance: 假客户端下「首次不回放 / 增量推送 / 失败退避」单测通过 (covers: S2; depends: T1, T2, T4)
- [x] T6: 插件层 `/bridge` 指令注册 + handler + `/bot status` 扩展 + 配置加载与 i18n — acceptance: 现有 handler 测试模式下新增用例通过；README 指令表已更新 (covers: S2; depends: T3, T5)
- [x] T7: 全量 `pytest` 回归 — acceptance: 离线测试全绿，无新增失败 (covers: S2; depends: T1–T6)
