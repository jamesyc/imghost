import asyncio
import math
import subprocess
import threading
from io import BytesIO
import json
from pathlib import Path
from time import monotonic, sleep

from fastapi.testclient import TestClient
from PIL import Image
from pillow_heif import register_heif_opener
from starlette.datastructures import Headers, UploadFile

from imghost.app_state import AppState
from imghost.config import load_settings
from imghost.main import app
from imghost.media_processors.image import TiffProcessor
from imghost.processors import (
    ANIMATED_THUMB_MAX_SOURCE_FRAMES,
    VIDEO_ANIMATED_THUMB_TIMEOUT_SECS,
    VIDEO_COMMAND_STDERR_LIMIT_BYTES,
    VIDEO_THUMB_MAX_INTERVAL_SECS,
    VIDEO_THUMB_MIN_INTERVAL_SECS,
    VIDEO_THUMB_WEBP_COMPRESSION_LEVEL,
    VIDEO_THUMB_WEBP_QUALITY,
    VIDEO_PROBE_TIMEOUT_SECS,
    VIDEO_REMUX_TIMEOUT_SECS,
    VIDEO_SINGLE_FRAME_TIMEOUT_SECS,
    build_processor_registry,
    MediaMetadata,
    ThumbnailResult,
    VideoProcessingError,
)
from imghost.service import CurrentActor
from imghost.processors import GifProcessor, MovProcessor, Mp4Processor

register_heif_opener(thumbnails=False)

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

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def sample_video_metadata(*, duration_secs: float = 4.0) -> MediaMetadata:
    return MediaMetadata(
        width=640,
        height=360,
        duration_secs=duration_secs,
        codec_hint=None,
        is_animated=True,
        mime_type="video/mp4",
        format="mp4",
    )


def sample_video_metadata_with(
    *,
    width: int | None = 640,
    height: int | None = 360,
    duration_secs: float | None = 4.0,
) -> MediaMetadata:
    return MediaMetadata(
        width=width,
        height=height,
        duration_secs=duration_secs,
        codec_hint=None,
        is_animated=True,
        mime_type="video/mp4",
        format="mp4",
    )


async def assert_async_method_yields_while_blocked(coro_factory) -> None:
    loop_progress = threading.Event()

    def waiter(result: dict[str, bool]) -> None:
        result["progressed"] = loop_progress.wait(0.05)

    waiter_result: dict[str, bool] = {"progressed": False}
    wait_thread = threading.Thread(target=waiter, args=(waiter_result,), daemon=True)
    wait_thread.start()

    task = asyncio.create_task(coro_factory())

    async def ticker() -> None:
        await asyncio.sleep(0)
        loop_progress.set()

    ticker_task = asyncio.create_task(ticker())
    await task
    await ticker_task
    wait_thread.join(timeout=1.0)
    assert waiter_result["progressed"] is True


def make_upload_file(name: str = "sample.mp4", payload: bytes = b"fake-video") -> UploadFile:
    return UploadFile(file=BytesIO(payload), filename=name, headers=Headers({"content-type": "video/mp4"}))


def modern_image_bytes(
    *,
    mode: str = "RGB",
    size: tuple[int, int] = (8, 8),
    color: str | tuple[int, int, int] | tuple[int, int, int, int] = "red",
    format_name: str = "HEIF",
) -> bytes:
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, format=format_name)
    return output.getvalue()


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


def test_registry_registers_heif_and_avif_processors() -> None:
    registry = build_processor_registry(50_000_000, video_thumb_frames=6)

    assert registry.get_processor("heic") is not None
    assert registry.get_processor("heif") is not None
    assert registry.get_processor("avif") is not None


