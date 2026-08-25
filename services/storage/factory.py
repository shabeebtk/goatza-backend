from django.conf import settings
from .r2 import R2Service


def get_storage_service():
    """
    The one storage backend.

    FILE_STORAGE_PROVIDER survives the removal of the second provider on
    purpose: a deployment still carrying the old value in its environment should
    fail loudly here rather than silently uploading somewhere unexpected.
    """
    provider = getattr(settings, "FILE_STORAGE_PROVIDER", "r2")

    if provider == "r2":
        return R2Service()

    raise ValueError(
        f"Invalid storage provider: {provider!r} (expected 'r2')"
    )
