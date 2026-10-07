#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${ENV_FILE:-/etc/openclaw-wechat-channel.env}"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a && . "$ENV_FILE" && set +a
fi

AGENT_WECHAT_URL="${AGENT_WECHAT_UPSTREAM_URL:-${AGENT_WECHAT_URL:-http://127.0.0.1:6174}}"
AGENT_WECHAT_CACHE_URL="${AGENT_WECHAT_CACHE_URL:-http://127.0.0.1:6175}"
AGENT_WECHAT_TOKEN_FILE="${AGENT_WECHAT_TOKEN_FILE:-/opt/docker/agent-wechat/secrets/token}"

ok() { printf 'ok: %s\n' "$*"; }
warn() { printf 'warn: %s\n' "$*" >&2; }
fail() { printf 'fail: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || fail "python3 not found"
command -v systemctl >/dev/null || warn "systemctl not found"
command -v openclaw >/dev/null || warn "openclaw not found in PATH"

[ -r "$AGENT_WECHAT_TOKEN_FILE" ] || fail "token file not readable: $AGENT_WECHAT_TOKEN_FILE"
python3 - "$AGENT_WECHAT_URL" "$AGENT_WECHAT_TOKEN_FILE" <<'PY'
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

base = sys.argv[1].rstrip("/")
token = Path(sys.argv[2]).read_text().strip()

def get(path):
    req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))

auth_ok = False
for path in ["/api/status/auth", "/api/auth/status"]:
    try:
        data = get(path)
    except urllib.error.HTTPError:
        continue
    if not isinstance(data, dict) or data.get("status") != "logged_in":
        raise SystemExit("fail: desktop account is not logged in")
    print("ok: desktop account logged in")
    auth_ok = True
    break
if not auth_ok:
    raise SystemExit("fail: auth status endpoint failed")

data = get("/api/chats?limit=5")
if not isinstance(data, list) or not data:
    raise SystemExit("fail: chat list is empty or invalid; check agent session and database keys")
print("ok: chat list is non-empty")
PY

python3 - "$AGENT_WECHAT_CACHE_URL" <<'PY'
import json
import sys
import urllib.error
import urllib.request

base = sys.argv[1].rstrip("/")
try:
    with urllib.request.urlopen(base + "/healthz", timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    print("ok: agent-wechat api cache health ->", data.get("stats", {}))
except (OSError, urllib.error.URLError) as exc:
    raise SystemExit("fail: agent-wechat api cache health failed: " + repr(exc))
PY

if command -v openclaw >/dev/null; then
  openclaw health || warn "openclaw health returned non-zero"
fi

if command -v systemctl >/dev/null; then
  systemctl is-active --quiet agent-wechat-api-cache.service ||
    fail "agent-wechat-api-cache.service not active"
  ok "agent-wechat-api-cache.service active"
  systemctl --user is-active --quiet openclaw-gateway.service ||
    fail "openclaw-gateway.service not active"
  ok "openclaw-gateway.service active"
fi

ok "read-only checks complete; real incoming-message reply and voice still need separate validation"
