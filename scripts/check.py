"""Run VestiAI's existing checks without opening real data or model connections."""
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    for name in list(os.environ):
        if name.startswith(("FITMIX_", "DASHSCOPE_")) or name == "WARDROBE_DATA_DIR":
            os.environ.pop(name)
    with tempfile.TemporaryDirectory(prefix="fitmix-check-") as temporary:
        os.environ.update(
            WARDROBE_DATA_DIR=str(Path(temporary) / "data"),
            FITMIX_CONFIG_PATH=str(Path(temporary) / "no-credentials.env"),
            FITMIX_RECOVERY_DISABLED="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        import requests
        def block_network(*args, **kwargs):
            raise AssertionError("自动测试禁止访问真实模型服务，请使用模拟服务")
        requests.sessions.Session.request = block_network
        try:
            result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover("tests"))
            if not result.wasSuccessful():
                return 1
            node = shutil.which("node")
            if not node:
                print("缺少 Node.js，前端检查未完成，请安装后重新运行。")
                return 1
            for command in (
                [node, "--check", "fitmix/static/app.js"],
                [node, "--check", "fitmix/static/pagination.js"],
                [node, "fitmix/static/pagination.js"],
                [node, "tests/test_frontend_reliability.js"],
                *[ ["bash", "-n", f"scripts/macos/{name}.command"] for name in ("setup","start","stop","build") ],
                [sys.executable, "-B", "-m", "gunicorn", "--check-config", "fitmix.app:app"],
            ):
                subprocess.run(command, check=True)
            print(f"全部检查通过：{result.testsRun} 项测试、前端语法、Mac 脚本和服务配置。")
            return 0
        finally:
            logging.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
