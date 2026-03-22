from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO
import json
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree

import cairosvg
from PIL import Image, ImageOps, UnidentifiedImageError


THUMB_WIDTH = 375
ANIMATED_ORIGINAL_THRESHOLD_BYTES = 2 * 1024 * 1024
ANIMATED_THUMB_MAX_SOURCE_FRAMES = 100
VIDEO_PROBE_TIMEOUT_SECS = 20
VIDEO_REMUX_TIMEOUT_SECS = 60
VIDEO_SINGLE_FRAME_TIMEOUT_SECS = 30
VIDEO_ANIMATED_THUMB_TIMEOUT_SECS = 40
UNSAFE_URL_PREFIXES = ("http:", "https:", "//", "javascript:", "data:", "file:")
SVG_DANGEROUS_ELEMENTS = {"script", "foreignobject", "iframe", "object", "embed", "audio", "video", "style"}
SVG_URL_ATTRS = {"href", "src"}


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    rejection_reason: str | None = None


@dataclass(slots=True)
class MediaMetadata:
    width: int | None
    height: int | None
    duration_secs: float | None
    codec_hint: str | None
    is_animated: bool
    mime_type: str
    format: str


@dataclass(slots=True)
class SanitizedFile:
    data: bytes
    mime_type: str
    format: str


@dataclass(slots=True)
class ThumbnailResult:
    data: bytes | None
    thumb_is_orig: bool
    format: str
    size: int


class MediaProcessor(ABC):
    @staticmethod
    @abstractmethod
    def supported_formats() -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def validate(self, payload: bytes) -> ValidationResult:
        raise NotImplementedError

    @abstractmethod
    async def extract_metadata(self, payload: bytes, format_hint: str) -> MediaMetadata:
        raise NotImplementedError

    @abstractmethod
    async def sanitize(self, payload: bytes, metadata: MediaMetadata) -> SanitizedFile:
        raise NotImplementedError

    @abstractmethod
    async def generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        raise NotImplementedError


class ProcessorRegistry:
    def __init__(self) -> None:
        self._processors: dict[str, MediaProcessor] = {}

    def register(self, processor: MediaProcessor) -> None:
        for format_name in processor.supported_formats():
            self._processors[format_name] = processor

    def get_processor(self, format_name: str) -> MediaProcessor | None:
        return self._processors.get(format_name.lower())


class PillowProcessor(MediaProcessor):
    def __init__(self, max_pixels: int) -> None:
        self.max_pixels = max_pixels

    async def validate(self, payload: bytes) -> ValidationResult:
        try:
            with Image.open(BytesIO(payload)) as image:
                width, height = image.size
                image.load()
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
            return ValidationResult(ok=False, rejection_reason="Unsupported or invalid image file.")

        if width * height > self.max_pixels:
            return ValidationResult(ok=False, rejection_reason="Image exceeds maximum pixel count.")
        return ValidationResult(ok=True)

    async def extract_metadata(self, payload: bytes, format_hint: str) -> MediaMetadata:
        with Image.open(BytesIO(payload)) as image:
            width, height = image.size
            is_animated = bool(getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) > 1)
            mime_type = Image.MIME.get(image.format or "", f"image/{format_hint}")
            fmt = (image.format or format_hint).lower()
        return MediaMetadata(
            width=width,
            height=height,
            duration_secs=None,
            codec_hint=None,
            is_animated=is_animated,
            mime_type=mime_type,
            format=fmt,
        )

    def _open_image(self, payload: bytes) -> Image.Image:
        return Image.open(BytesIO(payload))


class StaticPillowProcessor(PillowProcessor):
    save_format: str
    mime_type: str

    async def sanitize(self, payload: bytes, metadata: MediaMetadata) -> SanitizedFile:
        with self._open_image(payload) as image:
            image = ImageOps.exif_transpose(image)
            converted = image.convert("RGB") if self.save_format == "JPEG" and image.mode not in ("RGB", "L") else image
            output = BytesIO()
            save_kwargs: dict[str, object] = {}
            if self.save_format == "JPEG":
                save_kwargs["quality"] = 95
            converted.save(output, format=self.save_format, **save_kwargs)
        normalized_format = "jpeg" if self.save_format == "JPEG" else self.save_format.lower()
        return SanitizedFile(data=output.getvalue(), mime_type=self.mime_type, format=normalized_format)

    async def generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        with self._open_image(payload) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            image.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 100), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
        data = output.getvalue()
        return ThumbnailResult(data=data, thumb_is_orig=False, format="jpg", size=len(data))


class JpegProcessor(StaticPillowProcessor):
    save_format = "JPEG"
    mime_type = "image/jpeg"

    @staticmethod
    def supported_formats() -> list[str]:
        return ["jpeg", "jpg", "mpo"]


class PngProcessor(StaticPillowProcessor):
    save_format = "PNG"
    mime_type = "image/png"

    @staticmethod
    def supported_formats() -> list[str]:
        return ["png"]


class BmpProcessor(StaticPillowProcessor):
    save_format = "BMP"
    mime_type = "image/bmp"

    @staticmethod
    def supported_formats() -> list[str]:
        return ["bmp"]


