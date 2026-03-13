import asyncio
import subprocess
from io import BytesIO
import json

from fastapi.testclient import TestClient
from PIL import Image

from imghost.main import app
from imghost.processors import MediaMetadata
from imghost.processors import GifProcessor, MovProcessor, Mp4Processor

SVG_SAMPLE = b"""<svg xmlns="http://www.w3.org/2000/svg" width="32" height="24" onload="alert(1)">
<script>alert(1)</script>
<image href="https://example.com/track.png" width="10" height="10"/>
<rect width="32" height="24" fill="red"/>
</svg>"""


def animated_gif_bytes(size: tuple[int, int] = (24, 24), frame_count: int = 2) -> bytes:
    colors = ["red", "blue", "green", "yellow", "purple", "orange"]
    frames = [Image.new("RGBA", size, colors[index % len(colors)]) for index in range(frame_count)]
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return output.getvalue()


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

        thumb = client.get(f"/t/{media_id}.jpg")
        assert thumb.status_code == 200
        assert thumb.headers["content-type"] == "image/jpeg"
        assert thumb.content.startswith(b"\xff\xd8")


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


def test_mp4_processor_maps_ffprobe_metadata(monkeypatch) -> None:
    payload = b"fake-mp4"
    processor = Mp4Processor(max_pixels=50_000_000, thumb_frames=10)

    def fake_run(args, capture_output, text, check):
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

    def fake_run(args, capture_output, text, check):
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
