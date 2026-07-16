"""
fakes3 command-line interface.

Server management (`serve`, `status`, `stats`, `logs`), bucket and object
operations, configuration management, and diagnostics — all against the same
shared backend package the GUI uses.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import click

from .. import __version__
from ..client import ClientError, FakeS3Client
from ..config import (ServerConfig, config_file, load_file_config,
                      load_server_config, save_file_config)

DEFAULT_ENDPOINT = "http://localhost:9000"

BOOL_FIELDS = {"single_bucket", "auto_create"}
INT_FIELDS = {"port"}
LIST_FIELDS = {"vhost_bases"}
CONFIG_FIELDS = {"storage_root", "host", "port", "region", "single_bucket",
                 "bucket_name", "auto_create", "vhost_bases"}


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def fail(message: str) -> "click.exceptions.Exit":
    click.echo(f"error: {message}", err=True)
    return click.exceptions.Exit(1)


def get_client(ctx: click.Context) -> FakeS3Client:
    return FakeS3Client(ctx.obj["endpoint"])


@click.group()
@click.version_option(__version__, prog_name="fakes3")
@click.option("--endpoint", envvar="FAKE_S3_ENDPOINT", default=DEFAULT_ENDPOINT,
              show_default=True, help="Server endpoint for client commands.")
@click.pass_context
def cli(ctx: click.Context, endpoint: str) -> None:
    """fakes3 — a local Amazon S3 server replica for development."""
    ctx.ensure_object(dict)
    ctx.obj["endpoint"] = endpoint


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--host", help="Bind address (default from config: 0.0.0.0).")
@click.option("--port", type=int, help="Listen port (default from config: 9000).")
@click.option("--storage", type=click.Path(), help="Storage root directory.")
@click.option("--region", help="Region reported to clients.")
@click.option("--single-bucket/--multi-bucket", default=None,
              help="Alias every bucket name onto one shared root, or one folder per bucket.")
@click.option("--bucket-name", help="Bucket name shown by ListBuckets in single-bucket mode.")
@click.option("--auto-create/--no-auto-create", default=None,
              help="Auto-create buckets on first write.")
@click.option("--vhost-bases", help="Comma-separated base hosts for virtual-hosted addressing.")
@click.option("--log-file", type=click.Path(), help="Also write logs to this file.")
def serve(host, port, storage, region, single_bucket, bucket_name,
          auto_create, vhost_bases, log_file) -> None:
    """Run the fake S3 server in the foreground (Ctrl+C to stop)."""
    import uvicorn

    from ..core.engine import StorageEngine
    from ..logsys import RingBufferHandler, setup_logging
    from ..server.app import create_app
    from ..stats import Stats

    config = load_server_config()
    overrides = {"host": host, "port": port, "region": region,
                 "bucket_name": bucket_name, "single_bucket": single_bucket,
                 "auto_create": auto_create}
    for name, value in overrides.items():
        if value is not None:
            setattr(config, name, value)
    if storage:
        config.storage_root = Path(storage)
    if vhost_bases:
        config.vhost_bases = [h.strip().lower() for h in vhost_bases.split(",") if h.strip()]

    ring = RingBufferHandler()
    setup_logging(ring=ring, log_file=Path(log_file) if log_file else None)
    engine = StorageEngine(config)
    app = create_app(engine, stats=Stats(), ring=ring)
    click.echo(f"fakes3 {__version__} serving on http://{config.host}:{config.port} "
               f"(storage: {engine.storage_root})")
    uvicorn.run(app, host=config.host, port=config.port,
                log_config=None, access_log=False)


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show server status and uptime."""
    try:
        data = get_client(ctx).status()
    except ClientError as exc:
        click.echo(f"server: not reachable at {ctx.obj['endpoint']} ({exc.message})")
        raise click.exceptions.Exit(1)
    uptime = int(data["uptime_seconds"])
    click.echo(f"server:   running (fakes3 {data['version']})")
    click.echo(f"endpoint: {ctx.obj['endpoint']}")
    click.echo(f"uptime:   {uptime // 3600}h {uptime % 3600 // 60}m {uptime % 60}s")
    click.echo(f"storage:  {data['storage_root']}")
    cfg = data["config"]
    mode = "single-bucket" if cfg["single_bucket"] else "multi-bucket"
    click.echo(f"mode:     {mode} (bucket: {cfg['bucket_name']}, region: {cfg['region']})")


