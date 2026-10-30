# DiffBot 🚪

[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![NcatBot](https://img.shields.io/badge/Powered%20by-NcatBot-ff69b4.svg)](https://docs.ncatbot.xyz)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-116%20Passed-brightgreen.svg)]()
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?logo=docker&logoColor=white)](Dockerfile)

现代化 MediaWiki 变更监控与 QQ 群推送机器人。基于 [NcatBot](https://docs.ncatbot.xyz)（OneBot v11 / NapCat）与 MediaWiki Action API 构建，具备多站点多语言按群订阅、Diff 可视化卡片渲染、防刷屏聚合限流、以及海外社区（Discord / Twitter）动态转载桥接能力。

---

## ✨ 核心特性

- 🌐 **多站点与多语言支持**
  - 支持监控任意符合 MediaWiki Action API 标准的维基站点（官方站、自建站、Fandom、Miraheze 等）。
  - 支持多群独立订阅不同站点；中英文多语言模板驱动，原生接入 zoneinfo 时区转换。
  - 支持配置 MediaWiki Bot 账号凭据，解决私密维基或匿名编辑 403 权限问题。

- 🎨 **优雅的可视化 Diff 卡片**
  - 内置基于 Pillow 的深色图文卡片渲染引擎，将条目变更差异直接渲染为直观的红绿对比高亮卡片图。
  - 图片渲染异常或发送失败时自动优雅降级为结构化纯文本。

- 🛡️ **工业级防刷屏与流控设计**
  - **动态聚合（Digest）**：单次轮询变更数达到阈值时自动合并为聚合摘要推送，防止突发连续编辑炸群。
  - **群级令牌桶限流**：每群独立速率控制（支持热更新覆盖），队列积压超限自动合并。
  - **长文本智能分段**：单条消息超出字数限制按语义自然行切分分段。
  - **首次启动防回放**：新站点或机器人冷启动时仅快速对齐游标，绝不倒灌历史变更。
  - **健壮退避重试**：API 遇 429 限流或 5xx 故障自动触发指数退避，避免给维基源站施加额外压力。

- 🎯 **精细化的过滤与监视体系**
  - **命名空间过滤**：基于标准数字 ID（如 0 主条目、1 讨论页、10 模板），各语言站点行为统一。
  - **字节阈值过滤**：支持设定最小变更字节数（`minbytes`），过滤微小排版标点编辑。
  - **用户过滤**：支持屏蔽特定编辑者或机器人用户。
  - **页面白名单监视（Watchlist）**：支持仅推送关注列表内的核心重点页面。

- 🌉 **海外社区动态桥接（Discord / Twitter → QQ）**
  - **Discord 频道转载**：通过 Discord REST API 轮询指定文本/公告频道，过滤后附带首图推送到对应 QQ 群。
  - **Twitter 账号转载**：通过 [RSSHub](https://docs.rsshub.app/) 实例订阅 X/Twitter 用户时间线，无需官方高额 API 费用。

- 💾 **运行期热配置与数据持久化**
  - 核心采用 SQLite WAL 模式，内置 Schema v3 自动化热迁移与版本追踪。
  - 配置文件仅作首次安装引导“种子”，运行期间所有增删改查、开关、过滤均通过 QQ 群内指令热生效，重启绝不丢失。
  - 全指令操作审计日志（`audit` 表），追溯每一次管理操作的执行人与时间。

---

## 🏗️ 架构设计

```
plugins/diffbot/main.py     ← NcatBot 插件适配层（薄层：消息分发/权限鉴权/指令路由）
diffbot_core/               ← 框架无关核心（高内聚、可独立测试与迁移）
  ├── commands.py           声明式指令表、参数解析引擎与动态帮助生成
  ├── manage.py             管理与配置服务（站点/订阅/过滤器/群设置/审计写入）
  ├── config.py             数据模型与配置定义
  ├── mw_api.py             MediaWiki API 异步客户端（recentchanges / compare / 重试退避）
  ├── monitor.py            维基轮询引擎（游标追踪、精准去重、群级路由、运行时增删站点）
  ├── discord_api.py        Discord REST 客户端（频道鉴权校验 / 消息拉取 / 附件流式处理）
  ├── rss_api.py            RSS/Atom 异步解析客户端（对接 RSSHub Twitter 时间线）
  ├── repost.py             海外平台转载引擎（调度轮询、关键词/Bot过滤、入队分发）
  ├── repost_format.py      外部平台富文本到 QQ 格式转换器
  ├── formatter.py          变更通知多语言格式化引擎
  ├── render.py             Pillow 图文对比卡片渲染器
  ├── i18n.py               国际化本地化模版集合（zh / en）
  ├── pusher.py             异步令牌桶限流队列与消息发送器
  ├── store.py              SQLite 持久化数据层（WAL 模式 + Schema v3 自动迁移）
  └── healthcheck.py        容器健康检查（心跳文件 + SQLite 可读性检测）
```

---

## 🚀 快速上手

### 方式一：Docker Compose 部署（推荐生产环境，Linux）

1. **准备配置文件**
   ```bash
   git clone https://github.com/DOORS-Chinese-Wiki/Diffbot.git
   cd Diffbot

   # 复制宿主配置与环境变量模板
   cp config.example.yaml config.yaml
   cp .env.example .env
   ```

2. **填写关键配置**
   - 编辑 `.env`：填写 `QQ_ACCOUNT`（机器人登录的 QQ 号）。
   - 编辑 `config.yaml`：填写 `bot_uin` 与管理员 QQ `root`。

3. **启动容器**
   ```bash
   docker compose up -d --build
   ```

4. **首次扫码登录 QQ**
   ```bash
   docker compose logs -f napcat
   ```
   控制台将打印登录二维码，使用手机 QQ 扫码即可完成登录。

> 💡 **生产特性**：
> - 容器使用非 root 专用用户 `bot` 运行。
> - 配置有基于心跳文件的 Docker HEALTHCHECK（`python -m diffbot_core.healthcheck`）。
> - 容器生命周期完整支持 `SIGTERM` 优雅停机：取消监控轮询 → 排空待发队列 → 安全关闭数据库。

---

### 方式二：本地运行（Linux / Windows）

1. **环境准备与依赖安装**
   ```bash
   # 要求 Python 3.12+
   python -m venv .venv

   # Linux / macOS
   source .venv/bin/activate
   # Windows
   .venv\Scripts\activate

   pip install -r requirements.txt
   ```

2. **配置与验证**
   ```bash
   cp config.example.yaml config.yaml
   # 调整 config.yaml 中的 NapCat ws_uri（本地通常为 ws://localhost:3001）

   # 运行离线验证脚本（测试 MediaWiki 变更抓取与排版格式）
   python scripts/verify_zh_en.py
   ```

3. **启动机器人**
   ```bash
   python main.py
   ```

---

## ⚙️ 配置说明

### 1. 宿主配置 (`config.yaml`)

用于配置 NcatBot 运行时、NapCat 连接地址与超级管理员：

```yaml
bot_uin: '10000'          # 机器人 QQ 号
root: '10001'             # 最高管理员 QQ 号
adapters:
  - type: napcat
    platform: qq
    enabled: true
    config:
      ws_uri: ws://localhost:3001   # NapCat 的 WebSocket 监听地址
      ws_token: ""                  # 若开启了 Token 校验在此填写
plugin:
  load_plugin: true                 # 加载 plugins/ 目录下的插件
```

### 2. 插件种子配置 (`plugins/diffbot/config.yaml`)

此文件仅在数据库首次生成时提供**初始种子数据**，启动后系统所有配置保存在 `data/diffbot.db` 中。

```yaml
sites:
  doors_zh:
    name: DOORS 中文维基
    language: zh
    api_url: https://zh.doorsgame.wiki/w/api.php
    base_url: https://zh.doorsgame.wiki
    timezone: Asia/Shanghai
    icon: 🚪
    enabled: true

poll:
  interval: 60         # 基础轮询间隔（秒）
  jitter: 5            # 随机抖动时间，避免整齐并发
  batch_limit: 50      # 单次获取变更数量上限
  digest_threshold: 6  # 变更达到此条数自动聚合

push:
  throttle_per_minute: 5   # 每群每分钟最大推送条数
  max_diff_lines: 12       # 卡片展示的最大 diff 行数
  visual: true             # 是否默认开启图文卡片模式
```

### 3. 支持的环境变量

| 环境变量 | 默认值 | 作用说明 |
| :--- | :--- | :--- |
| `DIFFBOT_CONFIG` | `config/diffbot.yaml` | 自定义 DiffBot 配置文件路径 |
| `DIFFBOT_DATA_DIR` | `./data` | 数据持久化存储目录（存放 `diffbot.db` 与心跳文件） |
| `DIFFBOT_ADMINS` | 空 | 额外指定的全局超级管理员 QQ（英文逗号分割） |
| `DIFFBOT_DISCORD_TOKEN` | 空 | Discord Bot Token（用于 `/bridge` 转发功能） |
| `DIFFBOT_RSSHUB_URL` | `https://rsshub.app` | 自建或可用的 RSSHub 实例地址（用于 Twitter 转发） |
| `DIFFBOT_NO_UVLOOP` | `0` | 在 Linux 环境下设置为 `1` 可关闭 uvloop |

---

## 💬 完整群指令手册

> 💡 **提示**：所有指令均支持 `/cmd` 与 `cmd` 两种触发形式。在多维基订阅的群聊中，支持使用 `@站点ID` 或 `--site 站点ID` 针对特定站点操作。

### 1. 维基监控与订阅 (`/wiki`)

| 指令 | 说明 | 最低权限 |
| :--- | :--- | :--- |
| `/wiki list` | 列出当前群已订阅的维基站点详情及站点 ID | 所有人 |
| `/wiki add <URL> [选项]` | 实时校验 API 连通性，添加新站点并**立即开启监控** | 群管理员 |
| `/wiki remove <站点ID>` | 退订指定站点（本群不再接收，且不会被种子覆盖复活） | 群管理员 |
| `/wiki edit <站点ID> [选项]` | 动态修改指定站点的名称/语言/时区/API地址/抓取频率等 | 群管理员 |
| `/wiki enable\|disable <站点ID>` | 恢复或暂停本群对该站点的监控推送 | 群管理员 |
| `/wiki delete <站点ID>` | 全局彻底删除站点及其下全部订阅、游标与监视记录 | 超级管理员 |

*`/wiki add` 与 `/wiki edit` 支持的选项：`--lang <zh|en>`、`--name <名称>`、`--api <API_URL>`、`--user <用户名@机器人名:密码>`、`--interval <秒>`、`--digest <阈值>`*。

### 2. 精准页面监视 (`/page`)

| 指令 | 说明 | 最低权限 |
| :--- | :--- | :--- |
| `/page list [@站点]` | 查看本群针对目标站点的专属关注页面列表 | 所有人 |
| `/page watch <页面标题> [@站点]` | 将页面加入专属监视白名单（设置后**仅推送**列表内页面） | 群管理员 |
| `/page unwatch <页面标题> [@站点]`| 从监视白名单中移除特定页面 | 群管理员 |
| `/page clear [@站点]` | 清空监视列表（恢复全站过滤规则推送） | 群管理员 |

### 3. 过滤规则管理 (`/filter`)

| 指令 | 说明 | 最低权限 |
| :--- | :--- | :--- |
| `/filter show [@站点]` | 显示当前生效的过滤规则（命名空间、字节差、黑名单） | 所有人 |
| `/filter ns <0,1,10\|all> [@站点]` | 设置推送的命名空间数字 ID（`all` 为不过滤） | 群管理员 |
| `/filter minbytes <数值> [@站点]` | 设置变更触发推送的最小绝对字节差 | 群管理员 |
| `/filter ignore <用户名> [@站点]` | 忽略指定用户（再次执行或前缀 `-用户名` 可解除） | 群管理员 |

### 4. 手动查询与交互

| 指令 | 说明 | 最低权限 |
| :--- | :--- | :--- |
| `/diff <条目标题> [版本号] [@站点]` | 手动抓取并输出指定条目的最新版本差异 | 所有人 |
| `/recent [数量=5] [@站点]` | 手动查询目标站点的最新变更列表 | 所有人 |

### 5. 海外社区动态转载桥接 (`/bridge`)

| 指令 | 说明 | 最低权限 |
| :--- | :--- | :--- |
| `/bridge list` | 列出本群已绑定的 Discord 频道与 Twitter 动态源 | 所有人 |
| `/bridge add <频道ID\|@推特Handle\|URL>` | 绑定目标源并开启自动转载推送 | 群管理员 |
| `/bridge remove <源ID或Handle>` | 本群取消绑定特定源 | 群管理员 |
| `/bridge enable\|disable <源ID>` | 暂停或恢复本群对该源的接收 | 群管理员 |
| `/bridge filter show [--source 源]` | 查看转载过滤规则 | 所有人 |
| `/bridge filter keyword <关键词>` | 设置关键词过滤（`-词` 移除，`all` 清空） | 群管理员 |
| `/bridge filter ignore <作者>` | 设置作者屏蔽黑名单（`-作者` 移除） | 群管理员 |
| `/bridge filter bots on\|off` | 是否过滤 Discord 机器人的发言消息 | 群管理员 |
| `/bridge delete <源ID>` | 全局彻底删除指定动态源 | 超级管理员 |

### 6. 系统与环境管理 (`/bot`)

| 指令 | 说明 | 最低权限 |
| :--- | :--- | :--- |
| `/bot status` | 查看 DiffBot 运行状态（进程运行时间、群订阅数、全局统计） | 所有人 |
| `/bot visual on\|off` | 切换本群的**图文卡片模式**（开：发卡片图；关：发文本） | 群管理员 |
| `/bot lang zh\|en` | 切换本群的系统提示和指令回执语言 | 群管理员 |
| `/bot admin add\|remove <QQ>` | 添加或移除特权管理员白名单 | 超级管理员 |
| `/bot reload` | 重新读取配置种子并热同步调度引擎 | 超级管理员 |
| `/help [指令名]` | 智能生成帮助文档，自动根据发送者权限动态修剪 | 所有人 |

---

## 🖼️ 效果展示

### 图文卡片模式（默认开启）
通过 Pillow 渲染，包含条目名称、编辑者、变更字节数增减、编辑摘要、时区转换后的时间戳、红绿行内对比高亮与直达 Diff 页面的安全超链接。

### 纯文本推送示例
当设置 `/bot visual off` 或在网络较差环境下自动降级时输出：

```text
🚪 [DOORS 中文维基] 📝 页面「十字架」已更新
👤 编辑者: User:Seek
🕐 时间: 2026-10-31 14:20:15 CST
📊 变更: +312 字节
📝 摘要: 补充关于 50 关后柜内使用的机制描述

--- 变更详情 ---
🟩➕ 在特定房型中，持有十字架可直接压制突袭事件。
🟥➖ 旧版本中对部分怪物的效果存在偏差。

🔗 差异对比: https://zh.doorsgame.wiki/w/index.php?diff=11485&oldid=11480
```

---

## 🧪 测试与开发

本项目拥有高覆盖率的单元测试套件，测试覆盖了从 API 客户端重试、格式化模版、限流器令牌桶算法、命令解析树、到数据库降级与版本迁移等各个核心环节：

```bash
# 安装开发环境依赖
pip install -e ".[dev]"

# 执行全部测试套件（脱机秒级运行，无需网络或 QQ 凭据）
pytest
```

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源发布。欢迎为 DOORS 维基社区或 MediaWiki 开源生态提交 Issue 与 Pull Request！
