# WeChat GPT Voice Bridge

Linux service that connects a real WeChat Desktop call to the same-host Codex Voice Gateway. It forwards in-memory PCM audio between PulseAudio and the private Bridge WebSocket, restores the original WeChat audio route on exit, and does not replace the OpenClaw text channel.

## Runtime contract

- Python 3.11+ with `websockets`.
- Docker container named `agent-wechat`.
- Gateway at `ws://127.0.0.1:4317`.
- Shared random Bridge token in `/etc/wechat-gpt-voice-bridge.env`.
- Writable state directory `/var/lib/wechat-gpt-voice-bridge`.
- Pulse endpoints created by the main repository launch wrapper.

Install `bridge.py` and `pulse_audio.py` together under
`/opt/wechat-gpt-voice-bridge`, then install the env, refresh helper and
template systemd unit from `deploy/`. The PulseAudio module is shared with
the Xiaozhi backend; the latter never imports this Codex-specific bridge.

The Bridge prewarms Realtime while waiting for WeChat audio, retries changing Pulse stream IDs, sends one bounded opening request, and only then forwards the microphone. Voice selection cycles through the stable desktop V1 voice set.
