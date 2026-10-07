import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "select_voice_backend.py"
SPEC = importlib.util.spec_from_file_location("select_voice_backend", SCRIPT)
selector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selector)


class BackendSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        overrides = {
            "BACKEND_FILE": root / "backend",
            "ORIGINAL_OPENING": root / "original-opening.conf",
            "XIAOZHI_OPENING": root / "units" / "opening.conf",
            "XIAOZHI_UNIT_FILE": root / "xiaozhi.service",
            "DEVICE_STATE": root / "device.json",
            "BRIDGE": root / "call_bridge.py",
        }
        for name, value in overrides.items():
            patch = mock.patch.object(selector, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        selector.ORIGINAL_OPENING.write_text("[Service]\nEnvironment=TEST=1\n", encoding="ascii")
        selector.XIAOZHI_UNIT_FILE.write_text("[Unit]\nDescription=test\n", encoding="ascii")
        selector.BRIDGE.write_text("bridge", encoding="ascii")
        selector.DEVICE_STATE.write_text(json.dumps({"activated": True}), encoding="utf-8")

    def test_xiaozhi_switch_and_codex_rollback_only_touch_voice_controls(self):
        commands = []

        def run(*args):
            commands.append(args)
            return "active" if args[:2] == ("systemctl", "is-active") else ""

        with mock.patch.object(selector, "run", side_effect=run):
            selector.switch("xiaozhi")
            self.assertEqual(selector.current_backend(), "xiaozhi")
            self.assertEqual(selector.XIAOZHI_OPENING.read_bytes(), selector.ORIGINAL_OPENING.read_bytes())
            self.assertIn(("systemctl", "disable", "--now", selector.CODEX_INCOMING_UNIT), commands)
            self.assertIn(("systemctl", "disable", "--now", selector.CODEX_UNIT), commands)
            self.assertIn(("systemctl", "enable", "--now", selector.XIAOZHI_INCOMING_UNIT), commands)
            self.assertFalse(any("openclaw" in str(command).lower() for command in commands))

            commands.clear()
            selector.switch("codex")
            self.assertEqual(selector.current_backend(), "codex")
            self.assertIn(("systemctl", "disable", "--now", selector.XIAOZHI_INCOMING_UNIT), commands)
            self.assertIn(("systemctl", "enable", "--now", selector.CODEX_UNIT), commands)
        self.assertEqual(selector.BACKEND_FILE.stat().st_mode & 0o777, 0o644)

    def test_active_call_refuses_switch_before_any_service_change(self):
        with mock.patch.object(selector, "run", return_value="xiaozhi-vx-bridge@call.service loaded active running") as run:
            with self.assertRaisesRegex(RuntimeError, "End the active voice call"):
                selector.switch("codex")
        self.assertEqual(run.call_count, 1)
        self.assertFalse(selector.BACKEND_FILE.exists())

    def test_unactivated_device_refuses_switch_without_greeting_dependency(self):
        selector.DEVICE_STATE.write_text(json.dumps({"activated": False}), encoding="utf-8")
        with mock.patch.object(selector, "run", return_value="") as run:
            with self.assertRaisesRegex(RuntimeError, "not activated"):
                selector.switch("xiaozhi")
        self.assertEqual(run.call_count, 1)

    def test_unexpected_selector_or_modified_opening_fails_closed(self):
        selector.BACKEND_FILE.write_text("unknown\n", encoding="ascii")
        with mock.patch.object(selector, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "unsupported"):
                selector.switch("xiaozhi")
            run.assert_not_called()
        selector.BACKEND_FILE.unlink()
        selector.XIAOZHI_OPENING.parent.mkdir()
        selector.XIAOZHI_OPENING.write_text("customized\n", encoding="ascii")
        with self.assertRaisesRegex(RuntimeError, "locally modified"):
            selector.verify_xiaozhi_files()


if __name__ == "__main__":
    unittest.main()
