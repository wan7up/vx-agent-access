import runpy
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/desktop-rebuild/enter-session.py"

class GLibError(Exception):
    pass


class Node:
    def __init__(self, role, name, children=(), bounds=None):
        self.role, self.name, self.children, self.bounds = role, name, children, bounds

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_component_iface(self):
        return self

    def get_extents(self, coordinate_type):
        return self.bounds


def desktop(control, application="wechat"):
    return Node("desktop", "main", [Node("application", application, [control])])


class DesktopEntryTests(unittest.TestCase):
    def run_script(self, trees):
        clock = [0.0]
        atspi = types.SimpleNamespace(
            get_desktop=mock.Mock(side_effect=trees),
            CoordType=types.SimpleNamespace(SCREEN=0),
        )
        gi = types.ModuleType("gi")
        gi.require_version = mock.Mock()
        repository = types.ModuleType("gi.repository")
        repository.Atspi = atspi
        repository.GLib = types.SimpleNamespace(Error=GLibError)
        with (
            mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}),
            mock.patch("time.monotonic", side_effect=lambda: clock[0]),
            mock.patch("time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
            mock.patch("subprocess.run") as run,
            mock.patch("builtins.print"),
        ):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        return result.exception.code, run

    def test_transient_qr_does_not_prevent_remembered_login(self):
        bounds = types.SimpleNamespace(x=550, y=480, width=180, height=36)
        code, run = self.run_script([
            desktop(Node("push button", "QR Code")),
            desktop(Node("push button", "Enter Weixin", bounds=bounds)),
            desktop(Node("list", "Chats")),
        ])
        self.assertEqual(code, 0)
        run.assert_called_once_with(
            ["/opt/tools/click", "640", "498"],
            check=True, stdout=mock.ANY,
        )

    def test_already_entered_does_not_click(self):
        code, run = self.run_script([desktop(Node("list", "Chats"))])
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_stale_desktop_retries_without_clicking(self):
        code, run = self.run_script([
            GLibError("No such object path"),
            desktop(Node("list", "Chats")),
        ])
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_genuine_qr_login_remains_manual(self):
        code, run = self.run_script([desktop(Node("push button", "QR Code"))] * 21)
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_invalid_control_bounds_fail_closed(self):
        bounds = types.SimpleNamespace(x=-10, y=480, width=180, height=36)
        code, run = self.run_script([
            desktop(Node("push button", "Enter Weixin", bounds=bounds)),
        ])
        self.assertEqual(code, "Enter control bounds are invalid")
        run.assert_not_called()

    def test_web_helper_is_not_treated_as_native_client(self):
        bounds = types.SimpleNamespace(x=550, y=480, width=180, height=36)
        code, run = self.run_script([
            desktop(Node("push button", "Enter Weixin", bounds=bounds), "WeChatAppEx"),
            desktop(Node("list", "Chats")),
        ])
        self.assertEqual(code, 0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
