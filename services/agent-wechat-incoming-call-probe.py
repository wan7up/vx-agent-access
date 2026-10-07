#!/usr/bin/env python3
"""Wait for the Linux WeChat incoming-call Answer control."""

import argparse
import json
from pathlib import Path
import subprocess
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi


MAX_POPUP_WIDTH = 520
MAX_POPUP_HEIGHT = 320
X11_ANSWER_WIDTH = 30
X11_ANSWER_HEIGHT = 30


def command_output(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=2, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def x11_window_event(proc_root=Path("/proc")):
    screen = command_output("xdotool", "getdisplaygeometry").split()
    if len(screen) != 2 or not all(value.isdecimal() for value in screen):
        return None
    screen_width, screen_height = map(int, screen)
    windows = command_output(
        "xdotool", "search", "--onlyvisible", "--class", "^wechat$"
    ).split()
    for window in windows:
        if not window.isdecimal():
            continue
        geometry = dict(
            line.split("=", 1)
            for line in command_output("xdotool", "getwindowgeometry", "--shell", window).splitlines()
            if "=" in line
        )
        try:
            x, y, width, height = (
                int(geometry[key]) for key in ("X", "Y", "WIDTH", "HEIGHT")
            )
        except (KeyError, ValueError):
            continue
        if not (
            280 <= width <= 320
            and 90 <= height <= 120
            and 0 <= screen_width - (x + width) <= 30
            and 0 <= screen_height - (y + height) <= 45
        ):
            continue
        properties = command_output(
            "xprop", "-id", window, "WM_CLASS", "WM_NAME", "_NET_WM_WINDOW_TYPE"
        )
        if not (
            'WM_CLASS(STRING) = "wechat", "wechat"' in properties
            and 'WM_NAME(STRING) = "Weixin"' in properties
            and "_NET_WM_WINDOW_TYPE_UTILITY" in properties
        ):
            continue
        pid = command_output("xdotool", "getwindowpid", window)
        if not pid.isdecimal():
            continue
        try:
            if (proc_root / pid / "comm").read_text(encoding="ascii").strip() != "wechat":
                continue
        except OSError:
            continue
        return {
            "type": "incoming-call",
            "answer": {
                "x": x + width - 42,
                "y": y + height - 42,
                "width": X11_ANSWER_WIDTH,
                "height": X11_ANSWER_HEIGHT,
            },
            "popup": {"x": x, "y": y, "width": width, "height": height},
        }
    return None


def bounds(node):
    try:
        component = node.get_component_iface()
        rect = component.get_extents(Atspi.CoordType.SCREEN) if component else None
        if not rect or rect.width <= 0 or rect.height <= 0:
            return None
        return {"x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height}
    except Exception:
        return None


def children(node):
    try:
        return [node.get_child_at_index(index) for index in range(node.get_child_count())]
    except Exception:
        return []


def find_frames(node, depth=0):
    if not node or depth > 4:
        return []
    try:
        role = node.get_role_name().replace(" ", "-")
    except Exception:
        return []
    if role == "frame":
        return [node]
    result = []
    for child in children(node):
        result.extend(find_frames(child, depth + 1))
    return result


def find_named_button(node, name, depth=0):
    if not node or depth > 8:
        return None
    try:
        if node.get_role_name().replace(" ", "-") == "push-button" and (node.get_name() or "") == name:
            return node
    except Exception:
        return None
    for child in children(node):
        found = find_named_button(child, name, depth + 1)
        if found:
            return found
    return None


def current_answer_event(desktop):
    for frame in find_frames(desktop):
        frame_bounds = bounds(frame)
        if not frame_bounds:
            continue
        if frame_bounds["width"] > MAX_POPUP_WIDTH or frame_bounds["height"] > MAX_POPUP_HEIGHT:
            continue
        answer = find_named_button(frame, "Answer")
        answer_bounds = bounds(answer) if answer else None
        if not answer_bounds:
            continue
        return {
            "type": "incoming-call",
            "answer": answer_bounds,
            "popup": frame_bounds,
        }
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=0.4)
    parser.add_argument("--max-wait-seconds", type=float, default=0)
    args = parser.parse_args()

    Atspi.init()
    desktop = Atspi.get_desktop(0)
    deadline = time.monotonic() + args.max_wait_seconds if args.max_wait_seconds > 0 else None
    while True:
        event = current_answer_event(desktop) or x11_window_event()
        if event:
            print(json.dumps(event, separators=(",", ":")), flush=True)
            return
        if args.once:
            return
        if deadline is not None and time.monotonic() >= deadline:
            return
        time.sleep(max(0.1, args.poll_seconds))


if __name__ == "__main__":
    main()
