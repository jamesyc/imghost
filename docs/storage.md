# Storage

Storage is abstracted behind [`src/imghost/storage.py`](/home/james/imghost/src/imghost/storage.py).

## Supported backends

- `filesystem`
- `garage`

`garage` currently means an S3-compatible backend configured through the S3-style settings.

## Filesystem backend

Implemented by `LocalFilesystemBackend`.

Characteristics:

- stores objects under `IMGHOST_DATA_DIR`
- health check validates the root directory exists
- supports range requests by streaming local files
- simplest development backend

## S3-compatible backend

Implemented by `S3StorageBackend`.

Required settings:

- `S3_ENDPOINT_URL`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_BUCKET`
- `S3_REGION`

Characteristics:

- uses boto3
- performs `head_bucket` for health checks
- can create the bucket in `init_storage()`
- supports object streaming and range requests

## Media serving

Routes:

- `/i/{id}.{ext}` for original media
- `/t/{id}.{ext}` for thumbnails

Behavior:

- range requests supported
- long-lived cache headers
- content streamed from the storage backend

Thumbnail-specific status behavior:

- `200` when a thumbnail is available
- `202` while thumbnail generation is still `pending` or `processing`
- `404` when thumbnail generation failed, the media is missing, or the album is expired/unavailable

## ZIP streaming

Album ZIP downloads are streamed, not fully buffered in memory.

Implementation:

- ZIP assembly in [`src/imghost/service.py`](/home/james/imghost/src/imghost/service.py)
- bridge logic in [`src/imghost/zip_streaming.py`](/home/james/imghost/src/imghost/zip_streaming.py)

The route is:

- `GET /api/v1/album/{album_id}/zip`