def test_tiff_uploads_are_browser_normalized_to_jpeg(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    output = BytesIO()
    Image.new("RGB", (12, 9), "red").save(output, format="TIFF")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("scan.tiff", BytesIO(output.getvalue()), "image/tiff"))],
        )

        assert response.status_code == 200
        media_id = response.json()["media_id"]

        original = client.get(f"/i/{media_id}.jpeg")
        assert original.status_code == 200
        assert original.headers["content-type"] == "image/jpeg"
        assert original.content.startswith(b"\xff\xd8")

        wait_for_thumbnail(client, media_id)
        thumb = client.get(f"/t/{media_id}.jpg")
        assert thumb.status_code == 200
        assert thumb.headers["content-type"] == "image/jpeg"


def test_tiff_processor_uses_embedded_icc_profile_for_jpeg_normalization(monkeypatch) -> None:
    output = BytesIO()
    Image.new("CMYK", (8, 8), (0, 255, 255, 0)).save(output, format="TIFF")
    payload = output.getvalue()
    processor = TiffProcessor(max_pixels=50_000_000)
    metadata = MediaMetadata(
        width=8,
        height=8,
        duration_secs=None,
        codec_hint=None,
        is_animated=False,
        mime_type="image/tiff",
        format="tiff",
    )
    called: dict[str, object] = {}

    def fake_image_cms_profile(profile_bytes):
        called["icc_profile"] = profile_bytes.read()
        return "src-profile"

    def fake_create_profile(name):
        called["target_profile"] = name
        return "srgb-profile"

    def fake_profile_to_profile(image, source_profile, target_profile, outputMode):
        called["output_mode"] = outputMode
        called["source_profile"] = source_profile
        called["target_profile_obj"] = target_profile
        return image.convert(outputMode)

    monkeypatch.setattr("imghost.media_processors.image.ImageCms.ImageCmsProfile", fake_image_cms_profile)
    monkeypatch.setattr("imghost.media_processors.image.ImageCms.createProfile", fake_create_profile)
    monkeypatch.setattr("imghost.media_processors.image.ImageCms.profileToProfile", fake_profile_to_profile)

    with Image.open(BytesIO(payload)) as source:
        source.info["icc_profile"] = b"fake-icc-profile"
        monkeypatch.setattr(processor, "_open_image", lambda data: source.copy())

        sanitized = asyncio.run(processor.sanitize(payload, metadata))

    assert sanitized.format == "jpeg"
    assert sanitized.mime_type == "image/jpeg"
    assert called["icc_profile"] == b"fake-icc-profile"
    assert called["target_profile"] == "sRGB"
    assert called["output_mode"] == "RGB"


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


def test_svg_evil_input_corpus_rejects_malformed_fixture(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    payload = (FIXTURES_DIR / "evil_svg_malformed_unclosed.svg").read_bytes()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("vector.svg", BytesIO(payload), "image/svg+xml"))],
        )

        assert response.status_code == 415
        assert response.json()["detail"] == "Unsupported or invalid image file."


def test_svg_evil_input_corpus_sanitizes_doctype_entity_fixture(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    payload = (FIXTURES_DIR / "evil_svg_doctype_entity.svg").read_bytes()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("vector.svg", BytesIO(payload), "image/svg+xml"))],
        )

        assert response.status_code == 200
        media_id = response.json()["media_id"]
        original = client.get(f"/i/{media_id}.svg")
        assert original.status_code == 200
        assert b"<!DOCTYPE" not in original.content
        assert b"data:image" not in original.content
        assert b"https://example.com" not in original.content
        assert b'fill="red"' in original.content


def test_svg_evil_input_corpus_sanitizes_processing_instruction_fixture(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    payload = (FIXTURES_DIR / "evil_svg_stylesheet_processing_instruction.svg").read_bytes()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("vector.svg", BytesIO(payload), "image/svg+xml"))],
        )

        assert response.status_code == 200
        media_id = response.json()["media_id"]
        original = client.get(f"/i/{media_id}.svg")
        assert original.status_code == 200
        assert b"xml-stylesheet" not in original.content
        assert b"javascript:" not in original.content
        assert b"https://example.com/track.css" not in original.content
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


