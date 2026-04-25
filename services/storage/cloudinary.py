import time, uuid
import cloudinary
import cloudinary.utils
from django.conf import settings

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
            user = actor.user

            temp_post_id = str(uuid.uuid4())
            folder = f"users/{user.id}/posts/{temp_post_id}"

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