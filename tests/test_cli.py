"""CLI end-to-end tests: every command group against a live server."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_cli(endpoint: str, *args: str, config_home: Path | None = None,
            expect_ok: bool = True) -> str:
    env = {"FAKE_S3_ENDPOINT": endpoint}
    if config_home is not None:
        env["APPDATA"] = str(config_home)
    import os
    full_env = os.environ.copy()
    full_env.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "fakes3.cli", *args],
        cwd=PROJECT_ROOT, env=full_env, capture_output=True, text=True, timeout=60,
    )
    if expect_ok and result.returncode != 0:
        raise AssertionError(
            f"cli {' '.join(args)} failed ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}")
    return result.stdout


@pytest.fixture(scope="module")
def sample_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("cli") / "sample.txt"
    path.write_text("cli test payload", encoding="utf-8")
    return path


def test_cli_version(multi_server):
    out = run_cli(multi_server.endpoint, "--version")
    assert "fakes3" in out


def test_cli_status_and_stats(multi_server):
    assert "running" in run_cli(multi_server.endpoint, "status")
    assert "requests:" in run_cli(multi_server.endpoint, "stats")


def test_cli_bucket_and_object_flow(multi_server, sample_file, tmp_path):
    endpoint = multi_server.endpoint
    run_cli(endpoint, "bucket", "mb", "clibucket")
    assert "clibucket" in run_cli(endpoint, "bucket", "ls")

    run_cli(endpoint, "object", "put", "clibucket", "docs/sample.txt", str(sample_file),
            "--content-type", "text/plain", "--meta", "origin=cli")
    listing = run_cli(endpoint, "object", "ls", "clibucket", "--prefix", "docs/")
    assert "docs/sample.txt" in listing

    stat = run_cli(endpoint, "object", "stat", "clibucket", "docs/sample.txt")
    assert "text/plain" in stat
    assert "origin: cli" in stat

    dest = tmp_path / "downloaded.txt"
    run_cli(endpoint, "object", "get", "clibucket", "docs/sample.txt", str(dest))
    assert dest.read_text(encoding="utf-8") == "cli test payload"

    run_cli(endpoint, "object", "cp", "clibucket", "docs/sample.txt", "docs/copy.txt")
    run_cli(endpoint, "object", "mv", "clibucket", "docs/copy.txt", "archive/moved.txt")
    listing = run_cli(endpoint, "object", "ls", "clibucket")
    assert "archive/moved.txt" in listing
    assert "docs/copy.txt" not in listing

    run_cli(endpoint, "object", "rm", "clibucket", "archive/moved.txt")
    run_cli(endpoint, "object", "rm", "clibucket", "docs/sample.txt")
    run_cli(endpoint, "bucket", "rb", "clibucket")
    assert "clibucket" not in run_cli(endpoint, "bucket", "ls")


def test_cli_logs(multi_server):
    out = run_cli(multi_server.endpoint, "logs", "--limit", "20")
    assert "GET" in out or "PUT" in out


def test_cli_config_lifecycle(multi_server, tmp_path):
    home = tmp_path / "appdata"
    home.mkdir()
    endpoint = multi_server.endpoint

    run_cli(endpoint, "config", "set", "port", "9500", config_home=home)
    assert run_cli(endpoint, "config", "get", "port", config_home=home).strip() == "9500"

    shown = json.loads(run_cli(endpoint, "config", "show", config_home=home))
    assert shown["port"] == 9500

    export_file = tmp_path / "exported.json"
    run_cli(endpoint, "config", "export", str(export_file), config_home=home)
    assert json.loads(export_file.read_text())["server"]["port"] == 9500

    run_cli(endpoint, "config", "unset", "port", config_home=home)
    assert run_cli(endpoint, "config", "get", "port", config_home=home).strip() == "9000"

    run_cli(endpoint, "config", "import", str(export_file), config_home=home)
    assert run_cli(endpoint, "config", "get", "port", config_home=home).strip() == "9500"


def test_cli_doctor(multi_server, tmp_path):
    home = tmp_path / "appdata2"
    home.mkdir()
    out = run_cli(multi_server.endpoint, "doctor", config_home=home)
    assert "server reachable" in out
    assert "all checks passed" in out
