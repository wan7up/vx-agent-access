#!/bin/sh
set -eu

# Run after Tailscale is installed, signed in, and VOICE_GATEWAY_ALLOWED_ORIGIN
# is set to this machine's exact tailnet HTTPS origin. This is private Serve,
# not public Funnel.
tailscale serve --bg --yes --https=443 http://127.0.0.1:4317
