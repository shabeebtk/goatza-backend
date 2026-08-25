import time
from urllib.parse import urlparse
from django.conf import settings
from typing import Iterable, Optional

# 🔹 The accepted formats, everywhere.
#
# Images: what browsers render and what the client-side compressor emits.
DEFAULT_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

# Videos: exactly the two containers the client-side encoder produces.
#
# No "mov" and no "avi". A stored object is the byte-for-byte file the browser
# uploaded and nothing transcodes it, so anything outside this set would store
# successfully and then fail to play. The presigned PUT is signed against these
# content types (video/mp4, video/webm), which is what actually enforces the
# rule — this set is its readable half.
DEFAULT_VIDEO_EXTENSIONS = {"mp4", "webm"}



def allowed_image_extensions():
    """Image extensions accepted on an upload."""
    return DEFAULT_IMAGE_EXTENSIONS


def allowed_video_extensions():
    """Video extensions accepted on an upload."""
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
    A media URL is ours only if it is served from our public delivery origin.
    Everything we store is written as MEDIA_PUBLIC_BASE_URL + "/" + key, so a
    prefix check is the whole test — and it is what stops a client handing us a
    URL that points at somebody else's host.
    """
    if not url:
        return False
    return url.startswith(settings.MEDIA_PUBLIC_BASE_URL)


# The name every attach path calls. It was the provider switch during the
# migration; with one provider left it is a plain alias, kept so the call sites
# keep reading in terms of "is this media ours" rather than naming a host.
is_valid_media_source = is_valid_media_url


def extract_key_from_url(url: str) -> str:
    """
    The object key back out of a delivery URL.

    https://media.goatza.com/users/1/profile.webp?v=3 -> users/1/profile.webp

    The "?v=" suffix is a cache-buster stamped onto fixed-slot media (profile,
    cover, logo), which overwrite in place and would otherwise stay stale in the
    CDN. It is never part of the key. The extension IS part of the key and is
    kept.
    """
    if not url:
        return ""

    key = url
    if key.startswith(settings.MEDIA_PUBLIC_BASE_URL):
        key = key[len(settings.MEDIA_PUBLIC_BASE_URL):]

    key = key.lstrip("/")

    return key.split("?v=")[0]


# Same story as is_valid_media_source: the call sites say "give me the stored
# key for this URL" and no longer care which provider shape it came from.
extract_storage_key = extract_key_from_url


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

    Nothing generates poster frames server-side — the client captures one while
    it encodes the video — so this is the only thing standing between a video row
    and an arbitrary image URL.

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