# Codex Voice Gateway

Private TypeScript gateway for Codex App Server Realtime. It provides a browser WebRTC surface and a token-authenticated same-host WeChat Bridge endpoint. Codex App Server is spawned only over `stdio` and is never exposed as a network service.

## Linux build

```bash
npm ci
npm test
npm run build
```

Copy `.env.example` to `/etc/codex-voice-gateway.env`, create a dedicated `CODEX_HOME`, install its `config.toml.example`, and log in with the official account used for Voice. Never reuse a Codex++ or custom-provider home.

The Linux systemd example expects:

- application: `/opt/codex-voice/gateway`
- Codex CLI: `/opt/codex-voice/bin/codex`
- service user/home: `codex-voice` / `/var/lib/codex-voice-gateway`
- env: `/etc/codex-voice-gateway.env`

## Security

- Bind to `127.0.0.1` unless a separately reviewed private proxy is required.
- Generate an independent 32-byte `VOICE_GATEWAY_BRIDGE_TOKEN` and configure the same value in the Bridge env.
- Keep `CODEX_HOME`, SQLite and env files private.
- The Codex child receives a deliberately small environment and does not inherit API keys or custom Base URLs.
- Audio, SDP, tickets and raw tool output are not persisted.

The App Server Realtime API is experimental. Run `npm run doctor`, tests and a real audio regression after every Codex CLI upgrade.
