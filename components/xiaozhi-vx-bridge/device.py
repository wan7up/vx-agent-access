#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


APP_NAME = "py-xiaozhi"
APP_VERSION = "2.1.2"
BOARD_TYPE = "bread-compact-wifi"
OTA_URL = "https://api.tenclass.net/xiaozhi/ota/"


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def generated_device_id() -> str:
    octets = bytearray(secrets.token_bytes(6))
    octets[0] = (octets[0] | 0x02) & 0xFE
    return ":".join(f"{value:02x}" for value in octets)


def create_identity(hostname: str, machine_id: str, device_id: str | None = None) -> dict[str, Any]:
    device_id = device_id or generated_device_id()
    mac_clean = device_id.replace(":", "").lower()
    short_hash = hashlib.md5(mac_clean.encode()).hexdigest()[:8].upper()
    return {
        "device_id": device_id,
        "client_id": str(uuid4()),
        "serial_number": f"SN-{short_hash}-{mac_clean}",
        "hmac_key": hashlib.sha256(f"{hostname}||{device_id}||{machine_id}".encode()).hexdigest(),
        "activated": False,
        "network": {},
    }


def load_or_create_identity(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    identity = fresh_identity()
    atomic_write_json(path, identity)
    return identity


def fresh_identity() -> dict[str, Any]:
    machine_id_path = Path("/etc/machine-id")
    machine_id = machine_id_path.read_text(encoding="utf-8").strip() if machine_id_path.exists() else socket.gethostname()
    return create_identity(socket.gethostname(), machine_id)


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("8.8.8.8", 80))
            return str(connection.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float = 10) -> tuple[int, dict[str, Any]]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"message": body[:300]}
        return error.code, parsed


def ota(identity: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Device-Id": identity["device_id"],
        "Client-Id": identity["client_id"],
        "Content-Type": "application/json",
        "User-Agent": f"{BOARD_TYPE}/{APP_NAME}-{APP_VERSION}",
        "Accept-Language": "zh-CN",
        "Activation-Version": APP_VERSION,
    }
    payload = {
        "application": {"version": APP_VERSION, "elf_sha256": identity["hmac_key"]},
        "board": {
            "type": BOARD_TYPE,
            "name": APP_NAME,
            "ip": local_ip(),
            "mac": identity["device_id"],
        },
    }
    status, data = post_json(OTA_URL, headers, payload)
    if status != 200:
        raise RuntimeError(f"OTA request failed with HTTP {status}: {data.get('message', 'unknown error')}")
    websocket = data.get("websocket") or {}
    identity["network"] = {
        "websocket_url": websocket.get("url"),
        "websocket_token": websocket.get("token"),
    }
    return data


def activation_payload(identity: dict[str, Any], challenge: str) -> dict[str, Any]:
    signature = hmac.new(identity["hmac_key"].encode(), challenge.encode(), hashlib.sha256).hexdigest()
    return {
        "Payload": {
            "algorithm": "hmac-sha256",
            "serial_number": identity["serial_number"],
            "challenge": challenge,
            "hmac": signature,
        }
    }


def activation_headers(identity: dict[str, Any]) -> dict[str, str]:
    return {
        "Activation-Version": "2",
        "Device-Id": identity["device_id"],
        "Client-Id": identity["client_id"],
        "Content-Type": "application/json",
    }


def pcm_frames(pcm: bytes) -> list[bytes]:
    frame_bytes = 16000 * 2 * 20 // 1000
    if not 16000 <= len(pcm) <= 16000 * 2 * 30 or len(pcm) % 2:
        raise ValueError("Probe PCM must be 0.5-30s of 16 kHz mono PCM16.")
    return [
        pcm[offset : offset + frame_bytes].ljust(frame_bytes, b"\x00")
        for offset in range(0, len(pcm), frame_bytes)
    ]


def prepare(path: Path) -> int:
    identity = load_or_create_identity(path)
    response = ota(identity)
    activation = response.get("activation")
    if activation:
        identity["pending_activation"] = {
            "challenge": activation.get("challenge"),
            "code": activation.get("code"),
            "message": activation.get("message"),
        }
        identity["activated"] = False
        print(f"ACTIVATION_CODE={activation.get('code', '')}", flush=True)
        print(str(activation.get("message") or "请登录 xiaozhi.me 添加设备并输入验证码"), flush=True)
    else:
        identity["activated"] = True
        identity.pop("pending_activation", None)
        print("DEVICE_ACTIVATED=yes", flush=True)
    atomic_write_json(path, identity)
    return 0


