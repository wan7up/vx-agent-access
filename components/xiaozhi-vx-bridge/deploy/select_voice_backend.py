#!/usr/bin/env python3
"""Switch only the vx voice backend; never restart the OpenClaw text channel."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


BACKEND_FILE = Path("/etc/xiaozhi-vx-bridge.backend")
ORIGINAL_OPENING = Path("/etc/systemd/system/agent-wechat-incoming-call.service.d/opening.conf")
XIAOZHI_OPENING = Path("/etc/systemd/system/agent-wechat-incoming-call-xiaozhi.service.d/opening.conf")
XIAOZHI_UNIT_FILE = Path("/etc/systemd/system/agent-wechat-incoming-call-xiaozhi.service")
DEVICE_STATE = Path("/var/lib/xiaozhi-vx-bridge/device.json")
BRIDGE = Path("/opt/xiaozhi-vx-bridge/call_bridge.py")
CODEX_INCOMING_UNIT = "agent-wechat-incoming-call.service"
XIAOZHI_INCOMING_UNIT = "agent-wechat-incoming-call-xiaozhi.service"
CODEX_UNIT = "codex-voice-gateway.service"
PATTERNS = ("wechat-gpt-voice-bridge@*.service", "xiaozhi-vx-bridge@*.service")


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def current_backend() -> str:
    try:
        backend = BACKEND_FILE.read_text(encoding="ascii").strip().lower()
    except FileNotFoundError:
        return "codex"
    if backend not in ("codex", "xiaozhi"):
        raise RuntimeError("Backend selector contains an unsupported value.")
    return backend


def active_voice_units() -> list[str]:
    output = run(
        "systemctl", "list-units", "--type=service",
        "--state=activating,active,reloading", "--no-legend", "--plain", *PATTERNS,
    )
    return [line.split()[0] for line in output.splitlines() if line.strip()]


def atomic_selector(backend: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".xiaozhi-vx-bridge.", dir=BACKEND_FILE.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as file:
            file.write(backend + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, BACKEND_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify_xiaozhi_files() -> None:
    if not BRIDGE.is_file() or not XIAOZHI_UNIT_FILE.is_file() or not ORIGINAL_OPENING.is_file():
        raise RuntimeError("Xiaozhi bridge, incoming-call unit or private opening config is missing.")
    identity = json.loads(DEVICE_STATE.read_text(encoding="utf-8"))
    if identity.get("activated") is not True:
        raise RuntimeError("The Xiaozhi virtual device is not activated.")
    if XIAOZHI_OPENING.exists() and XIAOZHI_OPENING.read_bytes() != ORIGINAL_OPENING.read_bytes():
        raise RuntimeError("Refusing to overwrite a locally modified Xiaozhi opening config.")


def switch(backend: str) -> None:
    current_backend()
    active = active_voice_units()
    if active:
        raise RuntimeError("End the active voice call before switching: " + ", ".join(active))
    if backend == "xiaozhi":
        verify_xiaozhi_files()

    if backend == "xiaozhi":
        run("systemctl", "stop", CODEX_INCOMING_UNIT)
        run("systemctl", "stop", XIAOZHI_INCOMING_UNIT)
        XIAOZHI_OPENING.parent.mkdir(parents=True, exist_ok=True)
        XIAOZHI_OPENING.write_bytes(ORIGINAL_OPENING.read_bytes())
        os.chmod(XIAOZHI_OPENING, 0o644)
        run("systemctl", "daemon-reload")
        atomic_selector(backend)
        run("systemctl", "disable", "--now", CODEX_INCOMING_UNIT)
        run("systemctl", "disable", "--now", CODEX_UNIT)
        run("systemctl", "enable", "--now", XIAOZHI_INCOMING_UNIT)
    else:
        run("systemctl", "disable", "--now", XIAOZHI_INCOMING_UNIT)
        atomic_selector(backend)
        run("systemctl", "enable", "--now", CODEX_UNIT)
        run("systemctl", "enable", "--now", CODEX_INCOMING_UNIT)
    incoming = XIAOZHI_INCOMING_UNIT if backend == "xiaozhi" else CODEX_INCOMING_UNIT
    print(f"VOICE_BACKEND={current_backend()} incoming={run('systemctl', 'is-active', incoming)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", choices=("status", "xiaozhi", "codex"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root.")
    if args.backend == "status":
        print(f"VOICE_BACKEND={current_backend()}")
        print(f"ACTIVE_CALLS={','.join(active_voice_units()) or 'none'}")
        return
    switch(args.backend)


if __name__ == "__main__":
    main()
