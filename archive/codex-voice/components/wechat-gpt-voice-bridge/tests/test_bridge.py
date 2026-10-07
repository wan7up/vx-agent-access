import json
import unittest
from unittest import mock
import sys
from pathlib import Path
import tempfile

sys.modules.setdefault("websockets", mock.Mock())
import bridge
import pulse_audio as pulse


class RouteRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_uses_one_snapshot_and_ignores_other_clients(self):
        snapshot = {
            "sinks": [], "sources": [],
            "clients": [
                {"index": 9, "properties": {"application.process.binary": "wechat"}},
                {"index": 10, "properties": {"application.process.binary": "other"}},
            ],
            "sink_inputs": [
                {"index": 3, "sink": 1, "client": 10},
                {"index": 4, "sink": 2, "client": "9"},
            ],
            "source_outputs": [
                {"index": 5, "source": 3, "client": 10},
                {"index": 6, "source": 4, "client": "9"},
            ],
        }
        with mock.patch.object(pulse, "pactl", new=mock.AsyncMock(return_value=json.dumps(snapshot))) as pactl:
            route = await pulse.find_wechat_route(mock.Mock())
        self.assertEqual(route, pulse.PulseRoute("4", "6", "2", "4"))
        pactl.assert_awaited_once_with(mock.ANY, "--format=json", "list")

    async def test_call_uses_two_wechat_clients_and_ignores_stale_capture(self):
        snapshot = {
            "sinks": [], "sources": [],
            "clients": [
                {"index": 9, "properties": {"application.process.binary": "wechat"}},
                {"index": 1201, "properties": {"application.process.binary": "wechat"}},
                {"index": 65, "properties": {"application.process.binary": "pacat"}},
            ],
            "sink_inputs": [{"index": 40, "sink": 1, "client": "1201"}],
            "source_outputs": [
                {"index": 2, "source": 1, "client": "65"},
                {"index": 9, "source": 3, "client": "9"},
            ],
        }
        with mock.patch.object(pulse, "pactl", new=mock.AsyncMock(return_value=json.dumps(snapshot))):
            route = await pulse.find_wechat_route(mock.Mock())
        self.assertEqual(route, pulse.PulseRoute("40", "9", "1", "3"))

    async def test_snapshot_rejects_missing_sections_for_retry(self):
        with mock.patch.object(pulse, "pactl", new=mock.AsyncMock(return_value="{}")):
            with self.assertRaisesRegex(RuntimeError, "incomplete JSON snapshot"):
                await pulse.find_wechat_route(mock.Mock())

    async def test_route_activity_uses_one_snapshot(self):
        route = pulse.PulseRoute("4", "6", "2", "4")
        snapshot = {
            "sinks": [], "sources": [], "clients": [],
            "sink_inputs": [{"index": 4}],
            "source_outputs": [{"index": 6}],
        }
        with mock.patch.object(pulse, "pactl", new=mock.AsyncMock(return_value=json.dumps(snapshot))) as pactl:
            self.assertTrue(await pulse.route_is_active(mock.Mock(), route))
            snapshot["source_outputs"] = []
            pactl.return_value = json.dumps(snapshot)
            self.assertFalse(await pulse.route_is_active(mock.Mock(), route))
        self.assertEqual(pactl.await_count, 2)

    async def test_existing_pulse_devices_need_no_module_changes(self):
        snapshot = {
            "sinks": [{"name": pulse.CALL_PLAYBACK_SINK}, {"name": pulse.GPT_INJECT_SINK}],
            "sources": [{"name": pulse.GPT_MIC_SOURCE}],
            "clients": [], "sink_inputs": [], "source_outputs": [],
        }
        with mock.patch.object(pulse, "pactl", new=mock.AsyncMock(return_value=json.dumps(snapshot))) as pactl:
            await pulse.ensure_pulse_routes(mock.Mock())
        pactl.assert_awaited_once()

    async def test_active_route_probe_retries_one_transient_docker_failure(self):
        route = pulse.PulseRoute("10", "20", "speaker", "microphone")
        probe = mock.AsyncMock(side_effect=[RuntimeError("Docker command timed out."), True])
        with (
            mock.patch.object(pulse, "route_is_active", probe),
            mock.patch.object(pulse.asyncio, "sleep", new=mock.AsyncMock()) as sleep,
        ):
            self.assertTrue(await pulse.route_is_active_resilient(mock.Mock(), route))
        self.assertEqual(probe.await_count, 2)
        sleep.assert_awaited_once_with(pulse.ROUTE_PROBE_RETRY_SECONDS)

    async def test_active_route_probe_fails_after_bounded_retries(self):
        route = pulse.PulseRoute("10", "20", "speaker", "microphone")
        with (
            mock.patch.object(
                pulse,
                "route_is_active",
                new=mock.AsyncMock(side_effect=RuntimeError("Docker command timed out.")),
            ),
            mock.patch.object(pulse.asyncio, "sleep", new=mock.AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "after retrying"):
                await pulse.route_is_active_resilient(mock.Mock(), route)

    async def test_stale_route_is_rediscovered_without_restarting_bridge(self):
        original = pulse.PulseRoute("10", "20", "speaker", "microphone")
        replacement = pulse.PulseRoute("11", "21", "speaker", "microphone")
        moves = []

        async def move(_config, route):
            moves.append(route)
            if len(moves) == 1:
                raise RuntimeError("Failure: No such entity")

        with (
            mock.patch.object(pulse, "move_to_bridge", side_effect=move),
            mock.patch.object(pulse, "find_wechat_route", new=mock.AsyncMock(return_value=replacement)),
            mock.patch.object(pulse.asyncio, "sleep", new=mock.AsyncMock()),
        ):
            attached = await pulse.move_to_bridge_resilient(mock.Mock(), original)

        self.assertEqual(attached, replacement)
        self.assertEqual(moves, [original, replacement])

    def test_partially_moved_ids_keep_original_restore_devices(self):
        original = pulse.PulseRoute("10", "20", "speaker", "microphone")
        observed = pulse.PulseRoute("10", "21", pulse.CALL_PLAYBACK_SINK, "new-microphone")
        merged = pulse.preserve_previous_route(original, observed)
        self.assertEqual(merged.previous_sink, "speaker")
        self.assertEqual(merged.previous_source, "new-microphone")


class VoiceRotationTests(unittest.TestCase):
    def test_codex_config_extends_shared_pulse_settings(self):
        config = bridge.Config("agent-wechat", 90, "ws://127.0.0.1", "token")
        self.assertIsInstance(config, pulse.Config)
        self.assertIs(bridge.pulse, pulse)

    def test_default_rotation_uses_only_desktop_v1_voices(self):
        self.assertEqual(
            bridge.VOICE_ROTATION,
            ("juniper", "maple", "spruce", "ember", "breeze", "arbor", "sol", "cove"),
        )

    def test_rotation_persists_and_wraps_between_bridge_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "voice.json"
            voices = ("ember", "spruce", "cove")
            self.assertEqual(bridge.next_voice(voices, state), "ember")
            self.assertEqual(bridge.next_voice(voices, state), "spruce")
            self.assertEqual(bridge.next_voice(voices, state), "cove")
            self.assertEqual(bridge.next_voice(voices, state), "ember")
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
