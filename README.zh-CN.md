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

4. 在 Agent 里建一个 webhook 触发的例行任务，提示词大意是：请求体是 slack-bridge
   转发的 Slack 消息，按 `/workspace/slack-bot/README.md` 处理，注意 `is_owner`，
   然后运行 payload 里的 `reply.command` 回复。

## 运维：重启与恢复

电脑或云端机器重启、转发程序崩溃后怎么办，如何用 `status.sh` / `doctor.sh` / 日志检查，
如何用 `start.sh` / `restart.sh` 重启（需要 shell 里有 4 个环境变量；在 Grok Bot 的云端
电脑上，密钥会自动注入新进程），常见故障（Token 轮换后 `invalid_auth`、两个转发程序分走消息、
例行任务密钥更换后 webhook 401、Socket 反复断线）及解决办法，以及可定时运行的自愈脚本
`ensure-running.sh`（附 Grok Bot 例行任务提示词和 cron 写法），见
[docs/operations.md](docs/operations.md)（英文）。

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

[docs/assistant-view.md](docs/assistant-view.md)（英文）介绍 Slack 的 "Agents & AI Apps" assistant 视图：在任意频道旁打开的侧边对话面板、每次对话独立成线程、推荐提问、"正在思考" 状态，以及开启它需要改的 manifest 和转发程序。

## 发布

使用 GitHub CLI 设备登录（`gh auth login --web`），步骤见 [docs/publishing.md](docs/publishing.md)。

## 许可证

[MIT](LICENSE)
