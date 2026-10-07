import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "services" / "agent-wechat-incoming-call-probe.py"


def load_module():
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args: None
    repository = types.ModuleType("gi.repository")
    repository.Atspi = types.SimpleNamespace()
    gi.repository = repository
    with mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
        spec = importlib.util.spec_from_file_location("incoming_call_probe", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class X11IncomingCallTests(unittest.TestCase):
    def setUp(self):
        self.probe = load_module()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc_root = Path(self.temp.name)
        (self.proc_root / "186").mkdir()
        (self.proc_root / "186" / "comm").write_text("wechat\n", encoding="ascii")
        self.geometry = "WINDOW=23068710\nX=970\nY=672\nWIDTH=300\nHEIGHT=106\nSCREEN=0"
        self.properties = (
            'WM_CLASS(STRING) = "wechat", "wechat"\n'
            'WM_NAME(STRING) = "Weixin"\n'
            "_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_UTILITY, "
            "_KDE_NET_WM_WINDOW_TYPE_OVERRIDE, _NET_WM_WINDOW_TYPE_NORMAL"
        )

    def run_probe(self, geometry=None, properties=None, pid="186", windows="23068688\n23068710"):
        outputs = {
            ("xdotool", "getdisplaygeometry"): "1280 800",
            (
                "xdotool", "search", "--onlyvisible", "--class", "^wechat$"
            ): windows,
            ("xdotool", "getwindowgeometry", "--shell", "23068688"):
                "WINDOW=23068688\nX=150\nY=45\nWIDTH=980\nHEIGHT=710\nSCREEN=0",
            ("xdotool", "getwindowgeometry", "--shell", "23068710"):
                self.geometry if geometry is None else geometry,
            ("xprop", "-id", "23068710", "WM_CLASS", "WM_NAME", "_NET_WM_WINDOW_TYPE"):
                self.properties if properties is None else properties,
            ("xdotool", "getwindowpid", "23068710"): pid,
        }
        with mock.patch.object(self.probe, "command_output", side_effect=lambda *args: outputs.get(args, "")):
            return self.probe.x11_window_event(self.proc_root)

    def test_live_call_window_yields_green_answer_bounds(self):
        self.assertEqual(
            self.run_probe(),
            {
                "type": "incoming-call",
                "answer": {"x": 1228, "y": 736, "width": 30, "height": 30},
                "popup": {"x": 970, "y": 672, "width": 300, "height": 106},
            },
        )

    def test_normal_chat_window_is_not_a_call(self):
        self.assertIsNone(self.run_probe(windows="23068688"))

    def test_other_popup_type_or_process_is_rejected(self):
        self.assertIsNone(self.run_probe(properties=self.properties.replace("_UTILITY", "_NORMAL")))
        self.assertIsNone(self.run_probe(pid="999"))

    def test_unexpected_location_is_rejected(self):
        self.assertIsNone(self.run_probe(geometry=self.geometry.replace("X=970", "X=700")))


if __name__ == "__main__":
    unittest.main()
