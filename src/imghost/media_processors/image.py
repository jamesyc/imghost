from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from .. import processors as processor_module
from ..processors import MediaMetadata, MediaProcessor, SanitizedFile, ThumbnailResult, ValidationResult


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
            image.thumbnail(
                (processor_module.THUMB_WIDTH, processor_module.THUMB_WIDTH * 100),
                Image.Resampling.LANCZOS,
            )
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
        if metadata.is_animated and len(payload) <= processor_module.ANIMATED_ORIGINAL_THRESHOLD_BYTES:
            return ThumbnailResult(data=None, thumb_is_orig=True, format=metadata.format, size=len(payload))

        if metadata.is_animated:
            animated = self._animated_webp_thumbnail(payload)
            if animated is not None and len(animated) < len(payload):
                return ThumbnailResult(data=animated, thumb_is_orig=False, format="webp", size=len(animated))
            return ThumbnailResult(data=None, thumb_is_orig=True, format=metadata.format, size=len(payload))

        with self._open_image(payload) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            image.thumbnail(
                (processor_module.THUMB_WIDTH, processor_module.THUMB_WIDTH * 100),
                Image.Resampling.LANCZOS,
            )
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
                frame.thumbnail(
                    (processor_module.THUMB_WIDTH, processor_module.THUMB_WIDTH * 100),
                    Image.Resampling.LANCZOS,
                )
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
        if frame_count <= processor_module.ANIMATED_THUMB_MAX_SOURCE_FRAMES:
            return list(range(frame_count))
        sample_count = max(2, processor_module.ANIMATED_THUMB_MAX_SOURCE_FRAMES)
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
