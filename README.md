# Fake S3 — Local Amazon S3 Server Replica

fakes3 replicates the **real Amazon S3 REST/XML wire protocol** on your local
machine. AWS SDKs connect to it directly as a drop-in S3 replacement — no
wrapper code needed:

- **Laravel** — the native `s3` filesystem disk (`Storage::disk('s3')`) via
  league/flysystem-aws-s3-v3
- **Python** — boto3 / aioboto3
- **AWS CLI** — `aws --endpoint-url http://localhost:9000 s3 ...`

Objects are stored as plain files on disk, so you can browse them. By default
the server runs in **single-bucket mode**: whatever bucket name a client is
configured with (`mybucket`, `newsystemstorage`, ...) is treated as an alias
for the one shared storage root, so object keys map straight onto `storage/`:

```
storage/
└── exports/
    └── report.xlsx        ← key "exports/report.xlsx", any bucket name
```

Set `FAKE_S3_SINGLE_BUCKET=0` if you need real-S3 multi-bucket layout instead
(`storage/<bucket>/<key>`, buckets isolated from each other).

The project ships as a Python package with two front-ends over one shared
backend, plus a legacy single-file build:

| Entry point | What it is |
|---|---|
| `python -m fakes3` / `fakes3-cli.exe serve` | Headless server (CLI) |
| `python -m fakes3.gui` / `fakes3-gui.exe` | Desktop manager: server control, config, live logs, object browser, stats, tray |
| `fakes3.py` | Legacy single-file server (kept for drop-in use; the package is the maintained path) |

```
fakes3/
  core/      # storage engine: buckets, objects, metadata sidecars, multipart
  server/    # FastAPI app, S3 dispatch, admin API, in-process ServerController
  cli/       # click-based CLI (server, bucket, object, config, doctor commands)
  gui/       # PySide6 desktop app
  config.py  # ServerConfig + persisted config (%APPDATA%\fakes3\config.json)
  logsys.py  # logging + in-memory ring buffer (feeds GUI + admin API)
  stats.py   # request counters
  client.py  # stdlib S3/admin client used by the CLI
```

## 1. Setup and start

Python 3.10+ (a ready venv lives in `.venv/`):

```bash
pip install -r requirements.txt
python -m fakes3            # or: python fakes3.py (legacy single-file)
```

The server listens on **http://localhost:9000**. Verify:

```bash
curl http://localhost:9000/health
# {"status":"ok","storage_root":"...\\storage"}
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `FAKE_S3_PORT` | `9000` | Listen port |
| `FAKE_S3_STORAGE` | `./storage` | Where files are stored |
| `FAKE_S3_REGION` | `us-east-1` | Region reported to clients |
| `FAKE_S3_SINGLE_BUCKET` | `1` | All bucket names alias one shared storage root, objects at `storage/<key>` (`0` = one folder per bucket, like real S3) |
| `FAKE_S3_BUCKET_NAME` | `mybucket` | Bucket name reported by ListBuckets in single-bucket mode |
| `FAKE_S3_AUTO_CREATE` | `1` | Auto-create buckets on first write (`0` = require CreateBucket, like real S3) |
| `FAKE_S3_VHOST_BASES` | `localhost,host.docker.internal` | Base hosts for virtual-hosted-style addressing (`bucket.localhost`) |

## 2. Supported S3 operations

**Service:** ListBuckets.
**Bucket:** CreateBucket, DeleteBucket, HeadBucket, ListObjects (v1),
ListObjectsV2 (prefix, delimiter, pagination, encoding-type),
GetBucketLocation, DeleteObjects (bulk), ACL/versioning/config stubs.
**Object:** PutObject, GetObject (Range, conditional requests,
`response-*` overrides), HeadObject, DeleteObject, CopyObject
(`x-amz-copy-source`), ACL/tagging stubs.
**Multipart:** CreateMultipartUpload, UploadPart, CompleteMultipartUpload,
AbortMultipartUpload, ListParts, ListMultipartUploads.

Protocol details handled for SDK compatibility:

- **Path-style** (`localhost:9000/bucket/key`) and **virtual-hosted-style**
  (`bucket.localhost:9000/key`) addressing.
- **`aws-chunked` upload bodies** (chunk signatures and checksum trailers)
  are decoded transparently.
- **S3 XML errors** with real codes (`NoSuchKey`, `NoSuchBucket`,
  `BucketNotEmpty`, `InvalidPart`, ...), which SDK exception handling relies on.
- **ETags** are content MD5s; multipart ETags use the real
  `md5-of-part-md5s + "-N"` formula. `Content-Type` and `x-amz-meta-*`
  metadata are persisted and returned.
- **Presigned URLs** (both SigV4 `X-Amz-*` and legacy `Signature`/`Expires`
  forms): signatures are accepted without verification — any credentials
  work — but **expiry is enforced**, so temporary URLs really expire (403).

## 3. Client configuration

**Laravel** (`config/filesystems.php` s3 disk works as-is; only `.env` changes —
see [LARAVEL_ENV.md](LARAVEL_ENV.md) for a complete paste-ready file):

```env
FILESYSTEM_DISK=s3
AWS_ACCESS_KEY_ID=local
AWS_SECRET_ACCESS_KEY=local
AWS_DEFAULT_REGION=us-east-1
AWS_BUCKET=mybucket
AWS_ENDPOINT=http://localhost:9000
AWS_USE_PATH_STYLE_ENDPOINT=true
```

In the default single-bucket mode the `AWS_BUCKET` value is cosmetic — any
name maps to the same shared `storage/` root, so a Laravel app using
`newsystemstorage` and a Python service using `mybucket` still read and write
the same files.

```php
Storage::disk('s3')->put('exports/2026/report.xlsx', $contents);   // upload
Storage::disk('s3')->temporaryUrl('exports/2026/report.xlsx', now()->addHour());
Storage::disk('s3')->move('exports/a.xlsx', 'archive/a.xlsx');
```

Requires the flysystem adapter: `composer require league/flysystem-aws-s3-v3 "^3.0"`.

**Python (boto3):**

```python
import boto3
s3 = boto3.client("s3", endpoint_url="http://localhost:9000",
                  aws_access_key_id="local", aws_secret_access_key="local",
                  region_name="us-east-1")
