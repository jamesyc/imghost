from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator


CHUNK_SIZE = 64 * 1024


@dataclass
class StorageStream:
    status_code: int
    content_type: str
    content_length: int | None
    content_range: str | None
    body: AsyncIterator[bytes]


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
