"""
boto3 compatibility regression suite for the fakes3 server.

Runs against a live server subprocess (see conftest.py). These tests pin the
S3 wire-protocol behavior so the package refactor can be verified to be
behavior-identical.
"""

import hashlib
import time
import urllib.error
import urllib.request

import pytest
from botocore.exceptions import ClientError

BUCKET = "testbucket"


def error_code(exc: ClientError) -> str:
    return exc.response["Error"]["Code"]


@pytest.fixture(scope="module", autouse=True)
def bucket(s3):
    s3.create_bucket(Bucket=BUCKET)
    return BUCKET


# -- service / bucket ------------------------------------------------------

def test_health(multi_server):
    with urllib.request.urlopen(f"{multi_server.endpoint}/health") as resp:
        assert resp.status == 200
        assert b'"status"' in resp.read()


def test_bucket_lifecycle(s3):
    s3.create_bucket(Bucket="lifecycle")
    s3.head_bucket(Bucket="lifecycle")
    names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert "lifecycle" in names
    s3.delete_bucket(Bucket="lifecycle")
    with pytest.raises(ClientError) as err:
        s3.head_bucket(Bucket="lifecycle")
    assert err.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404


def test_delete_nonempty_bucket_conflict(s3):
    s3.create_bucket(Bucket="nonempty")
    s3.put_object(Bucket="nonempty", Key="x.txt", Body=b"x")
    with pytest.raises(ClientError) as err:
        s3.delete_bucket(Bucket="nonempty")
    assert error_code(err.value) == "BucketNotEmpty"
    s3.delete_object(Bucket="nonempty", Key="x.txt")
    s3.delete_bucket(Bucket="nonempty")


def test_missing_bucket_error(s3):
    with pytest.raises(ClientError) as err:
        s3.list_objects_v2(Bucket="no-such-bucket-here")
    assert error_code(err.value) == "NoSuchBucket"


def test_bucket_location(s3):
    loc = s3.get_bucket_location(Bucket=BUCKET)
    assert loc["LocationConstraint"] is None  # us-east-1 → empty constraint


# -- objects ---------------------------------------------------------------

def test_put_get_roundtrip_with_metadata(s3):
    body = b"hello fake s3" * 100
    s3.put_object(Bucket=BUCKET, Key="docs/hello.txt", Body=body,
                  ContentType="text/plain", Metadata={"owner": "tests"})
    obj = s3.get_object(Bucket=BUCKET, Key="docs/hello.txt")
    assert obj["Body"].read() == body
    assert obj["ContentType"] == "text/plain"
    assert obj["Metadata"] == {"owner": "tests"}
    assert obj["ETag"] == f'"{hashlib.md5(body).hexdigest()}"'


def test_head_object(s3):
    s3.put_object(Bucket=BUCKET, Key="head.bin", Body=b"12345")
    head = s3.head_object(Bucket=BUCKET, Key="head.bin")
    assert head["ContentLength"] == 5
    assert head["ETag"] == f'"{hashlib.md5(b"12345").hexdigest()}"'


def test_overwrite_is_silent(s3):
    s3.put_object(Bucket=BUCKET, Key="over.txt", Body=b"one")
    s3.put_object(Bucket=BUCKET, Key="over.txt", Body=b"two")
    assert s3.get_object(Bucket=BUCKET, Key="over.txt")["Body"].read() == b"two"


def test_delete_object_and_nosuchkey(s3):
    s3.put_object(Bucket=BUCKET, Key="gone.txt", Body=b"bye")
    s3.delete_object(Bucket=BUCKET, Key="gone.txt")
    s3.delete_object(Bucket=BUCKET, Key="gone.txt")  # idempotent
    with pytest.raises(ClientError) as err:
        s3.get_object(Bucket=BUCKET, Key="gone.txt")
    assert error_code(err.value) == "NoSuchKey"


def test_copy_object(s3):
    s3.put_object(Bucket=BUCKET, Key="src.txt", Body=b"copy me",
                  ContentType="text/plain", Metadata={"tag": "orig"})
    s3.copy_object(Bucket=BUCKET, Key="dst.txt",
                   CopySource={"Bucket": BUCKET, "Key": "src.txt"})
    obj = s3.get_object(Bucket=BUCKET, Key="dst.txt")
    assert obj["Body"].read() == b"copy me"
    assert obj["Metadata"] == {"tag": "orig"}  # COPY directive default


def test_copy_object_replace_metadata(s3):
    s3.put_object(Bucket=BUCKET, Key="src2.txt", Body=b"x", Metadata={"a": "1"})
    s3.copy_object(Bucket=BUCKET, Key="dst2.txt",
                   CopySource={"Bucket": BUCKET, "Key": "src2.txt"},
                   MetadataDirective="REPLACE", Metadata={"b": "2"},
                   ContentType="application/json")
    obj = s3.head_object(Bucket=BUCKET, Key="dst2.txt")
    assert obj["Metadata"] == {"b": "2"}
    assert obj["ContentType"] == "application/json"