class AnimatedPillowProcessor(PillowProcessor):
    mime_type: str

    async def sanitize(self, payload: bytes, metadata: MediaMetadata) -> SanitizedFile:
        if metadata.is_animated:
            return SanitizedFile(data=payload, mime_type=self.mime_type, format=metadata.format)

        with self._open_image(payload) as image:
            image = ImageOps.exif_transpose(image)
            output = BytesIO()
            image.save(output, format=metadata.format.upper())
        return SanitizedFile(data=output.getvalue(), mime_type=self.mime_type, format=metadata.format)

    async def generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        if metadata.is_animated and len(payload) <= ANIMATED_ORIGINAL_THRESHOLD_BYTES:
            return ThumbnailResult(data=None, thumb_is_orig=True, format=metadata.format, size=len(payload))

        if metadata.is_animated:
            animated = self._animated_webp_thumbnail(payload)
            if animated is not None and len(animated) < len(payload):
                return ThumbnailResult(data=animated, thumb_is_orig=False, format="webp", size=len(animated))
            return ThumbnailResult(data=None, thumb_is_orig=True, format=metadata.format, size=len(payload))

        with self._open_image(payload) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            image.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 100), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
        data = output.getvalue()
        return ThumbnailResult(data=data, thumb_is_orig=False, format="jpg", size=len(data))

    def _animated_webp_thumbnail(self, payload: bytes) -> bytes | None:
        with self._open_image(payload) as image:
            frame_count = getattr(image, "n_frames", 1)
            if frame_count <= 1:
                return None
            frames: list[Image.Image] = []
            durations: list[int] = []
            loop = image.info.get("loop", 0)
            for index in self._frame_indexes(frame_count):
                image.seek(index)
                frame = ImageOps.exif_transpose(image.copy())
                frame = frame.convert("RGBA")
                frame.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 100), Image.Resampling.LANCZOS)
                frames.append(frame)
                durations.append(int(image.info.get("duration", 100)))

        if not frames:
            return None
        output = BytesIO()
        frames[0].save(
            output,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop,
            quality=80,
            method=6,
        )
        return output.getvalue()

    def _frame_indexes(self, frame_count: int) -> list[int]:
        if frame_count <= ANIMATED_THUMB_MAX_SOURCE_FRAMES:
            return list(range(frame_count))
        sample_count = max(2, ANIMATED_THUMB_MAX_SOURCE_FRAMES)
        last_index = frame_count - 1
        indexes = {round(position * last_index / (sample_count - 1)) for position in range(sample_count)}
        return sorted(indexes)


class GifProcessor(AnimatedPillowProcessor):
    mime_type = "image/gif"

    @staticmethod
    def supported_formats() -> list[str]:
        return ["gif"]


class WebpProcessor(AnimatedPillowProcessor):
    mime_type = "image/webp"

    @staticmethod
    def supported_formats() -> list[str]:
        return ["webp"]


