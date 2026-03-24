from __future__ import annotations

import asyncio
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass


THUMB_WIDTH = 560
ANIMATED_ORIGINAL_THRESHOLD_BYTES = 2 * 1024 * 1024
ANIMATED_THUMB_MAX_SOURCE_FRAMES = 100
VIDEO_THUMB_WEBP_QUALITY = 85
VIDEO_THUMB_WEBP_COMPRESSION_LEVEL = 4
VIDEO_PROBE_TIMEOUT_SECS = 20
VIDEO_REMUX_TIMEOUT_SECS = 60
VIDEO_SINGLE_FRAME_TIMEOUT_SECS = 30
VIDEO_ANIMATED_THUMB_TIMEOUT_SECS = 40
VIDEO_COMMAND_STDERR_LIMIT_BYTES = 8192
VIDEO_THUMB_MIN_INTERVAL_SECS = 0.1
VIDEO_THUMB_MAX_INTERVAL_SECS = 60.0
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
    rotation_degrees: int = 0


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


class VideoProcessingError(RuntimeError):
    def __init__(
        self,
        *,
        tool: str,
        exit_code: int | None = None,
        timed_out: bool = False,
        stderr_excerpt: str = "",
    ) -> None:
        self.tool = tool
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.stderr_excerpt = stderr_excerpt
        reason = "timed out" if timed_out else f"failed with exit code {exit_code}" if exit_code is not None else "failed"
        message = f"{tool} {reason}"
        if stderr_excerpt:
            message = f"{message}: {stderr_excerpt}"
        super().__init__(message)


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

from .media_processors.image import (
    AnimatedPillowProcessor,
    AvifProcessor,
    BmpProcessor,
    GifProcessor,
    HeifProcessor,
    JpegProcessor,
    PillowProcessor,
    PngProcessor,
    StaticPillowProcessor,
    TiffProcessor,
    WebpProcessor,
)
from .media_processors.svg import SvgProcessor
from .media_processors.video import MovProcessor, Mp4Processor, VideoProcessor, WebmProcessor


def build_processor_registry(max_pixels: int, video_thumb_frames: int = 10) -> ProcessorRegistry:
    registry = ProcessorRegistry()
    registry.register(JpegProcessor(max_pixels))
    registry.register(PngProcessor(max_pixels))
    registry.register(GifProcessor(max_pixels))
    registry.register(WebpProcessor(max_pixels))
    registry.register(BmpProcessor(max_pixels))
    registry.register(HeifProcessor(max_pixels))
    registry.register(AvifProcessor(max_pixels))
    registry.register(TiffProcessor(max_pixels))
    registry.register(SvgProcessor(max_pixels))
    registry.register(Mp4Processor(max_pixels, video_thumb_frames))
    registry.register(MovProcessor(max_pixels, video_thumb_frames))
    registry.register(WebmProcessor(max_pixels, video_thumb_frames))
    return registry
