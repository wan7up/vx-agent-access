#!/bin/sh
set -eu

CODEX_HOME="${VOICE_GATEWAY_CODEX_HOME:-/var/lib/codex-voice-gateway/codex-home}"
CODEX_BIN="${VOICE_GATEWAY_CODEX_BIN:-/opt/codex-voice/bin/codex}"
mkdir -p "$CODEX_HOME"
chmod 700 "$CODEX_HOME"

if [ ! -f "$CODEX_HOME/config.toml" ]; then
  cat >&2 <<'EOF'
Missing Voice Gateway config.toml. Start from the project setup before logging in.
EOF
  exit 1
fi

if [ ! -x "$CODEX_BIN" ]; then
  CODEX_BIN="$(command -v codex || true)"
fi

if [ -z "$CODEX_BIN" ] || [ ! -x "$CODEX_BIN" ]; then
  cat >&2 <<'EOF'
Codex CLI was not found. Install a compatible CLI or set VOICE_GATEWAY_CODEX_BIN to its absolute path.
EOF
  exit 1
fi

# This opens a device-auth flow. Complete it with the official ChatGPT/Codex
# account that exposes Voice, not an external API provider account.
exec env -u OPENAI_API_KEY -u OPENAI_BASE_URL -u OPENAI_API_BASE -u CODEX_ACCESS_TOKEN \
  CODEX_HOME="$CODEX_HOME" "$CODEX_BIN" login --device-auth
