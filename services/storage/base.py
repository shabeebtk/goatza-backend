class BaseStorageService:
    def get_upload_config(self, user, upload_type: str):
        raise NotImplementedError

    def delete_file(self, public_id: str):
        raise NotImplementedError

    def get_media_metadata(self, public_id: str, media_type: str) -> dict:
        """
        Return trusted intrinsic media metadata (width/height, and duration for
        video) read straight from the storage provider — never from the client.
        Implementations must degrade gracefully and return an empty dict on any
        failure so callers can persist NULL dimensions without blocking uploads.
        """
        raise NotImplementedError