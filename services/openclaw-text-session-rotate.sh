#!/usr/bin/env bash
set -euo pipefail

STORE="${OPENCLAW_SESSION_STORE:-/root/.openclaw/agents/main/sessions/sessions.json}"
NODE="${OPENCLAW_NODE:-/opt/openclaw-runtime/bin/node}"
ENTRYPOINT="${OPENCLAW_ENTRYPOINT:-/opt/openclaw/dist/index.js}"

case "${1:-}" in
  "") dry_run=false ;;
  --dry-run) dry_run=true ;;
  *) printf 'Usage: %s [--dry-run]\n' "$0" >&2; exit 2 ;;
esac

keys="$(jq -r 'keys[] | select(. == "agent:main:main" or startswith("agent:main:wechat:"))' "$STORE")"
while IFS= read -r key; do
  [ -n "$key" ] || continue
  if [ "$dry_run" = true ]; then
    jq -cn --arg key "$key" '{key: $key, dryRun: true}'
    continue
  fi
  params="$(jq -cn --arg key "$key" '{key: $key, reason: "new"}')"
  "$NODE" "$ENTRYPOINT" gateway call sessions.reset \
    --params "$params" --timeout 30000 --json |
    jq -c 'if .ok then {ok, key, sessionId: .entry.sessionId} else error("session reset failed") end'
done <<< "$keys"
