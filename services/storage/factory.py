from django.conf import settings
from .cloudinary import CloudinaryService
from .r2 import R2Service


def get_storage_service():
    """
    R2 is the default. Cloudinary stays reachable behind the flag until every
    call site has moved and the cleanup stage removes it — flipping
    FILE_STORAGE_PROVIDER back to "cloudinary" is the rollback.
    """
    provider = getattr(settings, "FILE_STORAGE_PROVIDER", "r2")

    if provider == "r2":
        return R2Service()

    if provider == "cloudinary":
        return CloudinaryService()

    raise ValueError(
        f"Invalid storage provider: {provider!r} "
        f"(expected 'r2' or 'cloudinary')"
    )
