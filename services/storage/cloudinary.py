import time
import cloudinary
import cloudinary.utils
from django.conf import settings


class CloudinaryService:
    def get_upload_config(self, user, upload_type: str):
        folder = f"users/{user.id}/{upload_type}"
        timestamp = int(time.time())

        public_id = upload_type   

        params = {
            "timestamp": timestamp,
            "folder": folder,
            "public_id": public_id,
            "overwrite": "true",
            "invalidate": "true"
        }

        signature = cloudinary.utils.api_sign_request(
            params,
            settings.CLOUDINARY_API_SECRET
        )

        return {
            "provider": "cloudinary",
            "upload_url": f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/image/upload",
            "api_key": settings.CLOUDINARY_API_KEY,
            "cloud_name": settings.CLOUDINARY_CLOUD_NAME,
            "timestamp": timestamp,
            "signature": signature,
            "folder": folder,
            "public_id": public_id,
            "overwrite": "true",
        }

    def delete_file(self, public_id: str):
        cloudinary.uploader.destroy(public_id)