s3.upload_file("report.xlsx", "mybucket", "exports/2026/report.xlsx")
s3.download_file("mybucket", "exports/2026/report.xlsx", "report.xlsx")
```

**AWS CLI:**

```bash
aws --endpoint-url http://localhost:9000 s3 ls s3://mybucket/exports/
aws --endpoint-url http://localhost:9000 s3 cp report.xlsx s3://mybucket/exports/
```

**Docker:** if the app runs in a container while the server runs on the host,
point the endpoint at `http://host.docker.internal:9000`.

## 4. Important notes

- **Local development only.** Signatures are not verified and there is no
  authentication — anyone who can reach the port can read and write files.
  Do not expose it beyond your machine/network.
- Uploads are **atomic** (an object never appears half-written; concurrent
  writers to one key end with last-writer-wins, like S3), and uploading to
  an existing key **overwrites silently**, matching S3.
- Empty directories appear in listings as zero-byte `prefix/` marker
  objects, so Laravel's `makeDirectory`/`directoryExists` behave sensibly.
- `GET /health` is reserved as a liveness probe; hidden bookkeeping files
  use reserved suffixes (`.fake-s3-meta`, `.fake-s3-part`) and keys ending
  in those suffixes are rejected.
- Keys are normalized for filesystem storage: backslashes become `/`,
  `.`/`..` segments are collapsed.
- To reset all data, stop the server and delete the `storage/` folder.
- **Not implemented** (vs. real S3): versioning, signature verification,
  ACL enforcement (stubs report public-read), bucket policies / CORS /
  lifecycle (writes accepted and ignored; reads return the canonical
  "not found" codes), POST form uploads, UploadPartCopy, GetObjectAttributes.

## 5. CLI application

Every command works against any running fakes3 instance (package, exe, or
Docker) via `--endpoint` / `FAKE_S3_ENDPOINT` (default `http://localhost:9000`):

```bash
fakes3 serve --port 9000 --storage ./storage    # run the server (Ctrl+C stops)
fakes3 status                                   # status + uptime
fakes3 stats                                    # request counters, storage usage
fakes3 logs --follow                            # live server logs
fakes3 doctor                                   # diagnostics

fakes3 bucket ls | mb NAME | rb NAME [--force]
fakes3 object ls BUCKET [--prefix P]
fakes3 object put BUCKET KEY FILE [--content-type T] [--meta K=V]
fakes3 object get BUCKET KEY [DEST]
fakes3 object cp|mv BUCKET SRC_KEY DST_KEY [--dest-bucket B]
fakes3 object rm BUCKET KEY
fakes3 object stat BUCKET KEY

fakes3 config show | get KEY | set KEY VALUE | unset KEY | path
fakes3 config export FILE | import FILE
```

In development, run it as `python -m fakes3.cli`. The CLI talks to the server
over HTTP: S3 operations use the S3 API, management commands use the admin API
under `/_fakes3/` (status, stats, logs, config — bucket names can never
contain `_`, so the prefix cannot collide with S3 traffic).

## 6. GUI application

`python -m fakes3.gui` (or `fakes3-gui.exe`) opens a desktop manager built on
the same backend package:

- **Server panel** — start / stop / restart, status, uptime, endpoint.
- **Browser** — buckets and objects: upload (file dialog or drag-and-drop),
  download, rename/move, copy, delete, new folder, bucket create/rename/delete
  (multi-bucket mode), and an object metadata dialog (ETag, Content-Type,
  `x-amz-meta-*`).
- **Logs** — live request/error log with level filter, pause, auto-scroll.
- **Statistics** — request counts by method, errors, bytes in/out, storage usage.
- **Settings** — host, port, storage folder, region, bucket mode/name,
  auto-create, vhost bases; app preferences (minimize to tray, notifications,
  start with Windows, start server on launch, optional log file);
  config import/export.
- **Tray** — minimizes to the system tray; balloon notifications on server
  start/stop/errors; quick start/stop from the tray menu.

Settings persist to `%APPDATA%\fakes3\config.json` and are shared with
`fakes3 serve` and `fakes3 config`.

## 7. Standalone Windows executables

Build self-contained exes (no Python required on the target machine):

```powershell
pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Outputs land in `dist\`: **fakes3-cli.exe** (console) and **fakes3-gui.exe**
(windowed). Both are PyInstaller onefile builds with all dependencies embedded.

## 8. Verification

A boto3-based compatibility suite (`tests/`, 44 checks: bucket lifecycle,
uploads, downloads, ranges, conditionals, listings and pagination, multipart,
presigned URLs and expiry, bulk delete, aws-chunked framing, admin API,
in-process controller, CLI end-to-end) runs green against this server:

```bash
pip install -r requirements-dev.txt
pytest tests/
```

Set `FAKES3_TEST_TARGET` to point the suite at any entry point, e.g.
`FAKES3_TEST_TARGET="dist/fakes3-cli.exe serve" pytest tests/`.
