"""
The rules of server-side media processing, as data — the one place they live.

Nothing here imports anything. ``services/storage`` (paths.py, r2.py) and the
upload signature view read these sets too, and a constants module that pulled
in models or views would turn those imports into a cycle.

See "Server-side media processing" in CLAUDE.md for the flow these describe.
"""

MB = 1024 * 1024

# What a job is FOR. Decides the duration cap and the output size cap. Not the
# same thing as the upload type a source was signed under: the client signs a
# highlight's source as "posts", so a job carries both.
PURPOSES = ("posts", "highlights", "chat", "recruitments")

# POLICY types (apps/accounts/views/user_upload_signature_views.py) a `source`
# file may be signed under — the four that carry a video today.
SOURCE_UPLOAD_TYPES = frozenset({"posts", "recruitments", "chat", "highlights"})

# A highlight is a clip; a photo has nowhere to go there.
VIDEO_ONLY_PURPOSES = frozenset({"highlights"})

# The POLICY types a purpose's source may be signed under. A highlight's is
# signed as "posts" (or "highlights"); every other purpose only as itself. A
# post's media in users/<id>/highlights/ would be swept with that whole folder
# when the post is deleted (delete_folder on the media's folder).
PURPOSE_UPLOAD_TYPES = {
    "posts": frozenset({"posts"}),
    "highlights": frozenset({"posts", "highlights"}),
    "chat": frozenset({"chat"}),
    "recruitments": frozenset({"recruitments"}),
}

KIND_IMAGE = "image"
KIND_VIDEO = "video"

MAX_DURATION_SECONDS = {
    "posts": 300,
    "recruitments": 300,
    "highlights": 90,
    "chat": 90,
}

# A clip the phone says is 5:00 often probes at 5:01.4 — container rounding,
# a trailing audio packet. Refusing those would refuse exactly the users who
# trimmed to the limit.
DURATION_GRACE_SECONDS = 3

# Mirrors POLICY's ready-file video caps, so a server-made file is held to the
# same ceiling as a browser-made one. Not imported from POLICY: that module is
# a DRF view, and it imports this one.
OUTPUT_MAX_VIDEO_BYTES = {
    "posts": 80 * MB,
    "recruitments": 80 * MB,
    "highlights": 40 * MB,
    "chat": 80 * MB,
}

SOURCE_VIDEO_CONTENT_TYPES = frozenset({
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-m4v",
    "video/3gpp",
    "video/3gpp2",
    "video/x-matroska",
    "video/x-msvideo",
    "video/avi",
})

SOURCE_IMAGE_CONTENT_TYPES = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
    "image/avif",
    "image/tiff",
    "image/bmp",
})

# The extension a source key is stored under, after the `.src.` infix
# (services/storage/paths.py). Without a dot: the infix supplies it.
SOURCE_EXTENSIONS = {
    "video/quicktime": "mov",
    "video/x-m4v": "m4v",
    "video/3gpp": "3gp",
    "video/3gpp2": "3g2",
    "video/x-matroska": "mkv",
    "video/x-msvideo": "avi",
    "video/avi": "avi",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/heic": "heic",
    "image/heif": "heif",
    "image/avif": "avif",
    "image/tiff": "tif",
    "image/bmp": "bmp",
}

# Outputs never change once written — every one has a fresh uuid key.
OUTPUT_CACHE_CONTROL = "public, max-age=31536000, immutable"


def source_kind(content_type):
    """"video" | "image" for a source content type, or None."""
    if content_type in SOURCE_VIDEO_CONTENT_TYPES:
        return KIND_VIDEO
    if content_type in SOURCE_IMAGE_CONTENT_TYPES:
        return KIND_IMAGE
    return None


def source_kind_for_extension(extension):
    """The kind a source key's extension implies, or None."""
    for content_type, ext in SOURCE_EXTENSIONS.items():
        if ext == extension:
            return source_kind(content_type)
    return None


# ---------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------
# What a client is told when a job fails, keyed by the code it can branch on.
# The sentence is the whole user-facing message; error_detail on the job is
# for us and never leaves the server.
VIDEO_TOO_LONG = "VIDEO_TOO_LONG"
VIDEO_NO_VIDEO_TRACK = "VIDEO_NO_VIDEO_TRACK"
SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
SOURCE_MISSING = "SOURCE_MISSING"
SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
PROCESSOR_UNAVAILABLE = "PROCESSOR_UNAVAILABLE"
PROCESSING_TIMEOUT = "PROCESSING_TIMEOUT"
INTERNAL = "INTERNAL"

