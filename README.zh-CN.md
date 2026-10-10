<h1 align="center">Grok Slack Bridge</h1>

<p align="center">
  <a href="README.md">English</a> |
  <strong>简体中文</strong>
</p>

<p align="center">
  <strong>让你的 Agent 以独立 Bot 身份接入 Slack：支持私信和 @提及，实时响应，无需公网地址。</strong>
</p>

<p align="center">
  <a href="skills/slack-bridge/SKILL.md">
    <img alt="Agent Skill" src="https://img.shields.io/badge/Agent-Skill-2563eb?style=flat-square">
  </a>
  <a href="https://www.python.org/">
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-172554?style=flat-square">
  </a>
  <img alt="Transport" src="https://img.shields.io/badge/transport-Slack%20Socket%20Mode-0f8f88?style=flat-square">
  <img alt="Status" src="https://img.shields.io/badge/status-experimental-d97706?style=flat-square">
</p>

本项目提供 `slack-bridge` Skill：一份 Slack 应用清单（manifest）、一个基于
Socket Mode 的 Python 转发程序，以及一组 shell 脚本。别人私信 Bot 或在频道里
@它时，转发程序把消息推送到 Agent 的 webhook 例行任务，Agent 再用 `reply.sh`
以 Bot 身份回复。

虽然是为 Grok Bot 设计的，但它是通用的：Bot 名称、webhook 目标和工作区都是配置项，
任何能被 HTTPS webhook 唤醒、能执行 shell 命令的 Agent 都可以使用。

## 为什么需要这个工具

Agent 自带的 Slack 连接器只能读取 Slack、以你的身份发消息，Slack 里的消息无法唤醒
Agent；频道监听虽然能唤醒，但回复挂在共享的应用名下，也不支持私信"你的"Bot。
本项目让 Agent 拥有自己的 Slack Bot 用户，消息实时送达，并且 Slack 这一侧保持不变，
背后的 Agent 可以随时更换。

## 能力

| 能力 | 说明 |
| --- | --- |
| 独立 Bot | 自己的名字和头像，支持私信、频道 @提及和 `/grok` |
| Socket Mode | 只建出站 WebSocket，不需要公网地址 |
| 访问控制 | 私信、@提及、跟随线程、`/grok`、按钮、停止按钮共用同一套检查：校验工作区和应用 ID；默认只服务所有者；支持人类/Bot 的允许名单和拒绝名单（拒绝名单优先）、按真实 ID 匹配并可限定线程和过期时间的 Bot 条目、只能收紧的按频道覆盖 |
| 可靠投递 | 每条消息先写入 SQLite；去重持久化；操作状态机；webhook 超时记为 `unknown-result`，绝不盲目重发；重启后正在处理的操作标记为待核对，不会重放；重连后补读跟随的线程 |
| 触发与防循环 | `mention` / `thread_follow` / `all`；按线程任务限制 Bot 轮数和冷却时间；`stop` / `停` 立即停止，所有者说 `new` 才重置 |
| 安全发送 | `@channel`/`@here` 不会真的提醒所有人，无关的 @ 会被转成纯文本；有界、限速的发件队列；固定的错误提示 |
| 监控 | 进程健康和任务状态分开显示；重启、webhook 连续失败、长时间断线可报告到指定频道 |
| 密钥只在环境变量 | 不落盘、不进日志，启动后从进程环境中移除；配置文件拒绝写入类似 Token 的值 |

## 安装

需要 Python 3.10+、bash 和 Linux。

```bash
git clone https://github.com/xukp20/grok-slack-bridge.git
cd grok-slack-bridge
skills/slack-bridge/scripts/install.sh /workspace/slack-bot
```

