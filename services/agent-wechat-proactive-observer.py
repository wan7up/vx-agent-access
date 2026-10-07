#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path


BASE_URL = os.environ.get("AGENT_WECHAT_URL", "http://127.0.0.1:6174").rstrip("/")
TOKEN_FILE = Path(os.environ.get("AGENT_WECHAT_TOKEN_FILE", "/opt/docker/agent-wechat/secrets/token"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/var/lib/agent-wechat-proactive-observer/state.json"))
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "openclaw")
CHAT_IDS = [x.strip() for x in os.environ.get(
    "CHAT_IDS",
    "",
).split(",") if x.strip()]
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "30"))
MESSAGE_LIMIT = int(os.environ.get("MESSAGE_LIMIT", "40"))
BUFFER_SIZE = int(os.environ.get("BUFFER_SIZE", "12"))
IDLE_TRIGGER_SECONDS = int(os.environ.get("IDLE_TRIGGER_SECONDS", str(72 * 3600)))
PROACTIVE_COOLDOWN_SECONDS = int(os.environ.get("PROACTIVE_COOLDOWN_SECONDS", str(6 * 3600)))
IDLE_PROACTIVE_COOLDOWN_SECONDS = int(os.environ.get("IDLE_PROACTIVE_COOLDOWN_SECONDS", str(24 * 3600)))
MAX_PROACTIVE_PER_DAY = int(os.environ.get("MAX_PROACTIVE_PER_DAY", "3"))
OPENCLAW_TIMEOUT_SECONDS = int(os.environ.get("OPENCLAW_TIMEOUT_SECONDS", "180"))
BOT_NAMES = [x.strip() for x in os.environ.get("BOT_NAMES", "your_bot_name").split(",") if x.strip()]
AT_TERMS = [x.strip() for x in os.environ.get("AT_TERMS", "@your_bot_name").split(",") if x.strip()]

HELP_RE = re.compile(
    r"(有人知道|有人懂|有无.*懂|有没有.*懂|怎么|怎麼|点样|點樣|为什么|为什么|為什麼|能不能|可不可以|"
    r"可以帮|幫我|帮我|求助|救命|看得懂|睇得明|看一下|睇下|总结|總結|提醒|开会|開會|图片|圖片|文件|文档|文檔|\?)",
    re.I,
)
QUIET_OR_BAD_RE = re.compile(r"(红包|政治|吵架|骂|屌|隐私|密码|token|key|密钥|提现吗|提现吗|转账|轉賬)")


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}", flush=True)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "initialized": False,
            "highWater": {},
            "lastBotAt": {},
            "lastProactiveAt": {},
            "dailyCount": {},
            "buffers": {},
        }
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        return {"initialized": False, "highWater": {}, "lastBotAt": {}, "lastProactiveAt": {}, "dailyCount": {}, "buffers": {}}
    for key in ["highWater", "lastBotAt", "lastProactiveAt", "dailyCount", "buffers"]:
        state.setdefault(key, {})
    return state


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


def headers() -> dict:
    return {
        "Authorization": "Bearer " + TOKEN_FILE.read_text().strip(),
        "Content-Type": "application/json",
    }


def api_get(path: str):
    req = urllib.request.Request(BASE_URL + path, headers=headers())
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def api_post(path: str, payload: dict):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers(), method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def clean_text(value: str) -> str:
    value = (value or "").replace("\u2005", " ").replace("\u200b", "").strip()
    return re.sub(r"\s+", " ", value)


def message_text(msg: dict) -> str:
    return clean_text(msg.get("content") or "")


def is_mentioned(msg: dict) -> bool:
    if msg.get("isMentioned") is True:
        return True
    text = message_text(msg)
    return any(term and term in text for term in AT_TERMS)


def weak_bot_mention(msg: dict) -> bool:
    if is_mentioned(msg):
        return False
    text = message_text(msg)
    return any(name and name in text for name in BOT_NAMES)


def is_help_like(msg: dict) -> bool:
    text = message_text(msg)
    if not text:
        return False
    return bool(HELP_RE.search(text))


def is_idle_chat_candidate(msg: dict) -> bool:
    text = message_text(msg)
    if len(text) < 4:
        return False
    if QUIET_OR_BAD_RE.search(text):
        return False
    if is_mentioned(msg) or weak_bot_mention(msg) or is_help_like(msg):
        return False
    return True


def today_key() -> str:
    return time.strftime("%Y-%m-%d")


