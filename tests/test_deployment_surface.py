import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentSurfaceTests(unittest.TestCase):
    def test_desktop_uses_init_to_reap_orphaned_processes(self):
        compose = (ROOT / "deploy/desktop-rebuild/compose.yaml").read_text()
        self.assertRegex(compose, r"(?m)^    init: true$")

    def test_desktop_bounds_background_a11y_probes(self):
        dockerfile = (ROOT / "deploy/desktop-rebuild/Dockerfile").read_text()
        wrapper = ROOT / "deploy/desktop-rebuild/a11y-dump-wrapper.py"
        self.assertTrue(wrapper.is_file())
        self.assertIn("a11y-dump.real", dockerfile)
        self.assertIn("a11y-dump-wrapper.py", dockerfile)
        self.assertIn("LOCK_NB", wrapper.read_text())
        self.assertIn("timeout=TIMEOUT_SECONDS", wrapper.read_text())

    def test_repeated_image_overlay_preserves_original_probe(self):
        dockerfile = (ROOT / "deploy/desktop-rebuild/Dockerfile").read_text()
        self.assertIn("if [ ! -f /opt/tools/a11y-dump.real ]", dockerfile)

    def test_desktop_allows_slow_cold_pulseaudio_start(self):
        entrypoint = (ROOT / "deploy/desktop-rebuild/entrypoint.sh").read_text()
        self.assertIn("wait_for --attempts 600 pactl info", entrypoint)
        self.assertIn('for pulse_runtime in "$HOME/.config/pulse"/*-runtime', entrypoint)

    def test_desktop_runtime_does_not_persist_audio_sockets(self):
        compose = (ROOT / "deploy/desktop-rebuild/compose.yaml").read_text()
        self.assertIn("/run/user/1000:uid=1000,gid=1000,mode=0700,size=32m", compose)
        self.assertIn('ENABLE_VNC: "${ENABLE_VNC:-0}"', compose)

    def test_server_artifact_is_embedded_and_verified(self):
        source = (ROOT / "deploy/desktop-rebuild/Dockerfile.server").read_text()
        self.assertIn("COPY --chown=wechat:wechat agent-server.release", source)
        self.assertIn('ARG SERVER_SHA256', source)
        self.assertIn("sha256sum -c -", source)

    def test_desktop_entry_scan_recovers_from_transient_atspi_nodes(self):
        source = (ROOT / "deploy/desktop-rebuild/enter-session.py").read_text()
        self.assertIn("from gi.repository import Atspi, GLib", source)
        self.assertIn("except GLib.Error", source)
        self.assertIn("starting agent-server", source)

    def test_all_installed_sources_exist(self):
        script = (ROOT / "scripts/install-services.sh").read_text()
        sources = re.findall(r'install -m \d+ "\$ROOT/([^"]+)"', script)
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue((ROOT / source).is_file())

    def test_installer_does_not_reinstall_legacy_desktop_patches(self):
        script = (ROOT / "scripts/install-services.sh").read_text()
        for obsolete in (
            "entry-watchdog", "mention-repair", "cron-delivery-repair",
            "kill-wrapper", "a11y-cache-wrapper", "launch-wechat-with-pulse",
            "docker cp",
        ):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, script)

    def test_doctor_requires_login_and_nonempty_chat_list(self):
        script = (ROOT / "scripts/doctor.sh").read_text()
        self.assertIn('data.get("status") != "logged_in"', script)
        self.assertIn("not isinstance(data, list) or not data", script)
        self.assertNotIn("entry-watchdog.service", script)
        self.assertNotIn("mention-repair.service", script)

    def test_incoming_units_do_not_depend_on_retired_watchdog(self):
        for source in (
            "services/agent-wechat-incoming-call.service",
            "components/xiaozhi-vx-bridge/deploy/agent-wechat-incoming-call-xiaozhi.service",
        ):
            with self.subTest(source=source):
                self.assertNotIn("entry-watchdog.service", (ROOT / source).read_text())


if __name__ == "__main__":
    unittest.main()
