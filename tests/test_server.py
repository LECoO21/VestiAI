import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "macos" / "server.py"


@unittest.skipUnless(os.name == "posix" and shutil.which("ps"), "macOS/Linux launcher")
class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.state = self.directory / "state"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("FITMIX_", "DASHSCOPE_")) and key != "WARDROBE_DATA_DIR"
        }
        self.environment.update(
            FITMIX_RUNTIME_DIR=str(self.state),
            FITMIX_CONFIG_PATH=str(self.directory / "no-credentials.env"),
            WARDROBE_DATA_DIR=str(self.directory / "wardrobe"),
            PYTHONDONTWRITEBYTECODE="1",
        )

    def run_launcher(self, action):
        return subprocess.run(
            [sys.executable, "-B", str(LAUNCHER), action, "--port", str(self.port)],
            env=self.environment, cwd=ROOT, capture_output=True, text=True, timeout=30,
        )

    def tearDown(self):
        self.run_launcher("stop")
        self.temporary.cleanup()

    def test_server_survives_launcher_exit_and_reuses_running_instance(self):
        launcher = subprocess.Popen(
            [sys.executable, "-B", str(LAUNCHER), "start", "--port", str(self.port)],
            env=self.environment, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        stdout, stderr = launcher.communicate(timeout=30)
        self.assertEqual(launcher.returncode, 0, stderr)
        self.assertIn("已就绪", stdout)
        record = json.loads((self.state / "server.json").read_text())
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{self.port}/api/health", timeout=3) as response:
            ready = json.load(response)
        self.assertEqual(ready["pid"], record["pid"])
        self.assertEqual(ready["server_instance"], record["instance"])
        self.assertFalse(ready["credentials_configured"])
        parent_pid = int(subprocess.check_output(["ps", "-p", str(record["pid"]), "-o", "ppid="], text=True).strip())
        self.assertNotEqual(parent_pid, launcher.pid)
        again = self.run_launcher("start")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads((self.state / "server.json").read_text())["pid"], record["pid"])
        stopped = self.run_launcher("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertFalse((self.state / "server.json").exists())
        with self.assertRaises(OSError):
            opener.open(f"http://127.0.0.1:{self.port}/api/health", timeout=2)

    def test_occupied_port_is_reported_without_claiming_success(self):
        with socket.socket() as other_service:
            other_service.bind(("127.0.0.1", self.port))
            other_service.listen()
            result = self.run_launcher("start")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("已被其他服务占用", result.stderr)
            self.assertNotIn("已就绪", result.stdout)
            self.assertFalse((self.state / "server.json").exists())
            self.assertEqual(other_service.getsockname()[1], self.port)


if __name__ == "__main__":
    unittest.main()
