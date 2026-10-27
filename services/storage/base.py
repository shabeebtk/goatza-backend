class BaseStorageService:
    def get_upload_config(self, actor, upload_type: str, files: list,
                          temp_id: str = None):
        raise NotImplementedError

    def delete_file(self, public_id: str):
        raise NotImplementedError

    # ---- multipart (large videos) ----
    def create_multipart_upload(self, key: str, content_type: str) -> str:
        """Start a multipart upload and return its upload id. Raises."""
        raise NotImplementedError

    def presign_upload_part(self, key: str, upload_id: str,
                            part_number: int) -> str:
        """A presigned PUT URL for one part of `upload_id`."""
        raise NotImplementedError

    def complete_multipart_upload(self, key: str, upload_id: str,
                                  parts: list):
        """`parts` is [{"part_number", "etag"}], ascending. Raises."""
        raise NotImplementedError

    def abort_multipart_upload(self, key: str, upload_id: str):
        """Best-effort: must never raise, like delete_file."""
        raise NotImplementedError

    # ---- bucket configuration (operator tooling — these raise) ----
    def head_bucket(self):
        raise NotImplementedError

    def get_bucket_cors(self) -> list:
        """[] when no policy is set."""
        raise NotImplementedError

    def put_bucket_cors(self, rules: list):
        raise NotImplementedError

    def get_bucket_lifecycle_rules(self) -> list:
        """[] when no rules are set. Read-only: nothing writes these."""
        raise NotImplementedError

    def get_media_metadata(self, public_id: str, media_type: str) -> dict:
        """
        Return trusted intrinsic media metadata (width/height, and duration for
        video) read straight from the storage provider — never from the client.
        Implementations must degrade gracefully and return an empty dict on any
        failure so callers can persist NULL dimensions without blocking uploads.
        """
        raise NotImplementedError

    def ensure_video_derivatives(self, public_id: str) -> None:
        """
        Pre-generate the transcoded video the client actually plays, so the
        first viewer doesn't wait through an on-demand transcode.

        Implementations must be best-effort and idempotent: this is called on
        content-creation paths, so it must never raise, and it must be safe to
        call repeatedly for the same public_id (the backfill command relies on
        both).
        """
        raise NotImplementedError