def test_build_processor_registry_keeps_expected_format_mapping() -> None:
    registry = build_processor_registry(50_000_000, video_thumb_frames=6)

    assert registry.get_processor("jpg").__class__.__name__ == "JpegProcessor"
    assert registry.get_processor("jpeg").__class__.__name__ == "JpegProcessor"
    assert registry.get_processor("png").__class__.__name__ == "PngProcessor"
    assert registry.get_processor("gif").__class__.__name__ == "GifProcessor"
    assert registry.get_processor("webp").__class__.__name__ == "WebpProcessor"
    assert registry.get_processor("bmp").__class__.__name__ == "BmpProcessor"
    assert registry.get_processor("tif").__class__.__name__ == "TiffProcessor"
    assert registry.get_processor("tiff").__class__.__name__ == "TiffProcessor"
    assert registry.get_processor("svg").__class__.__name__ == "SvgProcessor"
    assert registry.get_processor("mp4").__class__.__name__ == "Mp4Processor"
    assert registry.get_processor("m4v").__class__.__name__ == "Mp4Processor"
    assert registry.get_processor("mov").__class__.__name__ == "MovProcessor"
    assert registry.get_processor("webm").__class__.__name__ == "WebmProcessor"


def test_mp4_processor_maps_ffprobe_metadata(monkeypatch) -> None:
    payload = b"fake-mp4"
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        if args[0] == "ffprobe":
            assert timeout == VIDEO_PROBE_TIMEOUT_SECS
            assert "-hide_banner" in args
            assert args[args.index("-v") + 1] == "error"
            assert stdout == subprocess.PIPE
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
    assert metadata.rotation_degrees == 0


def test_mp4_processor_maps_rotation_from_ffprobe_tags(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        assert args[0] == "ffprobe"
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 1080,
                            "height": 1920,
                            "duration": "2.5",
                            "tags": {"rotate": "90"},
                        }
                    ],
                    "format": {"duration": "2.5"},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    metadata = asyncio.run(processor.extract_metadata(b"fake-mp4", "mp4"))

    assert metadata.rotation_degrees == 90


def test_mp4_processor_maps_rotation_from_ffprobe_side_data_and_normalizes_negative_values(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        assert args[0] == "ffprobe"
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 1920,
                            "height": 1080,
                            "duration": "2.5",
                            "side_data_list": [{"rotation": -90}],
                        }
                    ],
                    "format": {"duration": "2.5"},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    metadata = asyncio.run(processor.extract_metadata(b"fake-mp4", "mp4"))

    assert metadata.rotation_degrees == 270


def test_mov_processor_sets_hevc_codec_hint(monkeypatch) -> None:
    processor = MovProcessor(max_pixels=50_000_000, thumb_frames=10)

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        assert timeout == VIDEO_PROBE_TIMEOUT_SECS
        assert "-hide_banner" in args
        assert args[args.index("-v") + 1] == "error"
        assert stdout == subprocess.PIPE
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

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        timeouts.append((args[0], timeout))
        if args[0] == "ffprobe":
            assert "-hide_banner" in args
            assert args[args.index("-v") + 1] == "error"
            assert stdout == subprocess.PIPE
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
        assert "-hide_banner" in args
        assert args[args.index("-v") + 1] == "error"
        assert stdout == subprocess.DEVNULL
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


def test_video_processor_reencodes_rotated_video_to_bake_in_orientation(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    ffmpeg_commands: list[list[str]] = []

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
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
                                "width": 1080,
                                "height": 1920,
                                "duration": "2.5",
                                "tags": {"rotate": "90"},
                            }
                        ],
                        "format": {"duration": "2.5"},
                    }
                ),
                stderr="",
            )
        ffmpeg_commands.append(args)
        Path(args[-1]).write_bytes(b"remuxed")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    metadata = asyncio.run(processor.extract_metadata(b"fake-mp4", "mp4"))
    sanitized = asyncio.run(processor.sanitize(b"fake-mp4", metadata))

    assert sanitized.data == b"remuxed"
    assert ffmpeg_commands
    assert "-c:v" in ffmpeg_commands[0]
    assert "libx264" in ffmpeg_commands[0]
    assert "-c:a" in ffmpeg_commands[0]
    assert "copy" in ffmpeg_commands[0]
    assert "-metadata:s:v:0" in ffmpeg_commands[0]
    assert "rotate=0" in ffmpeg_commands[0]
    assert ffmpeg_commands[0].count("copy") == 1


