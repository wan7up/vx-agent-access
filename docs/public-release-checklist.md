# GitHub 公开发布检查表

这份清单用于公开源码和流程参考，不代表干净主机部署已全部验证。
项目不宣传为一键部署。每次发布都应检查将要上传的文件和全部公开 refs，
不能只扫描工作目录。

## P0：隐私与凭据

- [ ] 全仓扫描并移除真实群 ID、群名、成员昵称、bot 名、局域网 IP、thread/session ID 和本机绝对路径。
- [ ] 确认 Git 历史中没有 token、API key、Codex 登录态、vx 登录态、SQLite、聊天记录、截图、音频或 SDP。
- [ ] 将生产运行记录移到不跟踪的私有目录；公开文档只保留脱敏兼容性结论。
- [ ] 给 secret、env、数据库、日志、备份和媒体文件补齐 `.gitignore`。
- [ ] 不公开含私人内容的旧 Git 历史；确认公开仓库的全部 refs 和资产均已审计。
- [ ] 旧历史先留私有本机备份，再删除旧云端仓库。若凭据曾泄漏，轮换相关凭据。

## P0：代码通用化

- [x] 将 `wechat-group-call.py` 的 `CHATROOMS`、成员 alias 和 bot mention 正则移到私有配置文件。
- [x] 将 incoming-call observer 的默认 conversation key 移到必填环境变量。
- [x] 清理 `probe-call-state.sh` 中生产成员名和群名。
- [ ] 若需发布自动安装器，先解决 DM-only 配置和上游插件版本/patch 的可重复性。
- [x] 为基础版和 Voice 版分别提供安装 profile，基础版不得安装或启用 Voice 服务。

## Voice 参考

- [x] 发布或合并 `codex-voice-gateway` 源码，提供 Linux systemd unit 和 env 样例。
- [x] 发布或合并 `wechat-gpt-voice-bridge` 源码，固定与 Gateway 的消息协议版本。
- [x] 当前小智设备身份与群/成员映射不放入源码。
- [ ] 新设备先验证 PulseAudio、设备绑定和手动双向通话，再启用自动接听。
- [ ] 只有使用归档 Codex 后端时，才检查独立 `CODEX_HOME`、CLI 版本、
      schema hash、feature 状态与 Voice 列表；不能继承其他 Codex 登录环境。

## P1：复现体验

- [ ] 提供经过验证的 agent-wechat Compose 样例或明确固定上游版本。
- [ ] `configure-env.sh` 能生成辅助服务配置和可合并的 OpenClaw channel patch；它不是第三方组件安装器。
- [ ] `doctor.sh` 区分 Basic/Voice profile，并检查架构、端口、权限、容器、登录态和服务依赖。
- [ ] 提供卸载脚本；默认保留 vx 数据、OpenClaw 数据和 Codex 登录态。
- [ ] 提供升级前备份与非破坏性回退命令。

## P1：测试与 CI

- [ ] Python 单元测试覆盖群配置、锁、Bridge 生命周期、自动接听和音色循环。
- [ ] Gateway TypeScript 测试覆盖 bridge 鉴权、音色白名单、session 互斥和异常消息。
- [ ] ShellCheck、Python compile、TypeScript build 和 `git diff --check` 进入 CI。
- [ ] 在干净的 Debian/Ubuntu/Armbian 主机按文档逐层完成一次基础文字链路复现。
- [ ] Voice 分别在 `amd64` 和 `arm64` 至少验证一个支持矩阵条目。

## P1：项目治理

- [x] 选择并添加 LICENSE；本仓库采用 MIT，第三方项目仍遵循各自许可证与服务条款。
- [x] 添加 SECURITY.md，说明凭据泄漏、公开端口和账号风险的报告方式。
- [ ] 添加 CONTRIBUTING.md、问题模板和支持矩阵。
- [x] README 明确非 vx/OpenAI/OpenClaw 官方项目，不承诺账号安全或上游兼容性。

## 发布前扫描

```bash
rg -n '192\.168\.|10\.[0-9]+\.|@chatroom|thread[_-]?id|session[_-]?id|Bearer|sk-|/Users/|/home/' . \
  -g '!node_modules/**' -g '!.git/**'
git status --short
git diff --check
python3 -m unittest discover -s tests -v
(cd components/xiaozhi-vx-bridge && python3 -m unittest discover -s tests -v)
(cd components/wechat-gpt-voice-bridge && python3 -m unittest discover -s tests -v)
```

扫描结果需要逐项判断；样例占位符可以保留，真实标识必须移除。
测试在组件目录运行。以上扫描不是完整密钥检测器；还应检查二进制、符号链接、
Git author email、备份文件和忽略规则。
