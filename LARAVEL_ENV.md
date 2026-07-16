# Laravel `.env` for fakes3

Complete paste-ready block for Laravel's built-in `s3` filesystem disk
(`config/filesystems.php` needs no changes; requires
`composer require league/flysystem-aws-s3-v3 "^3.0"`).

```env
FILESYSTEM_DISK=s3

AWS_ACCESS_KEY_ID=local
AWS_SECRET_ACCESS_KEY=local
AWS_DEFAULT_REGION=us-east-1
AWS_BUCKET=mybucket
AWS_USE_PATH_STYLE_ENDPOINT=true

AWS_ENDPOINT=http://localhost:9000
AWS_URL=http://localhost:9000/mybucket
```

## Which endpoint to use

`AWS_ENDPOINT` depends on where Laravel runs relative to the fakes3 server:

| Laravel runs...                                  | AWS_ENDPOINT                          |
|--------------------------------------------------|---------------------------------------|
| on the host (fakes3 on host or port-mapped Docker)| `http://localhost:9000`               |
| in Docker, same compose network as fakes3         | `http://fakes3:9000`                  |
| in Docker, fakes3 on the host / other container   | `http://host.docker.internal:9000`    |

## Notes on each key

- `FILESYSTEM_DISK=s3` — required; otherwise Laravel stays on the `local` disk.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — any non-empty values work.
  fakes3 accepts every signature, but the AWS SDK won't send requests without
  credentials configured.
- `AWS_DEFAULT_REGION=us-east-1` — must match `FAKE_S3_REGION` (default `us-east-1`).
- `AWS_BUCKET` — in fakes3's default single-bucket mode any name maps to the
  same shared `storage/` root, so this value is cosmetic. With
  `FAKE_S3_SINGLE_BUCKET=0` it must name a real bucket folder.
- `AWS_USE_PATH_STYLE_ENDPOINT=true` — keeps requests as
  `endpoint/bucket/key`. (fakes3 also understands virtual-hosted style for
  hosts listed in `FAKE_S3_VHOST_BASES`, but path-style is the reliable choice.)
- `AWS_ENDPOINT` — where the SDK sends API calls (see table above).
- `AWS_URL` — what `Storage::url()` prepends for public links. Use a host the
  **browser** can reach (usually `http://localhost:9000/mybucket`), even when
  `AWS_ENDPOINT` points at `fakes3` or `host.docker.internal` —
  container-internal hostnames don't resolve in the user's browser.
