# Codex Voice Archive

This is the previously usable Codex CLI App Server / WebRTC voice backend.
The current deployment uses Xiaozhi. Keeping these sources does not enable
Codex, start a second observer, or change an existing deployment.

## Contents

- `components/codex-voice-gateway/`: Gateway source, lockfile, tests and deployment examples.
- `components/wechat-gpt-voice-bridge/`: Codex-specific Bridge, tests and deployment examples.
- `docs/voice-deployment.md`: historical Codex architecture and setup reference.
- Private historical acceptance records and runtime notes are excluded from the
  public repository. They are not prerequisites for studying these sources.

The archived Bridge's `pulse_audio.py` is a link to the single shared module
in `components/wechat-gpt-voice-bridge` at the project root. When installing
the archived backend on another machine, copy the resolved module alongside
`bridge.py`; do not deploy a dangling link.

The backend was tested historically with a Mac browser and a Linux ARM64 host.
These checks do not guarantee that current Codex releases expose the same
experimental protocol. Validate authentication, schema and real audio in an
isolated environment before using it; see the
[current compatibility notes](../../docs/compatibility-and-recovery.md).

## Local Checks

```sh
cd archive/codex-voice/components/wechat-gpt-voice-bridge
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Gateway dependencies are regenerable from `package-lock.json`; they are not
kept as duplicate copies in this archive.

Keeping the archive does not activate or switch the live voice backend.