def test_bulk_delete(s3):
    keys = [f"bulk/{i}.txt" for i in range(5)]
    for key in keys:
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")
    result = s3.delete_objects(
        Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in keys]})
    assert {d["Key"] for d in result["Deleted"]} == set(keys)
    listing = s3.list_objects_v2(Bucket=BUCKET, Prefix="bulk/")
    assert listing["KeyCount"] == 0


# -- listings --------------------------------------------------------------

@pytest.fixture(scope="module")
def listing_keys(s3):
    keys = ["list/a.txt", "list/b.txt", "list/sub/c.txt", "list/sub/d.txt", "list2/e.txt"]
    for key in keys:
        s3.put_object(Bucket=BUCKET, Key=key, Body=key.encode())
    return keys


def test_list_prefix(s3, listing_keys):
    result = s3.list_objects_v2(Bucket=BUCKET, Prefix="list/")
    assert [o["Key"] for o in result["Contents"]] == [
        "list/a.txt", "list/b.txt", "list/sub/c.txt", "list/sub/d.txt"]


def test_list_delimiter_common_prefixes(s3, listing_keys):
    result = s3.list_objects_v2(Bucket=BUCKET, Prefix="list/", Delimiter="/")
    assert [o["Key"] for o in result["Contents"]] == ["list/a.txt", "list/b.txt"]
    assert [p["Prefix"] for p in result["CommonPrefixes"]] == ["list/sub/"]


def test_list_pagination_v2(s3, listing_keys):
    page1 = s3.list_objects_v2(Bucket=BUCKET, Prefix="list/", MaxKeys=2)
    assert page1["IsTruncated"] is True
    assert len(page1["Contents"]) == 2
    page2 = s3.list_objects_v2(Bucket=BUCKET, Prefix="list/", MaxKeys=10,
                               ContinuationToken=page1["NextContinuationToken"])
    keys = [o["Key"] for o in page1["Contents"]] + [o["Key"] for o in page2["Contents"]]
    assert keys == ["list/a.txt", "list/b.txt", "list/sub/c.txt", "list/sub/d.txt"]


def test_list_start_after(s3, listing_keys):
    result = s3.list_objects_v2(Bucket=BUCKET, Prefix="list/", StartAfter="list/b.txt")
    assert [o["Key"] for o in result["Contents"]] == ["list/sub/c.txt", "list/sub/d.txt"]


def test_list_v1_marker(s3, listing_keys):
    result = s3.list_objects(Bucket=BUCKET, Prefix="list/", Marker="list/a.txt")
    assert [o["Key"] for o in result["Contents"]] == [
        "list/b.txt", "list/sub/c.txt", "list/sub/d.txt"]


def test_directory_marker_listing(s3):
    s3.put_object(Bucket=BUCKET, Key="emptydir/", Body=b"")
    result = s3.list_objects_v2(Bucket=BUCKET, Prefix="emptydir/")
    assert [o["Key"] for o in result["Contents"]] == ["emptydir/"]
    assert result["Contents"][0]["Size"] == 0


# -- range + conditional requests -------------------------------------------

@pytest.fixture(scope="module")
def range_key(s3):
    s3.put_object(Bucket=BUCKET, Key="range.bin", Body=b"0123456789")
    return "range.bin"


def test_range_requests(s3, range_key):
    assert s3.get_object(Bucket=BUCKET, Key=range_key, Range="bytes=2-5")["Body"].read() == b"2345"
    assert s3.get_object(Bucket=BUCKET, Key=range_key, Range="bytes=7-")["Body"].read() == b"789"
    assert s3.get_object(Bucket=BUCKET, Key=range_key, Range="bytes=-3")["Body"].read() == b"789"


def test_range_unsatisfiable(s3, range_key):
    with pytest.raises(ClientError) as err:
        s3.get_object(Bucket=BUCKET, Key=range_key, Range="bytes=99-100")
    assert err.value.response["ResponseMetadata"]["HTTPStatusCode"] == 416


def test_conditional_if_none_match(s3, range_key):
    etag = s3.head_object(Bucket=BUCKET, Key=range_key)["ETag"]
    with pytest.raises(ClientError) as err:
        s3.get_object(Bucket=BUCKET, Key=range_key, IfNoneMatch=etag)
    assert err.value.response["ResponseMetadata"]["HTTPStatusCode"] == 304


def test_conditional_if_match_fails(s3, range_key):
    with pytest.raises(ClientError) as err:
        s3.get_object(Bucket=BUCKET, Key=range_key, IfMatch='"wrong-etag"')
    assert error_code(err.value) == "PreconditionFailed"