def daily_count_for(state: dict, chat_id: str) -> int:
    day = today_key()
    counts = state.setdefault("dailyCount", {}).setdefault(chat_id, {})
    for key in list(counts):
        if key != day:
            counts.pop(key, None)
    return int(counts.get(day, 0))


def bump_daily_count(state: dict, chat_id: str) -> None:
    day = today_key()
    counts = state.setdefault("dailyCount", {}).setdefault(chat_id, {})
    counts[day] = int(counts.get(day, 0)) + 1


def append_buffer(state: dict, chat_id: str, msg: dict) -> None:
    if msg.get("isSelf") is True:
        return
    text = message_text(msg)
    if not text:
        return
    item = {
        "localId": int(msg.get("localId") or 0),
        "senderName": msg.get("senderName") or msg.get("sender") or "unknown",
        "content": text[:500],
        "seenAt": time.time(),
    }
    buf = state.setdefault("buffers", {}).setdefault(chat_id, [])
    if not any(int(x.get("localId") or 0) == item["localId"] for x in buf):
        buf.append(item)
    del buf[:-BUFFER_SIZE]


def extract_session_id(chat_id: str) -> str | None:
    sessions_path = Path("/root/.openclaw/agents/main/sessions/sessions.json")
    try:
        sessions = json.loads(sessions_path.read_text())
    except Exception as exc:
        log(f"session store read failed: {exc!r}")
        return None
    key = f"agent:main:wechat:group:{chat_id}"
    item = sessions.get(key) or {}
    return item.get("sessionId")


def build_observer_prompt(chat_id: str, trigger: str, msg: dict, buffer: list[dict], idle_hours: float) -> str:
    lines = []
    for item in buffer[-BUFFER_SIZE:]:
        lines.append(f"- {item.get('senderName')}: {item.get('content')}")
    transcript = "\n".join(lines) or "(no recent buffer)"
    current_sender = msg.get("senderName") or msg.get("sender") or "unknown"
    current_text = message_text(msg)
    bot_names = ", ".join(BOT_NAMES) or "the configured bot"
    return f"""[WeChat proactive observer]
You are the configured WeChat bot in a trusted WeChat group.
Your configured display name(s): {bot_names}
The user configured you to sometimes proactively join the group conversation, not only respond to @ mentions.
Decide whether to send ONE short, natural, low-interruption group message now.

Rules:
- Output exactly NO_REPLY if joining would feel forced, noisy, too serious, or unnecessary.
- If replying, output only the final WeChat message text. No JSON, no analysis, no markdown table.
- Keep it short: preferably 1 sentence, max 60 Chinese characters.
- Do not pretend you saw images/files unless the current transcript explicitly includes their analyzed content.
- For idle small talk after a long absence, it is okay to casually pop up, but do not force a task.
- Current trigger: {trigger}
- Hours since your last visible group message: {idle_hours:.1f}
- Chat id: {chat_id}

Recent group transcript:
{transcript}

Current message:
{current_sender}: {current_text}
[/WeChat proactive observer]"""


def run_openclaw(session_id: str, prompt: str) -> str | None:
    cmd = [
        OPENCLAW_BIN,
        "agent",
        "--session-id",
        session_id,
        "--message",
        prompt,
        "--timeout",
        str(OPENCLAW_TIMEOUT_SECONDS),
        "--json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=OPENCLAW_TIMEOUT_SECONDS + 30,
        )
    except subprocess.TimeoutExpired:
        # Proactive replies are optional. Dropping one is safer than repeatedly
        # invoking the same stuck request and increasing Gateway memory pressure.
        log(f"openclaw timed out after {OPENCLAW_TIMEOUT_SECONDS + 30}s; dropping candidate")
        return None
    except OSError as exc:
        log(f"openclaw launch failed: {exc!r}")
        return None
    if proc.returncode != 0:
        log(f"openclaw failed rc={proc.returncode} stderr={proc.stderr[-500:]}")
        return None
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        log(f"openclaw json parse failed: {exc!r} stdout={proc.stdout[:300]!r}")
        return None
    payloads = ((obj.get("result") or {}).get("payloads") or [])
    texts = [clean_text(item.get("text") or "") for item in payloads if isinstance(item, dict)]
    texts = [text for text in texts if text]
    return "\n".join(texts).strip() if texts else None


def should_suppress_reply(text: str) -> bool:
    if not text:
        return True
    normalized = clean_text(text)
    if normalized.upper() == "NO_REPLY":
        return True
    if len(normalized) > 120:
        return True
    return False


