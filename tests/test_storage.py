from __future__ import annotations

import asyncio
from io import BytesIO
import pytest

from imghost.config import load_settings
from imghost.storage import LocalFilesystemBackend, S3StorageBackend, build_storage_backend


async def _read_stream_bytes(stream) -> bytes:
    data = bytearray()
    async for chunk in stream.body:
        data.extend(chunk)
    return bytes(data)


class _FakeStreamingBody:
    def __init__(self, data: bytes) -> None:
        self._buffer = BytesIO(data)

    def read(self, amount: int) -> bytes:
        return self._buffer.read(amount)

    def close(self) -> None:
        return None


class _FakeS3Client:
    def __init__(self, bucket_data: dict[str, bytes], *, bucket_exists: bool = True) -> None:
        self.bucket_data = bucket_data
        self.bucket_exists = bucket_exists

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.bucket_data[Key] = Body

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.bucket_data.pop(Key, None)

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in self.bucket_data:
            raise _fake_s3_error(404)
        return {"ContentLength": len(self.bucket_data[Key])}

    def get_object(self, *, Bucket: str, Key: str, Range: str | None = None) -> dict[str, object]:
        if Key not in self.bucket_data:
            raise _fake_s3_error(404)
        data = self.bucket_data[Key]
        content_range = None
        if Range is not None:
            _, raw = Range.split("=", 1)
            start_raw, end_raw = raw.split("-", 1)
            start = int(start_raw)
            end = int(end_raw)
            data = data[start : end + 1]
            content_range = f"bytes {start}-{end}/{len(self.bucket_data[Key])}"
        return {
            "Body": _FakeStreamingBody(data),
            "ContentLength": len(data),
            "ContentType": "application/octet-stream",
            "ContentRange": content_range,
        }

    def head_bucket(self, *, Bucket: str) -> None:
        if not self.bucket_exists:
            raise _fake_s3_error(404)

    def create_bucket(self, *, Bucket: str) -> None:
        self.bucket_exists = True

    def close(self) -> None:
        return None


class _FakeBotoSession:
    def __init__(self, client: _FakeS3Client) -> None:
        self._client = client

    def client(self, *args, **kwargs) -> _FakeS3Client:
        return self._client


def _fake_s3_error(status_code: int) -> Exception:
    exc = RuntimeError(f"s3 error {status_code}")
    exc.response = {"ResponseMetadata": {"HTTPStatusCode": status_code}}  # type: ignore[attr-defined]
    return exc


def test_build_storage_backend_defaults_to_filesystem(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)

    backend = build_storage_backend(load_settings())

    assert isinstance(backend, LocalFilesystemBackend)


def test_build_storage_backend_supports_garage_alias(monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "garage")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://garage:3900")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "imghost")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("S3_BUCKET", "imghost")
    monkeypatch.setenv("S3_REGION", "garage")

    backend = build_storage_backend(load_settings())

    assert isinstance(backend, S3StorageBackend)


def test_build_storage_backend_requires_s3_settings(monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "garage")
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)

    with pytest.raises(RuntimeError, match="S3_ENDPOINT_URL"):
        build_storage_backend(load_settings())


def test_local_filesystem_backend_stream_range_matches_expected_bytes(tmp_path) -> None:
    backend = LocalFilesystemBackend(tmp_path)
    payload = b"abcdefghij"

    asyncio.run(backend.put("sample.bin", payload))
    stream = asyncio.run(backend.get_stream("sample.bin", "bytes=2-5"))

    assert stream.status_code == 206
    assert stream.content_range == "bytes 2-5/10"
    assert stream.content_length == 4
    assert asyncio.run(_read_stream_bytes(stream)) == b"cdef"


def test_local_filesystem_backend_delete_missing_key_is_noop(tmp_path) -> None:
    backend = LocalFilesystemBackend(tmp_path)

    asyncio.run(backend.delete("missing.bin"))
    assert asyncio.run(backend.exists("missing.bin")) is False


def test_s3_storage_backend_stream_range_matches_filesystem_contract(monkeypatch) -> None:
    bucket_data = {"sample.bin": b"abcdefghij"}
    fake_client = _FakeS3Client(bucket_data)
    fake_boto3 = type(
        "_FakeBoto3",
        (),
        {"session": type("_FakeSessionModule", (), {"Session": lambda self=None: _FakeBotoSession(fake_client)})},
    )()
    monkeypatch.setitem(__import__("sys").modules, "boto3", fake_boto3)

    backend = S3StorageBackend(
        endpoint_url="http://garage:3900",
        access_key_id="imghost",
        secret_access_key="secret",
        bucket="imghost",
        region="garage",
    )

    stream = asyncio.run(backend.get_stream("sample.bin", "bytes=2-5"))

    assert stream.status_code == 206
    assert stream.content_range == "bytes 2-5/10"
    assert stream.content_length == 4
    assert asyncio.run(_read_stream_bytes(stream)) == b"cdef"


def test_s3_storage_backend_delete_missing_key_is_noop(monkeypatch) -> None:
    bucket_data: dict[str, bytes] = {}
    fake_client = _FakeS3Client(bucket_data)
    fake_boto3 = type(
        "_FakeBoto3",
        (),
        {"session": type("_FakeSessionModule", (), {"Session": lambda self=None: _FakeBotoSession(fake_client)})},
    )()
    monkeypatch.setitem(__import__("sys").modules, "boto3", fake_boto3)

    backend = S3StorageBackend(
        endpoint_url="http://garage:3900",
        access_key_id="imghost",
        secret_access_key="secret",
        bucket="imghost",
        region="garage",
    )

    asyncio.run(backend.delete("missing.bin"))
    assert asyncio.run(backend.exists("missing.bin")) is False
