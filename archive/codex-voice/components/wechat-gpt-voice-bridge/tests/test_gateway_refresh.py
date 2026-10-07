import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "codex-voice-gateway-refresh.sh"


class GatewayRefreshTests(unittest.TestCase):
    def run_script(self, curl_body: str):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls"

            (bin_dir / "curl").write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    echo curl >> "{calls}"
                    {curl_body}
                    """
                ),
                encoding="utf-8",
            )
            (bin_dir / "systemctl").write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    echo "systemctl $*" >> "{calls}"
                    """
                ),
                encoding="utf-8",
            )
            (bin_dir / "sleep").write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    echo sleep >> "{calls}"
                    """
                ),
                encoding="utf-8",
            )
            for executable in bin_dir.iterdir():
                executable.chmod(0o755)

            result = subprocess.run(
                ["bash", str(SCRIPT)],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
            )
            recorded = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
            return result, recorded

    def test_healthy_gateway_is_still_restarted_to_reset_webrtc_state(self):
        result, calls = self.run_script("exit 0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["systemctl restart codex-voice-gateway.service", "curl"])

    def test_gateway_is_restarted_once_then_waited_for(self):
        result, calls = self.run_script(
            textwrap.dedent(
                """\
                count_file="$(dirname "$0")/../curl-count"
                count=0
                [ ! -f "$count_file" ] || count=$(cat "$count_file")
                count=$((count + 1))
                echo "$count" > "$count_file"
                [ "$count" -ge 3 ]
                """
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count("systemctl restart codex-voice-gateway.service"), 1)
        self.assertEqual(calls.count("curl"), 3)
        self.assertEqual(calls.count("sleep"), 2)


if __name__ == "__main__":
    unittest.main()
