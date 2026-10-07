# Runtime Customizations

This project is intentionally narrow: it uses `agent-wechat` as the VX Desktop/API layer and adds OpenClaw channel behavior around it.

## Keep

- `agent-wechat` for Linux-hosted WeChat Desktop and REST/media APIs.
- Official `@agent-wechat/wechat` OpenClaw plugin as the main channel bridge.
- A local loopback API cache in front of `agent-wechat` so OpenClaw and helper services do not independently trigger the same expensive read scans.
- The proactive trusted-group observer, incoming-call observer, outbound image
  normalization and on-demand session maintenance tools.
- One desktop user and one session bus. The entrypoint prepares accessibility
  and audio once at startup.

## Avoid

- Coordinate-based fallback sends that can target the wrong conversation.
- Shipping upstream `agent-wechat` source inside this repo.
- Committing WeChat chat logs, screenshots, session files, tokens, model keys or generated test media.
- Keeping one-off patch scripts that only make sense for a past debugging session.

## Local API Cache

`agent-wechat-api-cache.service` listens on `127.0.0.1:6175` and forwards to the real `agent-wechat` server on `127.0.0.1:6174`.

It caches only safe read endpoints:

- `GET /api/status` and `GET /api/status/auth`
- `GET /api/chats`
- `GET /api/messages/:chatId`

For list-style reads with a `limit` query, it prefetches up to a configured larger limit and slices the JSON list back down for callers. This lets `limit=30` and `limit=40` requests for the same chat share one upstream read. If `AGENT_WECHAT_ALLOWED_CHAT_IDS` is set, `/api/chats` is also filtered to those configured DM/group IDs before OpenClaw sees unread chats. That prevents official accounts such as `newsapp` from hijacking the desktop window and interrupting sends. It bypasses cache for sends, media downloads, login/logout, chat opening and unknown endpoints. `POST /api/messages/send` also invalidates cached chat/message reads. This keeps the stable behavior of the official API while reducing duplicated accessibility scans from OpenClaw and the helper services.

## Retired Desktop Patches

The entry watchdog, accessibility-cache and kill wrappers, root/su audio
launcher, and mention/cron fallback loops were removed from maintained sources.
Private snapshots retain them for reference. The current installer
does not install or enable them. The rebuilt desktop includes one bounded
accessibility probe wrapper at image-build time, not periodic runtime injection.

## WeChat Group Voice Call

WeChat group calls are not exposed by the `agent-wechat` REST API, so the UI helper still initiates a call from the current trusted group. The selected voice Bridge handles live audio rather than OpenClaw TTS; the current backend is Xiaozhi.

- Call helper: `/root/.openclaw/workspace/scripts/wechat-group-call`
- Live bridge: `/opt/xiaozhi-vx-bridge/call_bridge.py` when Xiaozhi is selected, or `/opt/wechat-gpt-voice-bridge/bridge.py` for the Codex fallback.
- The Codex fallback reaches its Gateway on Linux loopback. Xiaozhi connects directly to its device WebSocket endpoint.
- Start flow: `wechat-group-call <name> --expected-chat-id <chatroom_id> --gpt-voice`.
- OpenClaw must wait for the helper result. If the exec tool returns `Command still running`, poll that process until it exits before deciding whether to reply `NO_REPLY` or send a visible failure message.
- The helper prewarms the selected Bridge before clicking `Finish`. Only the Codex fallback reads the exact triggering message for a one-time opening; Xiaozhi receives no opening text or persona from this helper.
- Once the real WeChat audio route appears, the selected Bridge forwards live audio.
  The Xiaozhi device sends the configured `你好` wake text once audio is ready;
  it does not read group history or inject the OpenClaw persona. The Codex
  fallback retains its separate opening behavior.
- The bridge routes only WeChat's real playback and microphone streams through separate PulseAudio endpoints and restores the original routes when the call ends.
- Gateway events explicitly marked recoverable are logged without terminating the call; unmarked or fatal session errors still stop the Bridge.

The outbound helper supports live Voice only. Its old `--say`/`--audio` TTS
path and `wechat-call-audio` helper are not part of the active installation.

OpenClaw workspace rules must use `--gpt-voice` only for explicit requests from a trusted group. The normal WeChat text channel remains independent and unchanged.

Trusted group IDs, display-name aliases, member aliases and the bot mention
come from private deployment configuration, not public source constants.

This is intentionally not part of the normal message-send path. It is a high-disturbance action and should run only from the group where the request was received.

## Current gap

Some historical OpenClaw/plugin compatibility fixes were applied to installed
bundles rather than a versioned source fork. This public repository does not
include those private bundles or claim to reproduce every historical patch.
Test your installed OpenClaw/plugin versions independently, particularly
manifest detection and group image tools.

The next clean step is either:

1. fork `@agent-wechat/wechat` and keep the custom channel behavior in source, or
2. create a deterministic patch pipeline with tests and version checks.

Until then, this repo keeps only the reusable helper services and deployment documentation.
