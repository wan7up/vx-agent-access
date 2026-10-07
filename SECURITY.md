# Security

This is an unofficial personal-channel integration, not a supported platform
bot API. Use a dedicated test account and obtain participants' consent.

- Bind REST/cache/Gateway control ports to loopback. Do not publish unauthenticated
  VNC or Agent control interfaces.
- Store tokens, account data, device identities and model credentials outside
  Git, with restrictive permissions. Public examples contain placeholders only.
- Incoming-call windows do not reliably identify the group. Text allowlists
  are not incoming-call allowlists.
- The desktop container reference uses ptrace-related permissions and relaxed
  seccomp for upstream desktop integration. Treat it as privileged local
  automation, not as a sandbox for untrusted code.
- Text and voice may be sent to separate cloud providers. Review their data
  policies and tool permissions before use.

Do not post credentials, chat logs, account IDs, screenshots or recovery archives
in public issues. Report vulnerabilities through this repository's private
security reporting feature. If unavailable, contact the maintainer without
sending private data publicly.

For accidental exposure, revoke affected credentials first, remove the material
from all published refs/assets and review account/device access. Deleting the
latest file alone does not remove Git history.
