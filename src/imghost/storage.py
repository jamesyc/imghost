from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


CHUNK_SIZE = 64 * 1024


@dataclass
class StorageStream:
    status_code: int
    content_type: str
    content_length: int | None
    content_range: str | None
    body: AsyncIterator[bytes]


class StorageBackend(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...

    async def get_size(self, key: str) -> int: ...

    async def get_bytes(self, key: str) -> bytes: ...

    async def health_check(self) -> bool: ...

    async def get_stream(self, key: str, range_header: str | None = None) -> StorageStream: ...

    async def init_storage(self) -> None: ...


class LocalFilesystemBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        return self.root / key

    async def put(self, key: str, data: bytes) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def delete(self, key: str) -> None:
        path = self._path_for(key)
        if path.exists():
            path.unlink()

    async def exists(self, key: str) -> bool:
        return self._path_for(key).exists()

    async def get_size(self, key: str) -> int:
        return self._path_for(key).stat().st_size

    async def get_bytes(self, key: str) -> bytes:
        return self._path_for(key).read_bytes()

    async def health_check(self) -> bool:
        return self.root.exists() and self.root.is_dir()

    async def init_storage(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def get_stream(self, key: str, range_header: str | None = None) -> StorageStream:
        path = self._path_for(key)
        size = path.stat().st_size
        start = 0
        end = size - 1
        status_code = 200
        content_range = None

        if range_header and range_header.startswith("bytes="):
            raw_start, _, raw_end = range_header[6:].partition("-")
            if raw_start:
                start = int(raw_start)
            if raw_end:
                end = int(raw_end)
            status_code = 206
            content_range = f"bytes {start}-{end}/{size}"

        length = max(0, end - start + 1)

        async def iterator() -> AsyncIterator[bytes]:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StorageStream(
            status_code=status_code,
            content_type="application/octet-stream",
            content_length=length,
            content_range=content_range,
            body=iterator(),
        )


class S3StorageBackend:
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        region: str,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.bucket = bucket
        self.region = region

    def _client(self):
        import boto3

        session = boto3.session.Session()
        return session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name=self.region,
        )

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return None
        metadata = response.get("ResponseMetadata")
        if not isinstance(metadata, dict):
            return None
        status = metadata.get("HTTPStatusCode")
        return int(status) if isinstance(status, int) else None

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put_sync, key, data)

    def _put_sync(self, key: str, data: bytes) -> None:
        client = self._client()
        try:
            client.put_object(Bucket=self.bucket, Key=key, Body=data)
        finally:
            client.close()

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete_sync, key)

    def _delete_sync(self, key: str) -> None:
        client = self._client()
        try:
            client.delete_object(Bucket=self.bucket, Key=key)
        finally:
            client.close()

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._exists_sync, key)

    def _exists_sync(self, key: str) -> bool:
        try:
            client = self._client()
            try:
                client.head_object(Bucket=self.bucket, Key=key)
            finally:
                client.close()
            return True
        except Exception as exc:
            if self._status_code(exc) == 404:
                return False
            raise

    async def get_size(self, key: str) -> int:
        return await asyncio.to_thread(self._get_size_sync, key)

    def _get_size_sync(self, key: str) -> int:
        client = self._client()
        try:
            response = client.head_object(Bucket=self.bucket, Key=key)
            return int(response["ContentLength"])
        finally:
            client.close()

    async def get_bytes(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get_bytes_sync, key)

    def _get_bytes_sync(self, key: str) -> bytes:
        client = self._client()
        try:
            response = client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                return body.read()
            finally:
                body.close()
        finally:
            client.close()

    async def health_check(self) -> bool:
        return await asyncio.to_thread(self._health_check_sync)

    def _health_check_sync(self) -> bool:
        try:
            client = self._client()
            try:
                client.head_bucket(Bucket=self.bucket)
            finally:
                client.close()
            return True
        except Exception:
            return False

    async def init_storage(self) -> None:
        await asyncio.to_thread(self._init_storage_sync)

    def _init_storage_sync(self) -> None:
        try:
            client = self._client()
            try:
                client.head_bucket(Bucket=self.bucket)
                return
            finally:
                client.close()
        except Exception as exc:
            if self._status_code(exc) != 404:
                raise
        client = self._client()
        try:
            client.create_bucket(Bucket=self.bucket)
        finally:
            client.close()

    async def get_stream(self, key: str, range_header: str | None = None) -> StorageStream:
        return await asyncio.to_thread(self._get_stream_sync, key, range_header)

    def _get_stream_sync(self, key: str, range_header: str | None) -> StorageStream:
        params = {"Bucket": self.bucket, "Key": key}
        if range_header:
            params["Range"] = range_header
        client = self._client()
        try:
            response = client.get_object(**params)
        except Exception:
            client.close()
            raise
        body = response["Body"]
        status_code = 206 if range_header else 200
        content_length = int(response["ContentLength"]) if response.get("ContentLength") is not None else None
        content_range = response.get("ContentRange")
        content_type = response.get("ContentType") or "application/octet-stream"

        async def iterator() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await asyncio.to_thread(body.read, CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            finally:
                body.close()
                client.close()

        return StorageStream(
            status_code=status_code,
            content_type=content_type,
            content_length=content_length,
            content_range=content_range,
            body=iterator(),
        )


def build_storage_backend(settings: "Settings") -> StorageBackend:
    backend = settings.storage_backend
    if backend in {"filesystem", "fs", "local"}:
        return LocalFilesystemBackend(settings.data_dir)
    if backend in {"garage", "s3"}:
        if not settings.s3_endpoint_url:
            raise RuntimeError("S3_ENDPOINT_URL is required for garage storage.")
        if not settings.s3_access_key_id:
            raise RuntimeError("S3_ACCESS_KEY_ID is required for garage storage.")
        if not settings.s3_secret_access_key:
            raise RuntimeError("S3_SECRET_ACCESS_KEY is required for garage storage.")
        if not settings.s3_bucket:
            raise RuntimeError("S3_BUCKET is required for garage storage.")
        return S3StorageBackend(
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
        )
    raise RuntimeError(f"Unsupported storage backend: {backend}")
