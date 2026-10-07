import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import device


class DeviceIdentityTests(unittest.TestCase):
    def test_identity_is_stable_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "device.json"
            first = device.load_or_create_identity(path)
            second = device.load_or_create_identity(path)
            self.assertEqual(first, second)
            self.assertEqual(path.stat().st_mode & 0o777, stat.S_IRUSR | stat.S_IWUSR)
            self.assertRegex(first["device_id"], r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
            first_octet = int(first["device_id"].split(":", 1)[0], 16)
            self.assertEqual(first_octet & 0x03, 0x02)

    def test_activation_signature_matches_hmac_sha256(self):
        identity = device.create_identity("host", "machine", "02:11:22:33:44:55")
        payload = device.activation_payload(identity, "challenge")
        self.assertEqual(payload["Payload"]["algorithm"], "hmac-sha256")
        self.assertEqual(len(payload["Payload"]["hmac"]), 64)

    def test_pcm_probe_frames_are_20ms_and_padded(self):
        frames = device.pcm_frames(bytes(16000 + 2))
        self.assertEqual(len(frames), 26)
        self.assertTrue(all(len(frame) == 640 for frame in frames))
        self.assertEqual(frames[-1][-638:], bytes(638))
        with self.assertRaises(ValueError):
            device.pcm_frames(bytes(99))

    def test_activation_survives_transient_network_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "device.json"
            original = device.load_or_create_identity(path)
            activation = {"activation": {"challenge": "challenge", "code": "123456"}}
            with (
                mock.patch.object(device, "ota", return_value=activation),
                mock.patch.object(device, "post_json", side_effect=[
                    TimeoutError("read timed out"),
                    URLError("temporary network error"),
                    (202, {}),
                    (200, {}),
                ]) as post,
                mock.patch.object(device.time, "sleep") as sleep,
            ):
                self.assertEqual(device.activate(path, 600), 0)
            self.assertEqual(post.call_count, 4)
            self.assertEqual(sleep.call_count, 3)
            saved = device.load_or_create_identity(path)
            self.assertEqual(saved["device_id"], original["device_id"])
            self.assertTrue(saved["activated"])
            self.assertNotIn("pending_activation", saved)

    def test_renew_unbound_identity_only_after_new_code_is_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "device.json"
            original = device.load_or_create_identity(path)
            with mock.patch.object(device, "ota", side_effect=TimeoutError("offline")):
                with self.assertRaises(TimeoutError):
                    device.renew_unbound_identity(path)
            self.assertEqual(device.load_or_create_identity(path), original)

            with mock.patch.object(device, "ota", return_value={
                "activation": {"challenge": "new-challenge", "code": "654321"}
            }):
                self.assertEqual(device.renew_unbound_identity(path), 0)
            renewed = device.load_or_create_identity(path)
            self.assertNotEqual(renewed["device_id"], original["device_id"])
            self.assertEqual(renewed["pending_activation"]["code"], "654321")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            renewed["activated"] = True
            device.atomic_write_json(path, renewed)
            with mock.patch.object(device, "ota") as ota:
                with self.assertRaisesRegex(RuntimeError, "activated"):
                    device.renew_unbound_identity(path)
                ota.assert_not_called()


if __name__ == "__main__":
    unittest.main()
