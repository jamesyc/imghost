from __future__ import annotations

import asyncio
import json
import math
import subprocess
import tempfile
from pathlib import Path

from .. import processors as processor_module
from ..processors import MediaMetadata, MediaProcessor, SanitizedFile, ThumbnailResult, ValidationResult, VideoProcessingError


class VideoProcessor(MediaProcessor):
    mime_type: str
    codec_hints: dict[str, str] = {}

    def __init__(self, max_pixels: int, thumb_frames: int = 10) -> None:
        self.max_pixels = max_pixels
        self.thumb_frames = max(1, thumb_frames)

    async def validate(self, payload: bytes) -> ValidationResult:
        try:
            metadata = await asyncio.to_thread(self._probe, payload)
        except RuntimeError:
            return ValidationResult(ok=False, rejection_reason="Unsupported or invalid video file.")
        if not self._has_valid_dimensions(metadata):
            return ValidationResult(ok=False, rejection_reason="Unsupported or invalid video file.")
        if not self._has_valid_duration(metadata):
            return ValidationResult(ok=False, rejection_reason="Unsupported or invalid video file.")
        if metadata.width * metadata.height > self.max_pixels:
            return ValidationResult(ok=False, rejection_reason="Image exceeds maximum pixel count.")
        return ValidationResult(ok=True)

    async def extract_metadata(self, payload: bytes, format_hint: str) -> MediaMetadata:
        return await asyncio.to_thread(self._probe, payload)

    async def sanitize(self, payload: bytes, metadata: MediaMetadata) -> SanitizedFile:
        return SanitizedFile(
            data=await asyncio.to_thread(self._remux, payload, metadata.format, metadata.rotation_degrees),
            mime_type=self.mime_type,
            format=metadata.format,
        )

    async def generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        duration = self._normalized_duration_secs(metadata)
        if duration is None:
            data = await asyncio.to_thread(self._single_frame_thumbnail, payload, metadata.format, seek_seconds=0.0)
            return ThumbnailResult(data=data, thumb_is_orig=False, format="jpg", size=len(data))
        if duration < 1.0:
            data = await asyncio.to_thread(
                self._single_frame_thumbnail,
                payload,
                metadata.format,
                seek_seconds=min(duration, 1.0),
            )
            return ThumbnailResult(data=data, thumb_is_orig=False, format="jpg", size=len(data))

        animated = await asyncio.to_thread(self._animated_thumbnail, payload, metadata.format, duration)
        if animated is not None and len(animated) < len(payload):
            return ThumbnailResult(data=animated, thumb_is_orig=False, format="webp", size=len(animated))

        data = await asyncio.to_thread(self._single_frame_thumbnail, payload, metadata.format, seek_seconds=1.0)
        return ThumbnailResult(data=data, thumb_is_orig=False, format="jpg", size=len(data))

    def _probe(self, payload: bytes) -> MediaMetadata:
        with self._temp_file(payload, self.supported_formats()[0]) as input_path:
            completed = self._run_probe_command(
                [
                    "ffprobe",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(input_path),
                ],
                timeout=processor_module.VIDEO_PROBE_TIMEOUT_SECS,
            )
        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("ffprobe returned invalid metadata") from exc

        streams = parsed.get("streams", [])
        video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        if video_stream is None:
            raise RuntimeError("no video stream found")
        codec_name = str(video_stream.get("codec_name") or "").lower()
        codec_tag = str(video_stream.get("codec_tag_string") or "").lower()
        codec_hint = self.codec_hints.get(codec_name) or self.codec_hints.get(codec_tag)
        duration = video_stream.get("duration") or parsed.get("format", {}).get("duration")
        return MediaMetadata(
            width=self._int_or_none(video_stream.get("width")),
            height=self._int_or_none(video_stream.get("height")),
            duration_secs=self._float_or_none(duration),
            codec_hint=codec_hint,
            is_animated=True,
            mime_type=self.mime_type,
            format=self.supported_formats()[0],
            rotation_degrees=self._rotation_degrees(video_stream),
        )

    def _remux(self, payload: bytes, extension: str, rotation_degrees: int = 0) -> bytes:
        with self._temp_file(payload, extension) as input_path, self._temp_output_file(extension) as output_path:
            args = self._sanitize_command(input_path, output_path, rotation_degrees=rotation_degrees)
            self._run_ffmpeg_command(
                args,
                timeout=processor_module.VIDEO_REMUX_TIMEOUT_SECS,
            )
            return output_path.read_bytes()

    def _single_frame_thumbnail(self, payload: bytes, extension: str, *, seek_seconds: float) -> bytes:
        with self._temp_file(payload, extension) as input_path, self._temp_output_file("jpg") as output_path:
            self._run_ffmpeg_command(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    f"{max(0.0, seek_seconds):.3f}",
                    "-i",
                    str(input_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={processor_module.THUMB_WIDTH}:-1",
                    str(output_path),
                ],
                timeout=processor_module.VIDEO_SINGLE_FRAME_TIMEOUT_SECS,
            )
            return output_path.read_bytes()

    def _animated_thumbnail(self, payload: bytes, extension: str, duration_secs: float) -> bytes | None:
        interval = max(duration_secs / self.thumb_frames, processor_module.VIDEO_THUMB_MIN_INTERVAL_SECS)
        interval = min(interval, processor_module.VIDEO_THUMB_MAX_INTERVAL_SECS)
        with self._temp_file(payload, extension) as input_path, self._temp_output_file("webp") as output_path:
            self._run_ffmpeg_command(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(input_path),
                    "-vf",
                    f"fps=1/{interval:.6f},scale={processor_module.THUMB_WIDTH}:-1",
                    "-c:v",
                    "libwebp",
                    "-quality",
                    str(processor_module.VIDEO_THUMB_WEBP_QUALITY),
                    "-compression_level",
                    str(processor_module.VIDEO_THUMB_WEBP_COMPRESSION_LEVEL),
                    "-lossless",
                    "0",
                    "-frames:v",
                    str(self.thumb_frames),
                    "-loop",
                    "0",
                    str(output_path),
                ],
                timeout=processor_module.VIDEO_ANIMATED_THUMB_TIMEOUT_SECS,
            )
            if not output_path.exists():
                return None
            return output_path.read_bytes()

    def _run_probe_command(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        return self._run_command(args, timeout=timeout, capture_stdout=True)

    def _run_ffmpeg_command(self, args: list[str], *, timeout: int) -> None:
        self._run_command(args, timeout=timeout, capture_stdout=False)

    def _run_command(self, args: list[str], *, timeout: int, capture_stdout: bool) -> subprocess.CompletedProcess[str]:
        with tempfile.NamedTemporaryFile(mode="w+b") as stderr_handle:
            try:
                completed = subprocess.run(
                    args,
                    stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                    stderr=stderr_handle,
                    text=capture_stdout,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise VideoProcessingError(
                    tool=args[0],
                    timed_out=True,
                    stderr_excerpt=self._stderr_excerpt(stderr_handle),
                ) from exc
            except OSError as exc:
                raise VideoProcessingError(
                    tool=args[0],
                    stderr_excerpt=self._stderr_excerpt(stderr_handle),
                ) from exc
            if completed.returncode != 0:
                raise VideoProcessingError(
                    tool=args[0],
                    exit_code=completed.returncode,
                    stderr_excerpt=self._stderr_excerpt(stderr_handle),
                )
            return completed

    def _stderr_excerpt(self, handle) -> str:
        handle.flush()
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - processor_module.VIDEO_COMMAND_STDERR_LIMIT_BYTES)
        handle.seek(start)
        raw = handle.read(processor_module.VIDEO_COMMAND_STDERR_LIMIT_BYTES)
        if isinstance(raw, str):
            excerpt = raw
        else:
            excerpt = raw.decode("utf-8", errors="replace")
        return excerpt.strip()

    def _temp_file(self, payload: bytes, extension: str):
        suffix = f".{extension}"
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        handle.write(payload)
        handle.flush()
        handle.close()
        path = Path(handle.name)
        return _TempPath(path)

    def _temp_output_file(self, extension: str):
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=f".{extension}")
        handle.close()
        path = Path(handle.name)
        path.unlink(missing_ok=True)
        return _TempPath(path)

    def _int_or_none(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _float_or_none(self, value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _rotation_degrees(self, video_stream: dict[str, object]) -> int:
        tags = video_stream.get("tags")
        if isinstance(tags, dict):
            normalized = self._normalized_rotation(tags.get("rotate"))
            if normalized:
                return normalized
        side_data_list = video_stream.get("side_data_list")
        if isinstance(side_data_list, list):
            for side_data in side_data_list:
                if not isinstance(side_data, dict):
                    continue
                normalized = self._normalized_rotation(side_data.get("rotation"))
                if normalized:
                    return normalized
        return 0

    def _normalized_rotation(self, value: object) -> int:
        if value is None:
            return 0
        try:
            rotation = int(round(float(value))) % 360
        except (TypeError, ValueError):
            return 0
        return rotation if rotation in {90, 180, 270} else 0

    def _sanitize_command(self, input_path: Path, output_path: Path, *, rotation_degrees: int) -> list[str]:
        args = [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-map_metadata",
            "-1",
        ]
        if rotation_degrees:
            args.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "copy",
                    "-metadata:s:v:0",
                    "rotate=0",
                ]
            )
        else:
            args.extend(["-c", "copy"])
        args.append(str(output_path))
        return args

    def _has_valid_dimensions(self, metadata: MediaMetadata) -> bool:
        if metadata.width is None or metadata.height is None:
            return False
        return metadata.width > 0 and metadata.height > 0

    def _has_valid_duration(self, metadata: MediaMetadata) -> bool:
        if metadata.duration_secs is None:
            return True
        return math.isfinite(metadata.duration_secs) and metadata.duration_secs >= 0.0

    def _normalized_duration_secs(self, metadata: MediaMetadata) -> float | None:
        duration = metadata.duration_secs
        if duration is None:
            return None
        if not math.isfinite(duration) or duration < 0.0:
            return None
        return duration


class Mp4Processor(VideoProcessor):
    mime_type = "video/mp4"

    @staticmethod
    def supported_formats() -> list[str]:
        return ["mp4", "m4v"]


class MovProcessor(VideoProcessor):
    mime_type = "video/quicktime"
    codec_hints = {"hevc": "hevc", "hev1": "hevc", "hvc1": "hevc"}

    @staticmethod
    def supported_formats() -> list[str]:
        return ["mov"]


class WebmProcessor(VideoProcessor):
    mime_type = "video/webm"
    codec_hints = {"vp9": "vp9"}

    @staticmethod
    def supported_formats() -> list[str]:
        return ["webm"]


class _TempPath:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        self.path.unlink(missing_ok=True)
