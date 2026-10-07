import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "agent-wechat-proactive-observer.py"
SPEC = importlib.util.spec_from_file_location("agent_wechat_proactive_observer", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RunOpenClawTests(unittest.TestCase):
    def test_timeout_drops_optional_candidate(self):
        with mock.patch.object(
            MODULE.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["openclaw"], timeout=210),
        ):
            self.assertIsNone(MODULE.run_openclaw("session", "prompt"))

    def test_launch_error_drops_optional_candidate(self):
        with mock.patch.object(MODULE.subprocess, "run", side_effect=OSError("unavailable")):
            self.assertIsNone(MODULE.run_openclaw("session", "prompt"))


if __name__ == "__main__":
    unittest.main()
