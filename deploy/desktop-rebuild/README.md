# Desktop Rebuild Reference

This overlays the existing Linux desktop image with a single-user session,
bounded accessibility probes and lifecycle protections. It is not a standalone
upstream image or a migration installer.

Supply a compatible `BASE_IMAGE` to `Dockerfile`. `Dockerfile.server` optionally
embeds your matching `agent-server.release`, verified using `SERVER_SHA256`.
That binary is not distributed here. Back up the database first and verify the
binary's schema compatibility; do not downgrade a migrated database in place.

Before using `compose.yaml`, set these private values in a local `.env`:

```dotenv
DESKTOP_IMAGE=your-local-desktop-image:tested
DESKTOP_TOKEN_FILE=/path/to/private/token
DESKTOP_DATA_VOLUME=your-desktop-data-volume
DESKTOP_HOME_VOLUME=your-desktop-home-volume
DESKTOP_NETWORK=your-desktop-network
```

The volumes/network must already exist and match your deployment. The API
defaults to loopback port 6274; the cache upstream example uses that port.
Do not apply this template over a running installation without matching its
paths, volumes, login state and backup plan.

See [compatibility and recovery](../../docs/compatibility-and-recovery.md).
