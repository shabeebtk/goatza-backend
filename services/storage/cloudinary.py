import time, uuid
import cloudinary
import cloudinary.utils
from django.conf import settings

class CloudinaryService:

    def get_upload_config(self, user, upload_type: str, count: int = 1):
        timestamp = int(time.time())

        upload_url = f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/auto/upload"

        uploads = []

        # -------------------------
        # PROFILE / COVER
        # -------------------------
        if upload_type in ["profile", "cover"]:

            folder = f"users/{user.id}/{upload_type}"
            public_id = upload_type

            params = {
                "timestamp": timestamp,
                "folder": folder,
                "public_id": public_id,
                "overwrite": "true",
            }

            signature = cloudinary.utils.api_sign_request(
                params,
                settings.CLOUDINARY_API_SECRET
            )

            uploads.append({
                "upload_url": upload_url,
                "api_key": settings.CLOUDINARY_API_KEY,
                "cloud_name": settings.CLOUDINARY_CLOUD_NAME,
                "timestamp": timestamp,
                "signature": signature,
                "folder": folder,
                "public_id": public_id,
                "overwrite": "true",
            })

            return {
                "provider": "cloudinary",
                "uploads": uploads  # ✅ consistent
            }

        # -------------------------
        # POSTS (BATCH)
        # -------------------------
        elif upload_type == "posts":

            temp_post_id = str(uuid.uuid4())
            folder = f"users/{user.id}/posts/{temp_post_id}"

            for _ in range(count):
                file_uuid = str(uuid.uuid4())

                params = {
                    "timestamp": timestamp,
                    "folder": folder,
                    "public_id": file_uuid,
                    "overwrite": "false",
                }

                signature = cloudinary.utils.api_sign_request(
                    params,
                    settings.CLOUDINARY_API_SECRET
                )

                uploads.append({
                    "upload_url": upload_url,
                    "api_key": settings.CLOUDINARY_API_KEY,
                    "cloud_name": settings.CLOUDINARY_CLOUD_NAME,
                    "timestamp": timestamp,
                    "signature": signature,
                    "folder": folder,
                    "public_id": file_uuid,
                    "overwrite": "false",
                })

            return {
                "provider": "cloudinary",
                "temp_post_id": temp_post_id,  # ✅ only for posts
                "uploads": uploads
            }
        
        elif upload_type in ['organization_logo', 'organization_cover']:
            temp_post_id = str(uuid.uuid4())
            folder = f"users/{user.id}/organizations/{temp_post_id}/{upload_type}"

            file_uuid = str(uuid.uuid4())

            params = {
                "timestamp": timestamp,
                "folder": folder,
                "public_id": file_uuid,
                "overwrite": "false",
            }

            signature = cloudinary.utils.api_sign_request(
                params,
                settings.CLOUDINARY_API_SECRET
            )

            uploads.append({
                "upload_url": upload_url,
                "api_key": settings.CLOUDINARY_API_KEY,
                "cloud_name": settings.CLOUDINARY_CLOUD_NAME,
                "timestamp": timestamp,
                "signature": signature,
                "folder": folder,
                "public_id": file_uuid,
                "overwrite": "false",
            })

            return {
                "provider": "cloudinary",
                "temp_post_id": temp_post_id, 
                "uploads": uploads
            }


        else:
            raise ValueError("Invalid upload type")

    def delete_file(self, public_id: str):
        cloudinary.uploader.destroy(public_id)


    def delete_folder_data(self, folder_path: str):
        """
        Deletes all resources inside a folder using prefix
        """
        cloudinary.api.delete_resources_by_prefix(folder_path)
        cloudinary.api.delete_folder(folder_path)