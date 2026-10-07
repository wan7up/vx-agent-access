# Xiaozhi vx Bridge

A Xiaozhi virtual device and Linux vx-call audio bridge. Dialing and
incoming-call scripts can select Xiaozhi or the archived Codex backend.
The OpenClaw text channel is independent of the voice backend.

`device.py` persists the virtual device identity, handles official OTA activation
and probes the WebSocket/Opus audio session. `call_bridge.py` imports only the
shared `wechat-gpt-voice-bridge/pulse_audio.py` route module while connecting
directly to Xiaozhi. The OTA, activation and message formats follow the
MIT-licensed `py-xiaozhi` reference client v2.1.2.

## Verification Scope

- One device activated and OTA/WSS hello accepted.
- A 16 kHz PCM16 speech sample produced STT and decodable TTS in `manual`,
  `auto` and `realtime` modes. `auto` and `realtime` needed a two-second
  trailing silence for the synthetic test sample.
- Two consecutive turns worked over the same WSS session in `realtime` mode.
- Real incoming vx calls auto-answered and completed bidirectional audio.
  These checks do not guarantee every client version, outbound flow or long call.
- The device's language and voice selection live in the Xiaozhi account, not
  this bridge.

## Isolated probe

The tested installation uses `/opt/xiaozhi-vx-bridge/.venv` with
`opuslib==3.0.1`, `websockets==15.0.1` and the system `libopus0`. The stable
device identity is `/var/lib/xiaozhi-vx-bridge/device.json` (0600). Do not
delete it or create a new device on every call.

```sh
runuser -u xiaozhi-voice -- \
  /opt/xiaozhi-vx-bridge/.venv/bin/python \
  /opt/xiaozhi-vx-bridge/device.py probe \
  --listen-mode realtime --tail-seconds 2 --turns 2 \
  --pcm-file /path/to/16000-hz-mono-pcm16.pcm
```

The probe prints STT/TTS events to the terminal and decodes the audio in memory;
do not run it with private conversations or under a persistent logging service.
The call bridge logs event types and transcript lengths, not transcript content.

## Audio Process Cleanup

Each service invocation tags its container-side `parec` and `pacat` with a
unique owner. Teardown removes only that owner's processes and drains the
host-side Docker pipes. An `ExecStopPost` hook in the bridge unit performs the
same idempotent cleanup after abnormal exits, including a killed bridge.
This requires the updated `pulse_audio.py` and bridge unit to be installed
together. The opt-in `tests/verify_audio_cleanup.py` checks real container
audio lifecycles without dialing or connecting to Xiaozhi.

## Call bridge staging

Install `xiaozhi-vx-bridge@.service` and the separate
`agent-wechat-incoming-call-xiaozhi.service` after validating a manual call.
Only one backend's incoming observer should be active.
The existing `WECHAT_PULSE_BRIDGE_DIR` points to the shared module's directory;
it is retained for compatibility with the installed unit.

`/opt/xiaozhi-vx-bridge/select_voice_backend.py xiaozhi` selects the new
backend and its incoming observer; `codex` selects the old backend and starts
its original services. It refuses to switch during an active call. Neither
command restarts OpenClaw text. See the
[voice deployment guide](../../docs/voice-deployment.md) for the checklist.

The bridge relays live microphone audio and server speech. After the vx call
audio route and playback are ready, it sends one device wake event so Xiaozhi
speaks first; the wording comes from Xiaozhi, not a hard-coded TTS file or
group message. Realtime listening remains enabled if the wake event does not
produce speech. Incoming calls are answered after an immediate window recheck,
with no four-second delay. Outgoing calls do not read a group message or write
an opening request for Xiaozhi. The original Codex backend keeps its opening
behavior. See [compatibility and recovery](../../docs/compatibility-and-recovery.md)
for timing boundaries and audio cleanup requirements.

Keep your private group/member mapping separate from the generic repository
copy. Back up your own deployment before changing backends; this repository
does not include private identities or production recovery snapshots.
