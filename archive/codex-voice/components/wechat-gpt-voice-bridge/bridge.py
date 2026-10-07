#!/usr/bin/env python3
"""Private group-call audio bridge for the WeChat desktop container."""

import argparse
import asyncio
import base64
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import signal
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import websockets

import pulse_audio as pulse

INPUT_RATE = 48_000
INPUT_SAMPLES = 480
INPUT_BYTES = INPUT_SAMPLES * 2
OUTPUT_RATE = 48_000
OUTPUT_SAMPLES = 480
REQUEST_DIR = Path(os.environ.get("WECHAT_GPT_VOICE_REQUEST_DIR", "/var/lib/wechat-gpt-voice-bridge/requests"))
REQUEST_MAX_AGE_SECONDS = 180
OPENING_REQUEST_MAX_CHARS = 1_000
OPENING_AUDIO_WAIT_SECONDS = 12
VOICE_STATE_FILE = Path(os.environ.get("WECHAT_GPT_VOICE_STATE_FILE", "/var/lib/wechat-gpt-voice-bridge/voice-rotation.json"))
VOICE_ROTATION = tuple(
    voice.strip()
    for voice in os.environ.get(
        "WECHAT_GPT_VOICE_ROTATION",
        "juniper,maple,spruce,ember,breeze,arbor,sol,cove",
    ).split(",")
    if voice.strip()
)


@dataclass(frozen=True)
class Config(pulse.Config):
    gateway_url: str
    bridge_token: str


def start_request_path(chat_id: str, request_dir: Path = REQUEST_DIR) -> Path:
    name = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()
    return request_dir / f"{name}.json"