ERROR_CODES = {
    VIDEO_TOO_LONG: "This video is longer than {max} — trim it and try again",
    VIDEO_NO_VIDEO_TRACK: "This file has no video in it. Try a different one",
    SOURCE_UNREADABLE: "We couldn't read this file. Try a different one",
    SOURCE_MISSING: "The upload didn't finish — please try again",
    SOURCE_TOO_LARGE: "This file is too large. Try a smaller one",
    OUTPUT_TOO_LARGE: (
        "This video is still too large after compressing. Try a shorter clip"
    ),
    PROCESSOR_UNAVAILABLE: (
        "We can't process files right now. Please try again later"
    ),
    PROCESSING_TIMEOUT: (
        "Processing this file took too long. Try a shorter clip"
    ),
    INTERNAL: "Something went wrong processing this file. Please try again",
}

# Failures that say something about OUR side (a deploy, an undersized worker,
# a bug) rather than the user's file. Logged at ERROR so Sentry raises them.
OPERATIONAL_ERROR_CODES = frozenset({
    PROCESSOR_UNAVAILABLE, PROCESSING_TIMEOUT, INTERNAL,
})

# The signature endpoint and POST /media/jobs answer with this when the flag,
# Celery or the worker says no. Lowercase like the codes in accounts' views.
SERVER_PROCESSING_UNAVAILABLE = "server_processing_unavailable"
SERVER_PROCESSING_UNAVAILABLE_MESSAGE = "Server processing is unavailable"

# Every attach path (services/storage/validators.py, the chat validators)
# refuses a `.src.` key with this: an original is attached only as the job's
# outputs, never as itself.
SOURCE_NOT_PROCESSED_MESSAGE = "This file hasn't finished processing."


def format_duration(seconds):
    """300 → "5 minutes", 60 → "1 minute", 90 → "90 seconds"."""
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def error_message(code, purpose=None):
    """The human sentence for ``code``, or None for no / an unknown code."""
    template = ERROR_CODES.get(code)
    if template is None:
        return None
    if code == VIDEO_TOO_LONG:
        cap = MAX_DURATION_SECONDS.get(purpose, MAX_DURATION_SECONDS["posts"])
        return template.format(max=format_duration(cap))
    return template


# ---------------------------------------------------------------
# Jobs and the sweeper
# ---------------------------------------------------------------
# Runs a job may start before it is given up on. A run killed by the hard time
# limit or an OOM leaves nothing behind but this counter, so it is what stops
# a file that kills the worker from killing it forever.
MAX_ATTEMPTS = 3

# Download retries on a network/botocore error, 30s apart, before INTERNAL.
SOURCE_FETCH_MAX_RETRIES = 2
SOURCE_FETCH_RETRY_COUNTDOWN = 30

# A queued job untouched this long has lost its dispatch (fallback="skip", a
# broker blip) and is dispatched again.
QUEUED_REDISPATCH_AFTER_SECONDS = 120

# On top of MEDIA_JOB_TIME_LIMIT: a processing job older than both cannot
# still be running, because the hard limit has already killed it.
STUCK_GRACE_SECONDS = 300

# A worker with no ffmpeg / pillow-heif (PROCESSOR_UNAVAILABLE) hands the job
# back without spending an attempt: during a cutover a worker that CAN convert
# is minutes away. Past an hour it is a broken deploy, and the job fails.
PROCESSOR_UNAVAILABLE_GIVE_UP_SECONDS = 60 * 60

# How long a failed job's source is kept (for a look at what broke) before the
# sweeper deletes it.
FAILED_SOURCE_TTL_SECONDS = 24 * 60 * 60

# Rows per sweeper branch per run. The sweep runs every two minutes; a backlog
# drains over a few runs rather than in one long transaction.
SWEEP_BATCH_SIZE = 100


# ---------------------------------------------------------------
# Pending publications
# ---------------------------------------------------------------
# A post or highlight whose media is still converting waits as a
# PendingPublication and is published by the server when the jobs finish.

TARGET_POST = "post"
TARGET_HIGHLIGHT = "highlight"

# Publication-only failure codes, beside the job codes above (a failed job
# fails its publication with the job's own code and sentence).
ACTOR_NO_LONGER_VALID = "ACTOR_NO_LONGER_VALID"
PUBLISH_REJECTED = "PUBLISH_REJECTED"

ACTOR_NO_LONGER_VALID_MESSAGE = "You're no longer able to post as this page"

# One job, one post or highlight: a job already held, published or attached
# is refused with this (MediaJobService.in_use).
JOB_ALREADY_USED_MESSAGE = "This upload is already used."
# A publish that raised something other than a validation error — a bug, not
# the author's request.
PUBLISH_INTERNAL_MESSAGE = (
    "Something went wrong publishing this. Please try again"
)

# Past this a publication stops waiting: a job that has not finished in two
# hours is stuck behind something the sweeper cannot fix.
PUBLICATION_TIMEOUT_SECONDS = 2 * 60 * 60

# GET /media/publications: what a client restores its "Posting…" bar from.
PUBLICATION_LIST_DAYS = 7
PUBLICATION_LIST_LIMIT = 20
PUBLICATION_PREVIEW_CHARS = 80
