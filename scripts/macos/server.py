"""Start a local server that survives its launcher/terminal closing."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("VESTIAI_RUNTIME_DIR", ROOT / "data"))
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@contextmanager
def server_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "server.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read_record() -> dict | None:
    try:
        record = json.loads((STATE_DIR / "server.json").read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError("服务状态文件无法读取，请检查 data/server.json") from exc
    if (
        not isinstance(record, dict) or type(record.get("pid")) is not int or record["pid"] <= 1
        or type(record.get("port")) is not int or not 1 <= record["port"] <= 65535
        or not isinstance(record.get("instance"), str) or len(record["instance"]) != 32
    ):
        raise RuntimeError("服务状态文件无效，未操作已有进程")
    return record


def process_matches(pid: int) -> bool:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == f"{sys.executable} -m vestiai.app"


def health(port: int) -> dict | None:
    try:
        with HTTP.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            value = json.load(response)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def matches(record: dict) -> bool:
    if not process_matches(record["pid"]):
        return False
    ready = health(record["port"])
    return bool(ready and ready.get("status") == "ok" and ready.get("pid") == record["pid"]
                and ready.get("server_instance") == record["instance"])


def start(port: int) -> dict:
    previous = read_record()
    if previous and matches(previous):
        if previous["port"] != port:
            raise RuntimeError(f"服务已运行在端口 {previous['port']}，请先停止再更换端口")
        return previous
    if previous and process_matches(previous["pid"]):
        raise RuntimeError("原服务仍在运行但未通过健康检查，请检查日志；未重复启动")
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"端口 {port} 已被其他服务占用，未终止其他程序")
    instance = uuid.uuid4().hex
    environment = os.environ.copy()
    environment.update(VESTIAI_HTTP_PORT=str(port), VESTIAI_SERVER_INSTANCE=instance, PYTHONUNBUFFERED="1")
    logfile = STATE_DIR / "server-startup.log"
    with logfile.open("ab") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "vestiai.app"], cwd=ROOT, env=environment,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    record = {"pid": process.pid, "port": port, "instance": instance}
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"服务启动失败，请查看 {logfile}")
            ready = health(port)
            if ready and ready.get("server_instance") == instance and ready.get("pid") == process.pid and ready.get("status") == "ok":
                temporary = STATE_DIR / "server.json.tmp"
                temporary.write_text(json.dumps(record), encoding="utf-8")
                temporary.chmod(0o600)
                os.replace(temporary, STATE_DIR / "server.json")
                return record
            time.sleep(0.2)
        raise RuntimeError(f"服务未及时就绪，请查看 {logfile}")
    except Exception:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise


def stop() -> None:
    record = read_record()
    if not record:
        return
    if not matches(record):
        if process_matches(record["pid"]):
            raise RuntimeError("无法核验原服务身份，未停止进程，请检查服务日志")
        (STATE_DIR / "server.json").unlink(missing_ok=True)
        return
    try:
        os.kill(record["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while process_matches(record["pid"]) and time.monotonic() < deadline:
        time.sleep(0.1)
    if process_matches(record["pid"]):
        raise RuntimeError("服务尚未停止，未强制终止进程")
    (STATE_DIR / "server.json").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="VestiAI 本地后台服务")
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("VESTIAI_HTTP_PORT", "5050")))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须位于 1–65535")
    try:
        with server_lock():
            if args.action == "start":
                record = start(args.port)
                print(f"VestiAI 已就绪：http://127.0.0.1:{record['port']}（后台进程 {record['pid']}）")
            elif args.action == "stop":
                stop()
                print("VestiAI 后台服务已停止")
            else:
                record = read_record()
                print(f"VestiAI 正在运行：http://127.0.0.1:{record['port']}" if record and matches(record) else "VestiAI 当前未运行")
    except (RuntimeError, OSError) as exc:
        parser.exit(1, f"操作未完成：{exc}\n")


if __name__ == "__main__":
    main()