def test_video_processor_converts_timeout_errors_into_runtime_failures(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    validation = asyncio.run(processor.validate(b"fake-mp4"))
    assert validation.ok is False
    assert validation.rejection_reason == "Unsupported or invalid video file."


def test_video_processor_binds_ffmpeg_stderr_excerpt_to_tail_only(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    huge_stderr = ("a" * (VIDEO_COMMAND_STDERR_LIMIT_BYTES + 128)) + "TAIL-END"

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        assert stderr is not None
        stderr.write(huge_stderr.encode("utf-8"))
        stderr.flush()
        return subprocess.CompletedProcess(args=args, returncode=9, stdout="")

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    try:
        processor._run_ffmpeg_command(["ffmpeg", "-hide_banner", "-v", "error", "-i", "in.mp4", "out.mp4"], timeout=1)
    except VideoProcessingError as exc:
        assert exc.tool == "ffmpeg"
        assert exc.exit_code == 9
        assert exc.timed_out is False
        assert len(exc.stderr_excerpt.encode("utf-8")) <= VIDEO_COMMAND_STDERR_LIMIT_BYTES
        assert exc.stderr_excerpt.endswith("TAIL-END")
        assert exc.stderr_excerpt != huge_stderr
    else:
        raise AssertionError("expected bounded ffmpeg failure")


def test_video_processor_probe_timeout_uses_bounded_stderr_excerpt(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    timeout_stderr = ("b" * (VIDEO_COMMAND_STDERR_LIMIT_BYTES + 64)) + "PROBE-TIMEOUT"

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        assert stderr is not None
        stderr.write(timeout_stderr.encode("utf-8"))
        stderr.flush()
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    validation = asyncio.run(processor.validate(b"fake-mp4"))
    assert validation.ok is False
    assert validation.rejection_reason == "Unsupported or invalid video file."

    try:
        processor._run_probe_command(["ffprobe", "-hide_banner", "-v", "error", "-of", "json", "in.mp4"], timeout=1)
    except VideoProcessingError as exc:
        assert exc.tool == "ffprobe"
        assert exc.timed_out is True
        assert exc.exit_code is None
        assert len(exc.stderr_excerpt.encode("utf-8")) <= VIDEO_COMMAND_STDERR_LIMIT_BYTES
        assert exc.stderr_excerpt.endswith("PROBE-TIMEOUT")
    else:
        raise AssertionError("expected bounded probe timeout failure")


def test_video_processor_rejects_zero_width_metadata(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    monkeypatch.setattr(processor, "_probe", lambda payload: sample_video_metadata_with(width=0))

    validation = asyncio.run(processor.validate(b"fake-mp4"))

    assert validation.ok is False
    assert validation.rejection_reason == "Unsupported or invalid video file."


def test_video_processor_rejects_zero_height_metadata(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    monkeypatch.setattr(processor, "_probe", lambda payload: sample_video_metadata_with(height=0))

    validation = asyncio.run(processor.validate(b"fake-mp4"))

    assert validation.ok is False


def test_video_processor_rejects_negative_dimensions(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    monkeypatch.setattr(processor, "_probe", lambda payload: sample_video_metadata_with(width=-1, height=-10))

    validation = asyncio.run(processor.validate(b"fake-mp4"))

    assert validation.ok is False


def test_video_processor_rejects_negative_duration(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    monkeypatch.setattr(processor, "_probe", lambda payload: sample_video_metadata_with(duration_secs=-1.0))

    validation = asyncio.run(processor.validate(b"fake-mp4"))

    assert validation.ok is False


def test_video_processor_rejects_nan_duration(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    monkeypatch.setattr(processor, "_probe", lambda payload: sample_video_metadata_with(duration_secs=math.nan))

    validation = asyncio.run(processor.validate(b"fake-mp4"))

    assert validation.ok is False


def test_video_processor_rejects_infinite_duration(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    monkeypatch.setattr(processor, "_probe", lambda payload: sample_video_metadata_with(duration_secs=math.inf))

    validation = asyncio.run(processor.validate(b"fake-mp4"))

    assert validation.ok is False


def test_video_processor_missing_duration_falls_back_to_single_frame(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    calls: list[tuple[str, float]] = []

    def fake_single_frame(payload: bytes, extension: str, *, seek_seconds: float) -> bytes:
        calls.append(("single", seek_seconds))
        return b"jpg-thumb"

    def fake_animated(payload: bytes, extension: str, duration_secs: float) -> bytes | None:
        calls.append(("animated", duration_secs))
        return b"webp-thumb"

    monkeypatch.setattr(processor, "_single_frame_thumbnail", fake_single_frame)
    monkeypatch.setattr(processor, "_animated_thumbnail", fake_animated)

    thumbnail = asyncio.run(processor.generate_thumbnail(b"fake-mp4", sample_video_metadata_with(duration_secs=None)))

    assert thumbnail.format == "jpg"
    assert calls == [("single", 0.0)]


def test_video_processor_very_long_duration_clamps_interval(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    seen_filters: list[str] = []
    seen_args: list[list[str]] = []

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        seen_args.append(args)
        seen_filters.append(args[args.index("-vf") + 1])
        output_path = args[-1]
        Path(output_path).write_bytes(b"webp-thumb")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="")

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    animated = processor._animated_thumbnail(b"fake-mp4", "mp4", VIDEO_THUMB_MAX_INTERVAL_SECS * 1000)

    assert animated == b"webp-thumb"
    assert seen_filters == [f"fps=1/{VIDEO_THUMB_MAX_INTERVAL_SECS:.6f},scale=560:-1"]
    args = seen_args[0]
    assert args[args.index("-c:v") + 1] == "libwebp"
    assert args[args.index("-quality") + 1] == str(VIDEO_THUMB_WEBP_QUALITY)
    assert args[args.index("-compression_level") + 1] == str(VIDEO_THUMB_WEBP_COMPRESSION_LEVEL)
    assert args[args.index("-lossless") + 1] == "0"


def test_video_processor_tiny_positive_duration_clamps_interval(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    seen_filters: list[str] = []

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        seen_filters.append(args[args.index("-vf") + 1])
        output_path = args[-1]
        Path(output_path).write_bytes(b"webp-thumb")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="")

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    animated = processor._animated_thumbnail(b"fake-mp4", "mp4", VIDEO_THUMB_MIN_INTERVAL_SECS / 1000)

    assert animated == b"webp-thumb"
    assert seen_filters == [f"fps=1/{VIDEO_THUMB_MIN_INTERVAL_SECS:.6f},scale=560:-1"]


def test_video_processor_normal_duration_behavior_is_unchanged(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    calls: list[str] = []

    def fake_animated(payload: bytes, extension: str, duration_secs: float) -> bytes | None:
        calls.append("animated")
        return b"webp-thumb"

    def fake_single_frame(payload: bytes, extension: str, *, seek_seconds: float) -> bytes:
        calls.append("single")
        return b"jpg-thumb"

    monkeypatch.setattr(processor, "_animated_thumbnail", fake_animated)
    monkeypatch.setattr(processor, "_single_frame_thumbnail", fake_single_frame)

    thumbnail = asyncio.run(processor.generate_thumbnail(b"x" * 50, sample_video_metadata_with(duration_secs=5.0)))

    assert thumbnail.format == "webp"
    assert calls == ["animated"]


def test_video_processing_error_handles_empty_stderr(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        assert stderr is not None
        stderr.flush()
        return subprocess.CompletedProcess(args=args, returncode=3, stdout="")

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    try:
        processor._run_ffmpeg_command(["ffmpeg", "-hide_banner", "-v", "error", "-i", "in.mp4", "out.mp4"], timeout=1)
    except VideoProcessingError as exc:
        assert exc.tool == "ffmpeg"
        assert exc.exit_code == 3
        assert exc.timed_out is False
        assert exc.stderr_excerpt == ""
        assert str(exc) == "ffmpeg failed with exit code 3"
    else:
        raise AssertionError("expected empty-stderr ffmpeg failure")


def test_video_processing_error_decodes_non_utf8_stderr_with_replacement(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    payload = (b"\xff\xfe\xfd" * 4000) + b"TAIL"

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        assert stderr is not None
        stderr.write(payload)
        stderr.flush()
        return subprocess.CompletedProcess(args=args, returncode=4, stdout="")

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    try:
        processor._run_ffmpeg_command(["ffmpeg", "-hide_banner", "-v", "error", "-i", "in.mp4", "out.mp4"], timeout=1)
    except VideoProcessingError as exc:
        assert exc.tool == "ffmpeg"
        assert exc.exit_code == 4
        assert exc.timed_out is False
        assert len(exc.stderr_excerpt.encode("utf-8")) <= VIDEO_COMMAND_STDERR_LIMIT_BYTES * 3
        assert "\ufffd" in exc.stderr_excerpt
        assert exc.stderr_excerpt.endswith("TAIL")
    else:
        raise AssertionError("expected non-utf8 ffmpeg failure")


def test_video_processing_error_message_includes_tail_excerpt(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def fake_run(args, stdout=None, stderr=None, text=False, timeout=None, check=False):
        assert stderr is not None
        stderr.write(b"failure-tail")
        stderr.flush()
        return subprocess.CompletedProcess(args=args, returncode=7, stdout="")

    monkeypatch.setattr("imghost.processors.subprocess.run", fake_run)

    try:
        processor._run_probe_command(["ffprobe", "-hide_banner", "-v", "error", "-of", "json", "in.mp4"], timeout=1)
    except VideoProcessingError as exc:
        assert str(exc) == "ffprobe failed with exit code 7: failure-tail"
    else:
        raise AssertionError("expected probe failure with formatted message")


def test_video_processor_extract_metadata_offloads_probe_to_thread(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    expected = sample_video_metadata()
    calls: list[tuple[object, tuple[object, ...]]] = []

    def fake_probe(payload: bytes) -> MediaMetadata:
        assert payload == b"fake-mp4"
        return expected

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args))
        return func(*args, **kwargs)

    monkeypatch.setattr(processor, "_probe", fake_probe)
    monkeypatch.setattr("imghost.processors.asyncio.to_thread", fake_to_thread)

    metadata = asyncio.run(processor.extract_metadata(b"fake-mp4", "mp4"))

    assert metadata == expected
    assert calls == [(fake_probe, (b"fake-mp4",))]


def test_video_processor_sanitize_offloads_remux_to_thread(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    metadata = sample_video_metadata()
    calls: list[tuple[object, tuple[object, ...]]] = []

    def fake_remux(payload: bytes, extension: str, rotation_degrees: int) -> bytes:
        assert payload == b"fake-mp4"
        assert extension == "mp4"
        assert rotation_degrees == 0
        return b"remuxed-video"

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args))
        return func(*args, **kwargs)

    monkeypatch.setattr(processor, "_remux", fake_remux)
    monkeypatch.setattr("imghost.processors.asyncio.to_thread", fake_to_thread)

    sanitized = asyncio.run(processor.sanitize(b"fake-mp4", metadata))

    assert sanitized.data == b"remuxed-video"
    assert sanitized.format == "mp4"
    assert calls == [(fake_remux, (b"fake-mp4", "mp4", 0))]


def test_video_processor_generate_thumbnail_offloads_single_frame_to_thread_for_short_videos(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    metadata = sample_video_metadata(duration_secs=0.5)
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def fake_single_frame(payload: bytes, extension: str, *, seek_seconds: float) -> bytes:
        assert payload == b"fake-mp4"
        assert extension == "mp4"
        assert seek_seconds == 0.5
        return b"jpg-thumb"

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(processor, "_single_frame_thumbnail", fake_single_frame)
    monkeypatch.setattr("imghost.processors.asyncio.to_thread", fake_to_thread)

    thumbnail = asyncio.run(processor.generate_thumbnail(b"fake-mp4", metadata))

    assert thumbnail.format == "jpg"
    assert thumbnail.data == b"jpg-thumb"
    assert calls == [(fake_single_frame, (b"fake-mp4", "mp4"), {"seek_seconds": 0.5})]


def test_video_processor_generate_thumbnail_offloads_animated_and_fallback_single_frame_to_thread(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)
    metadata = sample_video_metadata(duration_secs=5.0)
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def fake_animated(payload: bytes, extension: str, duration_secs: float) -> bytes | None:
        assert payload == b"fake-mp4"
        assert extension == "mp4"
        assert duration_secs == 5.0
        return None

    def fake_single_frame(payload: bytes, extension: str, *, seek_seconds: float) -> bytes:
        assert seek_seconds == 1.0
        return b"jpg-thumb"

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(processor, "_animated_thumbnail", fake_animated)
    monkeypatch.setattr(processor, "_single_frame_thumbnail", fake_single_frame)
    monkeypatch.setattr("imghost.processors.asyncio.to_thread", fake_to_thread)

    thumbnail = asyncio.run(processor.generate_thumbnail(b"fake-mp4", metadata))

    assert thumbnail.format == "jpg"
    assert thumbnail.data == b"jpg-thumb"
    assert calls == [
        (fake_animated, (b"fake-mp4", "mp4", 5.0), {}),
        (fake_single_frame, (b"fake-mp4", "mp4"), {"seek_seconds": 1.0}),
    ]


def test_video_processor_threaded_exceptions_propagate_or_reject(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("imghost.processors.asyncio.to_thread", fake_to_thread)

    def exploding_probe(payload: bytes) -> MediaMetadata:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(processor, "_probe", exploding_probe)
    validation = asyncio.run(processor.validate(b"fake-mp4"))
    assert validation.ok is False
    assert validation.rejection_reason == "Unsupported or invalid video file."

    def exploding_remux(payload: bytes, extension: str, rotation_degrees: int) -> bytes:
        raise RuntimeError("remux failed")

    monkeypatch.setattr(processor, "_remux", exploding_remux)
    try:
        asyncio.run(processor.sanitize(b"fake-mp4", sample_video_metadata()))
    except RuntimeError as exc:
        assert str(exc) == "remux failed"
    else:
        raise AssertionError("sanitize should propagate threaded remux failures")

    def exploding_animated(payload: bytes, extension: str, duration_secs: float) -> bytes | None:
        raise RuntimeError("thumb failed")

    monkeypatch.setattr(processor, "_animated_thumbnail", exploding_animated)
    try:
        asyncio.run(processor.generate_thumbnail(b"fake-mp4", sample_video_metadata(duration_secs=5.0)))
    except RuntimeError as exc:
        assert str(exc) == "thumb failed"
    else:
        raise AssertionError("generate_thumbnail should propagate threaded thumbnail failures")


def test_video_processor_extract_metadata_does_not_block_event_loop(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def slow_probe(payload: bytes) -> MediaMetadata:
        sleep(0.1)
        return sample_video_metadata()

    monkeypatch.setattr(processor, "_probe", slow_probe)

    asyncio.run(assert_async_method_yields_while_blocked(lambda: processor.extract_metadata(b"fake-mp4", "mp4")))


def test_video_processor_sanitize_does_not_block_event_loop(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def slow_remux(payload: bytes, extension: str, rotation_degrees: int) -> bytes:
        sleep(0.1)
        return b"remuxed-video"

    monkeypatch.setattr(processor, "_remux", slow_remux)

    asyncio.run(assert_async_method_yields_while_blocked(lambda: processor.sanitize(b"fake-mp4", sample_video_metadata())))


def test_video_processor_generate_thumbnail_does_not_block_event_loop(monkeypatch) -> None:
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def slow_animated(payload: bytes, extension: str, duration_secs: float) -> bytes | None:
        sleep(0.1)
        return b"w"

    monkeypatch.setattr(processor, "_animated_thumbnail", slow_animated)

    asyncio.run(
        assert_async_method_yields_while_blocked(
            lambda: processor.generate_thumbnail(b"x" * 50, sample_video_metadata(duration_secs=5.0))
        )
    )


def test_video_upload_path_can_progress_alongside_fast_status_check(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "async")

    started = threading.Event()
    release = threading.Event()

    def blocking_probe(self, payload: bytes) -> MediaMetadata:
        started.set()
        release.wait(timeout=1.0)
        return sample_video_metadata()

    def fake_remux(self, payload: bytes, extension: str, rotation_degrees: int) -> bytes:
        return b"clean-video"

    async def fast_thumb(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        return ThumbnailResult(data=b"jpg-thumb", thumb_is_orig=False, format="jpg", size=len(b"jpg-thumb"))

    monkeypatch.setattr(Mp4Processor, "_probe", blocking_probe)
    monkeypatch.setattr(Mp4Processor, "_remux", fake_remux)
    monkeypatch.setattr(Mp4Processor, "generate_thumbnail", fast_thumb)

    async def scenario() -> None:
        settings = load_settings()
        state = AppState(settings)
        await state.start()
        try:
            upload_task = asyncio.create_task(
                state.uploads.upload(
                    make_upload_file(),
                    None,
                    "Slow Video",
                    "video-upload-flow",
                    actor=CurrentActor(user=None, source="web"),
                )
            )
            deadline = monotonic() + 1.0
            while not started.is_set() and monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert started.is_set() is True

            readiness = await state.readiness_status()
            assert readiness["ok"] is True

            release.set()
            result = await upload_task
            assert result.media.media_type == "video"
        finally:
            release.set()
            await state.stop()

    asyncio.run(scenario())


def test_multiple_video_uploads_do_not_serialize_on_event_loop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "async")

    lock = threading.Lock()
    entered = 0
    both_started = threading.Event()
    release = threading.Event()

    def blocking_probe(self, payload: bytes) -> MediaMetadata:
        nonlocal entered
        with lock:
            entered += 1
            if entered == 2:
                both_started.set()
        release.wait(timeout=0.25)
        return sample_video_metadata()

    def fake_remux(self, payload: bytes, extension: str, rotation_degrees: int) -> bytes:
        return b"clean-video"

    async def fast_thumb(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        return ThumbnailResult(data=b"jpg-thumb", thumb_is_orig=False, format="jpg", size=len(b"jpg-thumb"))

    monkeypatch.setattr(Mp4Processor, "_probe", blocking_probe)
    monkeypatch.setattr(Mp4Processor, "_remux", fake_remux)
    monkeypatch.setattr(Mp4Processor, "generate_thumbnail", fast_thumb)

    async def scenario() -> None:
        settings = load_settings()
        state = AppState(settings)
        await state.start()
        try:
            upload_one = asyncio.create_task(
                state.uploads.upload(
                    make_upload_file("one.mp4", b"video-one"),
                    None,
                    "Video One",
                    "concurrent-video-one",
                    actor=CurrentActor(user=None, source="web"),
                )
            )
            upload_two = asyncio.create_task(
                state.uploads.upload(
                    make_upload_file("two.mp4", b"video-two"),
                    None,
                    "Video Two",
                    "concurrent-video-two",
                    actor=CurrentActor(user=None, source="web"),
                )
            )

            deadline = monotonic() + 0.15
            while not both_started.is_set() and monotonic() < deadline:
                await asyncio.sleep(0.01)

            release.set()
            first, second = await asyncio.gather(upload_one, upload_two)
            assert first.media.media_type == "video"
            assert second.media.media_type == "video"
            assert both_started.is_set() is True
        finally:
            release.set()
            await state.stop()

    asyncio.run(scenario())
