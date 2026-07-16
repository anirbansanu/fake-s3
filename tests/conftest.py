"""
Shared fixtures: launch a fakes3 server subprocess with an isolated storage
directory and yield a configured boto3 client.

The server entry point is auto-detected (package `fakes3` if present, else the
legacy single-file `fakes3.py`) and can be forced with FAKES3_TEST_TARGET:
    FAKES3_TEST_TARGET="python fakes3.py"          (any command line)
    FAKES3_TEST_TARGET="dist/fakes3-cli.exe serve" (built executable)
"""

import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import boto3
import pytest
from botocore.config import Config as BotoConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_target() -> list[str]:
    if (PROJECT_ROOT / "fakes3" / "__main__.py").is_file():
        return [sys.executable, "-m", "fakes3"]
    return [sys.executable, str(PROJECT_ROOT / "fakes3.py")]


def server_command() -> list[str]:
    override = os.environ.get("FAKES3_TEST_TARGET")
    if override:
        return shlex.split(override)
    return default_target()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerHandle:
    def __init__(self, port: int, storage: Path, process: subprocess.Popen, log_file: Path):
        self.port = port
        self.storage = storage
        self.process = process
        self.log_file = log_file
        self.endpoint = f"http://127.0.0.1:{port}"

    def log_output(self) -> str:
        try:
            return self.log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def client(self):
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id="local",
            aws_secret_access_key="local",
            region_name="us-east-1",
            config=BotoConfig(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 1},
                request_checksum_calculation="when_supported",
            ),
        )


def start_server(tmp_path: Path, *, single_bucket: bool, extra_env: dict | None = None) -> ServerHandle:
    port = free_port()
    storage = tmp_path / "storage"
    env = os.environ.copy()
    env.update({
        "FAKE_S3_PORT": str(port),
        "FAKE_S3_STORAGE": str(storage),
        "FAKE_S3_SINGLE_BUCKET": "1" if single_bucket else "0",
    })
    env.update(extra_env or {})
    # Server output goes to a file: writing to an unread PIPE would fill the
    # buffer after enough access-log lines and deadlock the server mid-request.
    log_file = tmp_path / "server.log"
    log_handle = log_file.open("wb")
    process = subprocess.Popen(
        server_command(),
        cwd=PROJECT_ROOT,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.close()  # the child keeps its own handle
    deadline = time.time() + 30
    last_error = None
    while time.time() < deadline:
        if process.poll() is not None:
            output = log_file.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"server exited early ({process.returncode}):\n{output}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                return ServerHandle(port, storage, process, log_file)
        except Exception as exc:  # noqa: BLE001 - retry until deadline
            last_error = exc
            time.sleep(0.2)
    process.kill()
    raise RuntimeError(f"server did not become healthy: {last_error}")


def stop_server(handle: ServerHandle) -> None:
    if sys.platform == "win32":
        # Kill the whole tree: PyInstaller onefile exes run the real server in
        # a child process that plain kill() would orphan.
        subprocess.run(["taskkill", "/PID", str(handle.process.pid), "/T", "/F"],
                       capture_output=True)
    else:
        handle.process.kill()
    handle.process.wait(timeout=10)


@pytest.fixture(scope="module")
def single_server(tmp_path_factory):
    handle = start_server(tmp_path_factory.mktemp("single"), single_bucket=True)
    yield handle
    stop_server(handle)


@pytest.fixture(scope="module")
def multi_server(tmp_path_factory):
    handle = start_server(tmp_path_factory.mktemp("multi"), single_bucket=False)
    yield handle
    stop_server(handle)


@pytest.fixture(scope="module")
def s3(multi_server):
    """Default client: multi-bucket mode (closest to real S3 semantics)."""
    return multi_server.client()
