import asyncio
import subprocess
from io import BytesIO
import json
from pathlib import Path
from time import monotonic, sleep

from fastapi.testclient import TestClient
from PIL import Image

from imghost.main import app
from imghost.processors import (
    ANIMATED_THUMB_MAX_SOURCE_FRAMES,
    VIDEO_ANIMATED_THUMB_TIMEOUT_SECS,
    VIDEO_PROBE_TIMEOUT_SECS,
    VIDEO_REMUX_TIMEOUT_SECS,
    VIDEO_SINGLE_FRAME_TIMEOUT_SECS,
    MediaMetadata,
)
from imghost.processors import GifProcessor, MovProcessor, Mp4Processor

SVG_SAMPLE = b"""<svg xmlns="http://www.w3.org/2000/svg" width="32" height="24" onload="alert(1)">
<script>alert(1)</script>
<image href="https://example.com/track.png" width="10" height="10"/>
<rect width="32" height="24" fill="red"/>
</svg>"""

SVG_HARDENING_SAMPLE = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="32" height="24">
  <style>.bg { fill: url(https://example.com/pattern.svg#x); }</style>
  <foreignObject width="10" height="10"><body xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></body></foreignObject>
  <image href="/track.png" width="10" height="10"/>
  <use xlink:href="/sprite.svg#icon"/>
  <rect width="32" height="24" style="fill:url(https://example.com/pattern.svg#x)" fill="red"/>
</svg>"""

SVG_FRAGMENT_SAMPLE = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="32" height="24">
  <defs>
    <linearGradient id="grad"><stop offset="0%" stop-color="red"/></linearGradient>
  </defs>
  <rect width="32" height="24" fill="url(#grad)"/>
  <use href="#shape"/>
</svg>"""

SVG_NAMESPACED_FRAGMENT_SAMPLE = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="32" height="24">
  <defs>
    <g id="shape"><circle cx="8" cy="8" r="4" fill="red"/></g>
  </defs>
  <use xlink:href="#shape"/>
</svg>"""

SVG_NAMESPACED_ATTR_SAMPLE = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:svg="http://www.w3.org/2000/svg" width="32" height="24">
  <rect width="32" height="24" svg:onload="alert(1)" onmouseover="alert(2)" fill="red"/>
</svg>"""

SVG_MIXED_CASE_DANGEROUS_SAMPLE = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:svg="http://www.w3.org/2000/svg" width="32" height="24">
  <svg:foreignObject width="10" height="10"><div xmlns="http://www.w3.org/1999/xhtml">bad</div></svg:foreignObject>
  <svg:style>.danger { fill: url(https://example.com/x.svg); }</svg:style>
  <rect width="32" height="24" fill="red"/>
</svg>"""


def animated_gif_bytes(size: tuple[int, int] = (24, 24), frame_count: int = 2) -> bytes:
    colors = ["red", "blue", "green", "yellow", "purple", "orange"]
    frames = [Image.new("RGBA", size, colors[index % len(colors)]) for index in range(frame_count)]
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return output.getvalue()


def wait_for_thumbnail(client: TestClient, media_id: str, *, suffix: str = "jpg", timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        response = client.get(f"/t/{media_id}.{suffix}")
        if response.status_code == 200:
            return
        assert response.status_code == 202
        sleep(0.02)
    raise AssertionError(f"thumbnail for {media_id} was not ready within {timeout} seconds")


def test_svg_upload_sanitizes_original_and_generates_thumbnail(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("vector.svg", BytesIO(SVG_SAMPLE), "image/svg+xml"))],
        )

        assert response.status_code == 200
        payload = response.json()
        media_id = payload["media_id"]

        original = client.get(f"/i/{media_id}.svg")
        assert original.status_code == 200
        assert original.headers["content-type"] == "image/svg+xml"
        assert b"<script" not in original.content
        assert b"onload=" not in original.content
        assert b"https://example.com" not in original.content

        wait_for_thumbnail(client, media_id)
        thumb = client.get(f"/t/{media_id}.jpg")
        assert thumb.status_code == 200
        assert thumb.headers["content-type"] == "image/jpeg"
        assert thumb.content.startswith(b"\xff\xd8")


def test_svg_upload_strips_foreign_object_style_and_non_fragment_references(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("vector.svg", BytesIO(SVG_HARDENING_SAMPLE), "image/svg+xml"))],
        )

        assert response.status_code == 200
        media_id = response.json()["media_id"]

        original = client.get(f"/i/{media_id}.svg")
        assert original.status_code == 200
        assert b"<foreignObject" not in original.content
        assert b"<style" not in original.content
        assert b"style=" not in original.content
        assert b'href="/track.png"' not in original.content
        assert b'xlink:href="/sprite.svg#icon"' not in original.content
        assert b'fill="red"' in original.content


def test_svg_upload_preserves_fragment_only_references(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("vector.svg", BytesIO(SVG_FRAGMENT_SAMPLE), "image/svg+xml"))],
        )

        assert response.status_code == 200
        media_id = response.json()["media_id"]

        original = client.get(f"/i/{media_id}.svg")
        assert original.status_code == 200
        assert b'fill="url(#grad)"' in original.content
        assert b'href="#shape"' in original.content


def test_svg_upload_preserves_namespaced_fragment_references(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("vector.svg", BytesIO(SVG_NAMESPACED_FRAGMENT_SAMPLE), "image/svg+xml"))],
        )

        assert response.status_code == 200
        media_id = response.json()["media_id"]

        original = client.get(f"/i/{media_id}.svg")
        assert original.status_code == 200
        assert b'xlink:href="#shape"' in original.content or b'href="#shape"' in original.content


def test_svg_upload_strips_namespaced_and_inline_event_attributes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("vector.svg", BytesIO(SVG_NAMESPACED_ATTR_SAMPLE), "image/svg+xml"))],
        )

        assert response.status_code == 200
        media_id = response.json()["media_id"]

        original = client.get(f"/i/{media_id}.svg")
        assert original.status_code == 200
        assert b"onload=" not in original.content
        assert b"onmouseover=" not in original.content
        assert b'fill="red"' in original.content


def test_svg_upload_strips_namespaced_dangerous_elements(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("vector.svg", BytesIO(SVG_MIXED_CASE_DANGEROUS_SAMPLE), "image/svg+xml"))],
        )

        assert response.status_code == 200
        media_id = response.json()["media_id"]

        original = client.get(f"/i/{media_id}.svg")
        assert original.status_code == 200
        assert b"foreignObject" not in original.content
        assert b"<style" not in original.content
        assert b'fill="red"' in original.content


def test_gif_processor_uses_original_for_small_animated_images(monkeypatch) -> None:
    payload = animated_gif_bytes()
    processor = GifProcessor(max_pixels=50_000_000)
    metadata = asyncio.run(processor.extract_metadata(payload, "gif"))
    result = asyncio.run(processor.generate_thumbnail(payload, metadata))

    assert metadata.is_animated is True
    assert result.thumb_is_orig is True
    assert result.data is None


def test_gif_processor_generates_animated_webp_when_threshold_exceeded(monkeypatch) -> None:
    payload = animated_gif_bytes(size=(256, 256), frame_count=8)
    monkeypatch.setattr("imghost.processors.ANIMATED_ORIGINAL_THRESHOLD_BYTES", 1)
    processor = GifProcessor(max_pixels=50_000_000)
    metadata = asyncio.run(processor.extract_metadata(payload, "gif"))
    result = asyncio.run(processor.generate_thumbnail(payload, metadata))

    assert metadata.is_animated is True
    assert result.thumb_is_orig is False
    assert result.format == "webp"
    assert result.data is not None
    assert result.size == len(result.data)


def test_gif_processor_caps_source_frame_sampling(monkeypatch) -> None:
    monkeypatch.setattr("imghost.processors.ANIMATED_THUMB_MAX_SOURCE_FRAMES", 3)
    processor = GifProcessor(max_pixels=50_000_000)

    assert processor._frame_indexes(3) == [0, 1, 2]
    assert processor._frame_indexes(10) == [0, 4, 9]


def test_gif_processor_sampling_keeps_first_and_last_frame() -> None:
    processor = GifProcessor(max_pixels=50_000_000)

    indexes = processor._frame_indexes(ANIMATED_THUMB_MAX_SOURCE_FRAMES + 25)

    assert indexes[0] == 0
    assert indexes[-1] == ANIMATED_THUMB_MAX_SOURCE_FRAMES + 24
    assert len(indexes) == ANIMATED_THUMB_MAX_SOURCE_FRAMES


def test_mp4_processor_maps_ffprobe_metadata(monkeypatch) -> None:
    payload = b"fake-mp4"
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def fake_run(args, capture_output, text, check, timeout):
        if args[0] == "ffprobe":
            assert timeout == VIDEO_PROBE_TIMEOUT_SECS
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "h264",
                                "width": 640,
                                "height": 360,
                                "duration": "2.5",
                            }
                        ],
                        "format": {"duration": "2.5"},
                    }
                ),
                stderr="",
            )
        raise AssertionError(args)

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)
    metadata = asyncio.run(processor.extract_metadata(payload, "mp4"))

    assert metadata.width == 640
    assert metadata.height == 360
    assert metadata.duration_secs == 2.5
    assert metadata.codec_hint is None
    assert metadata.format == "mp4"


def test_mov_processor_sets_hevc_codec_hint(monkeypatch) -> None:
    processor = MovProcessor(max_pixels=50_000_000, thumb_frames=10)

    def fake_run(args, capture_output, text, check, timeout):
        assert timeout == VIDEO_PROBE_TIMEOUT_SECS
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "hevc",
                            "codec_tag_string": "hvc1",
                            "width": 1920,
                            "height": 1080,
                            "duration": "4.0",
                        }
                    ],
                    "format": {"duration": "4.0"},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)
    metadata = asyncio.run(processor.extract_metadata(b"fake-mov", "mov"))

    assert metadata.codec_hint == "hevc"


def test_video_processor_uses_webp_for_long_videos_and_jpg_for_short_ones(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    monkeypatch.setattr(processor, "_animated_thumbnail", lambda payload, extension, duration_secs: b"webp-thumb")
    monkeypatch.setattr(processor, "_single_frame_thumbnail", lambda payload, extension, seek_seconds: b"jpg-thumb")

    long_result = asyncio.run(
        processor.generate_thumbnail(
            b"x" * 50,
            MediaMetadata(
                width=640,
                height=360,
                duration_secs=5.0,
                codec_hint=None,
                is_animated=True,
                mime_type="video/mp4",
                format="mp4",
            ),
        )
    )
    short_result = asyncio.run(
        processor.generate_thumbnail(
            b"x" * 50,
            MediaMetadata(
                width=640,
                height=360,
                duration_secs=0.5,
                codec_hint=None,
                is_animated=True,
                mime_type="video/mp4",
                format="mp4",
            ),
        )
    )

    assert long_result.format == "webp"
    assert long_result.data == b"webp-thumb"
    assert short_result.format == "jpg"
    assert short_result.data == b"jpg-thumb"


def test_video_processor_uses_expected_command_timeouts(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    timeouts: list[tuple[str, int]] = []

    def fake_run(args, capture_output, text, check, timeout):
        timeouts.append((args[0], timeout))
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "h264",
                                "width": 640,
                                "height": 360,
                                "duration": "4.0",
                            }
                        ],
                        "format": {"duration": "4.0"},
                    }
                ),
                stderr="",
            )
        output_path = args[-1]
        if output_path.endswith(".mp4"):
            Path(output_path).write_bytes(b"remuxed")
        elif output_path.endswith(".webp"):
            Path(output_path).write_bytes(b"webp-thumb")
        elif output_path.endswith(".jpg"):
            Path(output_path).write_bytes(b"jpg-thumb")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    metadata = asyncio.run(processor.extract_metadata(b"fake-mp4", "mp4"))
    assert metadata.width == 640

    sanitized = asyncio.run(processor.sanitize(b"fake-mp4", metadata))
    assert sanitized.format == "mp4"

    thumbnail = asyncio.run(processor.generate_thumbnail(b"fake-mp4", metadata))
    assert thumbnail.format in {"webp", "jpg"}

    assert ("ffprobe", VIDEO_PROBE_TIMEOUT_SECS) in timeouts
    assert ("ffmpeg", VIDEO_REMUX_TIMEOUT_SECS) in timeouts
    assert ("ffmpeg", VIDEO_ANIMATED_THUMB_TIMEOUT_SECS) in timeouts


def test_video_processor_converts_timeout_errors_into_runtime_failures(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def fake_run(args, capture_output, text, check, timeout):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    validation = asyncio.run(processor.validate(b"fake-mp4"))
    assert validation.ok is False
    assert validation.rejection_reason == "Unsupported or invalid video file."
