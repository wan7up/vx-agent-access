#!/usr/bin/env bash
set -euo pipefail

GATEWAY_SERVICE="${CODEX_VOICE_GATEWAY_SERVICE:-codex-voice-gateway.service}"
HEALTH_URL="${CODEX_VOICE_GATEWAY_HEALTH_URL:-http://127.0.0.1:4317/healthz}"

# The ARM64 node-webrtc runtime can retain unusable native state after a
# completed call. Give every call a fresh Gateway process until that upstream
# lifecycle is proven reusable on this host.
systemctl restart "$GATEWAY_SERVICE"

for _ in $(seq 1 100); do
  if curl --fail --silent --show-error --max-time 1 "$HEALTH_URL" >/dev/null; then
    exit 0
  fi
  sleep 0.1
done

echo "Codex Voice Gateway did not become healthy after restart" >&2
exit 1
