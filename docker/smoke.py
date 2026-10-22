"""Exercise the built image against a dedicated test database and optional Garage/Redis."""

import asyncio
from dataclasses import replace
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4
from zipfile import ZipFile

import httpx2
from PIL import Image, features
from pillow_heif import libheif_info

from imghost.processors import build_processor_registry


async def check_media(directory: Path) -> bytes:
    registry = build_processor_registry(1_000_000)
    png = b""
    for fmt in ["PNG", "JPEG", "WEBP", "TIFF", "HEIF", "AVIF"]:
        output = BytesIO()
        Image.new("RGB", (64, 48), "red").save(output, format=fmt)
        payload = output.getvalue()
        extension = {"JPEG": "jpg", "HEIF": "heic"}.get(fmt, fmt.lower())
        processor = registry.get_processor(extension)
        assert (await processor.validate(payload)).ok, fmt
        metadata = await processor.extract_metadata(payload, extension)
        sanitized = await processor.sanitize(payload, metadata)
        assert (await processor.generate_thumbnail(sanitized.data, metadata)).data, fmt
        if fmt == "PNG":
            png = payload

    svg = registry.get_processor("svg")
    payload = b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="48"><rect width="64" height="48" fill="red"/></svg>'
    metadata = await svg.extract_metadata(payload, "svg")
    assert (await svg.generate_thumbnail(payload, metadata)).data

    for extension, codec in [("mp4", "libx264"), ("webm", "libvpx-vp9"), ("mp4", "av1"), ("webm", "av1")]:
        video = directory / f"sample-{codec}.{extension}"
        if codec == "av1":
            # A checked-in fixture tests decoding without requiring an AV1 encoder.
            source = Path(__file__).with_name("fixtures") / "av1.mp4"
            args = ["-i", str(source), "-c", "copy"]
        else:
            args = ["-f", "lavfi", "-i", "color=red:size=64x48:rate=5", "-t", "1.2", "-c:v", codec]
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *args, str(video)], check=True, timeout=60)
        processor = registry.get_processor(extension)
        payload = video.read_bytes()
        assert (await processor.validate(payload)).ok, extension
        metadata = await processor.extract_metadata(payload, extension)
        sanitized = await processor.sanitize(payload, metadata)
        assert (await processor.generate_thumbnail(sanitized.data, metadata)).data, extension
        # Exercise the re-encoding path as well as normal stream-copy sanitization.
        rotated = await processor.sanitize(payload, replace(metadata, rotation_degrees=90))
        assert (await processor.validate(rotated.data)).ok, extension
        if extension == "mp4":
            tagged = directory / f"rotated-{codec}.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-display_rotation:v:0", "90",
                            "-i", str(video), "-c", "copy", str(tagged)], check=True, timeout=60)
            tagged_metadata = await processor.extract_metadata(tagged.read_bytes(), extension)
            assert tagged_metadata.rotation_degrees in {90, 270}
            normalized = await processor.sanitize(tagged.read_bytes(), tagged_metadata)
            normalized_metadata = await processor.extract_metadata(normalized.data, extension)
            assert (normalized_metadata.width, normalized_metadata.height) == (48, 64)
            assert normalized_metadata.rotation_degrees == 0
    return png


def check_app(directory: Path, png: bytes, mode: str) -> None:
    if "test" not in urlsplit(os.environ["DATABASE_URL"]).path.lower():
        raise RuntimeError("Smoke checks require a dedicated test database")
    origin = "http://127.0.0.1:18080"
    environment = dict(os.environ, BASE_URL=origin, SECRET_KEY="image-smoke-secret",
                       IMGHOST_DATA_DIR=str(directory / "data"), SESSION_COOKIE_SECURE="false",
                       REDIS_MODE="required" if mode == "standard" else "disabled",
                       TASK_QUEUE_MODE="redis" if mode == "standard" else "async",
                       TASK_WORKER_ENABLED="false", TASK_WORKER_QUEUES="default,thumbnails,cleanup",
                       SCHEDULER_ENABLED="true", APP_SCHEDULER_ENABLED="true" if mode == "beginner" else "false",
                       REDIS_PREFIX=f"smoke-{uuid4().hex}", PUBLIC_ORIGIN_ENABLED="false")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "imghost.main:app", "--host", "127.0.0.1", "--port", "18080"], env=environment)
    processes = [server]
    if mode == "standard":
        for command in ["run-worker", "run-scheduler"]:
            processes.append(subprocess.Popen([sys.executable, "-m", "imghost", command],
                                               env=dict(environment, TASK_WORKER_ENABLED="true")))
    try:
        with httpx2.Client(base_url=origin, timeout=30) as client:
            for attempt in range(60):
                if any(process.poll() is not None for process in processes):
                    raise RuntimeError("App, worker or scheduler exited before becoming ready")
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx2.TransportError:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError("App did not become ready")

            def upload(payload: bytes, filename: str, media_type: str) -> dict:
                response = client.post("/api/v1/upload", files={"file": (filename, payload, media_type)},
                                       headers={"Origin": origin, "Referer": origin + "/"})
                response.raise_for_status()
                item = response.json()
                media = client.get(item["media_url"])
                media.raise_for_status()
                ranged = client.get(item["media_url"], headers={"Range": "bytes=0-9"})
                assert ranged.status_code == 206
                assert ranged.content == media.content[:10]
                archive = client.get(f'/api/v1/album/{item["album_id"]}/zip')
                archive.raise_for_status()
                with ZipFile(BytesIO(archive.content)) as zipped:
                    assert len(zipped.namelist()) == 1
                for attempt in range(60):
                    thumbnail = client.get(item["thumb_url"])
                    if thumbnail.status_code == 200:
                        break
                    assert thumbnail.status_code == 202
                    time.sleep(0.25)
                else:
                    raise RuntimeError("Thumbnail worker did not finish")
                return item

            uploads = [(png, "smoke.png", "image/png")]
            for extension, media_type in [("mp4", "video/mp4"), ("webm", "video/webm")]:
                uploads.append(((directory / f"sample-av1.{extension}").read_bytes(), f"smoke-av1.{extension}", media_type))
            for payload, filename, media_type in uploads:
                upload(payload, filename, media_type)
            identity = f"smoke{uuid4().hex[:12]}"
            registered = client.post("/api/v1/auth/register", json={
                "username": identity, "email": f"{identity}@example.com", "password": "test-password",
            })
            registered.raise_for_status()
            assert client.get("/api/v1/user/me").status_code == 200
            assert client.get("/dashboard").status_code == 200
            for payload, filename, media_type in uploads:
                upload(payload, filename, media_type)
            rejected = client.post("/api/v1/auth/register", json={
                "username": identity + "long", "email": f"{identity}long@example.com", "password": "é" * 37,
            })
            assert rejected.status_code == 400
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "native"
    with tempfile.TemporaryDirectory(prefix="imghost-image-smoke-") as name:
        directory = Path(name)
        png = asyncio.run(check_media(directory))
        if mode != "native":
            check_app(directory, png, mode)
    versions = Path("/usr/local/share/imghost-native-versions.json")
    print(json.dumps({"mode": mode, "python": sys.version.split()[0], "pillow": Image.__version__,
                      "image_libraries": {name: features.version(name) for name in features.get_supported()},
                      "heif_libraries": libheif_info(),
                      "native": json.loads(versions.read_text()) if versions.exists() else "host", "ok": True}))