def renew_unbound_identity(path: Path) -> int:
    previous = load_or_create_identity(path)
    if previous.get("activated"):
        raise RuntimeError("Refusing to replace an activated device identity.")
    replacement = fresh_identity()
    response = ota(replacement)
    activation = response.get("activation")
    if not activation or not activation.get("code") or not activation.get("challenge"):
        raise RuntimeError("Replacement device did not receive an activation code.")
    replacement["pending_activation"] = {
        "challenge": activation["challenge"],
        "code": activation["code"],
        "message": activation.get("message"),
    }
    atomic_write_json(path, replacement)
    print(f"ACTIVATION_CODE={activation['code']}", flush=True)
    return 0


def activate(path: Path, wait_seconds: int) -> int:
    identity = load_or_create_identity(path)
    response = ota(identity)
    activation = response.get("activation")
    if not activation:
        identity["activated"] = True
        identity.pop("pending_activation", None)
        atomic_write_json(path, identity)
        print("DEVICE_ACTIVATED=yes", flush=True)
        return 0

    challenge = str(activation.get("challenge") or "")
    code = str(activation.get("code") or "")
    if not challenge or not code:
        raise RuntimeError("OTA response did not contain a complete activation challenge.")
    identity["pending_activation"] = {
        "challenge": challenge,
        "code": code,
        "message": activation.get("message"),
    }
    atomic_write_json(path, identity)
    print(f"ACTIVATION_CODE={code}", flush=True)
    print(str(activation.get("message") or "请登录 xiaozhi.me 添加设备并输入验证码"), flush=True)

    deadline = time.monotonic() + wait_seconds
    payload = activation_payload(identity, challenge)
    url = f"{OTA_URL.rstrip('/')}/activate"
    while time.monotonic() < deadline:
        try:
            status, _ = post_json(url, activation_headers(identity), payload)
        except (URLError, TimeoutError, OSError) as error:
            print(f"activation request {type(error).__name__}; retrying", file=sys.stderr, flush=True)
            time.sleep(5)
            continue
        if status == 200:
            identity["activated"] = True
            identity.pop("pending_activation", None)
            atomic_write_json(path, identity)
            print("DEVICE_ACTIVATED=yes", flush=True)
            return 0
        if status != 202:
            print(f"activation HTTP {status}; retrying", file=sys.stderr, flush=True)
        time.sleep(5)
    print("Activation timed out.", file=sys.stderr)
    return 2


