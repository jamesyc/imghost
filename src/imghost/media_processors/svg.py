from __future__ import annotations

from io import BytesIO
from xml.etree import ElementTree

import cairosvg
from PIL import Image

from .. import processors as processor_module
from ..processors import MediaMetadata, MediaProcessor, SanitizedFile, ThumbnailResult, ValidationResult


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
        png = cairosvg.svg2png(bytestring=payload, output_width=processor_module.THUMB_WIDTH)
        with Image.open(BytesIO(png)) as image:
            image = image.convert("RGB")
            image.thumbnail(
                (processor_module.THUMB_WIDTH, processor_module.THUMB_WIDTH * 100),
                Image.Resampling.LANCZOS,
            )
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
                if lowered in processor_module.SVG_URL_ATTRS and self._is_unsafe_external_ref(value):
                    del element.attrib[attr_name]

    def _remove_dangerous_elements(self, root: ElementTree.Element) -> None:
        for parent in root.iter():
            for child in list(parent):
                if self._local_name(child.tag).lower() in processor_module.SVG_DANGEROUS_ELEMENTS:
                    parent.remove(child)
                    continue
                self._remove_dangerous_elements(child)

    def _is_unsafe_external_ref(self, value: str) -> bool:
        lowered = value.lower()
        if lowered.startswith("#"):
            return False
        if lowered.startswith(processor_module.UNSAFE_URL_PREFIXES):
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