class SvgProcessor(MediaProcessor):
    def __init__(self, max_pixels: int) -> None:
        self.max_pixels = max_pixels

    @staticmethod
    def supported_formats() -> list[str]:
        return ["svg"]

    async def validate(self, payload: bytes) -> ValidationResult:
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError:
            return ValidationResult(ok=False, rejection_reason="Unsupported or invalid image file.")

        if not root.tag.endswith("svg"):
            return ValidationResult(ok=False, rejection_reason="Unsupported or invalid image file.")

        width, height = self._svg_dimensions(root)
        if width is not None and height is not None and width * height > self.max_pixels:
            return ValidationResult(ok=False, rejection_reason="Image exceeds maximum pixel count.")
        return ValidationResult(ok=True)

    async def extract_metadata(self, payload: bytes, format_hint: str) -> MediaMetadata:
        root = ElementTree.fromstring(payload)
        width, height = self._svg_dimensions(root)
        return MediaMetadata(
            width=width,
            height=height,
            duration_secs=None,
            codec_hint=None,
            is_animated=False,
            mime_type="image/svg+xml",
            format="svg",
        )

    async def sanitize(self, payload: bytes, metadata: MediaMetadata) -> SanitizedFile:
        root = ElementTree.fromstring(payload)
        self._sanitize_svg(root)
        sanitized = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        return SanitizedFile(data=sanitized, mime_type="image/svg+xml", format="svg")

    async def generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        png = cairosvg.svg2png(bytestring=payload, output_width=THUMB_WIDTH)
        with Image.open(BytesIO(png)) as image:
            image = image.convert("RGB")
            image.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 100), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
        data = output.getvalue()
        return ThumbnailResult(data=data, thumb_is_orig=False, format="jpg", size=len(data))

    def _sanitize_svg(self, root: ElementTree.Element) -> None:
        self._remove_dangerous_elements(root)
        for element in root.iter():
            for attr_name in list(element.attrib):
                value = element.attrib[attr_name].strip()
                lowered = self._local_name(attr_name).lower()
                if lowered.startswith("on"):
                    del element.attrib[attr_name]
                    continue
                if lowered == "style":
                    del element.attrib[attr_name]
                    continue
                if lowered in SVG_URL_ATTRS and self._is_unsafe_external_ref(value):
                    del element.attrib[attr_name]

    def _remove_dangerous_elements(self, root: ElementTree.Element) -> None:
        for parent in root.iter():
            for child in list(parent):
                if self._local_name(child.tag).lower() in SVG_DANGEROUS_ELEMENTS:
                    parent.remove(child)
                    continue
                self._remove_dangerous_elements(child)

    def _is_unsafe_external_ref(self, value: str) -> bool:
        lowered = value.lower()
        if lowered.startswith("#"):
            return False
        if lowered.startswith(UNSAFE_URL_PREFIXES):
            return True
        return True

    def _local_name(self, value: str) -> str:
        return value.split("}")[-1].split(":")[-1]

    def _svg_dimensions(self, root: ElementTree.Element) -> tuple[int | None, int | None]:
        width = self._parse_svg_length(root.attrib.get("width"))
        height = self._parse_svg_length(root.attrib.get("height"))
        if width is not None and height is not None:
            return width, height

        view_box = root.attrib.get("viewBox") or root.attrib.get("viewbox")
        if not view_box:
            return width, height
        parts = view_box.replace(",", " ").split()
        if len(parts) != 4:
            return width, height
        try:
            view_width = int(float(parts[2]))
            view_height = int(float(parts[3]))
        except ValueError:
            return width, height
        return width or view_width, height or view_height

    def _parse_svg_length(self, raw: str | None) -> int | None:
        if not raw:
            return None
        cleaned = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
        if not cleaned:
            return None
        try:
            return int(float(cleaned))
        except ValueError:
            return None


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
        if metadata.width is None or metadata.height is None:
            return ValidationResult(ok=False, rejection_reason="Unsupported or invalid video file.")
        if metadata.width * metadata.height > self.max_pixels:
            return ValidationResult(ok=False, rejection_reason="Image exceeds maximum pixel count.")
        return ValidationResult(ok=True)

    async def extract_metadata(self, payload: bytes, format_hint: str) -> MediaMetadata:
        return await asyncio.to_thread(self._probe, payload)

    async def sanitize(self, payload: bytes, metadata: MediaMetadata) -> SanitizedFile:
        return SanitizedFile(
            data=await asyncio.to_thread(self._remux, payload, metadata.format),
            mime_type=self.mime_type,
            format=metadata.format,
        )

    async def generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        duration = metadata.duration_secs or 0.0
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
            completed = self._run_command(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(input_path),
                ],
                timeout=VIDEO_PROBE_TIMEOUT_SECS,
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
        )

    def _remux(self, payload: bytes, extension: str) -> bytes:
        with self._temp_file(payload, extension) as input_path, self._temp_output_file(extension) as output_path:
            self._run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(input_path),
                    "-map_metadata",
                    "-1",
                    "-c",
                    "copy",
                    str(output_path),
                ],
                timeout=VIDEO_REMUX_TIMEOUT_SECS,
            )
            return output_path.read_bytes()

    def _single_frame_thumbnail(self, payload: bytes, extension: str, *, seek_seconds: float) -> bytes:
        with self._temp_file(payload, extension) as input_path, self._temp_output_file("jpg") as output_path:
            self._run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{max(0.0, seek_seconds):.3f}",
                    "-i",
                    str(input_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={THUMB_WIDTH}:-1",
                    str(output_path),
                ],
                timeout=VIDEO_SINGLE_FRAME_TIMEOUT_SECS,
            )
            return output_path.read_bytes()

    def _animated_thumbnail(self, payload: bytes, extension: str, duration_secs: float) -> bytes | None:
        interval = max(duration_secs / self.thumb_frames, 0.001)
        with self._temp_file(payload, extension) as input_path, self._temp_output_file("webp") as output_path:
            self._run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(input_path),
                    "-vf",
                    f"fps=1/{interval:.6f},scale={THUMB_WIDTH}:-1",
                    "-frames:v",
                    str(self.thumb_frames),
                    "-loop",
                    "0",
                    str(output_path),
                ],
                timeout=VIDEO_ANIMATED_THUMB_TIMEOUT_SECS,
            )
            if not output_path.exists():
                return None
            return output_path.read_bytes()

    def _run_command(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("video processing command failed") from exc

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


class Mp4Processor(VideoProcessor):
    mime_type = "video/mp4"

    @staticmethod
    def supported_formats() -> list[str]:
        return ["mp4"]


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


def build_processor_registry(max_pixels: int, video_thumb_frames: int = 10) -> ProcessorRegistry:
    registry = ProcessorRegistry()
    registry.register(JpegProcessor(max_pixels))
    registry.register(PngProcessor(max_pixels))
    registry.register(GifProcessor(max_pixels))
    registry.register(WebpProcessor(max_pixels))
    registry.register(BmpProcessor(max_pixels))
    registry.register(SvgProcessor(max_pixels))
    registry.register(Mp4Processor(max_pixels, video_thumb_frames))
    registry.register(MovProcessor(max_pixels, video_thumb_frames))
    registry.register(WebmProcessor(max_pixels, video_thumb_frames))
    return registry