1. 在 [api.slack.com/apps](https://api.slack.com/apps) 选择 **Create New App → From a manifest**，
   粘贴 [`slack-app-manifest.yaml`](skills/slack-bridge/manifest/slack-app-manifest.yaml)；
   生成带 `connections:write` 的 App-Level Token，并把应用安装到工作区。
   详细步骤见 [setup-slack-app.md](skills/slack-bridge/references/setup-slack-app.md)。
2. 通过环境变量提供密钥（不要写进文件）：

   | 变量 | 值 |
   | --- | --- |
   | `SLACK_BOT_TOKEN` | Bot User OAuth Token（`xoxb-…`） |
   | `SLACK_APP_TOKEN` | App-Level Token（`xapp-…`，需 `connections:write`） |
   | `GROK_WEBHOOK_URL` | Agent webhook 例行任务的地址（https） |
   | `GROK_WEBHOOK_AUTH` | 该 webhook 完整的 `Authorization` 头的值，例如 `Bearer …` |

3. 检查、启动并设置所有者：

   ```bash
   /workspace/slack-bot/scripts/doctor.sh
   /workspace/slack-bot/scripts/start.sh
   /workspace/slack-bot/scripts/set-owner.sh U0123456789   # 你的 Slack 成员 ID
   ```

4. 在 Agent 里建一个 webhook 触发的例行任务，完整提示词见
   [skills/slack-bridge/references/inbox-routine-prompt.md](skills/slack-bridge/references/inbox-routine-prompt.md)。
   大意是：请求体是 slack-bridge 转发的 Slack 消息，转发程序已经替你确认收到（👀）。
   如果 `routing.target` 是 `main`（私信和没有专属 Bot 的频道），例行任务**不在 Slack 里
   发任何消息**，只用 WakeParent 把事件信息和原样的 `reply.command` 交给主 Grok Bot 对话
   一次，然后结束；由主对话用 `reply.sh --op` 回复。如果是 `dedicated`，就在这次运行里
   直接用 `reply.command` 回复。注意 `is_owner` 和 `permissions`，Bot 发来的消息一律按
   不可信处理；reply.sh 返回退出码 3 表示用户已经停止了这个任务，不要再回复。

   **"收到"由转发程序在代码里完成。** 消息一被接受转发，转发程序就给它加上 👀
   （默认 `ack.mode: reaction`）。如果加表情失败（比如应用重新安装前令牌还没有
   `reactions:write`），会记日志，并在私信和 Agent 线程里改为显示"正在处理…"状态。
   这一步不会拖慢或阻塞转发。最终的 `reply.sh --op …`（或 `--no-reply`）会把它去掉。
   可以在 `config.json` 里用 `ack: {"mode": "reaction" | "status" | "none", "emoji": "eyes",
   "status_text": "正在处理…"}` 调整。所以用户只会看到 👀 和一条正式回复，不会再有临时的
   "收到"消息。

### 会话模型

每条 Slack 消息都会启动一次独立的例行任务运行，运行之间不共享记忆。所以默认情况下，
例行任务只做静默分发（👀 已经由转发程序加上）：私信和没有专属 Bot 的频道都交给**主** Grok Bot 对话处理，所有
Slack 工作共用一份有序的上下文。某个频道也可以路由到一个**专属** Grok Bot Agent
（有自己的 webhook）；配置里只写环境变量的**名字**，不写值：

```json
"session_routing": {"default": "main", "channels": {
  "C0RELEASE": {"target": "dedicated", "label": "发布频道 Bot",
                "webhook_url_env": "GROK_WEBHOOK_URL_RELEASE",
                "webhook_auth_env": "GROK_WEBHOOK_AUTH_RELEASE"}}},
"busy_policy": "interrupt_merge"
```

`busy_policy` 决定处理中的对话收到新消息时怎么办：`interrupt_merge`（默认，在安全点
暂停，把同一线程的追加消息合并进当前任务，只回复一次）或 `queue`（先做完当前任务，
再按顺序处理）。每个 payload 都带 `routing = {target, busy_policy, source, label, webhook}`。
未配置的频道、以及环境变量缺失的专属路由，都走默认 webhook。添加专属 Bot 需要先导出
两个环境变量再 `restart.sh`。详见 [docs/session-model.md](docs/session-model.md)（英文）。

### 给某个频道配专属 Bot

[docs/dedicated-channel-bot.md](docs/dedicated-channel-bot.md)（英文）按步骤记录了怎样给一个频道
单独配一个 Agent（例如机器人互聊的 `#my-bots`）：用
[人设模板](skills/slack-bridge/references/dedicated-agent-persona.md)建 Agent，写一份
[频道记忆文件](skills/slack-bridge/references/channel-memory-template.md)，用
[专属例行任务提示词](skills/slack-bridge/references/dedicated-routine-prompt.md)建 webhook 例行任务；
所有者从例行任务面板复制 webhook 地址和 Authorization，通过加密输入存成两个密钥（不要贴到聊天里），然后：

```bash
scripts/add-channel-route.sh --channel C0123456789 --label "发布频道 Bot" \
    --url-env GROK_WEBHOOK_URL_RELEASE --auth-env GROK_WEBHOOK_AUTH_RELEASE   # 先备份配置，不会自动重启
scripts/restart.sh && scripts/slackctl.sh routing --channel C0123456789
scripts/add-channel-route.sh --remove --channel C0123456789                    # 回滚，立即生效
```

脚本只接受环境变量的**名字**，像 URL、Token、`Bearer …` 的值会被拒绝且不回显；只显示变量
"已设置/缺失"。文档还说明了路由和访问控制、触发方式、`busy_policy` 的关系，以及移除/回滚清单。

### 顶层对话只做规划和分派，不要阻塞

Slack 消息转给主对话时，要等主对话**当前这一轮结束**才会送达；一轮里的等待（比如 `sleep 60`）
不会被新消息打断。所以主对话和每个频道的专属 Agent 都应该：简单问题直接答，耗时的工作交给后台任务，
每轮尽量短，不要 sleep 或轮询，发最终回复前先检查同一线程里有没有更新的未处理消息，再按 `busy_policy`
合并。详见 [docs/conversation-guidelines.md](docs/conversation-guidelines.md)（英文），可直接粘贴的规则见
[dispatcher-guideline.md](skills/slack-bridge/references/dispatcher-guideline.md)（英文）。

## 访问控制、触发方式与可靠性

默认配置很保守：只服务所有者，只响应私信和 @提及，不接受任何 Bot 的消息。

```bash
S=/workspace/slack-bot/scripts/slackctl.sh
$S config set human_access allowlist          # owner_only | allowlist | everyone
$S config set user_allowlist U0ALICE,U0BOB
$S config set user_denylist U0SPAM            # 拒绝名单永远优先
$S config set trigger thread_follow           # 被 @ 过的线程里后续消息不用再 @
$S access check --user U0ALICE --channel C0123 --entry mention   # 解释某个人会不会被处理
$S access validate
```

Bot 默认被拒绝。要试点某个 Bot，在 `bot_allowlist` 里写它真实的 user/bot/app ID，
可以限定频道或线程、设置过期时间和最多轮数：

```json
"bot_access": "allowlist",
"bot_allowlist": [{"label": "dot 试点", "user_id": "U0…", "bot_id": "B0…", "app_id": "A0…",
                   "threads": ["C0…:1791460290.248329"], "expires_at": "2026-10-09T09:00:00+08:00",
                   "max_turns": 3}]
```

不写 `channels` / `threads` / `expires_at` / `max_turns` 即表示任何频道/线程、永不过期。
防循环：`max_bot_turns`（默认 4）只限制同一线程里**连续**的 Bot 消息数；线程里只要有人类发言就清零，
所以 Bot 偶尔 @ Grok Bot 不受影响，只拦截没有人类插话的 Bot 互相来回。

在 Slack 里，被允许的人可以在线程里说 `stop` / `停` / `停止`；所有者还可以说
`new` / `新任务`（开始新任务并重置 Bot 轮数）、`resume`（恢复已停止/暂停的线程；
在正常进行中的线程里"继续"只是普通消息）、`status` / `状态`；`help` / `帮助` 显示说明。

每条消息在处理前先写入 `run/bridge.sqlite`。webhook 超时记为 `unknown-result`，
不会自动重发；重启后原本在处理中的操作变成 `needs-reconciliation`，需要用
`slackctl.sh ops list|show|resolve|retry` 处理。`scripts/status.sh` 分开显示进程健康和
任务状态；设置 `report_channel` 后，重启和连续失败会报告到 Slack。

所有配置项：[configuration.md](skills/slack-bridge/references/configuration.md)（英文）。

### 配置常见坑

完整列表和示例 `config.json` 见 [config-pitfalls.md](skills/slack-bridge/references/config-pitfalls.md)（英文），要点：

- **改了 scope 或事件订阅必须重新安装应用**（Install App → Reinstall），否则 Token 还是旧权限，接口报 `missing_scope`。
- **Agent 入口不出现**：需要订阅 `app_home_opened` 事件并开启 Messages tab。
- **频道里 @ 没反应 / `not_in_channel`**：先 `/invite @Grok Bot`，私有频道也一样；跟随线程和补读也只覆盖 Bot 所在的频道。
- **下载文件得到一个 HTML 页面**：缺少 `files:read`，Slack 返回的是登录页（HTTP 200）。
- **按钮没反应**：manifest 里 `interactivity.is_enabled` 需要是 `true`（Socket Mode 不需要 URL）。
- **一半消息丢失**：同一个应用开了两个 Socket Mode 连接，Slack 会把事件分给它们。
- **YAML 写法**：`#` 开头的颜色值要加引号（`"#111827"`），`[` 开头的值要加引号（`"[question or task]"`），值里有 `: ` 也要加引号，只用空格缩进，布尔值写 `true`/`false`。带逐行注释的推荐写法见 [slack-app-manifest.annotated.yaml](skills/slack-bridge/manifest/slack-app-manifest.annotated.yaml)。
- **config.json**：JSON 不能写注释，可以加一个 `"_note"` 键；Token 永远不要写进去；`bot_allowlist` 只认真实 ID（U/B/A），不认名字；`expires_at` 要带时区；按频道覆盖只能收紧；旧配置里的 `access` 以及缺少的 `session_routing` / `busy_policy` 用 `slackctl.sh migrate-config` 补齐；`webhook_url_env` / `webhook_auth_env` 只能写环境变量名，写 URL 或 Token 会被拒绝。

## 运维：重启与恢复

电脑或云端机器重启、转发程序崩溃后怎么办，如何用 `status.sh` / `doctor.sh` / 日志检查，
如何用 `start.sh` / `restart.sh` 重启（需要 shell 里有 4 个环境变量；在 Grok Bot 的云端
电脑上，密钥会自动注入新进程），常见故障（Token 轮换后 `invalid_auth`、两个转发程序分走消息、
例行任务密钥更换后 webhook 401、Socket 反复断线）及解决办法，以及可定时运行的自愈脚本
`ensure-running.sh`（附 Grok Bot 例行任务提示词和 cron 写法），以及云端电脑空闲时被冻结、
恢复后消息延迟送达的情况，见 [docs/operations.md](docs/operations.md)（英文）。

健康检查例行任务的提示词见
[references/health-check-routine-prompt.md](skills/slack-bridge/references/health-check-routine-prompt.md)。
推荐默认频率：白天每小时一次（例如北京时间 8 点到 24 点：`CRON_TZ=Asia/Shanghai 4 8-23 * * *`），
因为每次运行都会消耗额度，每半小时一次大约翻倍且多是误报。电脑刚从冻结中恢复时心跳看起来过期，
检查可能会重启转发程序；重启后正常属于已知误报，不通知主人，只有重启后仍不正常、脚本失败或被拦截、
或出现需要处理的新问题时才转交主会话。所有路由（主会话和各频道专属 Agent）共用同一个转发程序，
一个健康检查就够了。

## 切换 Agent、账号或 Token

Slack 应用和 Token 保持不变，只重新提供发生变化的部分：

| 变化 | 需要重新提供 | 然后执行 |
| --- | --- | --- |
| 换一个 Agent | `GROK_WEBHOOK_URL`、`GROK_WEBHOOK_AUTH` | `reconfigure.sh --ping-webhook` |
| 换账号或换机器 | 全部四个（Slack Token 不变） | `install.sh`、`reconfigure.sh`，并停止旧的转发程序 |
| 轮换 Token | `SLACK_BOT_TOKEN` 和/或 `SLACK_APP_TOKEN` | `reconfigure.sh` |

`reconfigure.sh` 会先运行 `doctor` 检查，全部通过才重启转发程序。同一个 Slack 应用
只能运行一个转发程序，否则 Socket Mode 会把消息分散到多个连接上。
详见 [reconnect.md](skills/slack-bridge/references/reconnect.md)。

## 其他接入方式

[docs/slack-connection-options.md](docs/slack-connection-options.md)（英文）记录了我们比较过的方案：

- **Agent 自带的 Slack MCP 连接器**：读取、搜索 Slack，以你的身份起草/发送消息；不能接收聊天。适合整理和跟进 Slack。
- **Composio 的 Slack 工具包**：需要单独的 OAuth 授权，只能主动调用，不会唤醒 Agent。适合补充原生连接器没有的操作。
- **Cursor Slack 应用 + 频道监听例行任务**（例如 `#ask-bot`，先把 Slack 连到 Cursor 账号并 `/invite @Cursor`）：能接收消息，但回复显示为 Cursor 应用，不支持私信。适合快速搭一个频道助手。

选择独立 Socket Mode 应用，是因为它有自己的 Bot 身份、支持私信、实时送达、无需公网地址，
并且换 Agent 或账号时只需重新指向 webhook。

**推荐组合：** Grok Bot（本仓库）负责在 Slack 里收发聊天；Grok 的 Slack 连接器（以你身份授权的 "Grok" 应用）建议保留，它让 Agent 能搜索、阅读你看得到的频道和历史消息，并在你确认后以你的名义起草或发送消息，Bot 本身只能看到发给它的消息。Cursor Slack 应用只用于旧的频道监听，现在可以卸载。

[docs/agent-view.md](docs/agent-view.md)（英文）介绍 Slack 的 Agent 功能，转发程序已支持、manifest 已默认开启：在任意频道旁打开的分屏面板、每次对话独立成一个线程、处理时显示"Working…"并带停止按钮、自动设置对话标题、推荐提问，以及告诉 Agent 你正在看哪个频道。如果 Slack 应用还没开启 Agent 功能，转发程序会自动退回普通私信的方式（两种情况下都会加 👀）。

## 发布

使用 GitHub CLI 设备登录（`gh auth login --web`），步骤见 [docs/publishing.md](docs/publishing.md)。

## 许可证

[MIT](LICENSE)
