#!/usr/bin/env python3
"""Bound the background AT-SPI probe without changing manual UI dumps."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time


REAL_DUMP = Path("/opt/tools/a11y-dump.real")
LOCK_FILE = Path(
    os.environ.get(
        "AGENT_WECHAT_A11Y_PROBE_LOCK",
        "/tmp/agent-wechat-a11y-probe.lock",
    )
)
TIMEOUT_SECONDS = float(
    os.environ.get("AGENT_WECHAT_A11Y_PROBE_TIMEOUT_SECONDS", "5")
)
LOCK_TIMEOUT_SECONDS = float(
    os.environ.get("AGENT_WECHAT_A11Y_LOCK_TIMEOUT_SECONDS", "3")
)


def acquire_lock(lock, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))


def main() -> int:
    if not REAL_DUMP.is_file():
        print(f"missing original dump: {REAL_DUMP}", file=sys.stderr)
        return 1

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as lock:
        # Health checks and API observations share the same desktop. Briefly
        # queue them instead of reporting a healthy desktop as unauthenticated.
        if not acquire_lock(lock, LOCK_TIMEOUT_SECONDS):
            print("a11y probe lock wait exceeded", file=sys.stderr)
            return 75

        try:
            completed = subprocess.run(
                [str(REAL_DUMP), *sys.argv[1:]],
                check=False,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            print(
                f"a11y probe exceeded {TIMEOUT_SECONDS:g}s",
                file=sys.stderr,
            )
            return 124
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
