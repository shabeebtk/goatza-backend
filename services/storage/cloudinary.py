import time, uuid, logging
import cloudinary
import cloudinary.utils
import cloudinary.api
# Explicit: `import cloudinary` does NOT pull in the uploader submodule, so
# `cloudinary.uploader.*` only resolves by accident when some other import
# (django-cloudinary-storage) happens to have loaded it first.
import cloudinary.uploader
from django.conf import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------
# VIDEO DELIVERY
# ---------------------------------------------------------------
# MUST stay byte-identical (including parameter order) with
# VIDEO_DELIVERY_TRANSFORM in the frontend (src/shared/services/cloudinaryDelivery.ts).
# A mismatch silently re-introduces first-view cold-start transcoding.
VIDEO_EAGER_TRANSFORMATION = "c_limit,h_1280,w_1280,q_auto:good,vc_h264"
VIDEO_EAGER_FORMAT = "mp4"

# HLS adaptive streaming. sp_hd generates a multi-rendition ladder + .m3u8 manifest.
# Adaptive streaming MUST be eager — Cloudinary cannot build it on request.
# NOTE: streaming-profile generation costs meaningfully more transformation credits
# than the single mp4 derivative — monitor usage after rollout.
VIDEO_HLS_TRANSFORMATION = "sp_hd"
VIDEO_HLS_FORMAT = "m3u8"


