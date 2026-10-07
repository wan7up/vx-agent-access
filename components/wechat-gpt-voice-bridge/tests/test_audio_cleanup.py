import asyncio
import re
import sys
import unittest
from unittest import mock

import pulse_audio as pulse


class AudioOwnerTests(unittest.TestCase):
    def test_manual_calls_have_distinct_owners(self):
        with mock.patch.dict(pulse.os.environ, {}, clear=True):
            first = pulse.Config("agent-wechat", 90)
            second = pulse.Config("agent-wechat", 90)
        self.assertNotEqual(first.audio_owner, second.audio_owner)
        self.assertTrue(pulse.audio_client_name(first, "capture").endswith("-capture"))

    def test_service_cleanup_reuses_invocation_owner(self):
        with mock.patch.dict(pulse.os.environ, {"INVOCATION_ID": "a" * 32}):
            first = pulse.Config("agent-wechat", 90)
            stop_post = pulse.Config("agent-wechat", 90)
        self.assertEqual(first.audio_owner, stop_post.audio_owner)

    def test_invalid_owner_or_role_fails_closed(self):
        with self.assertRaises(ValueError):
            pulse.audio_client_name(pulse.Config("agent-wechat", 90, audio_owner=".*"), "capture")
        with self.assertRaises(ValueError):
            pulse.audio_client_name(pulse.Config("agent-wechat", 90), ".*")


class AudioCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = pulse.Config("agent-wechat", 90, audio_owner="a" * 32)

    async def test_container_cleanup_runs_even_if_docker_client_has_exited(self):
        process = mock.Mock(returncode=0, communicate=mock.AsyncMock(return_value=(b"", b"")))
        with mock.patch.object(pulse, "docker", new=mock.AsyncMock(return_value=b"")) as docker:
            await pulse.stop_audio_processes(self.config, process)
        command = docker.await_args_list[0].args
        self.assertIn("pkill", command)
        self.assertIn("-TERM", command)
        self.assertEqual(command[-1], "^(parec|pacat) --client-name=headmao-voice-" + "a" * 32 + "-(capture|playback)( |$)")
        process.terminate.assert_not_called()

    async def test_capture_restart_does_not_match_playback_or_other_calls(self):
        with mock.patch.object(pulse, "docker", new=mock.AsyncMock(return_value=b"")) as docker:
            await pulse.stop_audio_processes(self.config, role="capture")
        pattern = docker.await_args_list[0].args[-1]
        self.assertTrue(pattern.endswith("-capture( |$)"))
        self.assertIsNotNone(re.search(pattern, "parec --client-name=headmao-voice-" + "a" * 32 + "-capture --raw"))
        self.assertIsNone(re.search(pattern, "pacat --client-name=headmao-voice-" + "a" * 32 + "-playback --playback"))
        self.assertIsNone(re.search(pattern, "parec --client-name=headmao-voice-" + "b" * 32 + "-capture --raw"))
        self.assertIsNone(re.search(pattern, "parec --raw --device=unrelated"))

    async def test_cleanup_is_idempotent_without_any_audio_processes(self):
        with mock.patch.object(pulse, "docker", new=mock.AsyncMock(return_value=b"")) as docker:
            await pulse.stop_audio_processes(self.config, None, None)
            await pulse.stop_audio_processes(self.config, None, None)
        self.assertEqual(docker.await_count, 4)
        self.assertTrue(all(call.kwargs["allowed_returncodes"] == (0, 1) for call in docker.await_args_list))

    async def test_unresponsive_process_escalates_to_kill(self):
        async def docker(_config, *args, **kwargs):
            nonlocal killed
            if "pgrep" in args:
                return b"" if killed else b"123\n"
            if "-KILL" in args:
                killed = True
            return b""

        killed = False
        with (
            mock.patch.object(pulse, "docker", new=mock.AsyncMock(side_effect=docker)),
            mock.patch.object(pulse.asyncio, "sleep", new=mock.AsyncMock()),
        ):
            await pulse.stop_audio_processes(self.config)
        self.assertTrue(killed)

    async def test_container_failure_still_reaps_host_client(self):
        process = mock.Mock(returncode=None, communicate=mock.AsyncMock(return_value=(b"", b"")))
        with mock.patch.object(pulse, "docker", new=mock.AsyncMock(side_effect=RuntimeError("unavailable"))):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                await pulse.stop_audio_processes(self.config, process)
        process.terminate.assert_called_once()
        process.communicate.assert_awaited_once()

    async def test_docker_failure_is_not_mistaken_for_no_matching_process(self):
        process = mock.Mock(
            returncode=1,
            communicate=mock.AsyncMock(return_value=(b"", b"Cannot connect to the Docker daemon")),
        )
        with mock.patch.object(pulse.asyncio, "create_subprocess_exec", new=mock.AsyncMock(return_value=process)):
            with self.assertRaisesRegex(RuntimeError, "Docker daemon"):
                await pulse.docker(self.config, "exec", allowed_returncodes=(0, 1))

    async def test_persistent_residual_is_reported_not_silently_ignored(self):
        with (
            mock.patch.object(pulse, "docker", new=mock.AsyncMock(return_value=b"123\n")),
            mock.patch.object(pulse.asyncio, "sleep", new=mock.AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not stop"):
                await pulse.stop_audio_processes(self.config)

    async def test_stop_process_tolerates_exit_during_termination(self):
        process = mock.Mock(
            returncode=None,
            terminate=mock.Mock(side_effect=ProcessLookupError),
            communicate=mock.AsyncMock(return_value=(b"", b"")),
        )
        await pulse.stop_process(process)

    async def test_stop_process_drains_blocked_capture_pipe(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import os, time; os.write(1, bytes(1024 * 1024)); time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.sleep(0.1)
            await asyncio.wait_for(pulse.stop_process(process), 8)
            self.assertIsNotNone(process.returncode)
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()


if __name__ == "__main__":
    unittest.main()
