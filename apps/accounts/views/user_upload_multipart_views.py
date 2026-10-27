"""
Multipart uploads for large videos.

A video over the client's threshold (16 MB) no longer goes up as ONE PUT. On a
phone a single 80 MB request is one long bet against the network: a
Wi-Fi→cellular handoff drops the TCP connection, and iOS suspends the page —
killing the request with it — the moment the screen locks or the user switches
apps. Every one of those used to throw the whole file away, client-side
encoding included.

In parts, the same failure costs one part. The client sends the parts one
after another, retries a part that failed, and after an app switch resumes
from the part that died instead of starting over.

THE FLOW
    POST upload/multipart/create    → upload_id + one presigned URL per part
    PUT  <part url> × n             → straight to R2, like a presigned PUT
    POST upload/multipart/complete  → R2 stitches the parts into the object
    POST upload/multipart/abort     → on a final failure or a cancel

THE KEY COMES FROM THE SIGNATURE ENDPOINT. The client asks for its batch as it
always has (video + poster) and hands the video's key back here, so the video
lands in the folder its poster is in and ``validate_thumbnail``'s same-folder
rule holds exactly as it does for a single PUT. It is also the one input here a
client could otherwise invent, which is why it is checked against the folder
scheme for this actor and this type, not just a prefix.

PART SIZE IS OURS. R2 requires every part except the last to be the SAME size,
and at least 5 MiB, so the server picks it and the client slices to exactly
what it is told.

Who may upload what is the signature endpoint's ``POLICY`` and
``resolve_upload_actor``, imported rather than restated.
"""

import logging
import math
import time

from core.views.base_views import BaseAPIView
from utils.response import response_data
from services.storage.factory import get_storage_service
from services.storage.paths import actor_key_prefixes, is_upload_key_for
from services.storage.r2 import (
    CONTENT_TYPE_EXTENSIONS,
    UPLOAD_URL_EXPIRES_IN,
    public_url_for,
)
from apps.accounts.views.user_upload_signature_views import (
    MB,
    POLICY,
    VIDEO_CONTENT_TYPES,
    resolve_upload_actor,
    upload_error,
)

logger = logging.getLogger(__name__)

# R2: every part but the last the SAME size, and at least 5 MiB. 8 MB keeps an
# 80 MB video to ten parts — few enough that per-request overhead is noise,
# small enough that a part lost to a dropped connection is a short re-send.
MULTIPART_PART_SIZE = 8 * MB

# A ceiling on the URLs one request can mint, independent of POLICY. The
# largest video any type allows today is ten parts, so this only matters if a
# cap is ever raised without anybody looking here.
MULTIPART_MAX_PARTS = 50

# Bounds on the opaque strings passed through to R2. Nothing legitimate comes
# near them; they stop a request body turning into an arbitrarily large call.
MAX_UPLOAD_ID_LENGTH = 1024
MAX_ETAG_LENGTH = 256


def _valid_upload_id(upload_id) -> bool:
    return (
        isinstance(upload_id, str)
        and bool(upload_id.strip())
        and len(upload_id) <= MAX_UPLOAD_ID_LENGTH
    )


def _owns_key(actor, key) -> bool:
    """
    Complete and abort name no upload type, so this is the actor-level half of
    the create check: the key sits under one of the caller's own prefixes. The
    upload id already binds the call to an upload the caller started; this is
    what stops a leaked one being driven from someone else's session.
    """
    return (
        isinstance(key, str)
        and key.startswith(actor_key_prefixes(actor))
        and ".." not in key.split("/")
    )


