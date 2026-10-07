import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/doctor.sh"


class DoctorTests(unittest.TestCase):
    def run_doctor(self, auth=None, chats=None, cache_status=200, service_status=0):
        auth = {"status": "logged_in"} if auth is None else auth
        chats = [{"id": "test@chatroom"}] if chats is None else chats
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path == "/healthz":
                    status, payload = cache_status, {"stats": {}}
                elif self.headers.get("Authorization") != "Bearer test-secret":
                    status, payload = 401, {}
                elif self.path == "/api/status/auth":
                    status, payload = 200, auth
                else:
                    status, payload = 200, chats
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "python3").symlink_to(sys.executable)
                for name in ("systemctl", "openclaw"):
                    command = root / name
                    command.write_text(f"#!/bin/sh\nexit {service_status if name == 'systemctl' else 0}\n")
                    command.chmod(0o755)
                token = root / "token"
                token.write_text("test-secret\n")
                base = f"http://127.0.0.1:{server.server_port}"
                env = dict(os.environ, PATH=directory, ENV_FILE=str(root / "absent.env"),
                           AGENT_WECHAT_UPSTREAM_URL=base, AGENT_WECHAT_URL="http://invalid",
                           AGENT_WECHAT_CACHE_URL=base, AGENT_WECHAT_TOKEN_FILE=str(token))
                result = subprocess.run(
                    ["/bin/bash", str(SCRIPT)], env=env, text=True, capture_output=True, timeout=10,
                )
                return result, requests
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_healthy_readonly_chain_passes(self):
        result, requests = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("real incoming-message reply and voice still need separate validation", result.stdout)
        self.assertEqual(requests, ["/api/status/auth", "/api/chats?limit=5", "/healthz"])
        self.assertNotIn("test-secret", result.stdout + result.stderr)

    def test_logged_out_fails_before_chat_checks(self):
        result, requests = self.run_doctor(auth={"status": "logged_out"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not logged in", result.stderr)
        self.assertEqual(requests, ["/api/status/auth"])

    def test_logged_in_but_empty_chat_list_fails(self):
        result, _ = self.run_doctor(chats=[])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("database keys", result.stderr)

    def test_invalid_chat_payload_fails(self):
        result, _ = self.run_doctor(chats={"error": "database unavailable"})
        self.assertNotEqual(result.returncode, 0)

    def test_cache_failure_is_not_reported_as_healthy(self):
        result, _ = self.run_doctor(cache_status=502)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cache health failed", result.stderr)

    def test_inactive_service_fails(self):
        result, _ = self.run_doctor(service_status=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("service not active", result.stderr)


if __name__ == "__main__":
    unittest.main()
