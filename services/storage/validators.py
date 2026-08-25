import re
from urllib.parse import urlparse
from django.conf import settings
from typing import Iterable, Optional

# 🔹 Default sets (reusable anywhere)
DEFAULT_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
# No "avi": no browser can play it in a <video> element, so accepting one only
# bought the user a full upload followed by an unplayable post. Matches
# CHAT_VIDEO_EXTENSIONS (messaging), the recruitments allowlist, and the
# frontend's VIDEO_EXTENSIONS.
DEFAULT_VIDEO_EXTENSIONS = {"mp4", "mov", "webm"}

# 🔹 R2 sets (doc §5.2) — narrower than the Cloudinary ones above.
# "mov" is deliberately gone: after the client-side encode (doc §4 G1) the
# browser always produces mp4/webm, so a stored .mov can only be something that
# bypassed the pipeline.
MEDIA_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MEDIA_VIDEO_EXTENSIONS = {"mp4", "webm"}


# TODO(stage-6): remove — only the cloudinary provider still calls this.
def is_valid_cloudinary_url(url: str) -> bool:
    return settings.CLOUDINARY_CLOUD_NAME in url


def is_valid_media_url(url: str) -> bool:
    """
    Domain pinning (doc §8.3): a client-submitted media URL is only trusted if
    it is served from OUR delivery domain. Anything else — another bucket, an
    attacker's host, a Cloudinary leftover — is rejected before the key inside
    it is ever compared against the actor's prefix.
    """
    if not url:
        return False

    base = (settings.MEDIA_PUBLIC_BASE_URL or "").rstrip("/")

    if not base:
        return False

    return url.startswith(f"{base}/")


def extract_key_from_media_url(url: str) -> str:
    """
    https://media.goatza.com/users/1/profile.webp?v=1724500000 → users/1/profile.webp

    The `?v=` suffix is the fixed-key cache-buster (doc §4 G6) — it is part of
    the STORED url but never part of the key, so it has to come off before the
    key is compared with the submitted public_id.

    Returns "" for anything not on our domain, so a caller that skips
    is_valid_media_url still can't extract a key from a foreign URL.
    """
    if not is_valid_media_url(url):
        return ""

    base = settings.MEDIA_PUBLIC_BASE_URL.rstrip("/")
    key = url[len(base):].lstrip("/")

    # Drop any query string (`?v=<ts>` today) and fragment.
    key = key.split("?", 1)[0].split("#", 1)[0]

    return key


# TODO(stage-2): delete — doc §4 G2 replaces it with a client-uploaded poster
# object. Kept until Stage 2 removes its callers (messaging, posts).
def build_video_thumbnail_url(public_id: str) -> str:
    """
    Cloudinary auto-generates a poster frame for any uploaded video: the same
    delivery URL with resource_type=video and an ``so_0`` (still, second 0)
    transformation, served as .jpg. Derived server-side from the public_id (the
    same shape the posts upload flow builds client-side) so the client can't
    supply an arbitrary thumbnail.
    """
    if not public_id:
        return ""
    return (
        f"https://res.cloudinary.com/{settings.CLOUDINARY_CLOUD_NAME}"
        f"/video/upload/so_0/{public_id}.jpg"
    )


# TODO(stage-6): remove — the R2 path uses extract_key_from_media_url below.
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


def validate_public_id(
    user,
    public_id: str,
    org=None   # optional
):
    """
    If org passed -> validate organization path
    Else -> validate user path
    """

    if org:
        expected_prefix = f"organizations/{org.id}/"
    else:
        expected_prefix = f"users/{user.id}/"

    

    if not public_id.startswith(expected_prefix):
        raise ValueError("Invalid public_id path")


def validate_media(
    user,
    url: str,
    public_id: str,
    *,
    org=None,   # optional
    allowed_extensions: Optional[Iterable[str]] = None,
    strict: bool = True
):
    """
    Generic validator (users + organizations)

    Examples:

    User:
    validate_media(
        user=request.user,
        url=...,
        public_id=...
    )

    Org:
    validate_media(
        user=request.user,
        org=request.actor.organization,
        url=...,
        public_id=...
    )
    """

    # source check
    if not is_valid_cloudinary_url(url):
        raise ValueError("Invalid media source")

    # extension check
    if strict:
        validate_file_extension(url, allowed_extensions)

    # path ownership check
    validate_public_id(
        user=user,
        public_id=public_id,
        org=org
    )

    # compare extracted public id from URL
    extracted = extract_public_id_from_url(url)

    if extracted != public_id:
        raise ValueError("Public ID mismatch")