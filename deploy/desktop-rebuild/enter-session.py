#!/usr/bin/env python3
"""Enter a remembered account before starting desktop API operations."""
import subprocess
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib


def find(node, role, name, depth=0):
    if depth > 24:
        return None
    try:
        if node.get_role_name() == role and node.get_name() == name:
            return node
        child_count = node.get_child_count()
    except GLib.Error:
        # WeChat can replace an AT-SPI node while the tree is being walked.
        # Treat that snapshot as stale and let the next polling pass retry.
        return None
    for index in range(child_count):
        try:
            child = node.get_child_at_index(index)
        except GLib.Error:
            continue
        if child:
            found = find(child, role, name, depth + 1)
            if found:
                return found
    return None


started = time.monotonic()
deadline = started + 90
clicked = False
while time.monotonic() < deadline:
    try:
        desktop = Atspi.get_desktop(0)
        for index in range(desktop.get_child_count()):
            try:
                application = desktop.get_child_at_index(index)
                if not application or application.get_name() != "wechat":
                    continue
                if find(application, "list", "Chats"):
                    print(
                        "Remembered account entered; native Chats control is available",
                        flush=True,
                    )
                    raise SystemExit(0)
                button = find(application, "push button", "Enter Weixin")
                if button and not clicked:
                    try:
                        bounds = button.get_component_iface().get_extents(
                            Atspi.CoordType.SCREEN
                        )
                        if not (
                            0 <= bounds.x < 1280
                            and 0 <= bounds.y < 800
                            and 0 < bounds.width <= 1280 - bounds.x
                            and 0 < bounds.height <= 800 - bounds.y
                        ):
                            raise SystemExit("Enter control bounds are invalid")
                        subprocess.run(
                            [
                                "/opt/tools/click",
                                str(bounds.x + bounds.width // 2),
                                str(bounds.y + bounds.height // 2),
                            ],
                            check=True,
                            stdout=subprocess.DEVNULL,
                        )
                        clicked = True
                    except GLib.Error:
                        continue
                if (
                    not clicked
                    and time.monotonic() - started >= 10
                    and find(application, "push button", "QR Code")
                ):
                    print(
                        "No remembered login; manual account login is required",
                        flush=True,
                    )
                    raise SystemExit(0)
            except GLib.Error:
                continue
    except GLib.Error:
        pass
    time.sleep(0.5)

print(
    "Native account entry did not become ready within 90 seconds; "
    "starting agent-server so the container remains available",
    flush=True,
)
