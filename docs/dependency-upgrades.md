# Dependency upgrades

Python follows 3.15.x. Python package requirements specify supported major-version ranges, without fixing minor or patch releases. uv applies a seven-day release delay to package resolution. The lockfile records a tested resolution; it is not a ceiling on future updates.

To refresh development dependencies:

```sh
uv lock --upgrade
uv sync --locked --extra dev
uv run --locked pytest -q
```

Pillow follows 13.x and is exempt from the seven-day release delay. Other packages keep that delay. Botocore currently requires python-dateutil below 3; allow uv to respect that upstream constraint.

CI refreshes dependencies once, runs the full test suite, and passes that lockfile to the amd64 and arm64 image verification jobs. Each image runs real image/video processing and anonymous/authenticated upload, download, range, ZIP, and thumbnail checks against Garage and Redis. AV1 MP4 and WebM checks use a checked-in fixture and exercise software decoding, thumbnails, and rotation re-encoding. Image check output also records the native codec versions bundled inside Pillow and pillow-heif.

After verification, each job exports its tested platform images with archive checksums and image configuration digests. Publishing loads and validates both architectures before uploading candidates, verifies their registry configuration digests, then creates release tags from those exact uploaded digests. The publish job never rebuilds images, so floating upstream releases cannot change a tested image during promotion.

## Container updates

The Python, Redis, Alpine, Nginx and PgBouncer image references float within their configured release channels. Garage has no floating stable image alias, so our shared Garage server/init image selects the latest stable 2.x official binary at build time. The server overrides the init entrypoint; both services use the same image.

The app resolves current stable FFmpeg 9.x, curl 8.x and Cairo 1.x (even minor releases) in the lightweight `native-versions` stage, then fetches and compiles exactly that manifest in the cached `native` stage. Refreshing discovery reuses the compiled libraries when the selected versions and other build inputs are unchanged. FFmpeg and curl signatures and Cairo's published SHA-256 are checked before compilation. Native release lines can update without changing source-version constants. The built versions are recorded in `/usr/local/share/imghost-native-versions.json`.

For local image refreshes, invalidate the stages which discover upstream releases:

```sh
docker build --pull --no-cache-filter native-versions -t imghost-build:latest .
docker build --pull --no-cache-filter garage -f docker/garage-init.Dockerfile -t imghost-garage-build:latest .
```

Pulling an image or rebuilding picks up its floating release channel; an already running container does not replace itself. Python package updates still pass through tests and, except for Pillow, the seven-day uv gate. Operating system images and verified native releases follow their upstream stable channels.

## Deploying without upgrading PostgreSQL

Keep all PostgreSQL image references, configuration, and storage mounts unchanged. Do not use a whole-stack `down`, `pull`, or an untargeted `up` for this upgrade.

Before changing Redis or Garage, stop new uploads, finish active jobs, then stop app, workers and scheduler. Preserve the current image IDs and take consistent snapshots of Redis data and both Garage volumes. Rehearse upgrades on copies first. Redis 8 may write persistence files that Redis 7 cannot read; rollback must restore the saved Redis 7 snapshot into a separate volume rather than reuse upgraded files.

Replace only the selected Redis, Garage, Garage init, PgBouncer, app, worker, scheduler and optional Nginx services. Use `--no-deps` when applying changes so Compose does not recreate PostgreSQL. Bring Garage up before running its init service, and wait for backing services to be ready before starting app and workers. Verify readiness, upload, thumbnail processing, media seeking, ZIP downloads, login, and queue recovery before reopening uploads.

The beginner stack keeps Redis disabled and background processing inside the app. Preserve the example files' local-network URL and origin defaults.
