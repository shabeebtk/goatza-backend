from core.views.base_views import BaseAPIView
from rest_framework.permissions import IsAuthenticated
from utils.response import response_data
from utils.errors import error_body
from services.storage.factory import get_storage_service
from organization.models import Organization
from organization.services.organization_member_service import OrganizationMemberService


# ---------------------------------------------------------------
# UPLOAD POLICY  (POST / R2 path only)
# ---------------------------------------------------------------
# What the client is allowed to ask for, per upload type, as data. The POST
# handler is a generic walk over this dict — a new media type is an entry here
# and a folder in services/storage/paths.py, and nothing else.
#
# This is enforced server-side because it decides what gets SIGNED: a presigned
# PUT is bound to one key and one content type, so anything this dict lets
# through is something the browser can then push straight into the bucket
# without the app server ever seeing the bytes. The client mirrors these limits
# for UX only.

MB = 1024 * 1024

# Everything is normalised client-side before upload, so the accepted set is
# narrow on purpose. No image/gif, no video/quicktime: the client emits webp/
# jpeg/png and mp4/webm, and storage keeps exactly what it is handed — nothing
# transcodes on the way in or out.
IMAGE_CONTENT_TYPES = {"image/webp", "image/jpeg", "image/png"}
VIDEO_CONTENT_TYPES = {"video/mp4", "video/webm"}

# A thumb is a poster frame / preview — an image, and a small one. Nothing
# derives one server-side; the client captures it while encoding the video.
THUMB_MAX_BYTES = 1 * MB

# Belt-and-braces ceiling on the request itself, independent of type: 10 images
# + 10 thumbs is the largest legitimate batch (posts).
MAX_FILES_PER_REQUEST = 20

POLICY = {
    # ---- single fixed-slot images (replace themselves on re-upload) ----
    "profile": {
        "images": {"min": 1, "max": 1, "max_bytes": 5 * MB},
        "video": {"min": 0, "max": 0, "max_bytes": 0},
        "exclusive": False,
        "image_thumbs": False,
    },
    "cover": {
        "images": {"min": 1, "max": 1, "max_bytes": 5 * MB},
        "video": {"min": 0, "max": 0, "max_bytes": 0},
        "exclusive": False,
        "image_thumbs": False,
    },
    "organization_logo": {
        "images": {"min": 1, "max": 1, "max_bytes": 5 * MB},
        "video": {"min": 0, "max": 0, "max_bytes": 0},
        "exclusive": False,
        "image_thumbs": False,
    },
    "organization_cover": {
        "images": {"min": 1, "max": 1, "max_bytes": 5 * MB},
        "video": {"min": 0, "max": 0, "max_bytes": 0},
        "exclusive": False,
        "image_thumbs": False,
    },

    # ---- single per-row images ----
    "achievements": {
        "images": {"min": 1, "max": 1, "max_bytes": 5 * MB},
        "video": {"min": 0, "max": 0, "max_bytes": 0},
        "exclusive": False,
        "image_thumbs": False,
    },
    "matches": {
        "images": {"min": 1, "max": 1, "max_bytes": 5 * MB},
        "video": {"min": 0, "max": 0, "max_bytes": 0},
        "exclusive": False,
        "image_thumbs": False,
    },

    # ---- batches ----
    # A post is a gallery of up to 10 images OR one video — never both, which is
    # what `exclusive` says. Image thumbs are optional here (a heavy image can
    # ship a cheap preview for the feed).
    "posts": {
        "images": {"min": 0, "max": 10, "max_bytes": 5 * MB},
        "video": {"min": 0, "max": 1, "max_bytes": 80 * MB},
        "exclusive": True,
        "image_thumbs": True,
    },
    # A recruitment post can carry a gallery AND a video (a pitch clip next to
    # facility photos), so no `exclusive`.
    "recruitments": {
        "images": {"min": 0, "max": 10, "max_bytes": 5 * MB},
        "video": {"min": 0, "max": 1, "max_bytes": 80 * MB},
        "exclusive": False,
        "image_thumbs": False,
    },

    # ---- video-only ----
    # Tighter than a post video: a highlight is a <=90s clip (HighlightService.
    # MAX_DURATION_SECONDS), so 40 MB is generous for a client-encoded one.
    "highlights": {
        "images": {"min": 0, "max": 0, "max_bytes": 0},
        "video": {"min": 1, "max": 1, "max_bytes": 40 * MB},
        "exclusive": False,
        "image_thumbs": False,
    },

    # ---- chat ----
    # One attachment per message, image or video. A video's thumb is mandatory
    # (the bubble renders the poster before playback); an image's is optional.
    "chat": {
        "images": {"min": 0, "max": 1, "max_bytes": 5 * MB},
        "video": {"min": 0, "max": 1, "max_bytes": 80 * MB},
        "exclusive": True,
        "image_thumbs": True,
    },

    # ---- support ----
    # Screenshots on a "report a problem" submission. No thumbs: a bug
    # screenshot is opened once, by a human, in the Django admin — it never
    # renders in a feed, so there is nothing for a preview to make faster, and
    # a second signed PUT per file would buy nothing.
    #
    # Three is a ceiling on EVIDENCE, not a gallery limit. Past three shots of
    # the same broken screen the extra ones stop adding anything a fix depends
    # on.
    #
    # Deliberately absent from USER_ONLY_TYPES and ORG_ONLY_TYPES below: either
    # actor can hit a bug, and a club that cannot attach a screenshot files a
    # worse report.
    "support": {
        "images": {"min": 1, "max": 3, "max_bytes": 5 * MB},
        "video": {"min": 0, "max": 0, "max_bytes": 0},
        "exclusive": False,
        "image_thumbs": False,
    },
}

