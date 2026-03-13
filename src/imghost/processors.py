from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree

import cairosvg
from PIL import Image, ImageOps, UnidentifiedImageError


THUMB_WIDTH = 375
ANIMATED_ORIGINAL_THRESHOLD_BYTES = 2 * 1024 * 1024
UNSAFE_URL_PREFIXES = ("http:", "https:", "//", "javascript:", "data:", "file:")


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
        return SanitizedFile(data=output.getvalue(), mime_type=self.mime_type, format=metadata.format)

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
        return ["jpeg", "jpg"]


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
            for index in range(frame_count):
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
        self._remove_scripts(root)
        for element in root.iter():
            for attr_name in list(element.attrib):
                value = element.attrib[attr_name].strip()
                lowered = attr_name.lower()
                if lowered.startswith("on"):
                    del element.attrib[attr_name]
                    continue
                if lowered.split("}")[-1] in {"href", "src"} and self._is_unsafe_external_ref(value):
                    del element.attrib[attr_name]

    def _remove_scripts(self, root: ElementTree.Element) -> None:
        for parent in root.iter():
            for child in list(parent):
                if child.tag.split("}")[-1].lower() == "script":
                    parent.remove(child)
                    continue
                self._remove_scripts(child)

    def _is_unsafe_external_ref(self, value: str) -> bool:
        lowered = value.lower()
        if lowered.startswith("#"):
            return False
        return lowered.startswith(UNSAFE_URL_PREFIXES)

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


def build_processor_registry(max_pixels: int) -> ProcessorRegistry:
    registry = ProcessorRegistry()
    registry.register(JpegProcessor(max_pixels))
    registry.register(PngProcessor(max_pixels))
    registry.register(GifProcessor(max_pixels))
    registry.register(WebpProcessor(max_pixels))
    registry.register(BmpProcessor(max_pixels))
    registry.register(SvgProcessor(max_pixels))
    return registry
