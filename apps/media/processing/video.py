"""
A video original → the MP4 + poster the browser would have made.

The SAME target as the client's encoder (frontend CLAUDE.md, Media), so a file
made here is indistinguishable downstream from one made on the phone:

    H.264 High, yuv420p, longest side ≤ 1280 (never upscaled, even sides),
    ≤ 30 fps, CRF 23 capped at 2.5 Mbps with a 5 Mb buffer, preset veryfast,
    +faststart, AAC 128k stereo 48 kHz when the source has audio, rotation
    baked in.

Beyond the client: HDR (iPhone HLG / Dolby Vision, PQ) is tone-mapped to SDR
BT.709 when this ffmpeg has zscale, and no metadata is carried over — an
iPhone clip's GPS position does not survive.

Every subprocess runs against ONE deadline (the task's soft limit minus a
minute), so the job records PROCESSING_TIMEOUT itself instead of being killed
mid-write by Celery.
"""

import functools
import json
import logging
import math
import os
import shutil
import subprocess
import time

from django.conf import settings

from apps.media.constants import (
    DURATION_GRACE_SECONDS,
    INTERNAL,
    OUTPUT_TOO_LARGE,
    PROCESSING_TIMEOUT,
    PROCESSOR_UNAVAILABLE,
    SOURCE_UNREADABLE,
    VIDEO_NO_VIDEO_TRACK,
    VIDEO_TOO_LONG,
)
from apps.media.processing.base import ProcessedMedia, ProcessingError

logger = logging.getLogger(__name__)

MAX_LONG_SIDE = 1280
MAX_FPS = 30
CRF = 23
MAXRATE_BPS = 2_500_000
AUDIO_BPS = 128_000
ENCODER_THREADS = 2

# The client ladder's fit: floor(cap × 0.92 × 8 / duration) − audio, never
# below 600k. The 8% is container overhead and rate-control overshoot.
FIT_HEADROOM = 0.92
MIN_FIT_VIDEO_BPS = 600_000

# color_transfer values ffprobe reports for HLG (iPhone HDR and Dolby Vision
# 8.4, which is HLG-based) and for PQ.
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}

POSTER_MAX_LONG_SIDE = 1080
POSTER_MAX_BYTES = 900 * 1024
POSTER_QUALITIES = (2, 4, 6, 9, 13)  # mjpeg -q:v; lower is better

PROBE_TIMEOUT_SECONDS = 60
STDERR_TAIL_BYTES = 4096

_warned_no_zscale = False


def tools_available():
    """True when ffmpeg and ffprobe are on this box — the worker's preflight."""
    return all(
        shutil.which(binary)
        for binary in (settings.FFMPEG_BIN, settings.FFPROBE_BIN)
    )


@functools.lru_cache(maxsize=None)
def has_zscale(ffmpeg_bin):
    """Whether this ffmpeg build has the zscale filter. Asked once per binary."""
    try:
        completed = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-filters"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    # Lines look like " TSC zscale  V->V  Apply resizing, colorspace ..."
    return any(
        line.split()[1:2] == ["zscale"]
        for line in completed.stdout.decode("utf-8", "replace").splitlines()
    )


def fit_video_bps(max_bytes, duration):
    """The video bitrate that lands a `duration`-second file under max_bytes."""
    return max(
        MIN_FIT_VIDEO_BPS,
        math.floor(max_bytes * FIT_HEADROOM * 8 / max(duration, 1)) - AUDIO_BPS,
    )


# ---------------------------------------------------------------
# subprocess plumbing
# ---------------------------------------------------------------
def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProcessingError(PROCESSING_TIMEOUT, "deadline reached")
    return remaining


def _run(cmd, deadline, *, limit=None):
    timeout = _remaining(deadline)
    if limit is not None:
        timeout = min(timeout, limit)

    try:
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run has already killed the child.
        raise ProcessingError(
            PROCESSING_TIMEOUT,
            f"{os.path.basename(cmd[0])} still running after {timeout:.0f}s",
        )
    except FileNotFoundError as e:
        raise ProcessingError(PROCESSOR_UNAVAILABLE, str(e))