# Actor restrictions, shared with the GET handler's guards below. An achievement
# or a match belongs to a person; a recruitment belongs to an org.
USER_ONLY_TYPES = {"profile", "cover", "achievements", "matches", "highlights"}
ORG_ONLY_TYPES = {"organization_logo", "organization_cover", "recruitments"}


class GetUploadConfigAPIView(BaseAPIView):
    """
    Uses request.actor

    Headers:
    X-Actor-Type: user | organization
    X-Actor-Id: <org_id>   (required when organization)

    POST → presigned PUTs, one per declared file
    """

    def post(self, request):
        """
        Body:
        {
            "type": "posts",
            "org_id": "<uuid>",              # only when acting for an org type
            "files": [
                {"content_type": "image/webp", "size_bytes": 812345, "kind": "image"}
            ]
        }

        Responds with one presigned PUT per file, IN REQUEST ORDER — the client
        pairs uploads back to its picked files (and a video to its thumb) by
        position.
        """
        try:
            body = request.data if isinstance(request.data, dict) else {}

            upload_type = body.get("type")
            org_id = body.get("org_id")
            files = body.get("files")

            if upload_type not in POLICY:
                msg = "Invalid upload type"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=400,
                    data=error_body(msg, "type")
                )

            # -----------------------------------
            # File policy (counts, kinds, sizes, pairing)
            # -----------------------------------
            error = self._validate_files(POLICY[upload_type], files)
            if error:
                msg, field = error
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=400,
                    data=error_body(msg, field)
                )

            actor = self.actor
            user = request.user
            if org_id: # for user want to access org directly
                try:
                    org = Organization.objects.select_related("profile").get(id=org_id)
                    if not OrganizationMemberService.is_organization_member(org, user):
                        msg = "You are not a member of this organization"
                        return response_data(
                            success=False,
                            message=msg,
                            error=msg,
                            status_code=400,
                            data=error_body(msg, "org_id")
                        )

                    actor.organization = org
                    actor.actor_type = "organization"

                except Organization.DoesNotExist:
                    msg = "Organization not found"
                    return response_data(
                        success=False,
                        message=msg,
                        error=msg,
                        status_code=404,
                        data=error_body(msg, "org_id")
                    )

            # -----------------------------------
            # Prevent wrong actor usage
            # -----------------------------------
            if upload_type in USER_ONLY_TYPES and not actor.is_user:
                msg = "Switch to your personal account for this upload"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=403,
                    data=error_body(msg, "type")
                )

            if upload_type in ORG_ONLY_TYPES and not actor.is_org:
                msg = "Switch to your organization account for this upload"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=403,
                    data=error_body(msg, "type")
                )

            storage = get_storage_service()

            config = storage.get_upload_config(
                actor=actor,
                upload_type=upload_type,
                files=files
            )

            return response_data(
                success=True,
                data=config
            )

        except ValueError as ve:
            msg = str(ve) or "Invalid upload request"
            return response_data(
                success=False,
                message=msg,
                error=msg,
                status_code=400,
                data=error_body(msg)
            )

        except Exception as e:
            return response_data(
                success=False,
                message="Failed to generate upload config",
                status_code=500,
                error=str(e)
            )

    # -----------------------------------------------------------
    # policy enforcement
    # -----------------------------------------------------------
    def _validate_files(self, policy, files):
        """
        Walk `files` against one POLICY entry.

        Returns None when the request is legal, or (message, field) for the
        first violation — the caller turns that into the standard error body.
        """
        if not isinstance(files, list) or not files:
            return ("Provide at least one file", "files")

        if len(files) > MAX_FILES_PER_REQUEST:
            return (
                f"Too many files (max {MAX_FILES_PER_REQUEST} per request)",
                "files"
            )

        images = policy["images"]
        video = policy["video"]

        image_count = 0
        video_count = 0
        thumb_count = 0

        for file in files:
            if not isinstance(file, dict):
                return ("Invalid file entry", "files")

            kind = file.get("kind")
            content_type = file.get("content_type")
            size_bytes = file.get("size_bytes")

            if kind not in {"image", "video", "thumb"}:
                return ("Invalid file kind", "kind")

            # bool is an int subclass — True would otherwise sail through.
            if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) \
                    or size_bytes <= 0:
                return ("Invalid file size", "size_bytes")

            if kind == "video":
                if content_type not in VIDEO_CONTENT_TYPES:
                    return ("Invalid file type", "content_type")
                # Before the size check: on an image-only type max_bytes is 0,
                # and "larger than 0MB" tells the client nothing.
                if video["max"] == 0:
                    return (
                        "Videos are not allowed for this upload type",
                        "files"
                    )
                if size_bytes > video["max_bytes"]:
                    return (
                        f"Video is larger than "
                        f"{video['max_bytes'] // MB}MB",
                        "size_bytes"
                    )
                video_count += 1

            else:  # image | thumb
                if content_type not in IMAGE_CONTENT_TYPES:
                    return ("Invalid file type", "content_type")

                if kind == "thumb":
                    if size_bytes > THUMB_MAX_BYTES:
                        return (
                            f"Thumbnail is larger than "
                            f"{THUMB_MAX_BYTES // MB}MB",
                            "size_bytes"
                        )
                    thumb_count += 1
                else:
                    # A thumb is still allowed on a video-only type (highlights)
                    # — this only rejects a standalone image there.
                    if images["max"] == 0:
                        return (
                            "Images are not allowed for this upload type",
                            "files"
                        )
                    if size_bytes > images["max_bytes"]:
                        return (
                            f"Image is larger than "
                            f"{images['max_bytes'] // MB}MB",
                            "size_bytes"
                        )
                    image_count += 1

        # -----------------------------------
        # counts
        # -----------------------------------
        if policy["exclusive"] and image_count and video_count:
            return ("Send images or a video, not both", "files")

        if image_count > images["max"]:
            # The multi-image cap is 10, so this reproduces the GET handler's
            # "Invalid count (1-10 allowed)" verbatim for posts/recruitments.
            if images["max"] == 1:
                return ("Only one image per upload", "files")
            return (f"Invalid count (1-{images['max']} allowed)", "files")

        if video_count > video["max"]:
            return ("Only one video per upload", "files")

        if not image_count and not video_count:
            return ("Provide at least one file", "files")

        if image_count < images["min"]:
            return ("Exactly one image is required", "files")

        if video_count < video["min"]:
            return ("Exactly one video is required", "files")

        # -----------------------------------
        # pairing
        # -----------------------------------
        # A video always ships its poster frame — nothing generates one for us
        # any more. A thumb with no parent in the same request is a stray:
        # it would be signed into the folder and then never referenced.
        if video_count:
            if thumb_count != 1:
                return (
                    "A video upload requires exactly one thumbnail",
                    "files"
                )
        elif thumb_count:
            if not policy["image_thumbs"] or thumb_count > image_count:
                return (
                    "A thumbnail must accompany the file it belongs to",
                    "files"
                )

        return None