def read_start_request(chat_id: str, request_dir: Path = REQUEST_DIR, *, consume: bool = True) -> str | None:
    path = start_request_path(chat_id, request_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        request = json.loads(raw)
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return None
    if not isinstance(request, dict) or request.get("chatId") != chat_id:
        path.unlink(missing_ok=True)
        return None
    created_at = request.get("createdAt")
    if not isinstance(created_at, (int, float)) or created_at > time.time() + 30 or time.time() - created_at > REQUEST_MAX_AGE_SECONDS:
        path.unlink(missing_ok=True)
        return None
    opening_request = request.get("openingRequest")
    if not isinstance(opening_request, str):
        path.unlink(missing_ok=True)
        return None
    opening_request = " ".join(opening_request.split()).strip()
    if not opening_request or len(opening_request) > OPENING_REQUEST_MAX_CHARS or "\0" in opening_request:
        path.unlink(missing_ok=True)
        return None
    if consume:
        path.unlink(missing_ok=True)
    return opening_request


def next_voice(voices: tuple[str, ...] = VOICE_ROTATION, state_file: Path = VOICE_STATE_FILE) -> str:
    if not voices:
        raise RuntimeError("WECHAT_GPT_VOICE_ROTATION must contain at least one voice.")
    state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_file.parent, 0o700)
    lock_path = state_file.with_suffix(state_file.suffix + ".lock")
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            index = int(state.get("nextIndex", 0)) if isinstance(state, dict) else 0
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            index = 0
        if index < 0:
            index = 0
        voice = voices[index % len(voices)]
        temporary = state_file.with_name(f".{state_file.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"nextIndex": index + 1, "lastVoice": voice}, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, state_file)
        finally:
            temporary.unlink(missing_ok=True)
        return voice


def read_config() -> Config:
    gateway_url = os.environ.get("VOICE_GATEWAY_URL", "").rstrip("/")
    bridge_token = os.environ.get("VOICE_GATEWAY_BRIDGE_TOKEN", "")
    if not gateway_url.startswith(("ws://", "wss://")) or not bridge_token:
        raise RuntimeError("VOICE_GATEWAY_URL and VOICE_GATEWAY_BRIDGE_TOKEN must be configured.")
    return Config(
        gateway_url=gateway_url,
        bridge_token=bridge_token,
        container=os.environ.get("WECHAT_CONTAINER", "agent-wechat"),
        call_wait_seconds=int(os.environ.get("WECHAT_CALL_WAIT_SECONDS", "90")),
    )


async def start_capture(config: Config) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        "docker",
        *pulse.pulse_args(
            config,
            "parec",
            f"--client-name={pulse.audio_client_name(config, 'capture')}",
            "--raw",
            f"--device={pulse.CALL_PLAYBACK_SINK}.monitor",
            "--format=s16le",
            "--rate=48000",
            "--channels=1",
            "--latency-msec=10",
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def start_playback(config: Config) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        "docker",
        *pulse.pulse_args(
            config,
            "pacat",
            f"--client-name={pulse.audio_client_name(config, 'playback')}",
            "--playback",
            f"--device={pulse.GPT_INJECT_SINK}",
            "--format=s16le",
            f"--rate={OUTPUT_RATE}",
            "--channels=1",
            "--latency-msec=10",
            interactive=True,
        ),
        stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def read_capture_frame(
    config: Config,
    route_ref: list[pulse.PulseRoute],
    capture_ref: list[asyncio.subprocess.Process | None],
) -> bytes | None:
    for attempt in range(pulse.CAPTURE_RESTART_ATTEMPTS + 1):
        capture = capture_ref[0]
        if not capture or not capture.stdout:
            raise RuntimeError("WeChat call audio capture is not running.")
        try:
            return await capture.stdout.readexactly(INPUT_BYTES)
        except asyncio.IncompleteReadError as error:
            if not await pulse.route_is_active_resilient(config, route_ref[0]):
                logging.info("WeChat call audio ended while capture was stopping.")
                return None
            if attempt >= pulse.CAPTURE_RESTART_ATTEMPTS:
                raise RuntimeError(
                    f"WeChat call audio capture repeatedly stopped with a partial {len(error.partial)}/{INPUT_BYTES}-byte frame."
                ) from error
            logging.warning(
                "WeChat call audio capture stopped with a partial %d/%d-byte frame. Restarting capture (%d/%d).",
                len(error.partial),
                INPUT_BYTES,
                attempt + 1,
                pulse.CAPTURE_RESTART_ATTEMPTS,
            )
            await pulse.stop_audio_processes(config, capture, role="capture")
            capture_ref[0] = await start_capture(config)
    raise RuntimeError("Unable to restart WeChat call audio capture.")


async def run_bridge(config: Config, chat_id: str) -> None:
    if not chat_id.endswith("@chatroom"):
        raise RuntimeError("Only group chatroom ids are accepted.")
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    request_path = start_request_path(chat_id)
    opening_request = read_start_request(chat_id, consume=False)
    voice = next_voice()
    bridge_started = time.monotonic()
    await pulse.ensure_pulse_routes(config)
    logging.info("Prewarming Codex Voice while waiting for WeChat call audio.")
    route_task = asyncio.create_task(pulse.wait_for_call_route(config))
    route = None
    route_ref: list[pulse.PulseRoute] = []
    capture_ref: list[asyncio.subprocess.Process | None] = [None]
    playback = None
    receive_task = None
    try:
        uri = f"{config.gateway_url}/api/bridges/wechat/live"
        ssl_context = ssl.create_default_context() if uri.startswith("wss://") else None
        async with websockets.connect(uri, additional_headers={"Authorization": f"Bearer {config.bridge_token}"}, ssl=ssl_context, max_size=256 * 1024, ping_interval=20, ping_timeout=20) as socket:
            await socket.send(json.dumps({"type": "start", "conversationKey": f"wechat:group:{chat_id}", "voice": voice}))
            logging.info("Selected rotating Codex Voice %s for this call.", voice)
            started = asyncio.Event()
            first_output_audio = asyncio.Event()
            first_input_audio = asyncio.Event()
            first_user_transcript = asyncio.Event()
            first_output_at: list[float | None] = [None]
            stop = asyncio.Event()

            async def send_audio() -> None:
                while not stop.is_set():
                    frame = await read_capture_frame(config, route_ref, capture_ref)
                    if frame is None:
                        return
                    await socket.send(json.dumps({"type": "audio", "audio": {"data": base64.b64encode(frame).decode("ascii"), "sampleRate": INPUT_RATE, "numChannels": 1, "samplesPerChannel": INPUT_SAMPLES}}, separators=(",", ":")))
                    if not first_input_audio.is_set():
                        first_input_audio.set()
                        logging.info(
                            "First WeChat input audio sent %.2fs after first Voice output.",
                            time.monotonic() - (first_output_at[0] or bridge_started),
                        )

            async def receive_audio() -> None:
                user_turn_number = 0
                user_turn_started_at: float | None = None
                pending_user_turn: int | None = None
                pending_user_done_at: float | None = None
                assistant_delta_logged_for: int | None = None
                async for raw in socket:
                    event = json.loads(raw)
                    kind = event.get("type")
                    if kind == "session.started":
                        started.set()
                        logging.info(
                            "Codex Voice prewarmed for group %s after %.2fs.",
                            chat_id,
                            time.monotonic() - bridge_started,
                        )
                    elif kind == "audio.delta":
                        audio = event.get("audio", {})
                        sample_rate = audio.get("sampleRate")
                        samples = audio.get("samplesPerChannel")
                        if sample_rate != OUTPUT_RATE or audio.get("numChannels") != 1 or samples != OUTPUT_SAMPLES:
                            raise RuntimeError(
                                "Gateway returned an unexpected audio format: "
                                f"{sample_rate} Hz, {audio.get('numChannels')} channels, {samples} samples"
                            )
                        frame = base64.b64decode(audio.get("data", ""), validate=True)
                        if len(frame) != samples * 2:
                            raise RuntimeError("Gateway returned an invalid audio frame.")
                        if not playback or not playback.stdin:
                            continue
                        if not first_output_audio.is_set():
                            first_output_audio.set()
                            first_output_at[0] = time.monotonic()
                            logging.info(
                                "First Codex Voice audio arrived after %.2fs.",
                                time.monotonic() - bridge_started,
                            )
                        playback.stdin.write(frame)
                        await playback.stdin.drain()
                    elif kind == "transcript.delta" and event.get("delta"):
                        role = event.get("role")
                        now = time.monotonic()
                        if role == "user":
                            if user_turn_started_at is None:
                                user_turn_number += 1
                                user_turn_started_at = now
                                logging.info(
                                    "Realtime user turn %d transcript started %.2fs after first Voice output.",
                                    user_turn_number,
                                    now - (first_output_at[0] or bridge_started),
                                )
                            if not first_user_transcript.is_set():
                                first_user_transcript.set()
                                logging.info(
                                    "First user transcript delta arrived %.2fs after first Voice output.",
                                    now - (first_output_at[0] or bridge_started),
                                )
                        elif (
                            role == "assistant"
                            and pending_user_turn is not None
                            and pending_user_done_at is not None
                            and assistant_delta_logged_for != pending_user_turn
                        ):
                            assistant_delta_logged_for = pending_user_turn
                            logging.info(
                                "Realtime assistant response to user turn %d started %.2fs after user transcript completion.",
                                pending_user_turn,
                                now - pending_user_done_at,
                            )
                    elif kind == "transcript.done":
                        role = str(event.get("role") or "unknown")
                        text = str(event.get("text") or "")
                        now = time.monotonic()
                        logging.info(
                            "Realtime %s transcript completed %.2fs after first Voice output (%d chars).",
                            role,
                            now - (first_output_at[0] or bridge_started),
                            len(text),
                        )
                        if role == "user":
                            if user_turn_started_at is None:
                                user_turn_number += 1
                            else:
                                logging.info(
                                    "Realtime user turn %d transcript completed %.2fs after its first delta.",
                                    user_turn_number,
                                    now - user_turn_started_at,
                                )
                            pending_user_turn = user_turn_number
                            pending_user_done_at = now
                            user_turn_started_at = None
                        elif role == "assistant" and pending_user_turn is not None and pending_user_done_at is not None:
                            logging.info(
                                "Realtime assistant response to user turn %d completed %.2fs after user transcript completion.",
                                pending_user_turn,
                                now - pending_user_done_at,
                            )
                            pending_user_turn = None
                            pending_user_done_at = None
                    elif kind == "session.error":
                        if event.get("recoverable") is True:
                            logging.warning(
                                "Recoverable Gateway realtime warning: %s",
                                event.get("message") or "unspecified warning",
                            )
                            continue
                        detail = str(event.get("message") or "unknown realtime error")[:300]
                        raise RuntimeError(f"Gateway realtime session failed: {detail}")
                    elif kind == "session.closed":
                        return

            async def watch_call_end() -> None:
                missing_since = None
                while True:
                    current_route = route_ref[0]
                    if await pulse.route_is_active_resilient(config, current_route):
                        missing_since = None
                        await asyncio.sleep(1)
                        continue

                    replacement = await pulse.find_wechat_route(config)
                    if replacement:
                        logging.info("WeChat call audio route changed; reconnecting PulseAudio route.")
                        route_ref[0] = await pulse.move_to_bridge_resilient(config, replacement)
                        missing_since = None
                        await asyncio.sleep(1)
                        continue

                    if missing_since is None:
                        missing_since = time.monotonic()
                    if time.monotonic() - missing_since >= pulse.CALL_ROUTE_MISSING_GRACE_SECONDS:
                        logging.info("WeChat call audio ended.")
                        return
                    await asyncio.sleep(0.5)

            receive_task = asyncio.create_task(receive_audio())
            started_task = asyncio.create_task(started.wait())
            try:
                done, _ = await asyncio.wait((started_task, receive_task), timeout=60, return_when=asyncio.FIRST_COMPLETED)
                if receive_task in done:
                    receive_task.result()
                    raise RuntimeError("Gateway closed before the realtime session started.")
                if started_task not in done:
                    raise TimeoutError("Timed out waiting for the Gateway realtime session.")

                done, _ = await asyncio.wait((route_task, receive_task), return_when=asyncio.FIRST_COMPLETED)
                if receive_task in done:
                    receive_task.result()
                    raise RuntimeError("Gateway closed before WeChat call audio became ready.")
                route = route_task.result()
                logging.info("WeChat call audio became ready after %.2fs.", time.monotonic() - bridge_started)
                route = await pulse.move_to_bridge_resilient(config, route)
                route_ref.append(route)
                playback = await start_playback(config)

                if opening_request:
                    await socket.send(json.dumps({"type": "opening", "text": opening_request}, ensure_ascii=False))
                    request_path.unlink(missing_ok=True)
                    logging.info("Queued the one-time Voice opening; microphone forwarding is held until speech starts.")
                    try:
                        await asyncio.wait_for(first_output_audio.wait(), OPENING_AUDIO_WAIT_SECONDS)
                    except TimeoutError:
                        logging.warning("Voice opening audio did not start within %ss; enabling microphone forwarding.", OPENING_AUDIO_WAIT_SECONDS)

                capture_ref[0] = await start_capture(config)
                logging.info("WeChat microphone forwarding started after %.2fs.", time.monotonic() - bridge_started)
                send_task = asyncio.create_task(send_audio())
                watch_task = asyncio.create_task(watch_call_end())
                done, pending = await asyncio.wait((receive_task, send_task, watch_task), return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()
            finally:
                stop.set()
                started_task.cancel()
                receive_task.cancel()
                await asyncio.gather(started_task, receive_task, return_exceptions=True)
                with contextlib.suppress(Exception):
                    await socket.send(json.dumps({"type": "stop"}))
    finally:
        route_task.cancel()
        await asyncio.gather(route_task, return_exceptions=True)
        try:
            await pulse.stop_audio_processes(config, capture_ref[0], playback)
        finally:
            if route:
                await pulse.restore_route(config, route_ref[0] if route_ref else route)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge an active WeChat group call to private Codex Voice.")
    parser.add_argument("--chat-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_bridge(read_config(), args.chat_id.strip()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except asyncio.CancelledError:
        logging.info("WeChat GPT Voice bridge stopped by service shutdown.")
        sys.exit(0)
    except Exception as error:
        logging.error("WeChat GPT Voice bridge stopped: %s: %s", type(error).__name__, error)
        sys.exit(1)
