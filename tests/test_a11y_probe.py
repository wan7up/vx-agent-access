import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "deploy/desktop-rebuild/a11y-dump-wrapper.py"
spec = importlib.util.spec_from_file_location("a11y_probe_wrapper", SOURCE)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


class A11yProbeTests(unittest.TestCase):
    def test_contending_probe_waits_and_then_acquires_lock(self):
        with tempfile.TemporaryFile() as lock:
            with mock.patch.object(wrapper.fcntl, "flock", side_effect=[BlockingIOError, None]):
                with mock.patch.object(wrapper.time, "sleep") as sleep:
                    self.assertTrue(wrapper.acquire_lock(lock, 3))
        sleep.assert_called_once()

    def test_lock_wait_is_bounded(self):
        with tempfile.TemporaryFile() as lock:
            with mock.patch.object(wrapper.fcntl, "flock", side_effect=BlockingIOError):
                self.assertFalse(wrapper.acquire_lock(lock, 0))

    def test_lock_timeout_never_starts_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            real_dump = path / "real-dump"
            real_dump.touch()
            with mock.patch.object(wrapper, "REAL_DUMP", real_dump):
                with mock.patch.object(wrapper, "LOCK_FILE", path / "probe.lock"):
                    with mock.patch.object(wrapper, "acquire_lock", return_value=False):
                        with mock.patch.object(wrapper.subprocess, "run") as run:
                            self.assertEqual(wrapper.main(), 75)
            run.assert_not_called()

    def test_dump_timeout_returns_failure_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            real_dump = path / "real-dump"
            real_dump.touch()
            with mock.patch.object(wrapper, "REAL_DUMP", real_dump):
                with mock.patch.object(wrapper, "LOCK_FILE", path / "probe.lock"):
                    with mock.patch.object(wrapper.subprocess, "run",
                                           side_effect=subprocess.TimeoutExpired("dump", 5)):
                        self.assertEqual(wrapper.main(), 124)
                    with (path / "probe.lock").open("a+") as lock:
                        self.assertTrue(wrapper.acquire_lock(lock, 0))

    def test_dump_arguments_and_exit_code_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            real_dump = path / "real-dump"
            real_dump.touch()
            with mock.patch.object(wrapper, "REAL_DUMP", real_dump):
                with mock.patch.object(wrapper, "LOCK_FILE", path / "probe.lock"):
                    with mock.patch.object(wrapper.sys, "argv", ["dump", "--format", "json"]):
                        with mock.patch.object(wrapper.subprocess, "run",
                                               return_value=mock.Mock(returncode=9)) as run:
                            self.assertEqual(wrapper.main(), 9)
            run.assert_called_once_with(
                [str(real_dump), "--format", "json"],
                check=False,
                timeout=wrapper.TIMEOUT_SECONDS,
            )


if __name__ == "__main__":
    unittest.main()
