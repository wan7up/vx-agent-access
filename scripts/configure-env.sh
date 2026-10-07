#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root on the OpenClaw Linux host." >&2
  exit 1
fi

ENV_FILE="${ENV_FILE:-/etc/openclaw-wechat-channel.env}"

prompt() {
  local label="$1"
  local default="$2"
  local value
  if [ -n "$default" ]; then
    read -r -p "$label [$default]: " value
    printf '%s' "${value:-$default}"
  else
    read -r -p "$label: " value
    printf '%s' "$value"
  fi
}

AGENT_WECHAT_UPSTREAM_URL="$(prompt 'agent-wechat upstream URL' 'http://127.0.0.1:6174')"
AGENT_WECHAT_CACHE_URL="$(prompt 'agent-wechat cache URL for OpenClaw/helpers' 'http://127.0.0.1:6175')"
AGENT_WECHAT_TOKEN_FILE="$(prompt 'agent-wechat token file' '/opt/docker/agent-wechat/secrets/token')"
CHAT_IDS="$(prompt 'enabled group chat IDs, comma-separated' '')"
DM_ALLOWLIST="$(prompt 'DM allowlist, comma-separated (for OpenClaw config reference)' '')"
BOT_NAME="$(prompt 'bot display name' '')"

if [ -z "$CHAT_IDS" ]; then
  echo "At least one group chat ID is required." >&2
  exit 1
fi

if [ -z "$BOT_NAME" ]; then
  echo "Bot display name is required." >&2
  exit 1
fi

tmp="$(mktemp)"
cat >"$tmp" <<EOF
AGENT_WECHAT_UPSTREAM_URL=$AGENT_WECHAT_UPSTREAM_URL
AGENT_WECHAT_CACHE_URL=$AGENT_WECHAT_CACHE_URL
AGENT_WECHAT_URL=$AGENT_WECHAT_CACHE_URL
AGENT_WECHAT_TOKEN_FILE=$AGENT_WECHAT_TOKEN_FILE
AGENT_WECHAT_CACHE_BIND=127.0.0.1
AGENT_WECHAT_CACHE_PORT=6175
AUTH_CACHE_TTL_SECONDS=30
CHATS_CACHE_TTL_SECONDS=20
MESSAGES_CACHE_TTL_SECONDS=12
CHATS_PREFETCH_LIMIT=50
MESSAGES_PREFETCH_LIMIT=50
AGENT_WECHAT_ALLOWED_CHAT_IDS=$DM_ALLOWLIST,$CHAT_IDS
CHAT_IDS=$CHAT_IDS
DM_ALLOWLIST=$DM_ALLOWLIST
MENTION_TERMS=@$BOT_NAME
BOT_NAMES=$BOT_NAME
AT_TERMS=@$BOT_NAME
OPENCLAW_TRACE_DIR=/root/.openclaw/agents/main/sessions
OPENCLAW_SEEN_SUPPRESS=1
OPENCLAW_TRACE_LOOKBACK_SECONDS=7200
IDLE_TRIGGER_SECONDS=259200
PROACTIVE_COOLDOWN_SECONDS=21600
IDLE_PROACTIVE_COOLDOWN_SECONDS=86400
MAX_PROACTIVE_PER_DAY=3
EOF

install -m 600 "$tmp" "$ENV_FILE"
rm -f "$tmp"

echo "Wrote $ENV_FILE"
echo "Next: update OpenClaw channels.wechat with the same DM/group allowlists, then run scripts/doctor.sh."
