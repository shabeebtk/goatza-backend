import re
import time
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

# R2 path only. Narrower than DEFAULT_VIDEO_EXTENSIONS on purpose: no "mov".
# Videos are now encoded client-side before upload, so the only two containers
# that can reach the bucket are the two the encoder emits — and the presigned
# PUT is signed against exactly those content types (video/mp4, video/webm).
# The Cloudinary set keeps "mov" because Cloudinary transcoded it for us.
R2_VIDEO_EXTENSIONS = {"mp4", "webm"}

# The image counterpart. Same members as DEFAULT_IMAGE_EXTENSIONS — no format
# was dropped — spelled out separately so the two providers' allowlists can
# move independently.
R2_IMAGE_EXTENSIONS = {"webp", "jpg", "jpeg", "png"}


def is_valid_cloudinary_url(url: str) -> bool:
    return settings.CLOUDINARY_CLOUD_NAME in url


# ---------------------------------------------------------------
# PROVIDER-AWARE ENTRY POINTS
# ---------------------------------------------------------------
# Every media-attaching call site (posts, highlights, chat, recruitments,
# matches, profile/org photos) goes through these rather than naming a provider
# directly, so the whole backend follows settings.FILE_STORAGE_PROVIDER and a
# rollback to Cloudinary stays one env var instead of a revert.


def is_valid_media_source(url: str) -> bool:
    """Is this URL served from the storage we actually uploaded to?"""
    if settings.FILE_STORAGE_PROVIDER == "r2":
        return is_valid_media_url(url)

    # TODO(cleanup-stage): drop this branch with the Cloudinary provider.
    return bool(url) and is_valid_cloudinary_url(url)


def extract_storage_key(url: str) -> str:
    """
    The stored identifier (R2 object key / Cloudinary public_id) back out of a
    delivery URL, so it can be compared against the one the client submitted.
    """
    if settings.FILE_STORAGE_PROVIDER == "r2":
        return extract_key_from_url(url)

    # TODO(cleanup-stage): drop this branch with the Cloudinary provider.
    return extract_public_id_from_url(url)


def allowed_image_extensions():
    """Image extensions accepted on the ACTIVE provider."""
    if settings.FILE_STORAGE_PROVIDER == "r2":
        return R2_IMAGE_EXTENSIONS

    # TODO(cleanup-stage): drop this branch with the Cloudinary provider.
    return DEFAULT_IMAGE_EXTENSIONS


def allowed_video_extensions():
    """
    Video extensions accepted on the ACTIVE provider. Never "mov" on the R2
    path: a stored file is now the exact bytes the client uploaded, and nothing
    transcodes a .mov into something a browser can play.
    """
    if settings.FILE_STORAGE_PROVIDER == "r2":
        return R2_VIDEO_EXTENSIONS

    # TODO(cleanup-stage): drop this branch with the Cloudinary provider.
    return DEFAULT_VIDEO_EXTENSIONS


def same_storage_folder(key_a: str, key_b: str) -> bool:
    """
    Do two keys live in the same folder?

    This is what binds a thumbnail to its video. Both are client-supplied now,
    and the ownership prefix check alone would happily accept a poster frame
    from a DIFFERENT post by the same user — the upload-config endpoint hands
    out one presigned batch per folder, so "same folder" is the evidence that
    the two files came from the same upload.
    """
    if not key_a or not key_b:
        return False
    return key_a.rsplit("/", 1)[0] == key_b.rsplit("/", 1)[0]


def with_cache_buster(url: str) -> str:
    """
    Stamp a fixed-key URL so a replacement is actually seen.

    profile / cover / logo / org-cover each live at ONE key per actor and are
    overwritten in place, so the CDN — and every browser that already fetched it
    — keeps serving the previous image behind an unchanged URL. Appending
    ?v=<unix ts> on every replace is what makes the new upload visible.

    The paired *_public_id column stays the bare key, and
    extract_key_from_url() strips ?v= precisely so delete-after-replace still
    targets the right object. An existing ?v= is replaced, never stacked.
    """
    if not url:
        return url

    return f"{url.split('?v=')[0]}?v={int(time.time())}"


def is_valid_media_url(url: str) -> bool:
    """
    R2 counterpart of is_valid_cloudinary_url: a media URL is ours only if it is
    served from our public delivery origin. Everything we store is written as
    settings.MEDIA_PUBLIC_BASE_URL + "/" + key, so a prefix check is the whole
    test — and it is what stops a client handing us a URL pointing at somebody
    else's host.
    """
    if not url:
        return False
    return url.startswith(settings.MEDIA_PUBLIC_BASE_URL)


def extract_key_from_url(url: str) -> str:
    """
    R2 counterpart of extract_public_id_from_url — the object key back out of a
    delivery URL.

    https://media.goatza.com/users/1/profile.webp?v=3 -> users/1/profile.webp

    The "?v=" suffix is a cache-buster the client appends to fixed-slot media
    (profile, cover, logo), which overwrite in place and would otherwise stay
    stale in the CDN. It is never part of the key. Unlike the Cloudinary
    extractor the extension IS kept: on R2 the extension is part of the key.
    """
    if not url:
        return ""

    key = url
    if key.startswith(settings.MEDIA_PUBLIC_BASE_URL):
        key = key[len(settings.MEDIA_PUBLIC_BASE_URL):]

    key = key.lstrip("/")

    return key.split("?v=")[0]


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
    if not is_valid_media_source(url):
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

    # compare extracted key / public id from URL
    extracted = extract_storage_key(url)

    if extracted != public_id:
        raise ValueError("Public ID mismatch")


def validate_thumbnail(
    user,
    url: str,
    *,
    parent_key: str,
    org=None
) -> str:
    """
    Full validation for a client-supplied poster frame, plus the rule that binds
    it to its video: our source, an image extension, the caller's own ownership
    prefix, and the SAME FOLDER as the video it belongs to.

    Nothing generates poster frames server-side any more (Cloudinary's so_0
    transform is gone with the provider), so this is the only thing standing
    between a video row and an arbitrary image URL.

    Returns the thumbnail's key. Raises ValueError like validate_media, so
    callers keep the error handling they already have.
    """
    if not url:
        raise ValueError("Thumbnail is required for video")

    key = extract_storage_key(url)

    validate_media(
        user,
        url,
        key,
        org=org,
        allowed_extensions=allowed_image_extensions(),
    )

    if not same_storage_folder(key, parent_key):
        raise ValueError("Thumbnail must belong to the same upload")

    return key