"""
Cloudflare R2 storage provider (S3-compatible).

The upload model is the inverse of Cloudinary's. Cloudinary is handed a signed
POST and decides the final path itself; R2 has no opinion — the backend picks
the object key, signs a PUT bound to that exact key and content type, and the
browser streams the bytes straight to the bucket. Nothing about the path is
client-supplied, which is what keeps the ownership prefix checks in
services/storage/validators.py meaningful.

Delivery is a plain public CDN URL (settings.MEDIA_PUBLIC_BASE_URL + "/" + key),
so there is no signing, no transformation pipeline and no derived assets — the
client encodes and thumbnails before it uploads.
"""

import logging

import boto3
from botocore.config import Config
from django.conf import settings

from .base import BaseStorageService
from .paths import FIXED_SLOT_TYPES, build_folder, build_object_name, new_temp_id

logger = logging.getLogger(__name__)


# How long a presigned PUT stays usable. Long enough for an 80 MB video on a
# weak mobile uplink, short enough that a leaked URL is worthless by the time
# anyone finds it.
UPLOAD_URL_EXPIRES_IN = 600

# The extension appended to the object key, derived from the ALREADY-VALIDATED
# content_type (the view rejects anything outside this map before we see it).
# R2 serves whatever Content-Type was bound at upload, but a real extension
# keeps the key readable, keeps `get_file_extension` in validators.py working on
# the delivery URL, and stops browsers guessing on direct hits.
CONTENT_TYPE_EXTENSIONS = {
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

# S3 caps a single delete_objects call at 1000 keys.
DELETE_BATCH_SIZE = 1000


class R2Service(BaseStorageService):

    def __init__(self):
        # Built on first use and reused for the life of the instance: creating a
        # boto3 client parses the botocore service model, which is far too
        # expensive to pay per upload request.
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
                # R2 has no regions, but botocore insists on one to sign with.
                region_name="auto",
                # R2 only accepts SigV4. Without this botocore can pick SigV2
                # for a custom endpoint and every upload 403s.
                config=Config(signature_version="s3v4"),
            )
        return self._client

    # -----------------------------------------
    # upload config (presigned PUTs)
    # -----------------------------------------
    def get_upload_config(self, actor, upload_type: str, files: list):
        """
        One presigned PUT per entry in `files`, returned in the SAME order the
        client sent them — that positional pairing is how the client maps an
        upload back to the file it picked (and a video to its thumb).

        `files` entries are already validated by the view: content_type is in
        CONTENT_TYPE_EXTENSIONS and the per-type counts/sizes hold.
        """

        # posts/recruitments upload into a batch folder named for a row that
        # does not exist yet; the client posts this id back with the create
        # request. Same generation and same response key as Cloudinary.
        temp_id = None
        if upload_type in {"posts", "recruitments"}:
            temp_id = new_temp_id()

        folder = build_folder(actor, upload_type, temp_id)

        uploads = []

        for file in files:
            content_type = file.get("content_type")
            extension = CONTENT_TYPE_EXTENSIONS.get(content_type)

            if not extension:
                raise ValueError("Invalid file type")

            if upload_type in FIXED_SLOT_TYPES:
                # The folder already ends with the slot name
                # (users/<id>/profile, organizations/<id>/logo), so the key is
                # just that plus an extension — a re-upload overwrites in place,
                # which is what overwrite="true" buys on the Cloudinary side.
                key = f"{folder}{extension}"
            else:
                key = f"{folder}/{build_object_name(upload_type)}{extension}"

            uploads.append(
                self._build_presigned_put(
                    key=key,
                    content_type=content_type,
                )
            )

        config = {
            "provider": "r2",
            "uploads": uploads,
        }

        if temp_id:
            config["temp_post_id"] = temp_id

        return config

    # -----------------------------------------
    # helper
    # -----------------------------------------
    def _build_presigned_put(self, *, key: str, content_type: str):
        """
        Bind bucket, key AND content type into the signature. Binding the
        content type is the point: the browser must send back the exact
        Content-Type header it was signed with, so a client cannot claim
        image/webp to pass validation and then PUT a 2 GB video.
        """
        upload_url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.R2_BUCKET,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=UPLOAD_URL_EXPIRES_IN,
        )

        return {
            "method": "PUT",
            "upload_url": upload_url,
            "key": key,
            "public_url": f"{settings.MEDIA_PUBLIC_BASE_URL}/{key}",
            "headers": {
                "Content-Type": content_type,
            },
            "expires_in": UPLOAD_URL_EXPIRES_IN,
        }

    # -----------------------------------------
    # deletion
    # -----------------------------------------
    def delete_file(self, key: str):
        """
        Best-effort, exactly like the folder sweep below: deletion runs on
        cleanup paths (replacing an avatar, removing a post) where the row is
        already gone. A storage failure must not surface as a failed delete.
        """
        if not key:
            return

        try:
            self.client.delete_object(
                Bucket=settings.R2_BUCKET,
                Key=key,
            )
        except Exception as e:  # botocore.exceptions.*, network, auth, ...
            logger.error(
                f"R2Service | delete_file failed | "
                f"key={key} | {str(e)}"
            )

    def delete_folder_data(self, folder_path: str):
        """
        Delete every object under a prefix. R2 has no folders — a "folder" is
        just a shared key prefix — so this lists and deletes in pages of
        DELETE_BATCH_SIZE.

        An empty or already-swept prefix is a no-op, not an error: callers
        (deleting a post, an org, a conversation) have no way to know whether
        anything was ever uploaded there.
        """
        if not folder_path:
            return

        try:
            paginator = self.client.get_paginator("list_objects_v2")
            pages = paginator.paginate(
                Bucket=settings.R2_BUCKET,
                Prefix=folder_path,
            )

            batch = []

            for page in pages:
                for obj in page.get("Contents", []):
                    batch.append({"Key": obj["Key"]})

                    if len(batch) >= DELETE_BATCH_SIZE:
                        self._delete_batch(batch)
                        batch = []

            if batch:
                self._delete_batch(batch)

        except Exception as e:  # botocore.exceptions.*, network, auth, ...
            logger.error(
                f"R2Service | delete_folder_data failed | "
                f"prefix={folder_path} | {str(e)}"
            )

    def _delete_batch(self, batch: list):
        result = self.client.delete_objects(
            Bucket=settings.R2_BUCKET,
            Delete={"Objects": batch, "Quiet": True},
        )

        # delete_objects returns 200 with a per-key error list rather than
        # raising, so partial failures are invisible unless we read them out.
        for error in result.get("Errors", []):
            logger.error(
                f"R2Service | delete_objects rejected a key | "
                f"key={error.get('Key')} | code={error.get('Code')} | "
                f"{error.get('Message')}"
            )

    # -----------------------------------------
    # metadata / derivatives — not R2's job
    # -----------------------------------------
    def get_media_metadata(self, public_id: str, media_type: str) -> dict:
        # Metadata now comes from the client: videos are encoded client-side
        # before upload, so width/height/duration are already known there.
        return {}

    def ensure_video_derivatives(self, public_id: str) -> None:
        # Derivatives now come from the client: videos are encoded client-side
        # before upload, so there is nothing left to pre-generate.
        return None
