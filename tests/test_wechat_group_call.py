import importlib.util
import io
import re
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wechat-group-call.py"


def load_module():
    spec = importlib.util.spec_from_file_location("wechat_group_call", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CHATROOMS = {
        "10000000001@chatroom": ["Alpha Group"],
        "10000000002@chatroom": ["Friends Group"],
    }
    module.MEMBERS = {
        "Expert Member": ["expert"],
        "Operator Member": ["operator"],
    }
    module.BOT_MENTION_RE = re.compile(r"^[＠@]ChannelBot(?:\s|[,:，：])*")
    return module


SELECTED_TEST_GROUP = """
- list-item "Alpha Group recent" [SELECTED]
- list-item "Friends Group recent"
- push-button "Group Call"  @(10,10 10x10)
"""

SELECTED_FRIEND_GROUP = """
- list-item "Alpha Group recent"
- list-item "Friends Group recent" [SELECTED]
- push-button "Group Call"  @(10,10 10x10)
"""

ADD_MEMBERS = """
- frame "Add Members"
- check-box "Expert Member"  @(20,20 10x10)
- push-button "Finish"  @(30,30 10x10)
"""

VOICE_CALL = """
- frame "Voice Call"
- push-button "Hang Up"  @(40,40 10x10)
"""


class GroupDetectionTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_selected_group_wins_when_both_names_are_visible(self):
        self.assertEqual(
            self.module.current_chat_from_tree(SELECTED_FRIEND_GROUP)["chatId"],
            "10000000002@chatroom",
        )
        self.assertEqual(
            self.module.current_chat_from_tree(SELECTED_TEST_GROUP)["chatId"],
            "10000000001@chatroom",
        )

    def test_ambiguous_group_tree_fails_closed(self):
        tree = SELECTED_FRIEND_GROUP.replace(" [SELECTED]", "")
        self.assertIsNone(self.module.current_chat_from_tree(tree))

    def test_open_expected_chat_retries_wrong_selection(self):
        trees = iter((SELECTED_TEST_GROUP, SELECTED_FRIEND_GROUP))
        with (
            mock.patch.object(self.module, "api_post_json", return_value={"status": 200, "body": "{}"}),
            mock.patch.object(self.module, "dump_tree", side_effect=lambda _container: next(trees)),
            mock.patch.object(self.module.time, "sleep"),
        ):
            result, _tree, current = self.module.open_expected_chat(
                "http://127.0.0.1:6175",
                "10000000002@chatroom",
                "agent-wechat",
            )
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(current["chatId"], "10000000002@chatroom")


class OpeningRequestTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_alias_resolves_to_wechat_member(self):
        self.assertEqual(self.module.resolve_member("operator"), "Operator Member")

    def test_exact_local_id_does_not_require_api_mention_flag(self):
        messages = [
            {"localId": 259, "type": 1, "isMentioned": True, "isSelf": False, "content": "@ChannelBot 早晨"},
            {"localId": 271, "type": 1, "isSelf": False, "content": "@ChannelBot\u2005打个电话比expert，叫佢唔好走住"},
        ]
        self.assertEqual(
            self.module.call_request_from_messages(messages, 271),
            "打个电话比expert，叫佢唔好走住",
        )

    def test_missing_exact_local_id_does_not_return_history(self):
        messages = [
            {"localId": 259, "type": 1, "isMentioned": True, "isSelf": False, "content": "@ChannelBot 早晨"},
        ]
        self.assertIsNone(self.module.call_request_from_messages(messages, 271))


class CallLockTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_second_group_call_operation_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "group-call.lock"
            first = self.module.acquire_call_lock(lock_path)
            try:
                with self.assertRaisesRegex(SystemExit, "already in progress"):
                    self.module.acquire_call_lock(lock_path)
            finally:
                first.close()


class GptVoiceControlFlowTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.module.VOICE_BACKEND_FILE = Path("/nonexistent-xiaozhi-backend-test")

    def test_backend_selection_defaults_to_original_bridge(self):
        with mock.patch.object(self.module, "run", return_value="old-unit\n") as run:
            self.assertEqual(self.module.start_gpt_voice_bridge("10000000001@chatroom"), "old-unit")
        self.assertIn(
            "--template=wechat-gpt-voice-bridge@.service",
            run.call_args_list[0].args[0],
        )

    def test_xiaozhi_selection_and_cross_backend_exclusion(self):
        self.module.VOICE_BACKEND = "xiaozhi"
        with mock.patch.object(self.module, "run", side_effect=["new-unit\n", "", "xiaozhi-vx-bridge@test.service loaded active running\n"]) as run:
            self.assertEqual(self.module.start_gpt_voice_bridge("10000000001@chatroom"), "new-unit")
            self.assertEqual(self.module.active_gpt_voice_bridge_units(), ["xiaozhi-vx-bridge@test.service"])
        self.assertIn("--template=xiaozhi-vx-bridge@.service", run.call_args_list[0].args[0])
        self.assertIn("wechat-gpt-voice-bridge@*.service", run.call_args_list[2].args[0])
        self.assertIn("xiaozhi-vx-bridge@*.service", run.call_args_list[2].args[0])

    def test_backend_file_is_read_on_each_call_without_restarting_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "backend"
            self.module.VOICE_BACKEND_FILE = path
            self.assertEqual(self.module.voice_bridge_template(), "wechat-gpt-voice-bridge@.service")
            path.write_text("xiaozhi\n", encoding="ascii")
            self.assertEqual(self.module.voice_bridge_template(), "xiaozhi-vx-bridge@.service")
            path.write_text("codex\n", encoding="ascii")
            self.assertEqual(self.module.voice_bridge_template(), "wechat-gpt-voice-bridge@.service")

    def test_unknown_backend_never_starts_a_bridge(self):
        self.module.VOICE_BACKEND = "unknown"
        with mock.patch.object(self.module, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "Unsupported WECHAT_VOICE_BACKEND"):
                self.module.start_gpt_voice_bridge("10000000001@chatroom")
            run.assert_not_called()

    def run_main(self, argv, *, open_tree=SELECTED_TEST_GROUP, dumps=None, opening_requests=None):
        started = []
        stopped = []
        clicks = []
        events = []
        dump_values = iter(dumps or (ADD_MEMBERS, ADD_MEMBERS, VOICE_CALL))
        patches = (
            mock.patch.object(sys, "argv", ["wechat-group-call", *argv]),
            mock.patch.object(self.module, "active_gpt_voice_bridge_units", return_value=[]),
            mock.patch.object(
                self.module,
                "write_gpt_voice_request",
                side_effect=lambda *args: (
                    opening_requests.append(args) if opening_requests is not None else None,
                    Path("/tmp/test-voice-request"),
                )[-1],
            ),
            mock.patch.object(
                self.module,
                "start_gpt_voice_bridge",
                side_effect=lambda chat_id: (events.append("start_bridge"), started.append(chat_id), "voice-unit")[-1],
            ),
            mock.patch.object(
                self.module,
                "stop_gpt_voice_bridge",
                side_effect=lambda unit: stopped.append(unit),
            ),
            mock.patch.object(
                self.module,
                "open_expected_chat",
                side_effect=lambda *_: (
                    events.append("open_chat"),
                    ({"attempt": 1}, open_tree, {"chatId": "10000000001@chatroom", "title": "Alpha Group"}),
                )[-1],
            ),
            mock.patch.object(self.module, "dump_tree", side_effect=lambda _container: next(dump_values)),
            mock.patch.object(self.module, "click", side_effect=lambda *args: (events.append("click"), clicks.append(args))),
            mock.patch.object(self.module.time, "sleep"),
            mock.patch.object(self.module, "log_event"),
            mock.patch.object(self.module, "acquire_call_lock", return_value=mock.Mock()),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
            with redirect_stdout(io.StringIO()):
                self.module.main()
        return started, stopped, clicks, events

    def test_real_gpt_call_starts_bridge_before_dialing(self):
        started, stopped, clicks, events = self.run_main(
            ["expert", "--expected-chat-id", "10000000001@chatroom", "--gpt-voice"]
        )
        self.assertEqual(started, ["10000000001@chatroom"])
        self.assertEqual(stopped, [])
        self.assertEqual(len(clicks), 3)
        self.assertLess(events.index("start_bridge"), events.index("open_chat"))
        self.assertLess(events.index("open_chat"), events.index("click"))

    def test_xiaozhi_dials_without_reading_or_writing_opening(self):
        self.module.VOICE_BACKEND = "xiaozhi"
        written = []
        with mock.patch.object(
            self.module, "fetch_call_request", side_effect=AssertionError("must not read message")
        ):
            started, stopped, clicks, events = self.run_main(
                ["expert", "--expected-chat-id", "10000000001@chatroom", "--gpt-voice",
                 "--voice-opening-local-id", "42"],
                opening_requests=written,
            )
        self.assertEqual(written, [])
        self.assertEqual(started, ["10000000001@chatroom"])
        self.assertEqual(stopped, [])
        self.assertEqual(len(clicks), 3)
        self.assertLess(events.index("start_bridge"), events.index("open_chat"))

    def test_gpt_dry_run_never_starts_bridge_or_clicks(self):
        started, stopped, clicks, _events = self.run_main(
            ["expert", "--expected-chat-id", "10000000001@chatroom", "--gpt-voice", "--dry-run"],
            dumps=(SELECTED_TEST_GROUP,),
        )
        self.assertEqual(started, [])
        self.assertEqual(stopped, [])
        self.assertEqual(clicks, [])

    def test_live_call_requires_voice_mode(self):
        with mock.patch.object(sys, "argv", ["wechat-group-call", "expert", "--expected-chat-id", "10000000001@chatroom"]):
            with self.assertRaisesRegex(SystemExit, "--gpt-voice is required"):
                self.module.main()

    def test_removed_tts_options_are_rejected(self):
        with mock.patch.object(sys, "argv", ["wechat-group-call", "expert", "--gpt-voice", "--say", "hello"]):
            with self.assertRaises(SystemExit) as error:
                self.module.main()
        self.assertEqual(error.exception.code, 2)

    def test_codex_opening_uses_exact_message(self):
        written = []
        with mock.patch.object(self.module, "fetch_call_request", return_value="打俾expert") as fetch:
            self.run_main(
                ["expert", "--expected-chat-id", "10000000001@chatroom", "--gpt-voice",
                 "--voice-opening-local-id", "42"],
                opening_requests=written,
            )
        fetch.assert_called_once_with("http://127.0.0.1:6175", "10000000001@chatroom", 42)
        self.assertEqual(written, [("10000000001@chatroom", "打俾expert")])

    def test_bridge_start_failure_removes_pending_opening(self):
        request_path = mock.Mock()
        with (
            mock.patch.object(sys, "argv", ["wechat-group-call", "expert", "--expected-chat-id", "10000000001@chatroom", "--gpt-voice"]),
            mock.patch.object(self.module, "active_gpt_voice_bridge_units", return_value=[]),
            mock.patch.object(self.module, "write_gpt_voice_request", return_value=request_path),
            mock.patch.object(self.module, "start_gpt_voice_bridge", side_effect=RuntimeError("start failed")),
            mock.patch.object(self.module, "open_expected_chat") as open_chat,
            mock.patch.object(self.module, "acquire_call_lock", return_value=mock.Mock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                self.module.main()
        request_path.unlink.assert_called_once_with(missing_ok=True)
        open_chat.assert_not_called()

    def test_failure_after_prewarm_stops_only_new_bridge(self):
        started = []
        stopped = []
        with (
            mock.patch.object(sys, "argv", ["wechat-group-call", "expert", "--expected-chat-id", "10000000001@chatroom", "--gpt-voice"]),
            mock.patch.object(self.module, "active_gpt_voice_bridge_units", return_value=[]),
            mock.patch.object(self.module, "write_gpt_voice_request", return_value=Path("/tmp/test-voice-request")),
            mock.patch.object(
                self.module,
                "start_gpt_voice_bridge",
                side_effect=lambda chat_id: started.append(chat_id) or "voice-unit",
            ),
            mock.patch.object(
                self.module,
                "stop_gpt_voice_bridge",
                side_effect=lambda unit: stopped.append(unit),
            ),
            mock.patch.object(self.module, "open_expected_chat", side_effect=RuntimeError("open failed")),
            mock.patch.object(self.module, "log_event"),
            mock.patch.object(self.module, "acquire_call_lock", return_value=mock.Mock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "open failed"):
                self.module.main()
        self.assertEqual(started, ["10000000001@chatroom"])
        self.assertEqual(stopped, ["voice-unit"])

    def test_existing_bridge_aborts_before_starting_another(self):
        with (
            mock.patch.object(sys, "argv", ["wechat-group-call", "expert", "--expected-chat-id", "10000000001@chatroom", "--gpt-voice"]),
            mock.patch.object(self.module, "active_gpt_voice_bridge_units", return_value=["wechat-gpt-voice-bridge@existing.service"]),
            mock.patch.object(self.module, "start_gpt_voice_bridge") as start,
            mock.patch.object(self.module, "acquire_call_lock", return_value=mock.Mock()),
        ):
            with self.assertRaises(SystemExit):
                self.module.main()
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
