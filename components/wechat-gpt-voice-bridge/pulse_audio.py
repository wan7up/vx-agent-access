"""Shared PulseAudio routing for a Linux vx call and a voice backend."""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field


CALL_ROUTE_MISSING_GRACE_SECONDS = 10
CALL_PLAYBACK_SINK = "wechat_call_playback"
GPT_INJECT_SINK = "gpt_voice_inject"
GPT_MIC_SOURCE = "gpt_voice_mic"
PULSE_ENV = ("HOME=/home/wechat", "XDG_RUNTIME_DIR=/run/user/1000", "PULSE_SERVER=unix:/run/user/1000/pulse/native")
CAPTURE_RESTART_ATTEMPTS = 2
ROUTE_MOVE_ATTEMPTS = 8
ROUTE_MOVE_RETRY_SECONDS = 0.15
ROUTE_PROBE_ATTEMPTS = 2
ROUTE_PROBE_RETRY_SECONDS = 0.5


@dataclass(frozen=True)
class Config:
    container: str
    call_wait_seconds: int
    audio_owner: str = field(
        default_factory=lambda: os.environ.get("INVOCATION_ID") or uuid.uuid4().hex,
        kw_only=True,
    )


@dataclass(frozen=True)
class PulseRoute:
    sink_input_id: str
    source_output_id: str
    previous_sink: str
    previous_source: str


async def docker(config: Config, *args: str, allowed_returncodes: tuple[int, ...] = (0,)) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("Docker command timed out.")
    if process.returncode not in allowed_returncodes or (process.returncode != 0 and stderr):
        raise RuntimeError((stderr or stdout or b"Docker command failed.").decode("utf-8", "replace").strip()[:400])
    return stdout


def pulse_args(config: Config, *command: str, interactive: bool = False) -> tuple[str, ...]:
    return ("exec", *(("-i",) if interactive else ()), "-u", "wechat", config.container, "env", *PULSE_ENV, *command)


async def pactl(config: Config, *args: str) -> str:
    return (await docker(config, *pulse_args(config, "pactl", *args))).decode("utf-8", "replace")


async def pulse_snapshot(config: Config) -> dict:
    try:
        snapshot = json.loads(await pactl(config, "--format=json", "list"))
    except json.JSONDecodeError as error:
        raise RuntimeError("PulseAudio returned an invalid JSON snapshot.") from error
    if not isinstance(snapshot, dict) or any(
        not isinstance(snapshot.get(key), list)
        for key in ("sinks", "sources", "clients", "sink_inputs", "source_outputs")
    ):
        raise RuntimeError("PulseAudio returned an incomplete JSON snapshot.")
    return snapshot


async def ensure_pulse_routes(config: Config) -> None:
    snapshot = await pulse_snapshot(config)
    sinks = {sink.get("name") for sink in snapshot["sinks"] if isinstance(sink, dict)}
    if CALL_PLAYBACK_SINK not in sinks:
        await pactl(config, "load-module", "module-null-sink", f"sink_name={CALL_PLAYBACK_SINK}", "sink_properties=device.description=WeChatCallPlayback", "rate=48000", "channels=1")
    if GPT_INJECT_SINK not in sinks:
        await pactl(config, "load-module", "module-null-sink", f"sink_name={GPT_INJECT_SINK}", "sink_properties=device.description=GptVoiceInject", "rate=16000", "channels=1")
    sources = {source.get("name") for source in snapshot["sources"] if isinstance(source, dict)}
    if GPT_MIC_SOURCE not in sources:
        await pactl(config, "load-module", "module-remap-source", f"master={GPT_INJECT_SINK}.monitor", f"source_name={GPT_MIC_SOURCE}", "source_properties=device.description=GptVoiceMic")


async def find_wechat_route(config: Config) -> PulseRoute | None:
    snapshot = await pulse_snapshot(config)
    clients = {
        str(client["index"])
        for client in snapshot["clients"]
        if isinstance(client, dict)
        and "index" in client
        and isinstance(client.get("properties"), dict)
        and "wechat" in str(client["properties"].get("application.process.binary", "")).casefold()
    }
    sink = next((item for item in snapshot["sink_inputs"] if isinstance(item, dict) and str(item.get("client")) in clients), None)
    source = next((item for item in snapshot["source_outputs"] if isinstance(item, dict) and str(item.get("client")) in clients), None)
    if not sink or not source:
        return None
    try:
        return PulseRoute(
            sink_input_id=str(sink["index"]),
            source_output_id=str(source["index"]),
            previous_sink=str(sink["sink"]),
            previous_source=str(source["source"]),
        )
    except KeyError as error:
        raise RuntimeError("PulseAudio returned an incomplete call route.") from error


async def wait_for_call_route(config: Config) -> PulseRoute:
    deadline = time.monotonic() + config.call_wait_seconds
    while time.monotonic() < deadline:
        try:
            route = await find_wechat_route(config)
        except RuntimeError as error:
            logging.warning("Could not inspect the pending WeChat audio route; retrying: %s", error)
            await asyncio.sleep(ROUTE_PROBE_RETRY_SECONDS)
            continue
        if route:
            return route
        await asyncio.sleep(0.25)
    raise RuntimeError("Timed out waiting for an active WeChat call audio route.")


