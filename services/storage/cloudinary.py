import time, uuid, logging
import cloudinary
import cloudinary.utils
import cloudinary.api
from django.conf import settings

logger = logging.getLogger(__name__)


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