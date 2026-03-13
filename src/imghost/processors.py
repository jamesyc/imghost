from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


THUMB_WIDTH = 375
ANIMATED_ORIGINAL_THRESHOLD_BYTES = 2 * 1024 * 1024


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


class PillowImageProcessor(MediaProcessor):
    def __init__(self, max_pixels: int) -> None:
        self.max_pixels = max_pixels

    @staticmethod
    def supported_formats() -> list[str]:
        return ["jpeg", "jpg", "png", "gif", "webp", "bmp"]

    async def validate(self, payload: bytes) -> ValidationResult:
        try:
            with Image.open(BytesIO(payload)) as image:
                width, height = image.size
        except UnidentifiedImageError:
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

    async def sanitize(self, payload: bytes, metadata: MediaMetadata) -> SanitizedFile:
        if metadata.is_animated:
            return SanitizedFile(data=payload, mime_type=metadata.mime_type, format=metadata.format)

        with Image.open(BytesIO(payload)) as image:
            image = ImageOps.exif_transpose(image)
            converted = image.convert("RGB") if image.mode not in ("RGB", "L") else image
            output = BytesIO()
            save_format = "JPEG" if metadata.format in {"jpeg", "jpg"} else metadata.format.upper()
            save_kwargs: dict[str, object] = {}
            if save_format == "JPEG":
                save_kwargs["quality"] = 95
            converted.save(output, format=save_format, **save_kwargs)
        return SanitizedFile(data=output.getvalue(), mime_type=metadata.mime_type, format=metadata.format)

    async def generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        if metadata.is_animated and len(payload) <= ANIMATED_ORIGINAL_THRESHOLD_BYTES:
            return ThumbnailResult(data=None, thumb_is_orig=True, format=metadata.format, size=len(payload))

        with Image.open(BytesIO(payload)) as image:
            if metadata.is_animated:
                image.seek(0)
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            image.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 100), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
        data = output.getvalue()
        return ThumbnailResult(data=data, thumb_is_orig=False, format="jpg", size=len(data))

