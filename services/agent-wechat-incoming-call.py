#!/usr/bin/env python3
"""Automatically answer WeChat calls and attach the private Codex Voice bridge."""

import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import time


CONTAINER = os.environ.get("WECHAT_CONTAINER", "agent-wechat")
PROBE_HOST = Path(os.environ.get("WECHAT_INCOMING_PROBE_HOST", "/usr/local/share/agent-wechat/incoming-call-probe.py"))
PROBE_CONTAINER = os.environ.get("WECHAT_INCOMING_PROBE_CONTAINER", "/opt/tools/incoming-call-probe")
CALL_LOCK_FILE = Path(os.environ.get("WECHAT_GROUP_CALL_LOCK", "/run/lock/wechat-group-call.lock"))
REQUEST_DIR = Path(os.environ.get("WECHAT_GPT_VOICE_REQUEST_DIR", "/var/lib/wechat-gpt-voice-bridge/requests"))
INCOMING_CHAT_ID = os.environ.get("WECHAT_INCOMING_VOICE_CHAT_ID", "").strip()
OPENING_REQUEST = os.environ.get(
    "WECHAT_INCOMING_VOICE_OPENING",
    "用自然粤语主动说：喂~HELLO啊~你搵我做咩~",
)
ANSWER_DELAY_SECONDS = max(0.0, float(os.environ.get("WECHAT_INCOMING_ANSWER_DELAY_SECONDS", "4")))
PROBE_MAX_WAIT_SECONDS = max(60.0, float(os.environ.get("WECHAT_INCOMING_PROBE_MAX_WAIT_SECONDS", str(23 * 60 * 60))))
VOICE_BACKEND = os.environ.get("WECHAT_VOICE_BACKEND", "").strip().lower()
VOICE_BACKEND_FILE = Path(os.environ.get("WECHAT_VOICE_BACKEND_FILE", "/etc/xiaozhi-vx-bridge.backend"))
VOICE_BRIDGE_TEMPLATES = {
    "codex": "wechat-gpt-voice-bridge@.service",
    "xiaozhi": "xiaozhi-vx-bridge@.service",
}


def run(command, timeout=30):
    process = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or f"command failed: {command}"
        raise RuntimeError(detail[:600])
    return process.stdout


def ensure_probe():
    if not PROBE_HOST.is_file():
        raise RuntimeError(f"missing incoming-call probe: {PROBE_HOST}")
    run([
        "docker", "exec", CONTAINER, "sh", "-c",
        "pkill -f '^python3 /opt/tools/incoming-call-probe( |$)' || true",
    ], timeout=10)
    run(["docker", "cp", str(PROBE_HOST), f"{CONTAINER}:{PROBE_CONTAINER}"], timeout=20)
    run(["docker", "exec", "-u", "0", CONTAINER, "chmod", "755", PROBE_CONTAINER], timeout=10)


def parse_probe_event(output):
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    event = json.loads(lines[-1])
    answer = event.get("answer") if isinstance(event, dict) else None
    if event.get("type") != "incoming-call" or not isinstance(answer, dict):
        raise RuntimeError("incoming-call probe returned an invalid event")
    return event


def wait_for_incoming_call():
    return parse_probe_event(
        run(
            ["docker", "exec", CONTAINER, PROBE_CONTAINER, "--max-wait-seconds", str(PROBE_MAX_WAIT_SECONDS)],
            timeout=PROBE_MAX_WAIT_SECONDS + 5 * 60,
        )
    )


def current_incoming_call():
    output = run(["docker", "exec", CONTAINER, PROBE_CONTAINER, "--once"], timeout=10)
    return parse_probe_event(output)


def acquire_call_lock():
    CALL_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = CALL_LOCK_FILE.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def active_voice_units():
    output = run(
        [
            "systemctl",
            "list-units",
            "--type=service",
            "--state=activating,active,reloading",
            "--no-legend",
            "--plain",
            "wechat-gpt-voice-bridge@*.service",
            "xiaozhi-vx-bridge@*.service",
        ],
        timeout=10,
    )
    return [line.split()[0] for line in output.splitlines() if line.strip()]