def _stderr_tail(completed):
    return completed.stderr[-STDERR_TAIL_BYTES:].decode("utf-8", "replace")


# ---------------------------------------------------------------
# probing
# ---------------------------------------------------------------
def probe(path, deadline, *, failure_code=SOURCE_UNREADABLE):
    """ffprobe's streams + format for `path`, as a dict."""
    completed = _run(
        [
            settings.FFPROBE_BIN, "-v", "error",
            "-print_format", "json", "-show_streams", "-show_format",
            path,
        ],
        deadline,
        limit=PROBE_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise ProcessingError(failure_code, _stderr_tail(completed))

    try:
        info = json.loads(completed.stdout or b"{}")
    except ValueError:
        raise ProcessingError(failure_code, "ffprobe output is not JSON")

    if not isinstance(info, dict):
        raise ProcessingError(failure_code, "ffprobe output is not an object")
    return info


def _video_stream(info):
    """The first real video stream — cover art (attached_pic) is not one."""
    for stream in info.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic"):
            continue
        return stream
    return None


def _has_audio(info):
    return any(
        stream.get("codec_type") == "audio"
        for stream in info.get("streams") or []
    )


def _duration(info, stream):
    """Seconds as a float, or None when neither the format nor stream says."""
    for value in ((info.get("format") or {}).get("duration"),
                  stream.get("duration")):
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    return None


def _frame_rate(stream):
    for key in ("avg_frame_rate", "r_frame_rate"):
        numerator, _, denominator = str(stream.get(key) or "").partition("/")
        try:
            rate = float(numerator) / float(denominator or 1)
        except (ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            return rate
    return None


# ---------------------------------------------------------------
# filters
# ---------------------------------------------------------------
def _scale(max_side, *, even):
    """
    Longest side ≤ max_side, never upscaled. ffmpeg has already applied the
    rotation by the time this runs (autorotate is on by default and inserts
    itself before -vf), so iw/ih are the DISPLAYED sides. -2 keeps the aspect
    ratio on an even number; the fixed side is truncated to even itself.
    """
    if even:
        long_w = f"trunc(min({max_side},iw)/2)*2"
        long_h = f"trunc(min({max_side},ih)/2)*2"
        keep = "-2"
    else:
        long_w = f"min({max_side},iw)"
        long_h = f"min({max_side},ih)"
        keep = "-1"
    return (
        f"scale=w='if(gte(iw,ih),{long_w},{keep})'"
        f":h='if(gte(iw,ih),{keep},{long_h})'"
    )


def _tonemap(transfer):
    """
    HDR → SDR BT.709: linearise, tone-map with hable, re-encode. Runs AFTER
    the scale, so the float stages work on ≤1280 frames instead of 4K. The
    input properties are spelled out because a resize can leave a frame's
    colour tags unset, and zscale refuses to guess.
    """
    return (
        f"zscale=tin={transfer}:pin=bt2020:min=bt2020nc:t=linear:npl=100,"
        "format=gbrpf32le,"
        "zscale=p=bt709,"
        "tonemap=tonemap=hable:desat=0,"
        "zscale=t=bt709:m=bt709:r=tv"
    )


def _warn_no_zscale():
    global _warned_no_zscale
    if not _warned_no_zscale:
        _warned_no_zscale = True
        logger.warning(
            "media.video | this ffmpeg has no zscale filter: HDR sources are "
            "converted WITHOUT tone-mapping and will look washed out | "
            "ffmpeg=%s", settings.FFMPEG_BIN,
        )


def _video_filters(stream):
    """The -vf chain and the output colour tags for this source stream."""
    filters = []

    rate = _frame_rate(stream)
    if rate and rate > MAX_FPS + 0.5:
        filters.append(f"fps={MAX_FPS}")

    filters.append(_scale(MAX_LONG_SIDE, even=True))

    color_args = []
    transfer = stream.get("color_transfer")
    if transfer in HDR_TRANSFERS:
        # Tagged BT.709 with or without the tone-map, so every player shows
        # the file the same way.
        color_args = [
            "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv",
        ]
        if has_zscale(settings.FFMPEG_BIN):
            filters.append(_tonemap(transfer))
        else:
            _warn_no_zscale()

    filters.append("format=yuv420p")
    return filters, color_args


# ---------------------------------------------------------------
# the passes
# ---------------------------------------------------------------
def _encode(source, output, *, video_index, has_audio, filters, color_args,
            rate_args, max_seconds, deadline):
    cmd = [
        settings.FFMPEG_BIN, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-y",
        "-i", source,
        # The stream by its index rather than 0:v:0, which can be a file's
        # cover art; audio is optional (`?`), so a silent source stays silent.
        "-map", f"0:{video_index}",
    ]
    if has_audio:
        cmd += ["-map", "0:a:0?"]

    cmd += [
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        *rate_args,
        *color_args,
        "-threads", str(ENCODER_THREADS),
    ]

    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", str(AUDIO_BPS), "-ac", "2",
                "-ar", "48000"]
    else:
        cmd += ["-an"]

    cmd += [
        # The cap itself for a clip inside the duration grace (see process).
        # Otherwise cap + grace + 1, which only bounds the work for a source
        # whose duration ffprobe could not read: anything that reaches it is
        # over the cap and refused afterwards.
        "-t", str(max_seconds),
        # Belt and braces for an old ffmpeg: autorotate already dropped the
        # rotation, this makes sure no tag claims otherwise.
        "-metadata:s:v:0", "rotate=0",
        "-movflags", "+faststart",
        output,
    ]

    completed = _run(cmd, deadline)
    if completed.returncode != 0 or not os.path.exists(output):
        # INTERNAL, not SOURCE_UNREADABLE: ffprobe already read this file, so
        # a failed encode is as likely our filter chain as the user's file,
        # and it should reach Sentry either way.
        raise ProcessingError(INTERNAL, "ffmpeg: " + _stderr_tail(completed))


def _measure(path, deadline):
    """Width, height and duration of OUR output, from ffprobe."""
    info = probe(path, deadline, failure_code=INTERNAL)
    stream = _video_stream(info)
    if stream is None:
        raise ProcessingError(INTERNAL, "output has no video stream")

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if not width or not height:
        raise ProcessingError(INTERNAL, "output has no dimensions")

    rotation = 0
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            rotation = side_data.get("rotation") or 0
    tag = (stream.get("tags") or {}).get("rotate")
    if rotation or tag not in (None, "0"):
        # Players would rotate an already-upright picture again. Logged, not
        # failed: width/height are still the real encoded frame.
        logger.error(
            "media.video | output still carries a rotation | rotation=%s | "
            "tag=%s", rotation, tag,
        )

    return width, height, _duration(info, stream) or 0.0


def _poster(video_path, work_dir, duration, deadline):
    """One frame of OUR output, ≤1080 on the long side, stepped to ≤900 KB."""
    at = min(1.0, duration * 0.1)
    poster = os.path.join(work_dir, "poster.jpg")

    for quality in POSTER_QUALITIES:
        completed = _run(
            [
                settings.FFMPEG_BIN, "-hide_banner", "-nostdin",
                "-loglevel", "error", "-y",
                "-ss", f"{at:.3f}", "-i", video_path,
                "-frames:v", "1", "-update", "1",
                "-vf", _scale(POSTER_MAX_LONG_SIDE, even=False),
                "-q:v", str(quality),
                poster,
            ],
            deadline,
        )
        if completed.returncode != 0 or not os.path.exists(poster) \
                or os.path.getsize(poster) == 0:
            raise ProcessingError(
                INTERNAL, "poster: " + _stderr_tail(completed)
            )
        if os.path.getsize(poster) <= POSTER_MAX_BYTES:
            return poster

    raise ProcessingError(
        OUTPUT_TOO_LARGE, f"poster over {POSTER_MAX_BYTES} bytes at q={quality}"
    )


def process(source_path, work_dir, *, max_duration, max_bytes,
            deadline) -> ProcessedMedia:
    """
    Convert one video. Raises ProcessingError for an unusable file, a box
    that cannot convert it, or a deadline passed.

    `max_duration` / `max_bytes` are the purpose's caps; `deadline` is a
    time.monotonic() reading.
    """
    info = probe(source_path, deadline)

    stream = _video_stream(info)
    if stream is None:
        raise ProcessingError(VIDEO_NO_VIDEO_TRACK, "no video stream")

    limit = max_duration + DURATION_GRACE_SECONDS
    duration = _duration(info, stream)
    if duration is not None and duration > limit:
        raise ProcessingError(VIDEO_TOO_LONG, f"{duration:.1f}s > {limit}s")

    # Inside the grace (a 5:00 clip probing at 5:01.4) the OUTPUT is cut to
    # the cap itself: every attach path holds the cap hard — highlights and
    # chat refuse 91 s, posts blank a 301 s duration — so a file made a
    # second over it would convert fine and then be refused.
    trim = duration is not None and duration > max_duration

    filters, color_args = _video_filters(stream)
    output = os.path.join(work_dir, "output.mp4")
    encode = functools.partial(
        _encode,
        source_path, output,
        video_index=stream.get("index", 0),
        has_audio=_has_audio(info),
        filters=filters,
        color_args=color_args,
        max_seconds=max_duration if trim else limit + 1,
        deadline=deadline,
    )

    # The CRF pass, capped at 2.5 Mbps — or lower when even a file that
    # stayed at that cap the whole way could not fit. That is most long clips
    # at the 300s/80 MB caps, and on a half-CPU worker a 5-minute encode is
    # ~10 minutes: aiming under the cap up front saves the second pass that
    # would otherwise follow it, and with it most of the time limit.
    maxrate = MAXRATE_BPS
    if duration is not None:
        maxrate = min(MAXRATE_BPS, fit_video_bps(max_bytes, duration))
    encode(rate_args=[
        "-crf", str(CRF),
        "-maxrate", str(maxrate), "-bufsize", str(2 * maxrate),
    ])

    width, height, out_duration = _measure(output, deadline)
    if out_duration > limit:
        raise ProcessingError(
            VIDEO_TOO_LONG, f"output {out_duration:.1f}s > {limit}s"
        )

    size_bytes = os.path.getsize(output)
    if size_bytes > max_bytes:
        # One retry at an AVERAGE bitrate computed to fit, which rate control
        # holds far more tightly than a CRF ceiling.
        fit = fit_video_bps(max_bytes, out_duration)
        logger.info(
            "media.video | %s bytes over the %s cap, re-encoding at %s bps",
            size_bytes, max_bytes, fit,
        )
        encode(rate_args=[
            "-b:v", str(fit), "-maxrate", str(fit), "-bufsize", str(2 * fit),
        ])
        width, height, out_duration = _measure(output, deadline)
        size_bytes = os.path.getsize(output)
        if size_bytes > max_bytes:
            raise ProcessingError(
                OUTPUT_TOO_LARGE,
                f"{size_bytes} bytes at {fit} bps > {max_bytes}",
            )

    poster = _poster(output, work_dir, out_duration, deadline)

    return ProcessedMedia(
        output_path=output,
        thumbnail_path=poster,
        width=width,
        height=height,
        # Never past the cap: a trimmed file can still measure a trailing
        # audio packet over it, and a source ffprobe could not time is not
        # trimmed at all. Either must not report a duration attach refuses.
        duration=min(int(round(out_duration)), max_duration),
        size_bytes=size_bytes,
    )