def process_candidate(state: dict, chat_id: str, msg: dict, trigger: str, idle_hours: float) -> bool:
    now = time.time()
    last_proactive = float(state.setdefault("lastProactiveAt", {}).get(chat_id, 0))
    cooldown = IDLE_PROACTIVE_COOLDOWN_SECONDS if trigger == "idle-smalltalk" else PROACTIVE_COOLDOWN_SECONDS
    if now - last_proactive < cooldown:
        log(f"skip cooldown chat={chat_id} trigger={trigger}")
        return False
    if daily_count_for(state, chat_id) >= MAX_PROACTIVE_PER_DAY:
        log(f"skip daily limit chat={chat_id} trigger={trigger}")
        return False
    session_id = extract_session_id(chat_id)
    if not session_id:
        log(f"skip no session chat={chat_id}")
        return False

    prompt = build_observer_prompt(chat_id, trigger, msg, state.setdefault("buffers", {}).get(chat_id, []), idle_hours)
    reply = run_openclaw(session_id, prompt)
    if should_suppress_reply(reply or ""):
        log(f"model chose no reply chat={chat_id} trigger={trigger} reply={(reply or '')[:80]!r}")
        return False

    result = api_post("/api/messages/send", {"chatId": chat_id, "text": reply})
    state["lastProactiveAt"][chat_id] = now
    state.setdefault("lastBotAt", {})[chat_id] = now
    bump_daily_count(state, chat_id)
    log(f"proactive sent chat={chat_id} trigger={trigger} result={result} text={reply[:80]!r}")
    return True


def initialize_chat(state: dict, chat_id: str, messages: list[dict], now: float) -> None:
    if not messages:
        state.setdefault("lastBotAt", {})[chat_id] = now
        state.setdefault("highWater", {})[chat_id] = 0
        return
    max_id = max(int(m.get("localId") or 0) for m in messages)
    state.setdefault("highWater", {})[chat_id] = max_id
    last_self_id = None
    for msg in messages:
        if msg.get("isSelf") is True:
            last_self_id = int(msg.get("localId") or 0)
    # Conservative startup: if recent history does not show a bot message, start
    # the 72h idle clock now rather than popping up from old backlog.
    state.setdefault("lastBotAt", {})[chat_id] = now if last_self_id is None else now
    for msg in messages[-BUFFER_SIZE:]:
        append_buffer(state, chat_id, msg)


def process_chat(state: dict, chat_id: str, now: float) -> None:
    encoded = urllib.parse.quote(chat_id, safe="")
    messages = api_get(f"/api/messages/{encoded}?limit={MESSAGE_LIMIT}")
    messages = sorted(messages, key=lambda m: int(m.get("localId") or 0))
    if not state.get("initialized"):
        initialize_chat(state, chat_id, messages, now)
        return
    high_water = int(state.setdefault("highWater", {}).get(chat_id, 0))
    max_id = max([int(m.get("localId") or 0) for m in messages] or [high_water])
    for msg in messages:
        local_id = int(msg.get("localId") or 0)
        if local_id <= high_water:
            continue
        if msg.get("isSelf") is True:
            state.setdefault("lastBotAt", {})[chat_id] = now
            continue
        append_buffer(state, chat_id, msg)
        if is_mentioned(msg):
            continue
        last_bot = float(state.setdefault("lastBotAt", {}).get(chat_id, now))
        idle_hours = max(0.0, (now - last_bot) / 3600)
        trigger = None
        if weak_bot_mention(msg):
            trigger = "weak-bot-mention"
        elif is_help_like(msg):
            trigger = "help-like"
        elif now - last_bot >= IDLE_TRIGGER_SECONDS and is_idle_chat_candidate(msg):
            trigger = "idle-smalltalk"
        if trigger:
            process_candidate(state, chat_id, msg, trigger, idle_hours)
    state["highWater"][chat_id] = max(high_water, max_id)


def main() -> None:
    if not CHAT_IDS:
        raise SystemExit("CHAT_IDS is required. Configure /etc/openclaw-wechat-channel.env.")
    log(
        "starting proactive observer "
        f"chats={CHAT_IDS} interval={INTERVAL_SECONDS}s idle={IDLE_TRIGGER_SECONDS}s "
        f"cooldown={PROACTIVE_COOLDOWN_SECONDS}s idleCooldown={IDLE_PROACTIVE_COOLDOWN_SECONDS}s"
    )
    while True:
        state = load_state()
        now = time.time()
        try:
            for chat_id in CHAT_IDS:
                process_chat(state, chat_id, now)
            if not state.get("initialized"):
                state["initialized"] = True
                log("initialized high water marks")
            save_state(state)
        except Exception as exc:
            log(f"error: {exc!r}")
            save_state(state)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
