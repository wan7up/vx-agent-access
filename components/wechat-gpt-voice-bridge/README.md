# Shared Call Audio

This directory retains the `pulse_audio.py` module used by the live Xiaozhi
bridge and the inactive Codex bridge. The historical directory name is kept
for installation compatibility; it does not start Codex.

The module provides call-stream discovery, virtual audio routing, route
restoration, capture recovery and owner-scoped container audio cleanup.
Its implementation and production paths were not changed during the local
project consolidation.

Codex-specific code is in
[`archive/codex-voice/components/wechat-gpt-voice-bridge`](../../archive/codex-voice/components/wechat-gpt-voice-bridge).
That archive uses a relative link to this module, not a second implementation.

Run the current cleanup tests here:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```
