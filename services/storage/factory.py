from django.conf import settings
from .cloudinary import CloudinaryService


def get_storage_service():
    provider = getattr(settings, "FILE_STORAGE_PROVIDER", "cloudinary")

    if provider == "cloudinary":
        return CloudinaryService()

    # future
    # if provider == "s3":
    #     return S3Service()

    raise ValueError("Invalid storage provider")