async def probe_websocket(
    path: Path,
    listen_seconds: int,
    pcm_file: Path | None = None,
    listen_mode: str = "auto",
    tail_seconds: float = 2.0,
    turns: int = 1,
) -> int:
    import websockets

    if not 0 <= tail_seconds <= 5:
        raise ValueError("Probe tail silence must be between 0 and 5 seconds.")
    if not 1 <= turns <= 3:
        raise ValueError("Probe turns must be between 1 and 3.")
    frames = pcm_frames(pcm_file.read_bytes()) if pcm_file else None
    if frames:
        import opuslib

    identity = load_or_create_identity(path)
    response = ota(identity)
    if response.get("activation"):
        raise RuntimeError("Device is not activated.")
    network = identity.get("network") or {}
    url = network.get("websocket_url")
    token = network.get("websocket_token")
    if not url or not token:
        raise RuntimeError("OTA response did not provide WebSocket credentials.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Protocol-Version": "1",
        "Device-Id": identity["device_id"],
        "Client-Id": identity["client_id"],
    }
    async with websockets.connect(
        url,
        additional_headers=headers,
        open_timeout=10,
        close_timeout=5,
        ping_interval=20,
        ping_timeout=20,
        max_size=10 * 1024 * 1024,
        compression=None,
    ) as connection:
        await connection.send(json.dumps({
            "type": "hello",
            "version": 1,
            "features": {"mcp": False},
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 20,
            },
        }))
        raw = await asyncio.wait_for(connection.recv(), timeout=10)
        if not isinstance(raw, str):
            raise RuntimeError("Expected JSON server hello.")
        hello = json.loads(raw)
        if hello.get("type") != "hello" or hello.get("transport") != "websocket":
            raise RuntimeError(f"Unexpected server hello: {hello.get('type')}")
        session_id = str(hello.get("session_id") or "")
        print(f"WSS_HELLO_OK=yes session={session_id[:8] or 'none'}", flush=True)
        if frames or listen_seconds > 0:
            if frames:
                encoder = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
                output_rate = (hello.get("audio_params") or {}).get("sample_rate", 24000)
                if output_rate not in (16000, 24000, 48000):
                    raise RuntimeError("Unexpected server Opus output sample rate.")
                decoder = opuslib.Decoder(output_rate, 1)
                for turn in range(1, turns + 1):
                    await connection.send(json.dumps({
                        "session_id": session_id,
                        "type": "listen",
                        "state": "start",
                        "mode": listen_mode,
                    }))
                    for frame in frames:
                        await connection.send(encoder.encode(frame, 320))
                        await asyncio.sleep(0.02)
                    if listen_mode == "manual":
                        await connection.send(json.dumps({
                            "session_id": session_id,
                            "type": "listen",
                            "state": "stop",
                        }))
                    else:
                        for _ in range(round(tail_seconds / 0.02)):
                            await connection.send(encoder.encode(bytes(640), 320))
                            await asyncio.sleep(0.02)
                    print(
                        f"PROBE_TURN={turn} INPUT_SECONDS="
                        f"{(len(frames) + (round(tail_seconds / 0.02) if listen_mode != 'manual' else 0)) * 0.02:.2f}",
                        flush=True,
                    )

                    deadline = time.monotonic() + 35
                    audio_samples = 0
                    saw_tts_stop = False
                    while time.monotonic() < deadline:
                        try:
                            message = await asyncio.wait_for(connection.recv(), timeout=max(0.1, deadline - time.monotonic()))
                        except asyncio.TimeoutError:
                            break
                        if isinstance(message, bytes):
                            decoded = decoder.decode(message, output_rate * 120 // 1000)
                            audio_samples += len(decoded) // 2
                        else:
                            event = json.loads(message)
                            kind = event.get("type")
                            state = event.get("state")
                            if kind in ("stt", "tts"):
                                text = str(event.get("text") or "")[:200]
                                print(f"EVENT turn={turn} type={kind} state={state or ''} text={text}", flush=True)
                            else:
                                print(f"EVENT turn={turn} type={kind} state={state or ''}", flush=True)
                            if kind == "tts" and state == "stop":
                                saw_tts_stop = True
                                break
                    print(f"PROBE_TURN={turn} OUTPUT_SECONDS={audio_samples / output_rate:.2f}", flush=True)
                    if not saw_tts_stop or not audio_samples:
                        raise RuntimeError(f"Xiaozhi did not complete an audible response to probe turn {turn}.")
                return 0
            await connection.send(json.dumps({
                "session_id": session_id,
                "type": "listen",
                "state": "start",
                "mode": listen_mode,
            }))
            try:
                while True:
                    message = await asyncio.wait_for(connection.recv(), timeout=listen_seconds)
                    if isinstance(message, str):
                        event = json.loads(message)
                        print(f"EVENT type={event.get('type')} state={event.get('state', '')}", flush=True)
            except asyncio.TimeoutError:
                pass
        return 0


def show(path: Path) -> int:
    identity = load_or_create_identity(path)
    print(json.dumps({
        "device_id": identity.get("device_id"),
        "client_id": identity.get("client_id"),
        "serial_number": identity.get("serial_number"),
        "activated": identity.get("activated", False),
        "has_websocket_credentials": bool((identity.get("network") or {}).get("websocket_url")),
        "pending_code": (identity.get("pending_activation") or {}).get("code"),
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal Xiaozhi virtual-device compatibility client")
    parser.add_argument("--state", type=Path, default=Path("/var/lib/xiaozhi-vx-bridge/device.json"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare")
    subparsers.add_parser("renew-unbound")
    activation = subparsers.add_parser("activate")
    activation.add_argument("--wait", type=int, default=600)
    probe = subparsers.add_parser("probe")
    probe.add_argument("--listen-seconds", type=int, default=0)
    probe.add_argument("--pcm-file", type=Path)
    probe.add_argument("--listen-mode", choices=("auto", "manual", "realtime"), default="auto")
    probe.add_argument("--tail-seconds", type=float, default=2.0)
    probe.add_argument("--turns", type=int, default=1)
    subparsers.add_parser("show")
    args = parser.parse_args()

    try:
        if args.command == "prepare":
            return prepare(args.state)
        if args.command == "renew-unbound":
            return renew_unbound_identity(args.state)
        if args.command == "activate":
            return activate(args.state, args.wait)
        if args.command == "probe":
            return asyncio.run(probe_websocket(
                args.state, args.listen_seconds, args.pcm_file, args.listen_mode, args.tail_seconds, args.turns
            ))
        return show(args.state)
    except (RuntimeError, ValueError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
