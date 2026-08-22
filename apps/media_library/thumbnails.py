"""Image thumbnails, and the dimensions the picker shows.

Kept from Studio's ``services.generate_image_thumbnail``; its ffmpeg-based video
thumbnail and ffprobe metadata extraction are not ported, because there is no
ffmpeg in this project's image and adding one is not this issue.

Two things this version does that Studio's does not. It bounds the pixel count
*before* decoding — a 10 KB PNG can declare 60000×60000 and cost gigabytes of
RAM to expand, which is a denial of service delivered as a valid upload — and it
never lets a thumbnail failure fail the upload. A thumbnail is a convenience;
the asset is the product.
"""

import contextlib
import io
import logging
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

__all__ = ["ThumbnailResult", "make_thumbnail"]


class ThumbnailResult:
    """The thumbnail, if one was made, plus the original's dimensions."""

    __slots__ = ("content", "height", "width")

    def __init__(self, content: ContentFile | None = None, width: int = 0, height: int = 0) -> None:
        self.content = content
        self.width = width
        self.height = height


def make_thumbnail(file_obj: Any) -> ThumbnailResult:
    """Return a JPEG thumbnail and the source dimensions. Never raises."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a pinned dependency
        logger.warning("Pillow is not installed; media thumbnails are disabled.")
        return ThumbnailResult()

    try:
        file_obj.seek(0)
        # Annotated Any because the variable is rebound across Pillow's Image /
        # ImageFile split by the conversion branches below.
        image: Any = Image.open(file_obj)
        width, height = image.size

        # Image.open is lazy: size is read from the header, so this check runs
        # before any pixel data is decoded.
        max_pixels = int(settings.MEDIA_MAX_IMAGE_PIXELS)
        if width * height > max_pixels:
            logger.warning("Refusing to thumbnail a %dx%d image (over %d pixels).", width, height, max_pixels)
            return ThumbnailResult(width=width, height=height)

        if image.mode in ("RGBA", "LA", "P"):
            if image.mode == "P":
                image = image.convert("RGBA")
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[-1] if "A" in image.mode else None)
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")

        max_width, max_height = settings.MEDIA_THUMBNAIL_SIZE
        image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        return ThumbnailResult(ContentFile(buffer.getvalue(), name="thumbnail.jpg"), width, height)
    except Exception:
        # Broad on purpose. Pillow raises a wide and version-dependent family
        # (DecompressionBombError, UnidentifiedImageError, OSError on truncated
        # data), and every one of them means the same thing here: no thumbnail,
        # upload proceeds.
        logger.exception("Failed to generate a media thumbnail")
        return ThumbnailResult()
    finally:
        with contextlib.suppress(OSError, ValueError):
            file_obj.seek(0)
