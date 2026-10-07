# 当前小智语音链路

当前源码基线为 `vx-agent-access-20261007-r5`，已有文字与语音实测；
验证范围与风险见[兼容性与回退](compatibility-and-recovery.md)。文字使用 OpenClaw，语音使用
一台已经绑定的小智虚拟设备；全套生产服务运行在 Linux，Mac 不参与实时链路。
旧 Codex Voice 是停用的回退实现，见
[Codex Voice 归档](../archive/codex-voice/README.md)，不要把它当成当前部署步骤。

## 音频路径

```text
vx 群/私聊来电或文字指令拨号
  -> 通话控制与来电探针
  -> vx 容器内 PulseAudio 通话播放流
  -> 共用 pulse_audio.py 路由与采集
  -> 小智 Bridge：16 kHz PCM 转 Opus
  -> 小智设备 WebSocket
  -> 小智返回 Opus，Bridge 解码并送入虚拟麦克风
  -> vx 通话中的对方
```

`components/xiaozhi-vx-bridge/` 保存当前 Bridge、设备绑定工具、service 样例
和测试。`components/wechat-gpt-voice-bridge/pulse_audio.py` 虽仍沿用旧目录名，
但它是两个后端共用的实际音频模块，不是废弃程序。
当前无需启动 Codex Gateway、App Server 或 WebRTC。

## 当前约定

- 同时只允许一通通话；主动拨号和接听共享 GUI 锁及活跃 Bridge 检查。
- 来电先预热小智，再确认窗口并接听；额外接听等待为零。
- vx 音频流出现后才开始双向转发，不能把小智建链成功当成通话已能出声。
- 音频就绪后只发送一次唤醒文字“你好”，让小智主动回应。
- 不读取群文字作为小智首句，不注入本地人设；角色、语言、声音由小智后台设置。
- 每通电话创建音频连接，但复用同一设备身份；不要反复注册设备。
- 挂断和异常退出按本次 owner 清理 `parec`/`pacat`，并恢复原音频路由。
- 日志只记录状态、帧数、信号统计和时延，不保存语音或转录内容。

## 分步接入

先完成[基础文字部署](basic-deployment.md)，不要同时首次调试两条链路。
本仓库安装辅助脚本并不完整安装小智后端，需要依次完成：

1. 在 Linux 上准备 Python 虚拟环境、系统 `libopus0`、`opuslib` 和
   `websockets`。已用版本与依赖见[组件说明](../components/xiaozhi-vx-bridge/README.md)。
2. 用 `device.py activate` 获取绑定码，在小智后台绑定一次虚拟设备。
   设备身份以私有文件持久化，不能给每通电话创建一个新设备。
3. 用 `device.py probe` 验证独立设备建链、识别及返回音频，
   再检查容器 PulseAudio 的通话播放 sink、注入 sink 和虚拟麦克风。
4. 安装共享 `pulse_audio.py` 与 `call_bridge.py`，配置容器名、
   音频设备、Python 路径及设备身份路径。
5. 按 Bridge 和来电观察器 service 样例修改本机路径、用户与权限，
   确认挂断清理的 `ExecStopPost` 同步安装。
6. 手动完成真实通话和多轮双向音频，再选择 `xiaozhi` 后端并启用来电观察器。
   如需主动拨号，另配置群/成员映射及文字 Agent 调用通话程序的权限。
7. 验证私聊来电、群来电、挂断、连续两通以及服务重启后的恢复。

当前来电不能可靠识别群 ID；所有来电映射到配置的默认渠道，只有在
专用账号和可信联系人范围内才应启用自动接听。文字白名单不等于来电白名单。

小智侧语言、角色和音色在后台配置；本地 Bridge 不继承 OpenClaw
记忆，也不需要 GPT/Codex 登录。云服务的收费、配额和可用性以其当时规则为准。

## 部署资料与验收

1. [小智组件说明](../components/xiaozhi-vx-bridge/README.md)
2. [设备与网络](device-and-network.md)
3. [兼容性、音频清理与回退](compatibility-and-recovery.md)

真实来电的双向对话才是最终验收。服务 `active`、设备在线或
WebSocket 已连接，单独都不能证明 vx 音频已经可用。不得在通话中部署或切换后端。
