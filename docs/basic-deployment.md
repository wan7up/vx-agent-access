# 基础文字链路参考

本页不是封装第三方依赖的一键安装器，而是当前实测组合的复现流程。开始前建议先阅读[系统原理与实现路径](how-it-works.md)，并以各上游项目当前版本的官方文档为准。

本指南部署以下链路：

```text
WeChat Desktop -> agent-wechat -> local cache -> OpenClaw WeChat plugin -> OpenClaw agent
```

完成后，vx 私聊和可信群聊可使用 OpenClaw 的人设、记忆、图片、文件和工具能力。Voice 不是本阶段的验收项。

## 1. 前置条件

推荐环境：

- Debian/Ubuntu/Armbian 类 Linux，使用 systemd。
- Docker 与 Docker Compose。
- 能运行 Linux vx 桌面端的 `amd64` 或 `arm64` 主机。
- 至少 4GB 内存；同时运行多个 Agent 或桌面程序时建议更多。
- 已安装并可正常对话的 OpenClaw。
- 仅从本机或可信局域网管理服务器。

先记录版本，便于以后定位上游升级问题：

```bash
uname -a
docker version
openclaw --version
```

## 2. 部署 agent-wechat

优先按照 [agent-wechat 上游项目](https://github.com/thisnick/agent-wechat) 的当前文档部署容器和持久卷。本仓库也提供一份从实机配置脱敏而来的 [Compose 参考](../deploy/basic/compose.yaml)：

```bash
cd deploy/basic
cp .env.example .env
mkdir -p secrets
openssl rand -hex 32 > secrets/token
chmod 600 secrets/token
docker compose up -d
```

默认约定：

- 容器名：`agent-wechat`
- REST API：`http://127.0.0.1:6174`
- token 文件：上述命令创建的 `deploy/basic/secrets/token`
- vx 数据、登录态和 token 位于宿主持久卷，不写进 Git 仓库

安全要求：

- 将 API 端口绑定到 `127.0.0.1`，不要直接发布到公网。
- token 文件权限设为 `0600`。
- 扫码登录后，确认容器重启不会丢失 vx 登录态。

最低验收：

```bash
docker ps --filter name=agent-wechat
curl -H "Authorization: Bearer $(cat secrets/token)" \
  http://127.0.0.1:6174/api/status/auth
```

其中 token 路径应换成自己的实际绝对路径；在 Compose 参考目录中可用 `$(pwd)/secrets/token`。后续运行 `configure-env.sh` 时也填写同一个绝对路径。

不同上游版本的认证状态路径可能为 `/api/auth/status`；本仓库 `doctor.sh` 会依次尝试两者。

## 3. 安装 OpenClaw vx 插件

在 OpenClaw 所在环境安装与当前 OpenClaw 版本兼容的 `@agent-wechat/wechat` 插件。常见安装入口为：

```bash
openclaw plugins install @agent-wechat/wechat
```

若上游或当前 OpenClaw 版本给出不同命令，以对应版本文档为准。安装后先不要开放所有联系人或群聊。

## 4. 配置本仓库

```bash
git clone https://github.com/wan7up/vx-agent-access.git
cd vx-agent-access
sudo scripts/configure-env.sh
sudo scripts/install-services.sh
```

交互配置会写入 `/etc/openclaw-wechat-channel.env`，权限为 `0600`。主要字段：

- `AGENT_WECHAT_UPSTREAM_URL`：真实 agent-wechat API，默认 `6174`。
- `AGENT_WECHAT_CACHE_URL`：供 OpenClaw 使用的本机缓存代理，默认 `6175`。
- `AGENT_WECHAT_ALLOWED_CHAT_IDS`：允许缓存代理看到的私聊与群聊 ID。
- `CHAT_IDS`：允许辅助服务观察的群聊 ID。
- `BOT_NAMES` / `MENTION_TERMS`：机器人的 vx 显示名。

## 5. 配置 OpenClaw channel

参考 [配置样例](../config/openclaw-wechat-channel.example.json)，将 `channels.wechat` 合并到自己的 OpenClaw 配置。重点：

- `serverUrl` 使用缓存代理 `http://127.0.0.1:6175`。
- token 从私有文件或 secret store 读取，不提交明文。
- `dmPolicy` 和 `groupPolicy` 使用 allowlist。
- 未明确配置的群默认禁用。
- 群聊默认 `requireMention: true`。

本仓库不自动覆盖 OpenClaw 主配置，避免破坏已有模型、渠道、人设和 MCP 设置。

## 6. 启用基础服务

先启用必要服务：

```bash
sudo systemctl enable --now agent-wechat-api-cache.service
```

确认文字收发稳定后，再按需启用：

```bash
sudo systemctl enable --now agent-wechat-proactive-observer.service
```

`proactive-observer` 会在群内主动发言，默认不建议在初次部署时启用。
旧入口 watchdog、mention/cron 修复和注入 wrapper 已从维护目录移除，
不要在单用户重建桌面上启用。桌面依赖由入口脚本一次性准备，不靠周期补丁注入。

## 7. 验收

```bash
sudo scripts/doctor.sh
systemctl --failed
```

依次实测：

1. allowlist 私聊提问并收到文字回复。
2. allowlist 群内不 mention 时保持安静。
3. mention 机器人后收到一次且仅一次回复。
4. 图片接收、图片理解和图片发送。
5. 重启 `agent-wechat` 后登录态和文字回复恢复。
6. 重启主机后所有必要服务恢复。

## 8. 回退

辅助服务可以独立停用：

```bash
sudo systemctl disable --now \
  agent-wechat-proactive-observer.service
```

如需绕过缓存代理，将 OpenClaw `serverUrl` 临时改回 `http://127.0.0.1:6174`，再停止 `agent-wechat-api-cache.service`。不要删除 vx 持久卷来解决普通服务故障。