def _ordered_parts(parts):
    """
    The client's part list, checked and sorted ascending — or (None, message).

    Parts may arrive in any order; once sorted they must run 1..n with no gap
    and no repeat. R2 would refuse a gap too, but with an error nobody could
    act on.
    """
    if not isinstance(parts, list) or not parts:
        return None, "Provide the uploaded parts"

    if len(parts) > MULTIPART_MAX_PARTS:
        return None, "Too many parts"

    ordered = []
    for part in parts:
        if not isinstance(part, dict):
            return None, "Invalid part"

        number = part.get("part_number")
        etag = part.get("etag")

        # bool is an int subclass — True would otherwise pass as part 1.
        if isinstance(number, bool) or not isinstance(number, int):
            return None, "Invalid part number"

        # Passed through exactly as the browser read it, quotes included;
        # stripped only to tell an empty one apart.
        if not isinstance(etag, str) or not etag.strip() \
                or len(etag) > MAX_ETAG_LENGTH:
            return None, "Every part needs its ETag"

        ordered.append({"part_number": number, "etag": etag})

    ordered.sort(key=lambda part: part["part_number"])

    if [part["part_number"] for part in ordered] != \
            list(range(1, len(ordered) + 1)):
        return None, "Parts must be numbered 1 to n with none missing"

    return ordered, None


class CreateMultipartUploadAPIView(BaseAPIView):
    """
    POST → start, or re-sign, a multipart upload for one video.

    Body:
    {
        "type": "posts",
        "org_id": "<uuid>",          # only when acting for an org type
        "key": "<the video key the signature endpoint issued>",
        "content_type": "video/mp4",
        "size_bytes": 41943040,
        "upload_id": "<optional>"    # fresh part URLs for THIS upload
    }

    Responds:
    {
        "upload_id", "key", "public_url", "part_size",
        "expires_in", "expires_at",
        "parts": [{"part_number": 1, "upload_url": "…"}, …]
    }

    `upload_id` is for a client whose part URLs expired, or were refused,
    part-way through: it gets new URLs for the SAME upload and carries on from
    the first part it has not sent. Starting a NEW upload could not resume
    anything — parts belong to the upload they were sent to, so completing a
    new one would mean sending every part again.
    """

    def post(self, request):
        key = None
        try:
            body = request.data if isinstance(request.data, dict) else {}

            upload_type = body.get("type")
            org_id = body.get("org_id")
            key = body.get("key")
            content_type = body.get("content_type")
            size_bytes = body.get("size_bytes")
            upload_id = body.get("upload_id")

            # Only a type that can carry a video can be uploaded in parts.
            if not isinstance(upload_type, str) or upload_type not in POLICY \
                    or POLICY[upload_type]["video"]["max"] < 1:
                return upload_error("Invalid upload type", "type")

            video = POLICY[upload_type]["video"]

            if not isinstance(content_type, str) \
                    or content_type not in VIDEO_CONTENT_TYPES:
                return upload_error("Invalid file type", "content_type")

            # bool is an int subclass — True would otherwise sail through.
            if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) \
                    or size_bytes <= 0:
                return upload_error("Invalid file size", "size_bytes")

            if size_bytes > video["max_bytes"]:
                return upload_error(
                    f"Video is larger than {video['max_bytes'] // MB}MB",
                    "size_bytes"
                )

            if upload_id is not None and not _valid_upload_id(upload_id):
                return upload_error("Invalid upload_id", "upload_id")

            actor, refusal = resolve_upload_actor(
                request, self.actor, upload_type, org_id
            )
            if refusal:
                return upload_error(*refusal)

            # A key the signature endpoint could have issued to THIS actor for
            # THIS type, with the extension THIS content type is stored under.
            if not is_upload_key_for(actor, upload_type, key) \
                    or not key.endswith(CONTENT_TYPE_EXTENSIONS[content_type]):
                return upload_error("Invalid key", "key")

            part_count = math.ceil(size_bytes / MULTIPART_PART_SIZE)
            if part_count > MULTIPART_MAX_PARTS:
                return upload_error("Too many parts", "size_bytes")

            storage = get_storage_service()

            # Read BEFORE signing, so it can only ever be early (see r2.py).
            expires_at = int(time.time()) + UPLOAD_URL_EXPIRES_IN

            if upload_id is None:
                upload_id = storage.create_multipart_upload(key, content_type)

            parts = [
                {
                    "part_number": number,
                    "upload_url": storage.presign_upload_part(
                        key, upload_id, number
                    ),
                }
                for number in range(1, part_count + 1)
            ]

            return response_data(
                success=True,
                data={
                    "upload_id": upload_id,
                    "key": key,
                    "public_url": public_url_for(key),
                    "part_size": MULTIPART_PART_SIZE,
                    "expires_in": UPLOAD_URL_EXPIRES_IN,
                    "expires_at": expires_at,
                    "parts": parts,
                }
            )

        except ValueError as ve:
            return upload_error(str(ve) or "Invalid upload request")

        except Exception as e:
            # The key only — a part URL carries its signature in the query
            # string and is never logged.
            logger.error(
                f"multipart create failed | key={key} | {str(e)}"
            )
            return response_data(
                success=False,
                message="Failed to start the upload",
                status_code=500,
                error=str(e)
            )


