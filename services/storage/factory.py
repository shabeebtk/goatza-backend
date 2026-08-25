from django.conf import settings
from .cloudinary import CloudinaryService
from .r2 import R2Service


def get_storage_service():
    """
    Cloudflare R2 is the provider (doc §5.2). "cloudinary" stays selectable via
    FILE_STORAGE_PROVIDER purely as an instant rollback flag while the migration
    lands — Stage 6 deletes the branch along with the package.
    """
    provider = getattr(settings, "FILE_STORAGE_PROVIDER", "r2")

    if provider == "r2":
        return R2Service()

    # TODO(stage-6): remove — Cloudinary goes away entirely.
    if provider == "cloudinary":
        return CloudinaryService()

    raise ValueError("Invalid storage provider")
