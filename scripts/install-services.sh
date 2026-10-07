#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root on the OpenClaw Linux host." >&2
  exit 1
fi

PROFILE="basic"
if [ "$#" -eq 2 ] && [ "$1" = "--profile" ]; then
  PROFILE="$2"
elif [ "$#" -ne 0 ]; then
  echo "Usage: $0 [--profile basic|voice]" >&2
  exit 2
fi
if [ "$PROFILE" != "basic" ] && [ "$PROFILE" != "voice" ]; then
  echo "Usage: $0 [--profile basic|voice]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-/etc/openclaw-wechat-channel.env}"
OPENCLAW_WORKSPACE="${OPENCLAW_WORKSPACE:-/root/.openclaw/workspace}"
OPENCLAW_SCRIPT_DIR="${OPENCLAW_SCRIPT_DIR:-$OPENCLAW_WORKSPACE/scripts}"

install -m 755 "$ROOT/services/agent-wechat-proactive-observer.py" /usr/local/bin/agent-wechat-proactive-observer.py
install -m 755 "$ROOT/services/agent-wechat-media-normalize.py" /usr/local/bin/agent-wechat-media-normalize.py
install -m 755 "$ROOT/services/agent-wechat-api-cache.py" /usr/local/bin/agent-wechat-api-cache.py
install -m 755 "$ROOT/services/openclaw-memory-guard.py" /usr/local/bin/openclaw-memory-guard.py

install -d -m 755 /usr/local/share/agent-wechat
install -m 644 "$ROOT/services/agent-wechat-proactive-observer.service" /etc/systemd/system/agent-wechat-proactive-observer.service
install -m 644 "$ROOT/services/agent-wechat-api-cache.service" /etc/systemd/system/agent-wechat-api-cache.service

install -d -m 700 /root/.config/systemd/user
install -m 644 "$ROOT/services/openclaw-memory-guard.service" /root/.config/systemd/user/openclaw-memory-guard.service
install -m 644 "$ROOT/services/openclaw-memory-guard.timer" /root/.config/systemd/user/openclaw-memory-guard.timer

if [ "$PROFILE" = "voice" ]; then
  install -m 755 "$ROOT/services/agent-wechat-incoming-call.py" /usr/local/bin/agent-wechat-incoming-call.py
  install -m 755 "$ROOT/services/agent-wechat-incoming-call-probe.py" /usr/local/share/agent-wechat/incoming-call-probe.py

  install -d -m 755 "$OPENCLAW_SCRIPT_DIR"
  if [ ! -e "$OPENCLAW_SCRIPT_DIR/wechat-group-call" ]; then
    install -m 755 "$ROOT/scripts/wechat-group-call.py" "$OPENCLAW_SCRIPT_DIR/wechat-group-call"
  else
    echo "Preserving existing dialer with private group/member mapping: $OPENCLAW_SCRIPT_DIR/wechat-group-call"
  fi
  install -m 755 "$ROOT/scripts/probe-call-state.sh" "$OPENCLAW_SCRIPT_DIR/probe-call-state"
  install -m 644 "$ROOT/services/agent-wechat-incoming-call.service" /etc/systemd/system/agent-wechat-incoming-call.service
fi

if [ ! -f "$ENV_FILE" ]; then
  install -m 600 "$ROOT/config/openclaw-wechat-channel.env.example" "$ENV_FILE"
  echo "Created $ENV_FILE from example. Edit it before enabling services on a new host."
fi

systemctl daemon-reload
XDG_RUNTIME_DIR=/run/user/0 systemctl --user daemon-reload || true

echo "Installed services. Enable with:"
echo "  systemctl enable --now agent-wechat-api-cache.service"
echo "  systemctl enable --now agent-wechat-proactive-observer.service"
echo "  XDG_RUNTIME_DIR=/run/user/0 systemctl --user enable --now openclaw-memory-guard.timer"
if [ "$PROFILE" = "voice" ]; then
  echo "  systemctl enable --now agent-wechat-incoming-call.service"
  echo
  echo "Installed Voice helper scripts under $OPENCLAW_SCRIPT_DIR."
fi
echo "Retired repair loops and desktop wrappers are not installed."
