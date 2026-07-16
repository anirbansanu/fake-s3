# Fake S3 — Local Amazon S3 Server Replica

`fakes3.py` is a single-file server that replicates the **real Amazon S3 REST/XML
wire protocol** on your local machine. AWS SDKs connect to it directly as a
drop-in S3 replacement — no wrapper code needed:

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

## 1. Setup and start

Python 3.10+ with two packages (a ready venv lives in `.venv/`):

```bash
pip install fastapi uvicorn
python fakes3.py
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

## 5. Verification

A boto3-based compatibility suite (32 checks: bucket lifecycle, uploads,
downloads, ranges, conditionals, listings and pagination, multipart,
presigned URLs and expiry, bulk delete, aws-chunked framing, virtual-hosted
addressing) runs green against this server.
