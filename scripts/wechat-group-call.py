#!/usr/bin/env python3
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def json_mapping_from_env(name):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{name} must be valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    result = {}
    for key, aliases in value.items():
        if not isinstance(key, str) or not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
            raise RuntimeError(f"{name} values must be arrays of strings")
        result[key] = aliases
    return result


MEMBERS = json_mapping_from_env("WECHAT_GROUP_CALL_MEMBERS_JSON")
CHATROOMS = json_mapping_from_env("WECHAT_GROUP_CALL_CHATROOMS_JSON")

COORD_RE = re.compile(r"@\((\d+),(\d+) (\d+)x(\d+)\)")
LOG_FILE = Path(os.environ.get("WECHAT_GROUP_CALL_LOG", "/var/log/wechat-group-call.log"))
BOT_NAMES = [name.strip() for name in os.environ.get("WECHAT_GROUP_CALL_BOT_NAMES", "").split(",") if name.strip()]
BOT_NAME_PATTERN = "|".join(re.escape(name) for name in BOT_NAMES) or r"(?!)"
BOT_MENTION_RE = re.compile(rf"^[＠@](?:{BOT_NAME_PATTERN})(?:\s|[,:，：])*")
GPT_VOICE_REQUEST_DIR = Path(os.environ.get("WECHAT_GPT_VOICE_REQUEST_DIR", "/var/lib/wechat-gpt-voice-bridge/requests"))
GPT_VOICE_OPENING_MAX_CHARS = 1_000
CALL_LOCK_FILE = Path(os.environ.get("WECHAT_GROUP_CALL_LOCK", "/run/lock/wechat-group-call.lock"))
VOICE_BACKEND = os.environ.get("WECHAT_VOICE_BACKEND", "").strip().lower()
VOICE_BACKEND_FILE = Path(os.environ.get("WECHAT_VOICE_BACKEND_FILE", "/etc/xiaozhi-vx-bridge.backend"))
VOICE_BRIDGE_TEMPLATES = {
    "codex": "wechat-gpt-voice-bridge@.service",
    "xiaozhi": "xiaozhi-vx-bridge@.service",
}


def log_event(event, payload):
    record = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        "payload": payload,
    }
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


def run(cmd, timeout=30):
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"command failed: {cmd}")
    return proc.stdout