class CloudinaryService:

    def get_upload_config(
        self,
        actor,
        upload_type: str,
        count: int = 1
    ):
        timestamp = int(time.time())

        upload_url = (
            f"https://api.cloudinary.com/v1_1/"
            f"{settings.CLOUDINARY_CLOUD_NAME}/auto/upload"
        )

        uploads = []

        # -----------------------------------------
        # USER PROFILE / COVER
        # -----------------------------------------
        if upload_type in {"profile", "cover"}:
            user = actor.user

            folder = f"users/{user.id}/{upload_type}"
            public_id = upload_type

            uploads.append(
                self._build_signed_upload(
                    upload_url=upload_url,
                    timestamp=timestamp,
                    folder=folder,
                    public_id=public_id,
                    overwrite="true"
                )
            )

            return {
                "provider": "cloudinary",
                "uploads": uploads
            }

        # -----------------------------------------
        # USER POSTS
        # -----------------------------------------
        elif upload_type == "posts":
            temp_post_id = str(uuid.uuid4())

            if actor.is_user:
                user = actor.user
                folder = f"users/{user.id}/posts/{temp_post_id}"

            elif actor.is_org:
                org = actor.organization
                folder = f"organizations/{org.id}/posts/{temp_post_id}"

        
            for _ in range(count):
                uploads.append(
                    self._build_signed_upload(
                        upload_url=upload_url,
                        timestamp=timestamp,
                        folder=folder,
                        public_id=str(uuid.uuid4()),
                        overwrite="false"
                    )
                )

            return {
                "provider": "cloudinary",
                "temp_post_id": temp_post_id,
                "uploads": uploads
            }

        # -----------------------------------------
        # RECRUITMENTS  (org only)
        # -----------------------------------------
        elif upload_type == "recruitments":
            org = actor.organization
            temp_id = str(uuid.uuid4())
            folder = f"organizations/{org.id}/recruitments/{temp_id}"

            for _ in range(count):
                uploads.append(
                    self._build_signed_upload(
                        upload_url=upload_url,
                        timestamp=timestamp,
                        folder=folder,
                        public_id=str(uuid.uuid4()),
                        overwrite="false"
                    )
                )

            return {
                "provider": "cloudinary",
                "temp_post_id": temp_id,
                "uploads": uploads
            }

        # -----------------------------------------
        # CHAT MEDIA  (user or org — direct messages)
        # -----------------------------------------
        # Everything a conversation uploads lives under chat/<actor path>. The
        # per-actor subfolder lets the message service re-validate that a media
        # URL was uploaded by the SENDER (not replayed from someone else's
        # folder) before it trusts it — see MessageService._validate_chat_image.
        elif upload_type == "chat":
            if actor.is_user:
                folder = f"chat/users/{actor.user.id}"
            elif actor.is_org:
                folder = f"chat/organizations/{actor.organization.id}"
            else:
                raise ValueError("Invalid actor for chat upload")

            for _ in range(count):
                uploads.append(
                    self._build_signed_upload(
                        upload_url=upload_url,
                        timestamp=timestamp,
                        folder=folder,
                        public_id=str(uuid.uuid4()),
                        overwrite="false"
                    )
                )

            return {
                "provider": "cloudinary",
                "uploads": uploads
            }

        # -----------------------------------------
        # ORGANIZATION LOGO
        # -----------------------------------------
        elif upload_type == "organization_logo":
            org = actor.organization

            uploads.append(
                self._build_signed_upload(
                    upload_url=upload_url,
                    timestamp=timestamp,
                    folder=f"organizations/{org.id}/logo",
                    public_id="logo",
                    overwrite="true"
                )
            )

            return {
                "provider": "cloudinary",
                "uploads": uploads
            }

        # -----------------------------------------
        # ORGANIZATION COVER
        # -----------------------------------------
        elif upload_type == "organization_cover":
            org = actor.organization

            uploads.append(
                self._build_signed_upload(
                    upload_url=upload_url,
                    timestamp=timestamp,
                    folder=f"organizations/{org.id}/cover",
                    public_id="cover",
                    overwrite="true"
                )
            )

            return {
                "provider": "cloudinary",
                "uploads": uploads
            }

        raise ValueError("Invalid upload type")

    # -----------------------------------------
    # helper
    # -----------------------------------------
    def _build_signed_upload(
        self,
        *,
        upload_url,
        timestamp,
        folder,
        public_id,
        overwrite
    ):
        params = {
            "timestamp": timestamp,
            "folder": folder,
            "public_id": public_id,
            "overwrite": overwrite,
        }

        signature = cloudinary.utils.api_sign_request(
            params,
            settings.CLOUDINARY_API_SECRET
        )

        return {
            "upload_url": upload_url,
            "api_key": settings.CLOUDINARY_API_KEY,
            "cloud_name": settings.CLOUDINARY_CLOUD_NAME,
            "timestamp": timestamp,
            "signature": signature,
            "folder": folder,
            "public_id": public_id,
            "overwrite": overwrite,
        }

    def delete_file(self, public_id: str):
        cloudinary.uploader.destroy(public_id)

    def delete_folder_data(self, folder_path: str):
        cloudinary.api.delete_resources_by_prefix(folder_path)
        cloudinary.api.delete_folder(folder_path)

    # -----------------------------------------
    # media metadata (server-side, never trust client)
    # -----------------------------------------
    def _ensure_config(self):
        """
        Idempotently point the global cloudinary client at our credentials.
        `django-cloudinary-storage` normally configures this lazily; setting it
        explicitly guarantees `cloudinary.api.resource` has creds in every
        process (web worker, management command, etc.).
        """
        cloudinary.config(
            cloud_name=settings.CLOUDINARY_CLOUD_NAME,
            api_key=settings.CLOUDINARY_API_KEY,
            api_secret=settings.CLOUDINARY_API_SECRET,
        )

    def get_media_metadata(self, public_id: str, media_type: str) -> dict:
        """
        Fetch intrinsic width/height (and duration for video) directly from
        Cloudinary using the stored public_id. The client never supplies these.

        Returns {"width": int, "height": int, ["duration": int]} on success, or
        an empty dict on any failure — extraction must never block an upload.
        """
        if not public_id:
            return {}

        # Videos live under a different resource_type; default is "image".
        resource_type = "video" if media_type == "video" else "image"

        try:
            self._ensure_config()
            resource = cloudinary.api.resource(
                public_id,
                resource_type=resource_type,
            )
        except Exception as e:  # cloudinary.exceptions.*, network, auth, ...
            logger.error(
                f"CloudinaryService | get_media_metadata failed | "
                f"public_id={public_id} | type={media_type} | {str(e)}"
            )
            return {}

        width = resource.get("width")
        height = resource.get("height")

        meta = {}
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            meta["width"] = width
            meta["height"] = height

        if resource_type == "video":
            duration = resource.get("duration")
            if duration is not None:
                try:
                    meta["duration"] = int(round(float(duration)))
                except (TypeError, ValueError):
                    pass

        return meta

    # -----------------------------------------
    # eager video derivatives (kills the cold-start black screen)
    # -----------------------------------------
    def ensure_video_derivatives(self, public_id: str) -> None:
        """
        Ask Cloudinary to pre-generate both derivatives the app plays: the
        universal mp4 and the HLS adaptive ladder.

        Without this, a derivative is built ON THE FIRST REQUEST: the first
        person to open a clip sits through a live transcode — multi-second black
        screen, worst on exactly the 4K/HEVC phone originals we store. Asking for
        it at creation time means the transcode races the user instead of
        blocking them. HLS goes further: a streaming manifest CANNOT be built on
        request at all, so eager generation is the only way to have one.

        ``raw_transformation`` is deliberate: it hands Cloudinary the exact
        parameter string, in order, so the derived asset is the same one the
        frontend's delivery URL asks for. Letting the SDK rebuild the
        transformation from keyword params would re-order it, and a re-ordered
        string is a DIFFERENT derived asset — the pre-generation would be paid
        for and then never used.

        Order matters to us, not to Cloudinary: mp4 stays first because it is the
        fallback that must never regress — old clips, HLS failures, and any
        client without MSE all land on it.

        Best-effort, exactly like get_media_metadata: this runs on create paths,
        so a Cloudinary outage must never surface as a failed post/highlight/
        message. Worst case we fall back to today's behaviour (cold start on
        first view, mp4 only). Idempotent — re-running ``explicit`` on an
        already-derived asset is harmless, which is what makes the backfill safe
        to re-run, including to add HLS to videos that predate it.
        """
        if not public_id:
            return

        try:
            self._ensure_config()
            cloudinary.uploader.explicit(
                public_id,
                type="upload",
                resource_type="video",
                # Cloudinary builds the derivatives in the background and returns
                # immediately — the create request never waits on a transcode.
                eager_async=True,
                eager=[
                    {
                        "raw_transformation": VIDEO_EAGER_TRANSFORMATION,
                        "format": VIDEO_EAGER_FORMAT,
                    },
                    {
                        "raw_transformation": VIDEO_HLS_TRANSFORMATION,
                        "format": VIDEO_HLS_FORMAT,
                    },
                ],
            )
        except Exception as e:  # cloudinary.exceptions.*, network, auth, ...
            logger.error(
                f"CloudinaryService | ensure_video_derivatives failed | "
                f"public_id={public_id} | {str(e)}"
            )