def test_conditional_if_modified_since(s3, range_key):
    from datetime import datetime, timedelta, timezone
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    with pytest.raises(ClientError) as err:
        s3.get_object(Bucket=BUCKET, Key=range_key, IfModifiedSince=future)
    assert err.value.response["ResponseMetadata"]["HTTPStatusCode"] == 304


# -- multipart ---------------------------------------------------------------

def test_multipart_roundtrip(s3):
    part1, part2 = b"a" * (5 * 1024 * 1024), b"b" * 1024
    mp = s3.create_multipart_upload(Bucket=BUCKET, Key="mp/big.bin",
                                    ContentType="application/x-test")
    upload_id = mp["UploadId"]
    e1 = s3.upload_part(Bucket=BUCKET, Key="mp/big.bin", UploadId=upload_id,
                        PartNumber=1, Body=part1)["ETag"]
    e2 = s3.upload_part(Bucket=BUCKET, Key="mp/big.bin", UploadId=upload_id,
                        PartNumber=2, Body=part2)["ETag"]

    parts = s3.list_parts(Bucket=BUCKET, Key="mp/big.bin", UploadId=upload_id)["Parts"]
    assert [p["PartNumber"] for p in parts] == [1, 2]

    done = s3.complete_multipart_upload(
        Bucket=BUCKET, Key="mp/big.bin", UploadId=upload_id,
        MultipartUpload={"Parts": [
            {"PartNumber": 1, "ETag": e1}, {"PartNumber": 2, "ETag": e2}]})
    combined = hashlib.md5(
        bytes.fromhex(e1.strip('"')) + bytes.fromhex(e2.strip('"'))).hexdigest()
    assert done["ETag"] == f'"{combined}-2"'

    obj = s3.get_object(Bucket=BUCKET, Key="mp/big.bin")
    assert obj["ContentType"] == "application/x-test"
    body = obj["Body"].read()
    assert len(body) == len(part1) + len(part2)
    assert body == part1 + part2


def test_multipart_abort(s3):
    mp = s3.create_multipart_upload(Bucket=BUCKET, Key="mp/aborted.bin")
    s3.upload_part(Bucket=BUCKET, Key="mp/aborted.bin", UploadId=mp["UploadId"],
                   PartNumber=1, Body=b"junk")
    s3.abort_multipart_upload(Bucket=BUCKET, Key="mp/aborted.bin", UploadId=mp["UploadId"])
    with pytest.raises(ClientError) as err:
        s3.list_parts(Bucket=BUCKET, Key="mp/aborted.bin", UploadId=mp["UploadId"])
    assert error_code(err.value) == "NoSuchUpload"
    with pytest.raises(ClientError):
        s3.get_object(Bucket=BUCKET, Key="mp/aborted.bin")


def test_multipart_invalid_part(s3):
    mp = s3.create_multipart_upload(Bucket=BUCKET, Key="mp/bad.bin")
    with pytest.raises(ClientError) as err:
        s3.complete_multipart_upload(
            Bucket=BUCKET, Key="mp/bad.bin", UploadId=mp["UploadId"],
            MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": '"beef"'}]})
    assert error_code(err.value) == "InvalidPart"
    s3.abort_multipart_upload(Bucket=BUCKET, Key="mp/bad.bin", UploadId=mp["UploadId"])


def test_list_multipart_uploads(s3):
    mp = s3.create_multipart_upload(Bucket=BUCKET, Key="mp/pending.bin")
    uploads = s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", [])
    assert any(u["UploadId"] == mp["UploadId"] for u in uploads)
    s3.abort_multipart_upload(Bucket=BUCKET, Key="mp/pending.bin", UploadId=mp["UploadId"])


# -- presigned URLs ----------------------------------------------------------

def test_presigned_get(s3):
    s3.put_object(Bucket=BUCKET, Key="signed.txt", Body=b"signed content")
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": "signed.txt"}, ExpiresIn=60)
    with urllib.request.urlopen(url) as resp:
        assert resp.read() == b"signed content"


def test_presigned_expiry(s3):
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": "signed.txt"}, ExpiresIn=1)
    time.sleep(2)
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(url)
    assert err.value.code == 403


# -- single-bucket mode ------------------------------------------------------

def test_single_bucket_aliasing(single_server):
    client = single_server.client()
    client.put_object(Bucket="bucket-one", Key="shared/file.txt", Body=b"aliased")
    obj = client.get_object(Bucket="bucket-two", Key="shared/file.txt")
    assert obj["Body"].read() == b"aliased"
    assert (single_server.storage / "shared" / "file.txt").is_file()


def test_single_bucket_display_name(single_server):
    client = single_server.client()
    names = [b["Name"] for b in client.list_buckets()["Buckets"]]
    assert names == ["mybucket"]