def acquire_call_lock(path=CALL_LOCK_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit("another WeChat group-call operation is already in progress")
    return handle


def docker_exec(container, *args, timeout=30):
    return run(["docker", "exec", container, *args], timeout=timeout)


def dump_tree(container, depth=26):
    dump_cmd = "/opt/tools/a11y-dump.real"
    try:
        docker_exec(container, "test", "-x", dump_cmd, timeout=5)
    except Exception:
        dump_cmd = "/opt/tools/a11y-dump"
    return docker_exec(
        container,
        dump_cmd,
        "--format",
        "aria",
        "--max-depth",
        str(depth),
        timeout=45,
    )


def click(container, x, y):
    docker_exec(container, "/opt/tools/click", str(x), str(y), "1", timeout=10)


def center_from_line(line):
    match = COORD_RE.search(line)
    if not match:
        return None
    x, y, w, h = map(int, match.groups())
    return x + w // 2, y + h // 2


def find_control(tree, role, name):
    needle = f'{role} "{name}"'
    for line in tree.splitlines():
        if needle in line:
            point = center_from_line(line)
            if point:
                return line, point
    return None, None


def find_finish(tree):
    for line in tree.splitlines():
        if 'push-button "Finish"' in line:
            point = center_from_line(line)
            if point:
                return line, point, "[DISABLED]" in line
    return None, None, False


def hangup_call(container):
    tree = dump_tree(container)
    _, point = find_control(tree, "push-button", "Hang Up")
    if not point:
        return False
    click(container, *point)
    return True


def gpt_voice_request_path(chat_id, request_dir=GPT_VOICE_REQUEST_DIR):
    name = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()
    return request_dir / f"{name}.json"


def write_gpt_voice_request(chat_id, opening_request, request_dir=GPT_VOICE_REQUEST_DIR):
    request_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(request_dir, 0o700)
    path = gpt_voice_request_path(chat_id, request_dir)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    payload = {"chatId": chat_id, "openingRequest": opening_request, "createdAt": time.time()}
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
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


def start_gpt_voice_bridge(chat_id):
    unit = run(["systemd-escape", f"--template={voice_bridge_template()}", chat_id], timeout=10).strip()
    run(["systemctl", "start", unit], timeout=20)
    return unit


def active_gpt_voice_bridge_units():
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


def stop_gpt_voice_bridge(unit):
    run(["systemctl", "stop", unit], timeout=20)


def current_chat_from_tree(tree):
    lines = tree.splitlines()
    for marker in ("[SELECTED]", "[FOCUSED]"):
        for line in lines:
            if marker not in line:
                continue
            for chat_id, names in CHATROOMS.items():
                for name in names:
                    if name and name in line:
                        return {"chatId": chat_id, "title": name}

    visible = []
    for chat_id, names in CHATROOMS.items():
        if any(name and name in tree for name in names):
            visible.append((chat_id, names[0]))
    if len(visible) == 1:
        chat_id, title = visible[0]
        return {"chatId": chat_id, "title": title}
    return None


def normalize_chat_id(chat_id):
    value = (chat_id or "").strip()
    if value.startswith("wechat:"):
        value = value[len("wechat:"):]
    return value


def chat_ref(chat_id):
    names = CHATROOMS.get(chat_id, [])
    title = names[0] if names else ""
    return {"chatId": chat_id, "title": title}


def validate_current_chat(current_chat, expected_chat_id):
    if not expected_chat_id:
        return
    expected = chat_ref(expected_chat_id)
    if not current_chat:
        raise SystemExit(
            "cannot identify current trusted WeChat group; "
            f"expected {expected['chatId']} ({expected['title']})"
        )
    if current_chat["chatId"] != expected_chat_id:
        current_title = current_chat.get("title") or ""
        raise SystemExit(
            "current WeChat chat mismatch: "
            f"expected {expected['chatId']} ({expected['title']}), "
            f"got {current_chat['chatId']} ({current_title})"
        )


def api_post_json(base_url, path, payload, timeout=30):
    url = base_url.rstrip("/") + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            return {
                "status": response.status,
                "body": response_body[-1200:],
            }
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        return {
            "status": exc.code,
            "body": response_body[-1200:],
            "error": str(exc),
        }


def open_expected_chat(base_url, chat_id, container, attempts=3):
    encoded = urllib.parse.quote(chat_id, safe="")
    failures = []
    for attempt in range(1, attempts + 1):
        response = api_post_json(
            base_url,
            f"/api/chats/{encoded}/open?clearUnreads=false",
            {},
            timeout=30,
        )
        if response.get("status") != 200:
            failures.append(f"attempt {attempt}: HTTP {response.get('status')}")
            continue
        time.sleep(0.5)
        tree = dump_tree(container)
        current_chat = current_chat_from_tree(tree)
        if current_chat and current_chat.get("chatId") == chat_id:
            return {
                "attempt": attempt,
                "response": response,
                "currentChat": current_chat,
            }, tree, current_chat
        current = (current_chat or {}).get("chatId") or "unknown"
        failures.append(f"attempt {attempt}: current chat is {current}")
    raise RuntimeError(
        "could not keep the expected WeChat group open before dialing: "
        + "; ".join(failures)
    )


def api_get_json(base_url, path, timeout=30):
    request = urllib.request.Request(base_url.rstrip("/") + path)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def strip_bot_mention(text):
    return BOT_MENTION_RE.sub("", text, count=1).strip()


def call_request_from_messages(messages, local_id):
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or int(message.get("localId") or 0) != local_id:
            continue
        if message.get("isSelf") is True:
            return None
        if int(message.get("type") or message.get("messageType") or 0) != 1:
            return None
        text = " ".join(str(message.get("content") or "").replace("\u2005", " ").split()).strip()
        if not BOT_MENTION_RE.match(text):
            return None
        command = strip_bot_mention(text)
        if not command or len(command) > GPT_VOICE_OPENING_MAX_CHARS or "\0" in command:
            return None
        return command
    return None


def fetch_call_request(base_url, chat_id, local_id):
    encoded = urllib.parse.quote(chat_id, safe="")
    messages = api_get_json(base_url, f"/api/messages/{encoded}?limit=20")
    return call_request_from_messages(messages, local_id)


def resolve_member(raw):
    raw = (raw or "").strip()
    folded = raw.casefold()
    for display, aliases in MEMBERS.items():
        candidates = [display, *aliases]
        for name in candidates:
            name_folded = name.casefold()
            if folded == name_folded or name_folded in folded:
                return display
    raise ValueError(f"unknown member alias: {raw}")


def member_candidates(display):
    return [display, *MEMBERS.get(display, [])]


def main():
    parser = argparse.ArgumentParser(description="Start a WeChat group voice call from the currently open group chat.")
    parser.add_argument("target", help="member display name or known alias")
    parser.add_argument("--container", default="agent-wechat")
    parser.add_argument("--dry-run", action="store_true", help="inspect controls without starting the call")
    parser.add_argument("--gpt-voice", action="store_true", help="connect the call to the configured live Voice bridge")
    parser.add_argument(
        "--voice-opening-request",
        help="one-time opening request for the Codex backend",
    )
    parser.add_argument(
        "--voice-opening-local-id",
        type=int,
        help="local id of the exact WeChat mention whose text becomes the one-time Voice opening",
    )
    parser.add_argument("--hangup-after", type=float, default=0, help="hang up after N seconds, for tests only")
    parser.add_argument(
        "--expected-chat-id",
        help="trusted WeChat chatroom id that must match the currently open group before dialing or sending text",
    )
    parser.add_argument("--agent-wechat-url", default=os.environ.get("AGENT_WECHAT_URL", "http://127.0.0.1:6175"))
    args = parser.parse_args()

    if not args.dry_run and not args.gpt_voice:
        raise SystemExit("--gpt-voice is required for a live call")
    target = resolve_member(args.target)
    result = {"target": target, "dryRun": args.dry_run, "steps": []}
    expected_chat_id = normalize_chat_id(args.expected_chat_id)
    if expected_chat_id:
        if expected_chat_id not in CHATROOMS:
            raise SystemExit(f"unknown trusted WeChat chat id for --expected-chat-id: {expected_chat_id}")
        result["expectedChat"] = chat_ref(expected_chat_id)
    if not args.dry_run and not expected_chat_id:
        raise SystemExit("--expected-chat-id is required for real WeChat group calls")
    timings = {"startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    timing_start = time.monotonic()
    result["timings"] = timings
    tree = None
    current_chat = None
    gpt_voice_unit = None
    gpt_voice_request_path_value = None
    call_initiated = False

    if args.voice_opening_request and not args.gpt_voice:
        raise SystemExit("--voice-opening-request requires --gpt-voice")
    if args.voice_opening_local_id is not None and not args.gpt_voice:
        raise SystemExit("--voice-opening-local-id requires --gpt-voice")
    if args.voice_opening_request and args.voice_opening_local_id is not None:
        raise SystemExit("--voice-opening-request and --voice-opening-local-id are mutually exclusive")
    if args.voice_opening_local_id is not None and args.voice_opening_local_id < 1:
        raise SystemExit("--voice-opening-local-id must be positive")
    voice_opening_request = None
    if args.gpt_voice and args.voice_opening_request:
        voice_opening_request = strip_bot_mention(" ".join(args.voice_opening_request.split()).strip())
        if not voice_opening_request or len(voice_opening_request) > GPT_VOICE_OPENING_MAX_CHARS or "\0" in voice_opening_request:
            raise SystemExit("--voice-opening-request is empty, too long, or contains invalid text")
    # Serializes the shared WeChat GUI through bridge startup and dialing.
    call_lock = acquire_call_lock()

    if not args.dry_run:
        active_voice_units = active_gpt_voice_bridge_units()
        if active_voice_units:
            raise SystemExit(
                "a Voice group call is already active or starting: "
                + ", ".join(active_voice_units)
            )
        codex_voice = voice_bridge_template() == VOICE_BRIDGE_TEMPLATES["codex"]
        if codex_voice:
            if voice_opening_request is None and args.voice_opening_local_id is not None:
                voice_opening_request = fetch_call_request(
                    args.agent_wechat_url,
                    expected_chat_id,
                    args.voice_opening_local_id,
                )
                result["voiceOpeningSource"] = {
                    "source": "exact-message" if voice_opening_request else "default",
                    "localId": args.voice_opening_local_id,
                }
            if voice_opening_request is None:
                voice_opening_request = f"向{target}打招呼，并告诉他语音已经接通。"
        started = time.monotonic()
        try:
            if codex_voice:
                gpt_voice_request_path_value = write_gpt_voice_request(expected_chat_id, voice_opening_request)
            gpt_voice_unit = start_gpt_voice_bridge(expected_chat_id)
        except Exception:
            if gpt_voice_request_path_value:
                gpt_voice_request_path_value.unlink(missing_ok=True)
            raise
        result["gptVoiceBridge"] = {"unit": gpt_voice_unit, "chatId": expected_chat_id, "prewarmed": True}
        timings["prewarmGptVoiceBridgeMs"] = round((time.monotonic() - started) * 1000)

    try:
        if tree is None:
            if not args.dry_run:
                started = time.monotonic()
                open_result, tree, current_chat = open_expected_chat(
                    args.agent_wechat_url,
                    expected_chat_id,
                    args.container,
                )
                result["openExpectedChat"] = open_result
                timings["openExpectedChatMs"] = round((time.monotonic() - started) * 1000)
            else:
                tree = dump_tree(args.container)
                current_chat = current_chat_from_tree(tree)
            if current_chat:
                result["currentChat"] = current_chat
            validate_current_chat(current_chat, expected_chat_id)
        if 'frame "Voice Call"' in tree:
            raise SystemExit("a voice call window is already open")

        if "Add Members" in tree:
            result["steps"].append({"control": "Add Members", "alreadyOpen": True})
            if args.dry_run:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
        else:
            line, point = find_control(tree, "push-button", "Group Call")
            if not point:
                raise SystemExit("Group Call button not found; open the target WeChat group chat first")
            result["steps"].append({"control": "Group Call", "point": point, "line": line.strip()})
            if args.dry_run:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return

            click(args.container, *point)
            time.sleep(2)

            tree = dump_tree(args.container)
            if "Add Members" not in tree:
                raise SystemExit("Add Members window did not appear after clicking Group Call")

        line = point = None
        selected_name = None
        for candidate in member_candidates(target):
            line, point = find_control(tree, "check-box", candidate)
            if point:
                selected_name = candidate
                break
        if not point:
            raise SystemExit(f"member checkbox not found: {target} ({', '.join(member_candidates(target))})")
        result["steps"].append({"control": selected_name or target, "point": point, "line": line.strip()})
        if "[CHECKED]" not in line:
            click(args.container, *point)
            time.sleep(1)
            tree = dump_tree(args.container)

        finish_line, finish_point, disabled = find_finish(tree)
        if not finish_point:
            raise SystemExit("Finish button not found")
        if disabled:
            raise SystemExit("Finish button is disabled after selecting target")
        result["steps"].append({"control": "Finish", "point": finish_point, "line": finish_line.strip()})

        click(args.container, *finish_point)
        call_initiated = True
        timings["voiceBridgePrewarmedBeforeFinish"] = not args.dry_run

        time.sleep(3)
        tree = dump_tree(args.container)
        result["started"] = 'frame "Voice Call"' in tree or "Hang Up" in tree or "started a voice call" in tree
        if not result["started"]:
            result["startedDetection"] = "not-confirmed-after-finish"

        if args.hangup_after > 0 and "hungUp" not in result:
            time.sleep(args.hangup_after)
            result["hungUp"] = hangup_call(args.container)
            if gpt_voice_unit:
                stop_gpt_voice_bridge(gpt_voice_unit)

        timings["totalMs"] = round((time.monotonic() - timing_start) * 1000)
        log_event("completed", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        if gpt_voice_unit and not call_initiated:
            try:
                stop_gpt_voice_bridge(gpt_voice_unit)
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log_event("failed", {"argv": sys.argv[1:], "error": str(exc)})
        print(f"wechat-group-call: {exc}", file=sys.stderr)
        raise SystemExit(1)
