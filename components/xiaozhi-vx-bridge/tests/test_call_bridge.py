import asyncio
import json
import sys
import unittest
from unittest import mock

sys.modules.setdefault("opuslib", mock.Mock())
sys.modules.setdefault("websockets", mock.Mock())
import call_bridge


class CallBridgeTests(unittest.TestCase):
    def test_tts_metrics_count_decoded_audio_without_retaining_content(self):
        metrics = call_bridge.TtsAudioMetrics(started_at=10)
        metrics.record(480, 0, 10.5)
        metrics.record(480, 200, 10.54)
        metrics.record(480, 300, 11.2)
        self.assertEqual((metrics.frames, metrics.samples), (3, 1440))
        self.assertEqual((metrics.above_floor_frames, metrics.peak_mean_abs), (2, 300))
        self.assertEqual(metrics.first_frame_at, 10.5)
        self.assertAlmostEqual(metrics.max_gap_seconds, 0.66)
        self.assertNotIn("pcm", vars(metrics))

    def test_pcm_level_distinguishes_silence_without_storing_audio(self):
        self.assertEqual(call_bridge.pcm_mean_abs(bytes(640)), 0)
        self.assertEqual(call_bridge.pcm_mean_abs(b"\xc8\x00" * 320), 200)

    def test_only_shared_pulse_module_is_loaded_for_audio_routing(self):
        self.assertEqual(call_bridge.pulse.__name__, "pulse_audio")
        config = call_bridge.pulse.Config("agent-wechat", 90)
        self.assertEqual((config.container, config.call_wait_seconds), ("agent-wechat", 90))
        self.assertFalse(hasattr(config, "gateway_url"))

    def test_listen_control_is_scoped_to_one_session(self):
        with mock.patch.object(call_bridge, "MODE", "realtime"):
            self.assertEqual(
                json.loads(call_bridge.listen_start("session-1")),
                {
                    "session_id": "session-1",
                    "type": "listen",
                    "state": "start",
                    "mode": "realtime",
                },
            )

    def test_probe_and_call_use_the_same_input_format(self):
        self.assertEqual(call_bridge.INPUT_RATE, 16000)
        self.assertEqual(call_bridge.INPUT_BYTES, 640)
        self.assertEqual(call_bridge.INPUT_SAMPLES, 320)

class CallBridgeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def check_teardown(self, ending=None, capture_error=None):
        async def incoming():
            await asyncio.Future()
            yield b""

        async def wait_forever(*args):
            await asyncio.Future()

        connection = mock.MagicMock()
        connection.recv = mock.AsyncMock(return_value=json.dumps({
            "type": "hello", "transport": "websocket", "session_id": "test-session",
            "audio_params": {"sample_rate": 24000},
        }))
        connection.send = mock.AsyncMock()
        connection.__aiter__.side_effect = incoming
        context = mock.MagicMock()
        context.__aenter__ = mock.AsyncMock(return_value=connection)
        context.__aexit__ = mock.AsyncMock(return_value=False)
        route = call_bridge.pulse.PulseRoute("10", "20", "speaker", "microphone")
        capture, playback = mock.Mock(), mock.Mock()
        with (
            mock.patch.object(asyncio.get_running_loop(), "add_signal_handler") as signal_handler,
            mock.patch.object(call_bridge.device, "load_or_create_identity", return_value={
                "device_id": "test-device", "client_id": "test-client",
                "network": {"websocket_url": "wss://example.invalid", "websocket_token": "test-only"},
            }),
            mock.patch.object(call_bridge.device, "ota", return_value={}),
            mock.patch.object(call_bridge.websockets, "connect", return_value=context),
            mock.patch.object(call_bridge.pulse, "ensure_pulse_routes", new=mock.AsyncMock()),
            mock.patch.object(call_bridge.pulse, "wait_for_call_route", new=mock.AsyncMock(return_value=route)),
            mock.patch.object(call_bridge.pulse, "move_to_bridge_resilient", new=mock.AsyncMock(return_value=route)),
            mock.patch.object(call_bridge, "start_capture", new=mock.AsyncMock(return_value=capture, side_effect=capture_error)),
            mock.patch.object(call_bridge, "start_playback", new=mock.AsyncMock(return_value=playback)),
            mock.patch.object(call_bridge, "send_microphone", side_effect=wait_forever),
            mock.patch.object(call_bridge, "log_microphone_health", side_effect=wait_forever),
            mock.patch.object(call_bridge, "watch_call_end", new=mock.AsyncMock(side_effect=ending)),
            mock.patch.object(call_bridge.pulse, "stop_audio_processes", new=mock.AsyncMock()) as stop,
            mock.patch.object(call_bridge.pulse, "restore_route", new=mock.AsyncMock()) as restore,
        ):
            error = capture_error or ending
            if error:
                with self.assertRaises(type(error)):
                    await call_bridge.run_call("test@chatroom")
            else:
                await call_bridge.run_call("test@chatroom")
        stop.assert_awaited_once_with(mock.ANY, None if capture_error else capture, playback)
        restore.assert_awaited_once_with(mock.ANY, route)
        self.assertEqual(signal_handler.call_args.args[0], call_bridge.signal.SIGTERM)

    async def test_normal_hangup_cleans_container_audio_and_restores_route(self):
        await self.check_teardown()

    async def test_service_cancellation_cleans_container_audio_and_restores_route(self):
        await self.check_teardown(ending=asyncio.CancelledError())

    async def test_partial_audio_start_failure_still_cleans_playback(self):
        await self.check_teardown(capture_error=RuntimeError("capture unavailable"))

    async def test_opening_starts_listening_then_sends_one_wake_event(self):
        connection = mock.Mock(send=mock.AsyncMock())
        await call_bridge.start_voice_with_greeting(connection, "session-1")
        messages = [json.loads(item.args[0]) for item in connection.send.await_args_list]
        self.assertEqual(messages, [
            {"session_id": "session-1", "type": "listen", "state": "start", "mode": call_bridge.MODE},
            {"session_id": "session-1", "type": "listen", "state": "detect", "text": "你好"},
        ])

    async def test_microphone_sends_20ms_opus_frames_until_route_ends(self):
        capture = mock.Mock()
        capture.stdout = asyncio.StreamReader()
        capture.stdout.feed_data(bytes(call_bridge.INPUT_BYTES))
        capture.stdout.feed_eof()
        connection = mock.Mock(send=mock.AsyncMock())
        route = call_bridge.pulse.PulseRoute("10", "20", "speaker", "microphone")
        encoder = mock.Mock(encode=mock.Mock(return_value=b"opus"))
        with (
            mock.patch.object(call_bridge.opuslib, "Encoder", return_value=encoder),
            mock.patch.object(call_bridge.pulse, "route_is_active_resilient", new=mock.AsyncMock(return_value=False)),
        ):
            await call_bridge.send_microphone(connection, mock.Mock(), [route], [capture])
        encoder.encode.assert_called_once_with(bytes(call_bridge.INPUT_BYTES), 320)
        connection.send.assert_awaited_once_with(b"opus")

    async def test_capture_requests_pulse_resampling_without_new_local_audio_devices(self):
        with mock.patch.object(
            asyncio, "create_subprocess_exec", new=mock.AsyncMock(return_value=mock.sentinel.process)
        ) as create:
            self.assertIs(
                await call_bridge.start_capture(call_bridge.pulse.Config("agent-wechat", 90)),
                mock.sentinel.process,
            )
        command = create.await_args.args
        self.assertIn("--rate=16000", command)
        self.assertIn("--channels=1", command)
        self.assertIn("--device=wechat_call_playback.monitor", command)
        self.assertTrue(any(arg.startswith("--client-name=headmao-voice-") and arg.endswith("-capture") for arg in command))

    async def test_playback_uses_negotiated_server_sample_rate(self):
        with mock.patch.object(
            asyncio, "create_subprocess_exec", new=mock.AsyncMock(return_value=mock.sentinel.process)
        ) as create:
            await call_bridge.start_playback(call_bridge.pulse.Config("agent-wechat", 90), 24000)
        self.assertIn("--rate=24000", create.await_args.args)
        self.assertIn("--device=gpt_voice_inject", create.await_args.args)
        self.assertTrue(any(arg.startswith("--client-name=headmao-voice-") and arg.endswith("-playback") for arg in create.await_args.args))

    async def test_non_group_call_fails_before_touching_device_or_pulse(self):
        with mock.patch.object(call_bridge.device, "load_or_create_identity") as load:
            with self.assertRaisesRegex(ValueError, "group chatroom"):
                await call_bridge.run_call("private-contact")
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
