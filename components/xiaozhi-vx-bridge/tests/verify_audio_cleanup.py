#!/usr/bin/env python3
"""Opt-in real container audio lifecycle check; never connects to Xiaozhi."""

import argparse
import asyncio
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, "/opt/xiaozhi-vx-bridge")
import call_bridge

pulse = call_bridge.pulse


async def owned_pids(config, role=None):
    prefix = pulse.audio_client_name(config, "capture").rsplit("-", 1)[0]
    pattern = rf"^(parec|pacat) --client-name={prefix}-{role or '(capture|playback)'}( |$)"
    return (await pulse.docker(
        config, *pulse.pulse_args(config, "pgrep", "-f", "--", pattern),
        allowed_returncodes=(0, 1),
    )).strip()


async def main(leave_for_stop_post):
    config = pulse.Config(os.environ.get("WECHAT_CONTAINER", "agent-wechat"), 90)
    if await pulse.find_wechat_route(config):
        raise RuntimeError("Refusing to run during a real call.")
    capture = playback = sentinel = None
    other = pulse.Config(config.container, 90, audio_owner=uuid.uuid4().hex)
    try:
        capture = await call_bridge.start_capture(config)
        playback = await call_bridge.start_playback(config, 24000)
        await asyncio.wait_for(capture.stdout.readexactly(call_bridge.INPUT_BYTES), 5)
        playback.stdin.write(bytes(960))
        await playback.stdin.drain()
        if leave_for_stop_post:
            print(f"READY owner={config.audio_owner}", flush=True)
            await asyncio.Future()
        sentinel = await call_bridge.start_capture(other)
        await asyncio.wait_for(sentinel.stdout.readexactly(call_bridge.INPUT_BYTES), 5)
        await pulse.stop_audio_processes(config, capture, role="capture")
        assert not await owned_pids(config, "capture")
        assert await owned_pids(config, "playback")
        capture = await call_bridge.start_capture(config)
        await asyncio.wait_for(capture.stdout.readexactly(call_bridge.INPUT_BYTES), 5)
        await pulse.stop_process(capture)
        await pulse.stop_audio_processes(config, capture, playback)
        await pulse.stop_audio_processes(config, capture, playback)
        assert not await owned_pids(config)
        assert await owned_pids(other, "capture")
        print("PASS: capture restart, detached client cleanup, idempotency, ownership isolation", flush=True)
    finally:
        await pulse.stop_audio_processes(config, capture, playback)
        await pulse.stop_audio_processes(other, sentinel)
        assert not await owned_pids(config)
        assert not await owned_pids(other)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--leave-for-stop-post", action="store_true")
    asyncio.run(main(parser.parse_args().leave_for_stop_post))
