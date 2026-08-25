from django.conf import settings

from core.views.base_views import BaseAPIView
from rest_framework.permissions import IsAuthenticated
from utils.response import response_data
from utils.errors import error_body
from services.storage.factory import get_storage_service
from organization.models import Organization
from organization.services.organization_member_service import OrganizationMemberService

MB = 1024 * 1024

# ---------------------------------------------------------------
# UPLOAD POLICY  (GOATZA_R2_MIGRATION.md §7 — single source of truth)
# ---------------------------------------------------------------
# Presigned PUTs cannot enforce a byte length at the storage layer, so the
# declared size is checked HERE, at config time, and the real size is clamped
# again at attach time (doc §8.4). Together with per-actor server-generated keys
# and a 10-minute expiry, that bounds the abuse surface.
#
# Three kinds exist. "image"/"video" are primaries — the thing the user picked.
# "thumb" is the client-generated companion object that replaces what Cloudinary
# used to synthesise at delivery time: a 640px WebP for images (doc §4 G3) and a
# captured poster frame for videos (doc §4 G2). A thumb is never uploaded alone.
IMAGE_CONTENT_TYPES = {"image/webp", "image/jpeg", "image/png"}
VIDEO_CONTENT_TYPES = {"video/mp4", "video/webm"}

IMAGE_RULE = {"content_types": IMAGE_CONTENT_TYPES, "max_bytes": 5 * MB}
VIDEO_RULE = {"content_types": VIDEO_CONTENT_TYPES, "max_bytes": 80 * MB}
THUMB_RULE = {"content_types": IMAGE_CONTENT_TYPES, "max_bytes": 1 * MB}

IMAGE_ONLY = {"image": IMAGE_RULE}
MEDIA_KINDS = {"image": IMAGE_RULE, "video": VIDEO_RULE, "thumb": THUMB_RULE}

# `max_primaries` counts images/videos only; `max_files` includes their thumbs,
# which is why the pair-capable types allow twice as many entries.
POLICY = {
    # Fixed-key slots: a second entry would sign the identical key twice.
    "profile": {"kinds": IMAGE_ONLY, "max_primaries": 1, "max_files": 1},
    "cover": {"kinds": IMAGE_ONLY, "max_primaries": 1, "max_files": 1},
    "organization_logo": {"kinds": IMAGE_ONLY, "max_primaries": 1, "max_files": 1},
    "organization_cover": {"kinds": IMAGE_ONLY, "max_primaries": 1, "max_files": 1},

    # Single images, already small enough that a thumb variant buys nothing.
    "achievements": {"kinds": IMAGE_ONLY, "max_primaries": 10, "max_files": 10},
    "matches": {"kinds": IMAGE_ONLY, "max_primaries": 10, "max_files": 10},

    # ≤10 images per post (unchanged), or exactly one video. Highlights ride on
    # this type too — see the note in STAGE1_NOTES.md.
    "posts": {"kinds": MEDIA_KINDS, "max_primaries": 10, "max_files": 20},
    "recruitments": {"kinds": MEDIA_KINDS, "max_primaries": 10, "max_files": 20},

    # One attachment per message, plus its thumb.
    "chat": {"kinds": MEDIA_KINDS, "max_primaries": 1, "max_files": 2},
}

PRIMARY_KINDS = {"image", "video"}


class UploadPolicyError(ValueError):
    """A policy rejection that knows which request field to blame."""

    def __init__(self, message, field="files"):
        super().__init__(message)
        self.field = field


