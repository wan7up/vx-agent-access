#!/usr/bin/env python3
"""Bridge one Linux vx call to an activated Xiaozhi virtual device."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import struct
from dataclasses import dataclass
from pathlib import Path
import sys
import time

import opuslib
import websockets

import device

PULSE_BRIDGE_DIR = Path(os.environ.get(
    "WECHAT_PULSE_BRIDGE_DIR",
    "/opt/wechat-gpt-voice-bridge",
))
if not (PULSE_BRIDGE_DIR / "pulse_audio.py").is_file():
    PULSE_BRIDGE_DIR = Path(__file__).resolve().parents[1] / "wechat-gpt-voice-bridge"
sys.path.insert(0, str(PULSE_BRIDGE_DIR))
import pulse_audio as pulse


INPUT_RATE = 16000
INPUT_BYTES = 640
INPUT_SAMPLES = 320
LOCK_FILE = Path("/run/lock/xiaozhi-vx-bridge.lock")
STATE_FILE = Path(os.environ.get(
    "XIAOZHI_DEVICE_STATE",
    "/var/lib/xiaozhi-vx-bridge/device.json",
))
MODE = os.environ.get("XIAOZHI_LISTEN_MODE", "realtime")
WAKE_WORD = "你好"


@dataclass
class TtsAudioMetrics:
    started_at: float
    frames: int = 0
    samples: int = 0
    above_floor_frames: int = 0
    peak_mean_abs: int = 0
    sentences: int = 0
    first_frame_at: float | None = None
    last_frame_at: float | None = None
    max_gap_seconds: float = 0

    def record(self, sample_count: int, mean_abs: int, now: float) -> None:
        if self.first_frame_at is None:
            self.first_frame_at = now
        if self.last_frame_at is not None:
            self.max_gap_seconds = max(self.max_gap_seconds, now - self.last_frame_at)
        self.last_frame_at = now
        self.frames += 1
        self.samples += sample_count
        self.above_floor_frames += mean_abs > 100
        self.peak_mean_abs = max(self.peak_mean_abs, mean_abs)


@dataclass
class MicAudioMetrics:
    frames: int = 0
    above_floor_frames: int = 0
    peak_mean_abs: int = 0
    last_frame_at: float | None = None

    def record(self, mean_abs: int, now: float) -> None:
        self.frames += 1
        self.above_floor_frames += mean_abs > 100
        self.peak_mean_abs = max(self.peak_mean_abs, mean_abs)
        self.last_frame_at = now


def pcm_mean_abs(pcm: bytes) -> int:
    if not pcm:
        return 0
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return sum(abs(sample) for sample in samples) // len(samples)


def listen_start(session_id: str) -> str:
    return json.dumps(
        {"session_id": session_id, "type": "listen", "state": "start", "mode": MODE},
        separators=(",", ":"),
    )


async def start_voice_with_greeting(connection: websockets.ClientConnection, session_id: str) -> None:
    await connection.send(listen_start(session_id))
    await connection.send(json.dumps(
        {"session_id": session_id, "type": "listen", "state": "detect", "text": WAKE_WORD},
        separators=(",", ":"),
    ))


async def start_capture(config: pulse.Config) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        "docker",
        *pulse.pulse_args(
            config, "parec", f"--client-name={pulse.audio_client_name(config, 'capture')}", "--raw",
            f"--device={pulse.CALL_PLAYBACK_SINK}.monitor",
            "--format=s16le", f"--rate={INPUT_RATE}", "--channels=1", "--latency-msec=20",
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def start_playback(config: pulse.Config, output_rate: int) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        "docker",
        *pulse.pulse_args(
            config, "pacat", f"--client-name={pulse.audio_client_name(config, 'playback')}",
            "--playback", f"--device={pulse.GPT_INJECT_SINK}",
            "--format=s16le", f"--rate={output_rate}", "--channels=1", "--latency-msec=20",
            interactive=True,
        ),
        stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def send_microphone(
    connection: websockets.ClientConnection,
    config: pulse.Config,
    route_ref: list[pulse.PulseRoute],
    capture_ref: list[asyncio.subprocess.Process | None],
    metrics: MicAudioMetrics | None = None,
) -> None:
    encoder = opuslib.Encoder(INPUT_RATE, 1, opuslib.APPLICATION_VOIP)
    restarts = 0
    while True:
        capture = capture_ref[0]
        if capture is None or capture.stdout is None:
            raise RuntimeError("Call capture is unavailable.")
        try:
            frame = await capture.stdout.readexactly(INPUT_BYTES)
        except asyncio.IncompleteReadError as error:
            if not await pulse.route_is_active_resilient(config, route_ref[0]):
                logging.info("Call capture ended with the call.")
                return
            if restarts >= pulse.CAPTURE_RESTART_ATTEMPTS:
                raise RuntimeError("Call capture stopped repeatedly.") from error
            restarts += 1
            await pulse.stop_audio_processes(config, capture, role="capture")
            capture_ref[0] = await start_capture(config)
            logging.warning("Restarted call capture (%d/%d).", restarts, pulse.CAPTURE_RESTART_ATTEMPTS)
            continue
        restarts = 0
        await connection.send(encoder.encode(frame, INPUT_SAMPLES))
        if metrics is not None:
            metrics.record(pcm_mean_abs(frame), time.monotonic())


async def log_microphone_health(metrics: MicAudioMetrics) -> None:
    previous_frames = 0
    previous_above_floor = 0
    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        logging.info(
            "Call uplink 5s: frames=%d aboveFloor=%d peakMeanAbs=%d lastFrameAge=%.1fs.",
            metrics.frames - previous_frames,
            metrics.above_floor_frames - previous_above_floor,
            metrics.peak_mean_abs,
            now - metrics.last_frame_at if metrics.last_frame_at is not None else -1,
        )
        previous_frames = metrics.frames
        previous_above_floor = metrics.above_floor_frames


async def watch_call_end(config: pulse.Config, route_ref: list[pulse.PulseRoute]) -> None:
    missing_since: float | None = None
    while True:
        if await pulse.route_is_active_resilient(config, route_ref[0]):
            missing_since = None
            await asyncio.sleep(1)
            continue
        replacement = await pulse.find_wechat_route(config)
        if replacement:
            route_ref[0] = await pulse.move_to_bridge_resilient(config, replacement)
            missing_since = None
            logging.info("Reattached changed call audio route.")
            await asyncio.sleep(1)
            continue
        missing_since = missing_since or time.monotonic()
        if time.monotonic() - missing_since >= pulse.CALL_ROUTE_MISSING_GRACE_SECONDS:
            logging.info("Call audio route ended.")
            return
        await asyncio.sleep(0.5)


async def run_call(chat_id: str) -> None:
    if not chat_id.endswith("@chatroom"):
        raise ValueError("Only configured group chatroom ids are accepted.")
    if MODE not in ("auto", "realtime"):
        raise ValueError("XIAOZHI_LISTEN_MODE must be auto or realtime.")

    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    config = pulse.Config(os.environ.get("WECHAT_CONTAINER", "agent-wechat"), 90)
    identity = device.load_or_create_identity(STATE_FILE)
    await pulse.ensure_pulse_routes(config)
    route_task = asyncio.create_task(pulse.wait_for_call_route(config))
    route: pulse.PulseRoute | None = None
    route_ref: list[pulse.PulseRoute] = []
    capture_ref: list[asyncio.subprocess.Process | None] = [None]
    mic_metrics = MicAudioMetrics()
    playback: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Task] = []
    started = time.monotonic()
    try:
        response = await asyncio.to_thread(device.ota, identity)
        if response.get("activation"):
            raise RuntimeError("Xiaozhi device is not activated.")
        network = identity.get("network") or {}
        if not network.get("websocket_url") or not network.get("websocket_token"):
            raise RuntimeError("Xiaozhi OTA did not return WebSocket credentials.")
        headers = {
            "Authorization": f"Bearer {network['websocket_token']}",
            "Protocol-Version": "1",
            "Device-Id": identity["device_id"],
            "Client-Id": identity["client_id"],
        }
        async with websockets.connect(
            network["websocket_url"], additional_headers=headers,
            open_timeout=10, close_timeout=5, ping_interval=20, ping_timeout=20,
            max_size=10 * 1024 * 1024, compression=None,
        ) as connection:
            await connection.send(json.dumps({
                "type": "hello", "version": 1, "features": {"mcp": False},
                "transport": "websocket",
                "audio_params": {
                    "format": "opus", "sample_rate": INPUT_RATE,
                    "channels": 1, "frame_duration": 20,
                },
            }))
            raw = await asyncio.wait_for(connection.recv(), timeout=10)
            if not isinstance(raw, str):
                raise RuntimeError("Xiaozhi hello was not JSON.")
            hello = json.loads(raw)
            if hello.get("type") != "hello" or hello.get("transport") != "websocket":
                raise RuntimeError("Unexpected Xiaozhi hello.")
            session_id = str(hello.get("session_id") or "")
            output_rate = (hello.get("audio_params") or {}).get("sample_rate", 24000)
            if not session_id or output_rate not in (16000, 24000, 48000):
                raise RuntimeError("Xiaozhi hello did not negotiate a valid audio session.")
            decoder = opuslib.Decoder(output_rate, 1)
            logging.info("Xiaozhi voice connected in %.2fs.", time.monotonic() - started)

            async def receive() -> None:
                nonlocal playback
                turn: TtsAudioMetrics | None = None

                def report_turn(reason: str) -> None:
                    nonlocal turn
                    if turn is None:
                        return
                    logging.info(
                        "Xiaozhi TTS %s: frames=%d audio=%.2fs elapsed=%.2fs "
                        "firstFrame=%.2fs maxGap=%.2fs sentences=%d "
                        "aboveFloor=%d peakMeanAbs=%d playbackAlive=%s.",
                        reason, turn.frames, turn.samples / output_rate,
                        time.monotonic() - turn.started_at,
                        (turn.first_frame_at - turn.started_at) if turn.first_frame_at else -1,
                        turn.max_gap_seconds, turn.sentences,
                        turn.above_floor_frames, turn.peak_mean_abs,
                        playback is not None and playback.returncode is None,
                    )
                    turn = None

                try:
                    async for message in connection:
                        if isinstance(message, bytes):
                            pcm = decoder.decode(message, output_rate * 120 // 1000)
                            if playback and playback.stdin:
                                playback.stdin.write(pcm)
                                await playback.stdin.drain()
                            if turn is not None:
                                turn.record(len(pcm) // 2, pcm_mean_abs(pcm), time.monotonic())
                            continue
                        event = json.loads(message)
                        kind = event.get("type")
                        state = event.get("state")
                        if kind == "stt":
                            logging.info("User speech recognized (%d chars).", len(str(event.get("text") or "")))
                        elif kind == "tts":
                            if state == "start":
                                report_turn("superseded")
                                turn = TtsAudioMetrics(time.monotonic())
                                logging.info("Xiaozhi started speaking after %.2fs.", time.monotonic() - started)
                            elif state == "sentence_start" and turn is not None:
                                turn.sentences += 1
                            elif state == "stop":
                                report_turn("stop")
                                await connection.send(listen_start(session_id))
                        elif kind == "error":
                            raise RuntimeError("Xiaozhi reported a voice session error.")
                finally:
                    report_turn("connection-ended")
                raise RuntimeError("Xiaozhi voice connection closed during the call.")

            receive_task = asyncio.create_task(receive())
            tasks.append(receive_task)
            done, _ = await asyncio.wait((route_task, receive_task), return_when=asyncio.FIRST_COMPLETED)
            if receive_task in done:
                receive_task.result()
            route = route_task.result()
            logging.info("Call audio became ready after %.2fs.", time.monotonic() - started)
            route = await pulse.move_to_bridge_resilient(config, route)
            route_ref.append(route)
            playback = await start_playback(config, output_rate)
            capture_ref[0] = await start_capture(config)
            await start_voice_with_greeting(connection, session_id)
            logging.info("Call microphone forwarding started in %s mode.", MODE)
            logging.info("Requested Xiaozhi opening after call audio became ready.")
            tasks.extend([
                asyncio.create_task(send_microphone(connection, config, route_ref, capture_ref, mic_metrics)),
                asyncio.create_task(log_microphone_health(mic_metrics)),
                asyncio.create_task(watch_call_end(config, route_ref)),
            ])
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    finally:
        route_task.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(route_task, *tasks, return_exceptions=True)
        try:
            await pulse.stop_audio_processes(config, capture_ref[0], playback)
        finally:
            if route:
                await pulse.restore_route(config, route_ref[0] if route_ref else route)


def main() -> int:
    parser = argparse.ArgumentParser(description="Connect a vx group call to Xiaozhi voice.")
    parser.add_argument("--chat-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with LOCK_FILE.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A Xiaozhi call bridge is already running.") from None
        asyncio.run(run_call(args.chat_id.strip()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except asyncio.CancelledError:
        logging.info("Xiaozhi call bridge stopped by service shutdown.")
        raise SystemExit(0) from None
    except Exception as error:
        logging.error("Xiaozhi call bridge stopped: %s: %s", type(error).__name__, error)
        raise SystemExit(1) from None