async def move_to_bridge(config: Config, route: PulseRoute) -> None:
    await pactl(config, "move-sink-input", route.sink_input_id, CALL_PLAYBACK_SINK)
    await pactl(config, "move-source-output", route.source_output_id, GPT_MIC_SOURCE)


def preserve_previous_route(previous: PulseRoute, replacement: PulseRoute) -> PulseRoute:
    return PulseRoute(
        sink_input_id=replacement.sink_input_id,
        source_output_id=replacement.source_output_id,
        previous_sink=(
            previous.previous_sink
            if replacement.sink_input_id == previous.sink_input_id
            else replacement.previous_sink
        ),
        previous_source=(
            previous.previous_source
            if replacement.source_output_id == previous.source_output_id
            else replacement.previous_source
        ),
    )


async def move_to_bridge_resilient(config: Config, route: PulseRoute) -> PulseRoute:
    current = route
    last_error = None
    for attempt in range(ROUTE_MOVE_ATTEMPTS):
        try:
            await move_to_bridge(config, current)
            return current
        except RuntimeError as error:
            last_error = error
            if attempt + 1 >= ROUTE_MOVE_ATTEMPTS:
                break
            await asyncio.sleep(ROUTE_MOVE_RETRY_SECONDS)
            replacement = await find_wechat_route(config)
            if replacement:
                current = preserve_previous_route(current, replacement)
            logging.warning(
                "WeChat call audio route changed during setup; retrying move (%d/%d).",
                attempt + 1,
                ROUTE_MOVE_ATTEMPTS,
            )
    raise RuntimeError("Could not attach the changing WeChat call audio route.") from last_error


async def restore_route(config: Config, route: PulseRoute) -> None:
    for command in (("move-sink-input", route.sink_input_id, route.previous_sink), ("move-source-output", route.source_output_id, route.previous_source)):
        try:
            await pactl(config, *command)
        except RuntimeError:
            pass


async def route_is_active(config: Config, route: PulseRoute) -> bool:
    snapshot = await pulse_snapshot(config)
    active_sinks = {str(item["index"]) for item in snapshot["sink_inputs"] if isinstance(item, dict) and "index" in item}
    active_sources = {str(item["index"]) for item in snapshot["source_outputs"] if isinstance(item, dict) and "index" in item}
    return route.sink_input_id in active_sinks and route.source_output_id in active_sources


async def route_is_active_resilient(config: Config, route: PulseRoute) -> bool:
    last_error = None
    for attempt in range(ROUTE_PROBE_ATTEMPTS):
        try:
            return await route_is_active(config, route)
        except RuntimeError as error:
            last_error = error
            if attempt + 1 >= ROUTE_PROBE_ATTEMPTS:
                break
            logging.warning(
                "Could not inspect the active WeChat audio route; retrying (%d/%d): %s",
                attempt + 1,
                ROUTE_PROBE_ATTEMPTS,
                error,
            )
            await asyncio.sleep(ROUTE_PROBE_RETRY_SECONDS)
    raise RuntimeError("Could not inspect the active WeChat call audio route after retrying.") from last_error


async def stop_process(process: asyncio.subprocess.Process | None) -> None:
    if not process:
        return
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.communicate(), 3)
    except TimeoutError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.wait_for(process.communicate(), 3)


def audio_client_name(config: Config, role: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", config.audio_owner) or role not in ("capture", "playback"):
        raise ValueError("Invalid audio process owner or role.")
    return f"headmao-voice-{config.audio_owner}-{role}"


async def stop_audio_processes(
    config: Config,
    *processes: asyncio.subprocess.Process | None,
    role: str | None = None,
) -> None:
    name = audio_client_name(config, role or "capture")
    prefix = name.rsplit("-", 1)[0]
    pattern = rf"^(parec|pacat) --client-name={prefix}-{role or '(capture|playback)'}( |$)"
    # Killing docker exec alone leaves its container-side process alive.
    try:
        for signal_name in ("TERM", "KILL"):
            await docker(
                config, *pulse_args(config, "pkill", f"-{signal_name}", "-f", "--", pattern),
                allowed_returncodes=(0, 1),
            )
            for _ in range(10):
                remaining = await docker(
                    config, *pulse_args(config, "pgrep", "-f", "--", pattern),
                    allowed_returncodes=(0, 1),
                )
                if not remaining.strip():
                    return
                await asyncio.sleep(0.1)
        raise RuntimeError("Owned container audio processes did not stop.")
    finally:
        await asyncio.gather(*(stop_process(process) for process in processes))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Clean up one Voice service's container audio processes.")
    parser.add_argument("--cleanup", action="store_true", required=True)
    parser.parse_args()
    if not os.environ.get("INVOCATION_ID"):
        parser.error("Cleanup requires a systemd INVOCATION_ID.")
    asyncio.run(stop_audio_processes(Config(os.environ.get("WECHAT_CONTAINER", "agent-wechat"), 90)))
