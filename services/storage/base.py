class BaseStorageService:
    def get_upload_config(self, actor, upload_type: str, **kwargs):
        """
        Return everything the client needs to upload directly to the provider.

        The kwargs differ per provider because the handshakes differ:
        R2Service takes `files` — the client encodes first and declares
        {content_type, size_bytes, kind} per object, so each presigned PUT can
        bind an exact Content-Type. CloudinaryService takes `count` and signs
        that many open-ended POSTs; it goes away in Stage 6 along with this
        split.
        """
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