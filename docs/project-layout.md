# 项目目录

```text
vx-agent-access/
  README.md                   架构、设备条件与流程入口
  VERSION                     公开源码基线
  components/                 小智 Bridge 与共用音频模块
  services/ scripts/ deploy/   文字、桌面与通话辅助程序
  tests/                      当前运行链路测试
  docs/                       部署说明、兼容性与故障边界
  archive/codex-voice/         历史 Codex Voice 源码和通用说明
  .private/                   本机私有记录与备份，不提交 Git
```

第三方客户端、Agent、镜像、账号和模型分别管理。本仓库不包含上游源码、
安装好的依赖、登录态、聊天数据或恢复用二进制。

运行数据应与源码分离：账号 HOME/data 使用持久卷；设备身份、token 和
Agent 会话放在权限受限的系统目录，不能存进公开配置样例。

私有现场记录及完整历史备份不参与安装。失败方案、退役 helper 和一次性
补丁不放回维护目录。需要回退时使用自己部署的匹配快照，而不是公共示例中的路径。
