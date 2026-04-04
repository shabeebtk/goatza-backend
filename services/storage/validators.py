import re
from urllib.parse import urlparse
from django.conf import settings
from typing import Iterable, Optional


# 🔹 Default sets (reusable anywhere)
DEFAULT_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
DEFAULT_VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "webm"}


def is_valid_cloudinary_url(url: str) -> bool:
    return settings.CLOUDINARY_CLOUD_NAME in url


def extract_public_id_from_url(url: str) -> str:
    """
    Example:
    /upload/v123/users/1/profile.jpg → users/1/profile
    """
    match = re.search(r"/upload/(?:v\d+/)?(.+)\.", url)
    return match.group(1) if match else ""


def get_file_extension(url: str) -> str:
    path = urlparse(url).path
    return path.split(".")[-1].lower()


def validate_file_extension(
    url: str,
    allowed_extensions: Optional[Iterable[str]] = None
):
    ext = get_file_extension(url)

    if allowed_extensions:
        allowed = {e.lower() for e in allowed_extensions}
        if ext not in allowed:
            raise ValueError(f"Invalid file type: .{ext}")

    return ext


def validate_public_id(user, public_id: str):
    expected_prefix = f"users/{user.id}/"

    if not public_id.startswith(expected_prefix):
        raise ValueError("Invalid public_id path")


def validate_media(
    user,
    url: str,
    public_id: str,
    *,
    allowed_extensions: Optional[Iterable[str]] = None,
    strict: bool = True
):
    """
    Generic validator (reusable for images, videos, docs)

    Params:
    - allowed_extensions → {"jpg", "png"} etc.
    - strict → if False, skip extension validation
    """

    if not is_valid_cloudinary_url(url):
        raise ValueError("Invalid media source")

    # Extension validation
    if strict:
        validate_file_extension(url, allowed_extensions)

    # Public ID validation
    validate_public_id(user, public_id)

    extracted = extract_public_id_from_url(url)

    if extracted != public_id:
        raise ValueError("Public ID mismatch")