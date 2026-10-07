#!/bin/sh
set -eu

OUT="${1:-/tmp/haomao-call-state.log}"
DURATION="${2:-90}"

rm -f "$OUT"
end=$(( $(date +%s) + DURATION ))

while [ "$(date +%s)" -lt "$end" ]; do
  echo "=== TS $(date '+%H:%M:%S')" >> "$OUT"
  docker exec -u wechat agent-wechat sh -lc 'export HOME=/home/wechat XDG_RUNTIME_DIR=/run/user/1000; echo CLIENTS; pactl list short clients; echo SINK_INPUTS; pactl list short sink-inputs; echo SOURCE_OUTPUTS; pactl list short source-outputs' >> "$OUT" 2>&1 || true
  docker exec agent-wechat /opt/tools/a11y-dump.real --format aria --max-depth 28 2>/dev/null \
    | grep -Ei 'Voice Call|Hang Up|Cancel|Finish|Add Members|Waiting|Connecting|Ringing|started a voice call|Voice call ended|Mute|Microphone|Speaker|Answer|Decline|接听|拒绝|等待|通话|已接通|正在' >> "$OUT" 2>&1 || true
  sleep 1
done