def configured_chat_id(chat_id=None):
    value = (chat_id if chat_id is not None else INCOMING_CHAT_ID).strip()
    if not value.endswith("@chatroom"):
        raise RuntimeError("WECHAT_INCOMING_VOICE_CHAT_ID must be a configured group chatroom id")
    return value


def request_path(chat_id=None):
    chat_id = configured_chat_id(chat_id)
    return REQUEST_DIR / f"{hashlib.sha256(chat_id.encode('utf-8')).hexdigest()}.json"


def write_opening_request(chat_id=None, opening=OPENING_REQUEST):
    chat_id = configured_chat_id(chat_id)
    REQUEST_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(REQUEST_DIR, 0o700)
    path = request_path(chat_id)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"chatId": chat_id, "openingRequest": opening, "createdAt": time.time()}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def voice_bridge_template():
    try:
        backend = VOICE_BACKEND or VOICE_BACKEND_FILE.read_text(encoding="ascii").strip().lower()
    except FileNotFoundError:
        backend = "codex"
    try:
        return VOICE_BRIDGE_TEMPLATES[backend]
    except KeyError:
        raise RuntimeError(f"Unsupported WECHAT_VOICE_BACKEND: {backend}") from None


def bridge_unit(chat_id=None):
    chat_id = configured_chat_id(chat_id)
    return run(["systemd-escape", f"--template={voice_bridge_template()}", chat_id], timeout=10).strip()


def start_bridge(unit):
    run(["systemctl", "start", unit], timeout=30)


def stop_bridge(unit):
    run(["systemctl", "stop", unit], timeout=30)


def answer_call(event):
    answer = event["answer"]
    values = [answer.get(key) for key in ("x", "y", "width", "height")]
    if not all(isinstance(value, int) for value in values):
        raise RuntimeError("Answer control has invalid bounds")
    x, y, width, height = values
    if width <= 0 or height <= 0 or not (0 <= x < 10000 and 0 <= y < 10000):
        raise RuntimeError("Answer control is outside expected screen bounds")
    run(["docker", "exec", CONTAINER, "/opt/tools/click", str(x + width // 2), str(y + height // 2), "1"], timeout=10)


def handle_incoming_call(event):
    lock = acquire_call_lock()
    if lock is None:
        logging.warning("Incoming call left ringing because another WeChat call operation holds the lock.")
        return False
    request = None
    unit = None
    try:
        active = active_voice_units()
        if active:
            logging.warning("Incoming call left ringing because a Voice Bridge is already active: %s", ", ".join(active))
            return False
        codex_voice = voice_bridge_template() == VOICE_BRIDGE_TEMPLATES["codex"]
        if codex_voice:
            request = write_opening_request()
        unit = bridge_unit()
        start_bridge(unit)
        if codex_voice and ANSWER_DELAY_SECONDS:
            logging.info("Prewarming Voice for %.1fs before answering incoming call.", ANSWER_DELAY_SECONDS)
            time.sleep(ANSWER_DELAY_SECONDS)
        event = current_incoming_call()
        if event is None:
            raise RuntimeError("incoming call ended before answering")
        answer_call(event)
        logging.info("Answered incoming WeChat call with Voice Bridge %s.", unit)
        return True
    except Exception:
        if unit:
            try:
                stop_bridge(unit)
            except Exception as error:
                logging.error("Could not stop failed incoming Voice Bridge %s: %s", unit, error)
        if request:
            request.unlink(missing_ok=True)
        raise
    finally:
        lock.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not INCOMING_CHAT_ID.endswith("@chatroom"):
        raise SystemExit("WECHAT_INCOMING_VOICE_CHAT_ID must be a configured group chatroom id")
    while True:
        try:
            ensure_probe()
            event = wait_for_incoming_call()
            if event is None:
                continue
            if not handle_incoming_call(event):
                time.sleep(2)
        except Exception as error:
            logging.error("Incoming-call observer cycle failed: %s: %s", type(error).__name__, error)
            time.sleep(3)


if __name__ == "__main__":
    main()