@cli.command()
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Show request counters and storage usage."""
    data = get_client(ctx).stats()
    click.echo(f"uptime:       {int(data['uptime_seconds'])}s")
    click.echo(f"requests:     {data['requests']}")
    click.echo(f"errors:       {data['errors_client']} client / {data['errors_server']} server")
    click.echo(f"transferred:  in {human_size(data['bytes_in'])} / out {human_size(data['bytes_out'])}")
    click.echo(f"storage:      {data['storage']['objects']} objects, "
               f"{human_size(data['storage']['bytes'])}")
    if data["by_method"]:
        methods = ", ".join(f"{m}={n}" for m, n in sorted(data["by_method"].items()))
        click.echo(f"by method:    {methods}")


@cli.command()
@click.option("--limit", default=50, show_default=True, help="Number of records.")
@click.option("--level", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"],
              case_sensitive=False), help="Minimum log level.")
@click.option("--follow", "-f", is_flag=True, help="Poll for new records (Ctrl+C to stop).")
@click.pass_context
def logs(ctx: click.Context, limit: int, level: str | None, follow: bool) -> None:
    """Show recent server logs."""
    client = get_client(ctx)

    def print_records(records: list[dict]) -> None:
        for record in records:
            ts = datetime.fromtimestamp(record["ts"]).strftime("%H:%M:%S")
            click.echo(f"{ts} {record['level']:<8} {record['message']}")

    records = client.logs(limit=limit, level=level)
    print_records(records)
    if not follow:
        return
    last_ts = records[-1]["ts"] if records else 0.0
    try:
        while True:
            time.sleep(1.0)
            fresh = [r for r in client.logs(limit=500, level=level) if r["ts"] > last_ts]
            if fresh:
                print_records(fresh)
                last_ts = fresh[-1]["ts"]
    except KeyboardInterrupt:
        pass


@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Diagnostics: config, storage, and server reachability."""
    problems = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal problems
        mark = "[ OK ]" if ok else "[FAIL]"
        click.echo(f"{mark} {label}" + (f" - {detail}" if detail else ""))
        if not ok:
            problems += 1

    cfg_path = config_file()
    check("config file", True, f"{cfg_path} ({'present' if cfg_path.is_file() else 'defaults'})")

    config = load_server_config()
    storage = Path(config.storage_root).resolve()
    try:
        storage.mkdir(parents=True, exist_ok=True)
        probe = storage / ".fakes3-doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("storage writable", True, str(storage))
    except OSError as exc:
        check("storage writable", False, f"{storage}: {exc}")

    client = get_client(ctx)
    if client.is_reachable():
        data = client.status()
        check("server reachable", True,
              f"{ctx.obj['endpoint']} (fakes3 {data['version']})")
    else:
        check("server reachable", False,
              f"nothing answering at {ctx.obj['endpoint']} (start one with: fakes3 serve)")

    click.echo("all checks passed" if problems == 0 else f"{problems} problem(s) found")
    if problems:
        raise click.exceptions.Exit(1)


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

@cli.group()
def bucket() -> None:
    """Bucket management."""


@bucket.command("ls")
@click.pass_context
def bucket_ls(ctx: click.Context) -> None:
    """List buckets."""
    for entry in get_client(ctx).list_buckets():
        click.echo(f"{entry['created']:<26} {entry['name']}")


@bucket.command("mb")
@click.argument("name")
@click.pass_context
def bucket_mb(ctx: click.Context, name: str) -> None:
    """Create a bucket."""
    get_client(ctx).create_bucket(name)
    click.echo(f"created bucket {name}")


@bucket.command("rb")
@click.argument("name")
@click.option("--force", is_flag=True, help="Delete all objects first.")
@click.pass_context
def bucket_rb(ctx: click.Context, name: str, force: bool) -> None:
    """Delete a bucket (must be empty unless --force)."""
    get_client(ctx).delete_bucket(name, force=force)
    click.echo(f"deleted bucket {name}")


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------

@cli.group("object")
def object_group() -> None:
    """Object management."""


@object_group.command("ls")
@click.argument("bucket")
@click.option("--prefix", help="Only keys starting with this prefix.")
@click.pass_context
def object_ls(ctx: click.Context, bucket: str, prefix: str | None) -> None:
    """List objects in a bucket."""
    listing = get_client(ctx).list_objects(bucket, prefix=prefix)
    for obj in listing["objects"]:
        click.echo(f"{obj['last_modified']:<26} {human_size(obj['size']):>10}  {obj['key']}")
    if not listing["objects"]:
        click.echo("(empty)")


@object_group.command("put")
@click.argument("bucket")
@click.argument("key")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--content-type", help="Content-Type to store.")
@click.option("--meta", multiple=True, metavar="K=V", help="User metadata (repeatable).")
@click.pass_context
def object_put(ctx: click.Context, bucket: str, key: str, file: str,
               content_type: str | None, meta: tuple[str, ...]) -> None:
    """Upload a file."""
    metadata = {}
    for item in meta:
        name, _, value = item.partition("=")
        if not name or not value:
            raise fail(f"invalid --meta {item!r}, expected K=V")
        metadata[name] = value
    etag = get_client(ctx).put_object(bucket, key, Path(file),
                                      content_type=content_type, metadata=metadata)
    click.echo(f"uploaded {file} -> s3://{bucket}/{key} (etag {etag})")


