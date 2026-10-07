import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "services" / "agent-wechat-incoming-call.py"


def load_module():
    spec = importlib.util.spec_from_file_location("agent_wechat_incoming_call", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVENT = {"type": "incoming-call", "answer": {"x": 1228, "y": 736, "width": 30, "height": 30}}


class IncomingCallTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.module.ANSWER_DELAY_SECONDS = 0
        self.module.INCOMING_CHAT_ID = "10000000002@chatroom"
        self.module.VOICE_BACKEND_FILE = Path("/nonexistent-xiaozhi-backend-test")

    def test_probe_start_cleans_stale_container_processes(self):
        with (
            mock.patch.object(self.module, "PROBE_HOST", mock.Mock(is_file=lambda: True)),
            mock.patch.object(self.module, "run") as run,
        ):
            self.module.ensure_probe()
        self.assertIn("pkill -f", run.call_args_list[0].args[0][-1])
        self.assertEqual(run.call_count, 3)
        self.assertEqual(
            run.call_args_list[2].args[0],
            ["docker", "exec", "-u", "0", self.module.CONTAINER,
             "chmod", "755", self.module.PROBE_CONTAINER],
        )

    def test_xiaozhi_backend_unit_and_both_active_patterns(self):
        self.module.VOICE_BACKEND = "xiaozhi"
        with mock.patch.object(
            self.module, "run",
            side_effect=["xiaozhi-unit\n", "wechat-gpt-voice-bridge@existing.service loaded active running\n"],
        ) as run:
            self.assertEqual(self.module.bridge_unit(), "xiaozhi-unit")
            self.assertEqual(self.module.active_voice_units(), ["wechat-gpt-voice-bridge@existing.service"])
        self.assertIn("--template=xiaozhi-vx-bridge@.service", run.call_args_list[0].args[0])
        self.assertIn("xiaozhi-vx-bridge@*.service", run.call_args_list[1].args[0])

    def test_unknown_backend_does_not_start_a_bridge(self):
        self.module.VOICE_BACKEND = "unknown"
        with mock.patch.object(self.module, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "Unsupported WECHAT_VOICE_BACKEND"):
                self.module.bridge_unit()
            run.assert_not_called()

    def test_backend_file_updates_between_incoming_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "backend"
            self.module.VOICE_BACKEND_FILE = path
            self.assertEqual(self.module.voice_bridge_template(), "wechat-gpt-voice-bridge@.service")
            path.write_text("xiaozhi\n", encoding="ascii")
            self.assertEqual(self.module.voice_bridge_template(), "xiaozhi-vx-bridge@.service")

    def test_bridge_starts_before_answer_click(self):
        events = []
        request = mock.Mock()
        lock = mock.Mock()
        with (
            mock.patch.object(self.module, "acquire_call_lock", return_value=lock),
            mock.patch.object(self.module, "active_voice_units", return_value=[]),
            mock.patch.object(self.module, "write_opening_request", return_value=request),
            mock.patch.object(self.module, "bridge_unit", return_value="incoming-unit"),
            mock.patch.object(self.module, "start_bridge", side_effect=lambda _unit: events.append("start")),
            mock.patch.object(self.module, "current_incoming_call", return_value=EVENT),
            mock.patch.object(self.module, "answer_call", side_effect=lambda _event: events.append("answer")),
        ):
            self.assertTrue(self.module.handle_incoming_call(EVENT))
        self.assertEqual(events, ["start", "answer"])
        lock.close.assert_called_once()

    def test_probe_has_an_internal_deadline_before_host_timeout(self):
        self.module.PROBE_MAX_WAIT_SECONDS = 60
        with mock.patch.object(self.module, "run", return_value="") as run:
            self.assertIsNone(self.module.wait_for_incoming_call())
        command, timeout = run.call_args.args[0], run.call_args.kwargs["timeout"]
        self.assertEqual(command[-2:], ["--max-wait-seconds", "60"])
        self.assertEqual(timeout, 360)

    def test_existing_bridge_leaves_call_ringing(self):
        lock = mock.Mock()
        with (
            mock.patch.object(self.module, "acquire_call_lock", return_value=lock),
            mock.patch.object(self.module, "active_voice_units", return_value=["existing.service"]),
            mock.patch.object(self.module, "start_bridge") as start,
            mock.patch.object(self.module, "answer_call") as answer,
        ):
            self.assertFalse(self.module.handle_incoming_call(EVENT))
        start.assert_not_called()
        answer.assert_not_called()
        lock.close.assert_called_once()

    def test_configured_delay_rechecks_call_before_answering(self):
        self.module.ANSWER_DELAY_SECONDS = 4
        fresh_event = {"type": "incoming-call", "answer": {"x": 100, "y": 200, "width": 20, "height": 20}}
        events = []
        with (
            mock.patch.object(self.module, "acquire_call_lock", return_value=mock.Mock()),
            mock.patch.object(self.module, "active_voice_units", return_value=[]),
            mock.patch.object(self.module, "write_opening_request", return_value=mock.Mock()),
            mock.patch.object(self.module, "bridge_unit", return_value="incoming-unit"),
            mock.patch.object(self.module, "start_bridge", side_effect=lambda _unit: events.append("start")),
            mock.patch.object(self.module.time, "sleep", side_effect=lambda seconds: events.append(("sleep", seconds))),
            mock.patch.object(self.module, "current_incoming_call", return_value=fresh_event),
            mock.patch.object(self.module, "answer_call", side_effect=lambda event: events.append(("answer", event))),
        ):
            self.assertTrue(self.module.handle_incoming_call(EVENT))
        self.assertEqual(events, ["start", ("sleep", 4), ("answer", fresh_event)])

    def test_xiaozhi_answers_without_delay_or_opening_request(self):
        self.module.VOICE_BACKEND = "xiaozhi"
        self.module.ANSWER_DELAY_SECONDS = 4
        with (
            mock.patch.object(self.module, "acquire_call_lock", return_value=mock.Mock()),
            mock.patch.object(self.module, "active_voice_units", return_value=[]),
            mock.patch.object(self.module, "write_opening_request") as opening,
            mock.patch.object(self.module, "bridge_unit", return_value="xiaozhi-unit"),
            mock.patch.object(self.module, "start_bridge") as start,
            mock.patch.object(self.module, "current_incoming_call", return_value=EVENT) as recheck,
            mock.patch.object(self.module.time, "sleep") as sleep,
            mock.patch.object(self.module, "answer_call") as answer,
        ):
            self.assertTrue(self.module.handle_incoming_call(EVENT))
        opening.assert_not_called()
        recheck.assert_called_once_with()
        sleep.assert_not_called()
        start.assert_called_once_with("xiaozhi-unit")
        answer.assert_called_once_with(EVENT)

    def test_lock_contention_leaves_call_ringing(self):
        with (
            mock.patch.object(self.module, "acquire_call_lock", return_value=None),
            mock.patch.object(self.module, "active_voice_units") as active,
            mock.patch.object(self.module, "answer_call") as answer,
        ):
            self.assertFalse(self.module.handle_incoming_call(EVENT))
        active.assert_not_called()
        answer.assert_not_called()

    def test_answer_failure_stops_only_new_bridge_and_removes_request(self):
        request = mock.Mock()
        lock = mock.Mock()
        stopped = []
        with (
            mock.patch.object(self.module, "acquire_call_lock", return_value=lock),
            mock.patch.object(self.module, "active_voice_units", return_value=[]),
            mock.patch.object(self.module, "write_opening_request", return_value=request),
            mock.patch.object(self.module, "bridge_unit", return_value="incoming-unit"),
            mock.patch.object(self.module, "start_bridge"),
            mock.patch.object(self.module, "current_incoming_call", return_value=EVENT),
            mock.patch.object(self.module, "answer_call", side_effect=RuntimeError("click failed")),
            mock.patch.object(self.module, "stop_bridge", side_effect=lambda unit: stopped.append(unit)),
        ):
            with self.assertRaisesRegex(RuntimeError, "click failed"):
                self.module.handle_incoming_call(EVENT)
        self.assertEqual(stopped, ["incoming-unit"])
        request.unlink.assert_called_once_with(missing_ok=True)

    def test_request_is_private_and_contains_generic_opening(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.module.REQUEST_DIR = Path(temporary)
            path = self.module.write_opening_request()
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["chatId"], "10000000002@chatroom")
            self.assertIn("喂~HELLO啊~你搵我做咩~", payload["openingRequest"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
