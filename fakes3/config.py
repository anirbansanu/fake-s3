"""
Server configuration.

Precedence (applied by the callers): explicit arguments > FAKE_S3_* environment
variables > persisted config file > dataclass defaults. The environment names
match the original single-file fakes3.py, so existing setups keep working.
"""

import json
import os
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Mapping


def _truthy(value: str) -> bool:
    return value.lower() not in {"0", "false", "no"}


DEFAULT_VHOST_BASES = ["localhost", "host.docker.internal"]


@dataclass
class ServerConfig:
    storage_root: Path = Path("storage")
    host: str = "0.0.0.0"
    port: int = 9000
    region: str = "us-east-1"
    single_bucket: bool = True
    bucket_name: str = "mybucket"
    auto_create: bool = True
    vhost_bases: list[str] = field(default_factory=lambda: list(DEFAULT_VHOST_BASES))

    def __post_init__(self):
        self.storage_root = Path(self.storage_root)
        self.vhost_bases = [h.strip().lower() for h in self.vhost_bases if h.strip()]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None,
                 base: "ServerConfig | None" = None) -> "ServerConfig":
        """Config from FAKE_S3_* variables, overriding `base` (or defaults)."""
        env = os.environ if env is None else env
        config = base if base is not None else cls()
        values = asdict(config)
        if "FAKE_S3_STORAGE" in env:
            values["storage_root"] = Path(env["FAKE_S3_STORAGE"])
        if "FAKE_S3_HOST" in env:
            values["host"] = env["FAKE_S3_HOST"]
        if "FAKE_S3_PORT" in env:
            values["port"] = int(env["FAKE_S3_PORT"])
        if "FAKE_S3_REGION" in env:
            values["region"] = env["FAKE_S3_REGION"]
        if "FAKE_S3_SINGLE_BUCKET" in env:
            values["single_bucket"] = _truthy(env["FAKE_S3_SINGLE_BUCKET"])
        if "FAKE_S3_BUCKET_NAME" in env:
            values["bucket_name"] = env["FAKE_S3_BUCKET_NAME"]
        if "FAKE_S3_AUTO_CREATE" in env:
            values["auto_create"] = _truthy(env["FAKE_S3_AUTO_CREATE"])
        if "FAKE_S3_VHOST_BASES" in env:
            values["vhost_bases"] = env["FAKE_S3_VHOST_BASES"].split(",")
        return cls(**values)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["storage_root"] = str(self.storage_root)
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> "ServerConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def config_dir() -> Path:
    """Per-user config directory (%APPDATA%\\fakes3 on Windows)."""
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "fakes3"
    return Path.home() / ".config" / "fakes3"


def config_file() -> Path:
    return config_dir() / "config.json"


def load_file_config(path: Path | None = None) -> dict:
    """Raw persisted settings (server + app preferences), {} if none."""
    target = path or config_file()
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_file_config(data: dict, path: Path | None = None) -> Path:
    target = path or config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return target


def load_server_config(env: Mapping[str, str] | None = None) -> ServerConfig:
    """File config overridden by environment variables — the standard chain."""
    stored = load_file_config().get("server", {})
    base = ServerConfig.from_dict(stored) if stored else ServerConfig()
    return ServerConfig.from_env(env, base=base)
