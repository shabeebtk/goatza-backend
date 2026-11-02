"""
A photo original → the JPEG pair the browser would have made.

    full   longest side ≤ 2560, JPEG q85 (stepping down until ≤ 5 MB — the
           ready-image cap in POLICY), optimized, progressive
    thumb  longest side ≤ 640, JPEG q80

Orientation is baked in (EXIF transpose), an animation keeps its first frame,
transparency is composited on white, and no EXIF is written back — an iPhone
photo's GPS position does not survive. The ICC profile does, so a Display P3
photo keeps its colour.

HEIC/HEIF open through pillow-heif; AVIF is native in Pillow 12.
"""

import functools
import os

from apps.media.constants import (
    MB,
    OUTPUT_TOO_LARGE,
    SOURCE_TOO_LARGE,
    SOURCE_UNREADABLE,
)
from apps.media.processing.base import ProcessedMedia, ProcessingError

FULL_MAX_LONG_SIDE = 2560
FULL_MAX_BYTES = 5 * MB
FULL_QUALITIES = (85, 78, 70, 62, 55)

THUMB_MAX_LONG_SIDE = 640
THUMB_QUALITY = 80

# A 48 MP phone photo is ~49 M pixels; past this it is a decompression bomb,
# not a photo. Checked from the header, before a single pixel is decoded.
MAX_IMAGE_PIXELS = 120_000_000

# Modes whose ICC profile still describes the pixels after .convert("RGB").
# A CMYK or greyscale profile on an RGB JPEG would be worse than none.
_ICC_SAFE_MODES = {"RGB", "RGBA", "RGBX", "P"}


def imaging_available():
    """True when Pillow and pillow-heif import — the worker's preflight."""
    try:
        import PIL  # noqa: F401
        import pillow_heif  # noqa: F401
    except ImportError:
        return False
    return True


@functools.lru_cache(maxsize=1)
def _register_openers():
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def _to_rgb(img):
    """Any mode → RGB, alpha composited on white."""
    from PIL import Image

    if img.mode.startswith("I;16") or img.mode == "I":
        # 16-bit greyscale (PNG/TIFF): a plain convert clips everything above
        # 255 to white.
        img = img.convert("I").point(lambda value: value * (1 / 256))
        img = img.convert("L")

    if img.mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")

    if img.mode in ("LA", "La", "PA", "RGBa"):
        img = img.convert("RGBA")

    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.getchannel("A"))
        return background

    if img.mode != "RGB":
        return img.convert("RGB")

    return img


def _save_jpeg(img, path, *, qualities, max_bytes, icc_profile):
    """Save at the first quality that fits ``max_bytes``; its size, or raise."""
    for quality in qualities:
        img.save(
            path,
            "JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
            icc_profile=icc_profile,
            exif=b"",
        )
        size = os.path.getsize(path)
        if max_bytes is None or size <= max_bytes:
            return size

    raise ProcessingError(
        OUTPUT_TOO_LARGE,
        f"{size} bytes at quality {qualities[-1]} > {max_bytes}",
    )


def process(source_path, work_dir) -> ProcessedMedia:
    """Convert one image. Raises ProcessingError for an unusable file."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    _register_openers()

    try:
        with Image.open(source_path) as img:
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise ProcessingError(
                    SOURCE_TOO_LARGE, f"{img.width}x{img.height} pixels"
                )

            source_mode = img.mode
            icc_profile = img.info.get("icc_profile") \
                if source_mode in _ICC_SAFE_MODES else None

            # JPEG can decode at 1/2, 1/4, 1/8 scale straight from the DCT —
            # a 48 MP photo then never exists in memory at full size. Asks
            # for at least the full output on BOTH sides, so nothing is lost.
            if img.format == "JPEG":
                img.draft("RGB", (FULL_MAX_LONG_SIDE, FULL_MAX_LONG_SIDE))

            # Frame 0 of an animation; a loaded copy, independent of the file.
            frame = ImageOps.exif_transpose(img)

        frame = _to_rgb(frame)

    except ProcessingError:
        raise
    except Image.DecompressionBombError as e:
        raise ProcessingError(SOURCE_TOO_LARGE, str(e))
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            EOFError) as e:
        # Unknown format, truncated file, corrupt data — Pillow and its
        # plugins raise all of these for a file they cannot read.
        raise ProcessingError(SOURCE_UNREADABLE, f"{type(e).__name__}: {e}")

    lanczos = Image.Resampling.LANCZOS

    full = frame
    full.thumbnail((FULL_MAX_LONG_SIDE, FULL_MAX_LONG_SIDE), lanczos)
    full_path = os.path.join(work_dir, "full.jpg")
    size_bytes = _save_jpeg(
        full, full_path,
        qualities=FULL_QUALITIES,
        max_bytes=FULL_MAX_BYTES,
        icc_profile=icc_profile,
    )

    thumb = full.copy()
    thumb.thumbnail((THUMB_MAX_LONG_SIDE, THUMB_MAX_LONG_SIDE), lanczos)
    thumb_path = os.path.join(work_dir, "thumb.jpg")
    _save_jpeg(
        thumb, thumb_path,
        qualities=(THUMB_QUALITY,),
        max_bytes=None,
        icc_profile=icc_profile,
    )

    return ProcessedMedia(
        output_path=full_path,
        thumbnail_path=thumb_path,
        width=full.width,
        height=full.height,
        duration=None,
        size_bytes=size_bytes,
    )