@object_group.command("get")
@click.argument("bucket")
@click.argument("key")
@click.argument("dest", type=click.Path(), required=False)
@click.pass_context
def object_get(ctx: click.Context, bucket: str, key: str, dest: str | None) -> None:
    """Download an object (default destination: the key's file name)."""
    target = Path(dest) if dest else Path(key.rsplit("/", 1)[-1])
    get_client(ctx).get_object(bucket, key, target)
    click.echo(f"downloaded s3://{bucket}/{key} -> {target}")


@object_group.command("rm")
@click.argument("bucket")
@click.argument("key")
@click.pass_context
def object_rm(ctx: click.Context, bucket: str, key: str) -> None:
    """Delete an object."""
    get_client(ctx).delete_object(bucket, key)
    click.echo(f"deleted s3://{bucket}/{key}")


@object_group.command("cp")
@click.argument("bucket")
@click.argument("src_key")
@click.argument("dst_key")
@click.option("--dest-bucket", help="Copy into a different bucket.")
@click.pass_context
def object_cp(ctx: click.Context, bucket: str, src_key: str, dst_key: str,
              dest_bucket: str | None) -> None:
    """Copy an object."""
    target_bucket = dest_bucket or bucket
    get_client(ctx).copy_object(bucket, src_key, target_bucket, dst_key)
    click.echo(f"copied s3://{bucket}/{src_key} -> s3://{target_bucket}/{dst_key}")


@object_group.command("mv")
@click.argument("bucket")
@click.argument("src_key")
@click.argument("dst_key")
@click.option("--dest-bucket", help="Move into a different bucket.")
@click.pass_context
def object_mv(ctx: click.Context, bucket: str, src_key: str, dst_key: str,
              dest_bucket: str | None) -> None:
    """Move/rename an object (copy + delete)."""
    target_bucket = dest_bucket or bucket
    get_client(ctx).move_object(bucket, src_key, target_bucket, dst_key)
    click.echo(f"moved s3://{bucket}/{src_key} -> s3://{target_bucket}/{dst_key}")


@object_group.command("stat")
@click.argument("bucket")
@click.argument("key")
@click.pass_context
def object_stat(ctx: click.Context, bucket: str, key: str) -> None:
    """Show object metadata."""
    info = get_client(ctx).head_object(bucket, key)
    click.echo(f"key:           {info['key']}")
    click.echo(f"size:          {info['size']} ({human_size(info['size'])})")
    click.echo(f"etag:          {info['etag']}")
    click.echo(f"content-type:  {info['content_type']}")
    click.echo(f"last-modified: {info['last_modified']}")
    for name, value in sorted(info["metadata"].items()):
        click.echo(f"meta {name}: {value}")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@cli.group("config")
def config_group() -> None:
    """Persisted configuration (used by serve and the GUI)."""


def _coerce(key: str, value: str):
    if key in INT_FIELDS:
        return int(value)
    if key in BOOL_FIELDS:
        lowered = value.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise fail(f"{key} expects a boolean, got {value!r}")
    if key in LIST_FIELDS:
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


@config_group.command("show")
def config_show() -> None:
    """Show the effective server configuration."""
    click.echo(json.dumps(load_server_config().to_dict(), indent=2))


@config_group.command("path")
def config_path() -> None:
    """Print the config file location."""
    click.echo(str(config_file()))


@config_group.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """Print one effective configuration value."""
    if key not in CONFIG_FIELDS:
        raise fail(f"unknown key {key!r} (known: {', '.join(sorted(CONFIG_FIELDS))})")
    value = load_server_config().to_dict()[key]
    click.echo(json.dumps(value) if not isinstance(value, str) else value)


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Persist a configuration value (takes effect on next server start)."""
    if key not in CONFIG_FIELDS:
        raise fail(f"unknown key {key!r} (known: {', '.join(sorted(CONFIG_FIELDS))})")
    data = load_file_config()
    server = data.setdefault("server", {})
    server[key] = _coerce(key, value)
    ServerConfig.from_dict(server)  # validate before saving
    path = save_file_config(data)
    click.echo(f"set {key} = {server[key]} (in {path})")


@config_group.command("unset")
@click.argument("key")
def config_unset(key: str) -> None:
    """Remove a persisted value (falls back to the default)."""
    data = load_file_config()
    if key in data.get("server", {}):
        del data["server"][key]
        save_file_config(data)
        click.echo(f"unset {key}")
    else:
        click.echo(f"{key} was not set")


@config_group.command("export")
@click.argument("file", type=click.Path())
def config_export(file: str) -> None:
    """Export the persisted configuration to a JSON file."""
    Path(file).write_text(json.dumps(load_file_config(), indent=2), encoding="utf-8")
    click.echo(f"exported configuration to {file}")


@config_group.command("import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
def config_import(file: str) -> None:
    """Import configuration from a JSON file (replaces the persisted config)."""
    data = json.loads(Path(file).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise fail("config file must contain a JSON object")
    ServerConfig.from_dict(data.get("server", {}))  # validate before saving
    path = save_file_config(data)
    click.echo(f"imported configuration into {path}")


def main() -> None:
    try:
        cli(prog_name="fakes3")
    except ClientError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
