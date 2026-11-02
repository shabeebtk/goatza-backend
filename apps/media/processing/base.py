"""
What the converters hand back, and the two ways they fail.

The converters (video.py, image.py) know nothing about jobs, R2 or Django
models: a local file goes in, local files and measurements come out. That keeps
them runnable by hand against a file on disk.
"""

from dataclasses import dataclass


class ProcessingError(Exception):
    """
    An EXPECTED failure: the file is unusable (too long, unreadable, no video
    track) or the box cannot convert it (no ffmpeg). Carries one of the
    ERROR_CODES. The job is marked failed and the task does not raise — a
    Celery retry would only fail the same way again.
    """

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class SourceFetchError(Exception):
    """
    The source could not be DOWNLOADED (network, R2 5xx) — the one transient
    failure, which the task retries a couple of times before giving up.
    """


@dataclass
class ProcessedMedia:
    output_path: str
    thumbnail_path: str
    width: int
    height: int
    duration: int | None  # whole seconds; None for an image
    size_bytes: int
