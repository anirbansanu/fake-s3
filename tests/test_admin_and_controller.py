"""Phase 2 checks: admin API endpoints and the in-process ServerController."""

import json
import urllib.request

import pytest


def admin_get(endpoint: str, path: str) -> dict:
    with urllib.request.urlopen(f"{endpoint}/_fakes3/{path}", timeout=5) as resp:
        return json.loads(resp.read())


# -- admin API over the wire (subprocess server from conftest) ----------------

def test_admin_status(multi_server):
    data = admin_get(multi_server.endpoint, "status")
    assert data["status"] == "running"
    assert data["version"]
    assert data["config"]["single_bucket"] is False
    assert data["uptime_seconds"] >= 0


def test_admin_stats_counts_requests(multi_server, s3):
    before = admin_get(multi_server.endpoint, "stats")
    s3.list_buckets()
    after = admin_get(multi_server.endpoint, "stats")
    assert after["requests"] > before["requests"]
    assert "storage" in after
    assert after["by_method"].get("GET", 0) > 0


def test_admin_logs_capture_access(multi_server, s3):
    s3.list_buckets()
    data = admin_get(multi_server.endpoint, "logs")
    assert any("GET" in entry["message"] for entry in data["logs"])


def test_admin_config(multi_server):
    data = admin_get(multi_server.endpoint, "config")
    assert data["port"] == multi_server.port


def test_admin_prefix_not_a_bucket(s3):
    # A bucket named "_fakes3" is impossible, so no collision is possible;
    # S3 clients never see the admin routes.
    from botocore.exceptions import ClientError, ParamValidationError
    with pytest.raises((ClientError, ParamValidationError)):
        s3.create_bucket(Bucket="_fakes3")


# -- in-process controller ----------------------------------------------------

def test_controller_lifecycle(tmp_path):
    from fakes3.config import ServerConfig
    from fakes3.server.controller import ServerController
    from tests.conftest import free_port

    events = []
    config = ServerConfig(storage_root=tmp_path / "storage",
                          host="127.0.0.1", port=free_port())
    controller = ServerController(config, on_event=lambda e, m: events.append(e))

    controller.start()
    assert controller.is_running
    assert controller.uptime_seconds >= 0

    status = admin_get(controller.endpoint, "status")
    assert status["status"] == "running"

    controller.restart()
    assert controller.is_running
    assert admin_get(controller.endpoint, "status")["status"] == "running"

    controller.stop()
    assert not controller.is_running
    assert events == ["started", "stopped", "started", "restarted", "stopped"]


def test_controller_start_error_port_conflict(tmp_path, multi_server):
    from fakes3.config import ServerConfig
    from fakes3.server.controller import ServerController

    config = ServerConfig(storage_root=tmp_path / "storage",
                          host="127.0.0.1", port=multi_server.port)
    controller = ServerController(config)
    with pytest.raises(RuntimeError):
        controller.start()
    assert not controller.is_running
