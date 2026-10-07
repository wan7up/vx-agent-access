#!/usr/bin/env bash
set -euo pipefail
umask 077
ulimit -c 0

if [ "$(id -u)" != 1000 ]; then
  echo "Desktop session must run as wechat (uid 1000)" >&2
  exit 1
fi

wait_for() {
  local attempts=100
  if [ "${1:-}" = "--attempts" ]; then
    attempts="$2"
    shift 2
  fi
  local attempt
  for attempt in $(seq 1 "$attempts"); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  echo "Required desktop dependency is not ready: $1" >&2
  exit 1
}

mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
dbus-daemon --session --nofork --address="$DBUS_SESSION_BUS_ADDRESS" &
wait_for dbus-send --session --print-reply --reply-timeout=1000 \
  --dest=org.freedesktop.DBus /org/freedesktop/DBus \
  org.freedesktop.DBus.ListNames

# A container restart can leave Xvfb's lock/socket behind even though the
# display process is gone. Remove only stale artifacts; never disturb a live
# display that responds to xdpyinfo.
display_number="${DISPLAY#:}"
display_lock="/tmp/.X${display_number}-lock"
display_socket="/tmp/.X11-unix/X${display_number}"
if [ -e "$display_lock" ] || [ -S "$display_socket" ]; then
  if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    rm -f "$display_lock" "$display_socket"
  fi
fi

Xvfb "$DISPLAY" -screen 0 1280x800x24 -dpi 96 -nolisten tcp &
wait_for xdpyinfo -display "$DISPLAY"
fluxbox &
dunst &

/usr/libexec/at-spi-bus-launcher &
wait_for dbus-send --session --print-reply --reply-timeout=1000 \
  --dest=org.a11y.Bus /org/a11y/bus org.a11y.Bus.GetAddress
dbus-send --session --print-reply --reply-timeout=2000 \
  --dest=org.a11y.Bus /org/a11y/bus \
  org.freedesktop.DBus.Properties.Set \
  string:org.a11y.Status string:IsEnabled variant:boolean:true >/dev/null
dbus-send --session --print-reply --reply-timeout=2000 \
  --dest=org.a11y.Bus /org/a11y/bus \
  org.freedesktop.DBus.Properties.Set \
  string:org.a11y.Status string:ScreenReaderEnabled variant:boolean:true >/dev/null

# PulseAudio stores a runtime symlink in the persistent HOME. Remove only
# links whose target disappeared with a previous container instance.
for pulse_runtime in "$HOME/.config/pulse"/*-runtime; do
  if [ -L "$pulse_runtime" ] && [ ! -e "$pulse_runtime" ]; then
    rm -f "$pulse_runtime"
  fi
done
env -u PULSE_SERVER pulseaudio --start --exit-idle-time=-1
# PulseAudio can take longer on a cold ARM restart while its runtime socket is
# recreated. Bound the wait at 60 seconds instead of restarting the container
# after the normal 10-second dependency window.
wait_for --attempts 600 pactl info
pactl load-module module-null-sink sink_name=wechat_call_playback \
  sink_properties=device.description=WeChatCallPlayback rate=48000 channels=1 >/dev/null
pactl load-module module-null-sink sink_name=gpt_voice_inject \
  sink_properties=device.description=GptVoiceInject rate=48000 channels=1 >/dev/null
pactl load-module module-remap-source master=gpt_voice_inject.monitor \
  source_name=gpt_voice_mic source_properties=device.description=GptVoiceMic >/dev/null
pactl set-default-sink wechat_call_playback
pactl set-default-source gpt_voice_mic

if [ "${ENABLE_VNC:-1}" = 1 ]; then
  x11vnc -display "$DISPLAY" -forever -nopw -shared -viewonly \
    -xkb -rfbport 5900 -listen 127.0.0.1 &
  websockify --web /opt/novnc 127.0.0.1:6080 localhost:5900 &
fi

/usr/bin/wechat &
python3 /opt/enter-session.py
# The server and every docker-exec tool inherit the same user and session bus.
exec /opt/agent-server/agent-server
