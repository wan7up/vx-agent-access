#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
from pathlib import Path


UNIT = os.environ.get("OPENCLAW_GATEWAY_UNIT", "openclaw-gateway.service")
STATE_FILE = Path(os.environ.get("STATE_FILE", "/root/.local/state/openclaw-memory-guard.json"))
MEMORY_LIMIT_BYTES = int(os.environ.get("MEMORY_LIMIT_BYTES", str(1200 * 1024 * 1024)))
SWAP_LIMIT_BYTES = int(os.environ.get("SWAP_LIMIT_BYTES", str(512 * 1024 * 1024)))
CONSECUTIVE_BREACHES = int(os.environ.get("CONSECUTIVE_BREACHES", "3"))
QUIET_SECONDS = int(os.environ.get("QUIET_SECONDS", "180"))

ACTIVITY_RE = re.compile(
    r"\[agent\].*run|Reply deliver start|Dispatching segment|proactive sent|wechat-group-call",
    re.I,
)


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}", flush=True)


def run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"consecutiveBreaches": 0}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def read_metrics() -> tuple[int, int]:
    proc = run([
        "systemctl",
        "--user",
        "show",
        UNIT,
        "-p",
        "MemoryCurrent",
        "-p",
        "MemorySwapCurrent",
    ])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "failed to read Gateway memory")
    values = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        if value.isdigit():
            values[key] = int(value)
    return values.get("MemoryCurrent", 0), values.get("MemorySwapCurrent", 0)


def gateway_is_active() -> bool:
    return run(["systemctl", "--user", "is-active", "--quiet", UNIT]).returncode == 0


def has_recent_activity() -> bool:
    proc = run([
        "journalctl",
        "--user",
        "-u",
        UNIT,
        "--since",
        f"-{QUIET_SECONDS} seconds",
        "-o",
        "cat",
        "--no-pager",
    ])
    return bool(ACTIVITY_RE.search(proc.stdout))


def main() -> int:
    state = load_state()
    if not gateway_is_active():
        state["consecutiveBreaches"] = 0
        save_state(state)
        log(f"skip: {UNIT} is not active")
        return 0

    memory, swap = read_metrics()
    breached = memory >= MEMORY_LIMIT_BYTES or swap >= SWAP_LIMIT_BYTES
    state.update({
        "checkedAt": time.time(),
        "memoryCurrent": memory,
        "memorySwapCurrent": swap,
    })
    if not breached:
        state["consecutiveBreaches"] = 0
        save_state(state)
        return 0

    count = int(state.get("consecutiveBreaches", 0)) + 1
    state["consecutiveBreaches"] = count
    save_state(state)
    log(f"pressure: memory={memory} swap={swap} consecutive={count}/{CONSECUTIVE_BREACHES}")

    if count < CONSECUTIVE_BREACHES:
        return 0
    if has_recent_activity():
        log(f"defer restart: Gateway had activity in the last {QUIET_SECONDS}s")
        return 0

    log(f"restarting {UNIT} after sustained memory pressure")
    proc = run(["systemctl", "--user", "restart", UNIT], timeout=75)
    state["consecutiveBreaches"] = 0
    state["lastRestartAt"] = time.time()
    state["lastRestartReturnCode"] = proc.returncode
    save_state(state)
    if proc.returncode != 0:
        log(f"restart failed: {proc.stderr.strip()}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
