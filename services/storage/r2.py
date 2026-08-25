import uuid, logging

import boto3
from botocore.config import Config
from django.conf import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------
# CONTENT TYPE → EXTENSION
# ---------------------------------------------------------------
# The key's extension is derived from the content type the VIEW already
# validated against the policy table (doc §7), never from a client-supplied
# filename: the extension ends up inside the presigned key, and the key is the
# one part of the upload the client must not be able to steer.
CONTENT_TYPE_EXTENSIONS = {
    "image/webp": "webp",
    "image/jpeg": "jpg",
    "image/png": "png",
    "video/mp4": "mp4",
    "video/webm": "webm",
}

# Presigned PUTs are short-lived on purpose (doc §8.1): the client encodes
# BEFORE asking for a config, so it already holds the bytes and uploads
# immediately. Ten minutes covers a slow mobile upload of an 80 MB post video
# and nothing more.
PRESIGN_EXPIRY_SECONDS = 600

# S3 DeleteObjects hard limit — one request carries at most 1000 keys.
DELETE_BATCH_SIZE = 1000


class R2Service:
    """
    Cloudflare R2 (S3-compatible) implementation of BaseStorageService.

    Uploads are presigned PUTs signed here and executed by the browser, so file
    bytes never pass through Django — the same property the Cloudinary signed
    POST flow had. What changed is the direction of the handshake: the client
    encodes first and declares {content_type, size_bytes, kind}, which lets us
    bind the exact Content-Type into each signature.
    """

    def __init__(self):
        # Built lazily and cached per instance — botocore client construction
        # parses service models off disk and is far too expensive to repeat
        # per file in a 20-entry post config.
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=(
                    f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
                ),
                aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                # R2 has no regions; "auto" is what Cloudflare's S3 API expects.
                region_name="auto",
                # s3v4 is mandatory — R2 rejects the legacy s3 signature.
                config=Config(signature_version="s3v4"),
            )
        return self._client

    # -----------------------------------------
    # upload config
    # -----------------------------------------
    def get_upload_config(
        self,
        actor,
        upload_type: str,
        files: list
    ):
        """
        Build one presigned PUT per entry in `files`.

        `files` is the already-policy-validated list from the view: each entry
        is {"content_type": str, "size_bytes": int, "kind": "image"|"video"|"thumb"}.
        Order is preserved — the client matches uploads[i] back to its files[i].

        The folder scheme is identical to the Cloudinary one (doc §3.2), so
        every existing actor-prefix ownership check keeps working unchanged.
        """
        # -----------------------------------------
        # USER PROFILE / COVER
        # -----------------------------------------
        # One fixed key per user per slot — the R2 equivalent of Cloudinary's
        # fixed public_id + overwrite=true: a PUT to the same key replaces the
        # object in place. Cache-busting on replace is the caller's job
        # (doc §4 G6, wired at the call sites in Stage 2).
        if upload_type in {"profile", "cover"}:
            user = actor.user

            return {
                "provider": "r2",
                "uploads": [
                    self._build_upload(
                        key=self._fixed_key(
                            f"users/{user.id}/{upload_type}", files[0]
                        ),
                        content_type=files[0]["content_type"],
                    )
                ],
            }

        # -----------------------------------------
        # USER POSTS
        # -----------------------------------------
        elif upload_type == "posts":
            temp_post_id = str(uuid.uuid4())

            if actor.is_user:
                folder = f"users/{actor.user.id}/posts/{temp_post_id}"

            elif actor.is_org:
                folder = f"organizations/{actor.organization.id}/posts/{temp_post_id}"

            else:
                raise ValueError("Invalid actor for post upload")

            return {
                "provider": "r2",
                "temp_post_id": temp_post_id,
                "uploads": self._build_random_uploads(folder, files),
            }

        # -----------------------------------------
        # RECRUITMENTS  (org only)
        # -----------------------------------------
        elif upload_type == "recruitments":
            org = actor.organization
            temp_id = str(uuid.uuid4())
            folder = f"organizations/{org.id}/recruitments/{temp_id}"

            return {
                "provider": "r2",
                "temp_post_id": temp_id,
                "uploads": self._build_random_uploads(folder, files),
            }

        # -----------------------------------------
        # CHAT MEDIA  (user or org — direct messages)
        # -----------------------------------------
        # Everything a conversation uploads lives under chat/<actor path>. The
        # per-actor subfolder lets the message service re-validate that a media
        # URL was uploaded by the SENDER (not replayed from someone else's
        # folder) before it trusts it — see MessageService._validate_chat_image.
        elif upload_type == "chat":
            if actor.is_user:
                folder = f"chat/users/{actor.user.id}"
            elif actor.is_org:
                folder = f"chat/organizations/{actor.organization.id}"
            else:
                raise ValueError("Invalid actor for chat upload")

            return {
                "provider": "r2",
                "uploads": self._build_random_uploads(folder, files),
            }

        # -----------------------------------------
        # ACHIEVEMENT PROOF / SHOWCASE IMAGE · MATCH DIARY PHOTO
        # -----------------------------------------
        # One image per award (or per match), so a random key — unlike
        # profile/cover, which each own a single fixed slot per user and are
        # meant to replace themselves. A user can hold 20 achievements and a
        # season of matches; replacing one image must never clobber another's.
        #
        # Both are user-only and land under users/<id>/<type>. The folder is
        # built from upload_type rather than hardcoded, so the next
        # person-owned image type is one entry in ALLOWED_TYPES and nothing
        # here.
        elif upload_type in {"achievements", "matches"}:
            user = actor.user

            return {
                "provider": "r2",
                "uploads": self._build_random_uploads(
                    f"users/{user.id}/{upload_type}", files
                ),
            }

        # -----------------------------------------
        # ORGANIZATION LOGO
        # -----------------------------------------
        elif upload_type == "organization_logo":
            org = actor.organization

            return {
                "provider": "r2",
                "uploads": [
                    self._build_upload(
                        key=self._fixed_key(
                            f"organizations/{org.id}/logo", files[0]
                        ),
                        content_type=files[0]["content_type"],
                    )
                ],
            }

        # -----------------------------------------
        # ORGANIZATION COVER
        # -----------------------------------------
        elif upload_type == "organization_cover":
            org = actor.organization

            return {
                "provider": "r2",
                "uploads": [
                    self._build_upload(
                        key=self._fixed_key(
                            f"organizations/{org.id}/cover", files[0]
                        ),
                        content_type=files[0]["content_type"],
                    )
                ],
            }

        raise ValueError("Invalid upload type")

    # -----------------------------------------
    # key helpers
    # -----------------------------------------
    def _extension(self, file_entry: dict) -> str:
        content_type = file_entry.get("content_type")
        ext = CONTENT_TYPE_EXTENSIONS.get(content_type)

        if not ext:
            # The view's policy check should have caught this already; failing
            # loudly here keeps an extension-less key from ever being signed.
            raise ValueError(f"Unsupported file type: {content_type}")

        return ext

    def _fixed_key(self, folder: str, file_entry: dict) -> str:
        """`users/<id>/profile` + image/webp → `users/<id>/profile.webp`."""
        return f"{folder}.{self._extension(file_entry)}"

    def _random_key(self, folder: str, file_entry: dict) -> str:
        """`users/<id>/posts/<temp>` + video/mp4 → `.../<uuid>.mp4`."""
        return f"{folder}/{uuid.uuid4()}.{self._extension(file_entry)}"

    def _build_random_uploads(self, folder: str, files: list) -> list:
        return [
            self._build_upload(
                key=self._random_key(folder, file_entry),
                content_type=file_entry["content_type"],
            )
            for file_entry in files
        ]

    # -----------------------------------------
    # presign
    # -----------------------------------------
    def _build_upload(self, *, key: str, content_type: str) -> dict:
        """
        One presigned PUT. ContentType is bound into the signature, so the
        browser MUST send the identical header — swapping the bytes for another
        media type under the same signature fails at R2, not at us.
        """
        upload_url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.R2_BUCKET,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=PRESIGN_EXPIRY_SECONDS,
            HttpMethod="PUT",
        )

        return {
            "method": "PUT",
            "upload_url": upload_url,
            "key": key,
            "public_url": f"{settings.MEDIA_PUBLIC_BASE_URL}/{key}",
            "headers": {"Content-Type": content_type},
            "expires_in": PRESIGN_EXPIRY_SECONDS,
        }

    # -----------------------------------------
    # delete
    # -----------------------------------------
    def delete_file(self, public_id: str):
        """
        `public_id` is the R2 key. Best-effort, exactly like the Cloudinary
        implementation: deletes run on remove/replace paths where a storage
        outage must never surface as a failed request. Worst case an orphan
        object survives in the bucket.
        """
        if not public_id:
            return

        try:
            self.client.delete_object(
                Bucket=settings.R2_BUCKET,
                Key=public_id,
            )
        except Exception as e:  # botocore ClientError, network, auth, ...
            logger.error(
                f"R2Service | delete_file failed | "
                f"key={public_id} | {str(e)}"
            )

    def delete_folder_data(self, folder_path: str):
        """
        Delete every object under a key prefix (a post's folder, say). R2 has no
        real directories, so "delete the folder" is "list the prefix and delete
        what's there" — paginated because a prefix can hold more than one page,
        and batched because DeleteObjects takes at most 1000 keys per call.

        Tolerant of an empty or missing prefix: listing simply yields no keys.
        Best-effort for the same reason as delete_file.
        """
        if not folder_path:
            return

        try:
            paginator = self.client.get_paginator("list_objects_v2")
            batch = []

            for page in paginator.paginate(
                Bucket=settings.R2_BUCKET,
                Prefix=folder_path,
            ):
                for obj in page.get("Contents", []):
                    batch.append({"Key": obj["Key"]})

                    if len(batch) == DELETE_BATCH_SIZE:
                        self._delete_batch(batch)
                        batch = []

            if batch:
                self._delete_batch(batch)

        except Exception as e:  # botocore ClientError, network, auth, ...
            logger.error(
                f"R2Service | delete_folder_data failed | "
                f"prefix={folder_path} | {str(e)}"
            )

    def _delete_batch(self, batch: list):
        self.client.delete_objects(
            Bucket=settings.R2_BUCKET,
            Delete={"Objects": batch, "Quiet": True},
        )

    # -----------------------------------------
    # dead provider capabilities (doc §4)
    # -----------------------------------------
    def get_media_metadata(self, public_id: str, media_type: str) -> dict:
        """
        Always {}. Doc §4 G4: R2 is dumb storage with no media API, so
        width/height/duration now come from the client at attach time and are
        clamped server-side. Kept on the interface so the Stage 2 call-site
        removal stays a separate, reviewable change.
        """
        return {}

    def ensure_video_derivatives(self, public_id: str) -> None:
        """
        No-op. Doc §4 G1: there is no delivery-time transcode to pre-warm —
        the client encodes to H.264 mp4 before upload, so the stored object IS
        the object every viewer plays. Kept on the interface for the same
        reason as get_media_metadata.
        """
        return None