class CompleteMultipartUploadAPIView(BaseAPIView):
    """
    POST → stitch the uploaded parts into the final object.

    Body:     {"key", "upload_id", "parts": [{"part_number", "etag"}],
               "org_id"?}
    Responds: {"key", "public_url"}

    `org_id` only when create was called with one — the same actor resolution
    applies to all three endpoints.
    """

    def post(self, request):
        key = None
        try:
            body = request.data if isinstance(request.data, dict) else {}

            key = body.get("key")
            upload_id = body.get("upload_id")
            org_id = body.get("org_id")

            if not _valid_upload_id(upload_id):
                return upload_error("Invalid upload_id", "upload_id")

            parts, error = _ordered_parts(body.get("parts"))
            if error:
                return upload_error(error, "parts")

            actor, refusal = resolve_upload_actor(
                request, self.actor, None, org_id
            )
            if refusal:
                return upload_error(*refusal)

            if not _owns_key(actor, key):
                return upload_error("Invalid key", "key")

            storage = get_storage_service()
            storage.complete_multipart_upload(key, upload_id, parts)

            return response_data(
                success=True,
                data={"key": key, "public_url": public_url_for(key)}
            )

        except ValueError as ve:
            return upload_error(str(ve) or "Invalid upload request")

        except Exception as e:
            logger.error(
                f"multipart complete failed | key={key} | {str(e)}"
            )
            return response_data(
                success=False,
                message="Failed to complete the upload",
                status_code=500,
                error=str(e)
            )


class AbortMultipartUploadAPIView(BaseAPIView):
    """
    POST → abandon an upload and drop its parts. Best-effort.

    Body: {"key", "upload_id", "org_id"?}

    Called when an upload finally fails or the author cancels. It answers 200
    once the request is valid whatever R2 says: the client has already moved
    on, and an upload nobody manages to abort is cleared by the bucket's
    multipart abort rule within days anyway.
    """

    def post(self, request):
        key = None
        try:
            body = request.data if isinstance(request.data, dict) else {}

            key = body.get("key")
            upload_id = body.get("upload_id")
            org_id = body.get("org_id")

            if not _valid_upload_id(upload_id):
                return upload_error("Invalid upload_id", "upload_id")

            actor, refusal = resolve_upload_actor(
                request, self.actor, None, org_id
            )
            if refusal:
                return upload_error(*refusal)

            if not _owns_key(actor, key):
                return upload_error("Invalid key", "key")

            storage = get_storage_service()
            storage.abort_multipart_upload(key, upload_id)

            return response_data(success=True, data={"key": key})

        except ValueError as ve:
            return upload_error(str(ve) or "Invalid upload request")

        except Exception as e:
            logger.error(
                f"multipart abort failed | key={key} | {str(e)}"
            )
            return response_data(
                success=False,
                message="Failed to abort the upload",
                status_code=500,
                error=str(e)
            )
