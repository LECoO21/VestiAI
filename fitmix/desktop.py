from __future__ import annotations

import os
import subprocess
import traceback
from pathlib import Path


def prepare_environment() -> Path:
    support_dir = Path(
        os.environ.get("FITMIX_SUPPORT_DIR", Path.home() / "Library" / "Application Support" / "FitMix")
    )
    support_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    support_dir.chmod(0o700)
    os.environ["WARDROBE_DATA_DIR"] = str(support_dir / "data")
    os.environ["FITMIX_CONFIG_PATH"] = str(support_dir / ".env")
    os.environ["FITMIX_DESKTOP"] = "1"
    return support_dir


def show_launch_error(message: str) -> None:
    script = 'on run argv\ndisplay alert "FitMix 无法启动" message (item 1 of argv) as critical\nend run'
    subprocess.run(["osascript", "-e", script, message], check=False)


def main() -> None:
    support_dir = prepare_environment()
    from fitmix.app import app
    import webview

    port = int(os.environ.get("FITMIX_HTTP_PORT", "0")) or None
    webview.create_window(
        "FitMix",
        app,
        width=1440,
        height=920,
        min_size=(960, 680),
        background_color="#f4f1ed",
        text_select=True,
        zoomable=True,
        http_port=port,
    )
    webview.start(gui="cocoa", private_mode=False, storage_path=str(support_dir / "webview"))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        support_dir = Path(os.environ.get("FITMIX_SUPPORT_DIR", Path.home() / "Library" / "Application Support" / "FitMix"))
        support_dir.mkdir(parents=True, exist_ok=True)
        (support_dir / "launcher.log").write_text(traceback.format_exc(), encoding="utf-8")
        show_launch_error(str(exc))