class GetUploadConfigAPIView(BaseAPIView):
    """
    Uses request.actor

    Headers:
    X-Actor-Type: user | organization
    X-Actor-Id: <org_id>   (required when organization)

    POST is the live contract (doc §5.1): the client encodes/compresses FIRST,
    then declares exactly what it is about to upload, so the backend can bind
    the real Content-Type into each presigned PUT and check the declared size
    against the policy table above.

    GET is the legacy Cloudinary contract, alive only while
    FILE_STORAGE_PROVIDER=cloudinary is a rollback option.
    """

    ALLOWED_TYPES = {
        "profile",
        "cover",
        "posts",
        "organization_logo",
        "organization_cover",
        "recruitments",
        # Chat media — works for both user and org actors (no actor-type guard
        # below), scoped server-side to chat/<actor path>.
        "chat",
        # Achievement proof/showcase image. User-only (guarded below) and scoped
        # to users/<id>/achievements — an achievement belongs to a person, so an
        # org actor has nothing to upload here.
        "achievements",
        # Match diary photo. Same shape and same reasoning as achievements:
        # user-only, scoped to users/<id>/matches. A match entry belongs to the
        # player who played it, and there is no org-side match diary to upload
        # for.
        "matches",
    }

    USER_ONLY_TYPES = {
        "profile",
        "cover",
        "achievements",
        "matches",
    }

    ORG_ONLY_TYPES = {
        "organization_logo",
        "organization_cover",
        "recruitments",
    }

    # -----------------------------------------
    # POST — upload config v2 (R2 presigned PUTs)
    # -----------------------------------------
    def post(self, request):
        try:
            if getattr(settings, "FILE_STORAGE_PROVIDER", "r2") != "r2":
                msg = "upload config v2 requires the r2 provider"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=400,
                    data=error_body(msg, "provider")
                )

            upload_type = request.data.get("type")
            org_id = request.data.get("org_id")
            files = request.data.get("files")

            if upload_type not in self.ALLOWED_TYPES:
                msg = "Invalid upload type"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=400,
                    data=error_body(msg, "type")
                )

            files = self._validate_files(upload_type, files)

            actor = self.actor
            resolved = self._resolve_org_actor(actor, request.user, org_id)
            if resolved is not None:
                return resolved

            guard = self._guard_actor_type(actor, upload_type)
            if guard is not None:
                return guard

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

        except UploadPolicyError as pe:
            msg = str(pe)
            return response_data(
                success=False,
                message=msg,
                error=msg,
                status_code=400,
                data=error_body(msg, pe.field)
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

    # -----------------------------------------
    # policy enforcement
    # -----------------------------------------
    def _validate_files(self, upload_type: str, files) -> list:
        """
        Check the declared file list against POLICY and return it normalised.

        Raises UploadPolicyError on every rejection so the caller can answer with
        the right field. The returned entries are the ONLY thing the storage
        service sees — it derives each key's extension from `content_type`, so
        nothing here may be passed through unvalidated.
        """
        policy = POLICY.get(upload_type)

        if policy is None:
            # A type in ALLOWED_TYPES with no POLICY row is a wiring bug, not a
            # client error — but refusing is still safer than signing unpoliced.
            raise UploadPolicyError("Invalid upload type", "type")

        if not isinstance(files, list) or not files:
            raise UploadPolicyError("At least one file is required")

        if len(files) > policy["max_files"]:
            raise UploadPolicyError(
                f"Too many files for this upload type "
                f"(max {policy['max_files']})"
            )

        cleaned = []

        for entry in files:
            if not isinstance(entry, dict):
                raise UploadPolicyError("Invalid file entry")

            kind = entry.get("kind")
            content_type = entry.get("content_type")
            size_bytes = entry.get("size_bytes")

            rule = policy["kinds"].get(kind)
            if rule is None:
                raise UploadPolicyError(
                    f"Unsupported upload kind for this type: {kind}"
                )

            if content_type not in rule["content_types"]:
                raise UploadPolicyError(
                    f"Unsupported file type: {content_type}"
                )

            # bool is an int subclass — exclude it explicitly so `True` can't
            # sail through as a 1-byte file.
            if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) \
                    or size_bytes < 1:
                raise UploadPolicyError("Invalid file size")

            if size_bytes > rule["max_bytes"]:
                raise UploadPolicyError(
                    f"File too large (max {rule['max_bytes'] // MB} MB "
                    f"for a {kind})"
                )

            cleaned.append({
                "kind": kind,
                "content_type": content_type,
                "size_bytes": size_bytes,
            })

        self._validate_pairing(policy, cleaned)

        return cleaned

    def _validate_pairing(self, policy: dict, files: list):
        """
        Thumbs exist only as companions (doc §4 G2/G3), and a video is always
        requested together with its poster so both objects land in the same
        folder in one round trip.
        """
        videos = [f for f in files if f["kind"] == "video"]
        primaries = [f for f in files if f["kind"] in PRIMARY_KINDS]
        thumbs = [f for f in files if f["kind"] == "thumb"]

        if not primaries:
            raise UploadPolicyError(
                "A thumbnail must accompany the image or video it belongs to"
            )

        if videos:
            # One video per post/message, and nothing else riding along with it.
            if len(videos) != 1 or len(primaries) != 1 or len(thumbs) != 1:
                raise UploadPolicyError(
                    "A video upload must be requested on its own, "
                    "with exactly one thumbnail"
                )
            return

        if len(primaries) > policy["max_primaries"]:
            raise UploadPolicyError(
                f"Too many files for this upload type "
                f"(max {policy['max_primaries']})"
            )

        if len(thumbs) > len(primaries):
            raise UploadPolicyError(
                "A thumbnail must accompany the image or video it belongs to"
            )

    # -----------------------------------------
    # shared guards (identical for GET and POST)
    # -----------------------------------------
    def _resolve_org_actor(self, actor, user, org_id):
        """
        Act as an organization named in the body/query rather than the actor
        headers. Returns an error Response, or None when there is nothing to do.
        """
        if not org_id:
            return None

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

        return None

    def _guard_actor_type(self, actor, upload_type):
        """Prevent wrong actor usage. Returns an error Response, or None."""
        if upload_type in self.USER_ONLY_TYPES and not actor.is_user:
            msg = "Switch to your personal account for this upload"
            return response_data(
                success=False,
                message=msg,
                error=msg,
                status_code=403,
                data=error_body(msg, "type")
            )

        if upload_type in self.ORG_ONLY_TYPES and not actor.is_org:
            msg = "Switch to your organization account for this upload"
            return response_data(
                success=False,
                message=msg,
                error=msg,
                status_code=403,
                data=error_body(msg, "type")
            )

        return None

    # -----------------------------------------
    # GET — legacy Cloudinary contract
    # -----------------------------------------
    # TODO(stage-6): remove. Only reachable while FILE_STORAGE_PROVIDER is
    # flipped back to "cloudinary" as a rollback; R2Service takes a `files`
    # list, not a `count`, so this contract cannot serve it.
    def get(self, request):
        try:
            if getattr(settings, "FILE_STORAGE_PROVIDER", "r2") != "cloudinary":
                msg = "upload config v1 requires the cloudinary provider"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=400,
                    data=error_body(msg, "provider")
                )

            upload_type = request.query_params.get("type")
            org_id = request.query_params.get("org_id")

            try:
                count = int(request.query_params.get("count", 1))
            except (TypeError, ValueError):
                count = 0

            if upload_type not in self.ALLOWED_TYPES:
                msg = "Invalid upload type"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=400,
                    data=error_body(msg, "type")
                )

            if count < 1 or count > 10:
                msg = "Invalid count (1-10 allowed)"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=400,
                    data=error_body(msg, "count")
                )

            actor = self.actor
            user = request.user
            if org_id: # for user want to access org directly
                resolved = self._resolve_org_actor(actor, user, org_id)
                if resolved is not None:
                    return resolved

            # -----------------------------------
            # Prevent wrong actor usage
            # -----------------------------------
            guard = self._guard_actor_type(actor, upload_type)
            if guard is not None:
                return guard

            storage = get_storage_service()

            config = storage.get_upload_config(
                actor=actor,
                upload_type=upload_type,
                count=count
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
