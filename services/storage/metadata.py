"""
Client-supplied media metadata: width, height, duration, size.

These used to be read back from the storage provider, which made them
trustworthy. There is nothing to ask now — the object is the exact bytes the
browser uploaded — so the client reports them and the server only sanity-checks
the numbers.

That changes what they are FOR. They are cosmetic: width/height stop the feed
reflowing while an image loads, duration prints "0:42" on a video tile. Nothing
authorises anything, so a bad value must never fail a request — an out-of-range
or malformed number becomes NULL and the UI falls back to whatever it does for
older rows that have no dimensions either.

Hard limits (what may be uploaded at all) live in the upload-config POLICY and
in each service's own gates. These are not those.
"""

MB = 1024 * 1024

# A dimension no real photo or video reaches (8K is 7680 wide). Anything beyond
# it is a typo or a probe, not a picture.
MAX_DIMENSION = 8192

# Seconds. Per media type, matching the product caps.
MAX_POST_VIDEO_DURATION = 300
MAX_HIGHLIGHT_DURATION = 90
MAX_CHAT_VIDEO_DURATION = 300

# Bytes. Mirrors the upload-config POLICY, which is what actually got signed.
MAX_VIDEO_BYTES = 80 * MB
MAX_HIGHLIGHT_BYTES = 40 * MB
MAX_IMAGE_BYTES = 5 * MB


def clamp_int(value, *, minimum=1, maximum=None):
    """
    A positive int inside [minimum, maximum], or None.

    None for anything that is not a plain in-range integer — a string, a float,
    a bool, a negative, an overflow. bool is excluded explicitly because it is
    an int subclass and ``True`` would otherwise store as 1.
    """
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, int):
        return None

    if value < minimum:
        return None

    if maximum is not None and value > maximum:
        return None

    return value


def clamp_dimensions(width, height):
    """(width, height) as a pair — either may independently come back None."""
    return (
        clamp_int(width, maximum=MAX_DIMENSION),
        clamp_int(height, maximum=MAX_DIMENSION),
    )


def clamp_duration(value, maximum):
    """Whole seconds within [1, maximum], else None."""
    return clamp_int(value, maximum=maximum)


def clamp_size_bytes(value, maximum):
    """Byte count within [1, maximum], else None."""
    return clamp_int(value, maximum=maximum)
