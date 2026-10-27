"""
Cloudflare R2 storage provider (S3-compatible).

R2 has no opinion about where a file goes: the backend picks the object key,
signs a PUT bound to that exact key and content type, and the browser streams
the bytes straight to the bucket. Nothing about the path is client-supplied,
which is what keeps the ownership prefix checks in
services/storage/validators.py meaningful.

Delivery is a plain public CDN URL (settings.MEDIA_PUBLIC_BASE_URL + "/" + key),
so there is no signing, no transformation pipeline and no derived assets — the
client encodes and thumbnails before it uploads.

Large videos go up as an S3 multipart upload instead of one PUT (see the
multipart section below): a dropped connection then costs one part, not the
whole file — which on a phone is the difference between a retry and a failed
post.
"""

import logging
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings

from .base import BaseStorageService
from .paths import (
    FIXED_SLOT_TYPES,
    TEMP_BATCH_TYPES,
    build_folder,
    build_object_name,
    new_temp_id,
)

logger = logging.getLogger(__name__)


# How long a presigned PUT (and a multipart part URL) stays usable.
#
# 600s was too short for what actually happens on a phone. A post is up to 20
# sequential PUTs (10 photos and their 10 thumbs) signed by ONE request, so the
# last URL in a batch has already waited out every upload before it. An 80 MB
# video on a 1 Mbps uplink is ~11 minutes on its own. And a retried or resumed
# upload — after a dropped connection, or after iOS suspended the page while
# the screen was locked — only starts once a failed first attempt has already
# spent part of the window. An hour covers all three.
#
# The longer window does not widen what a URL can do: it is still bound to ONE
# key under the actor's own prefix and ONE content type, so a leaked URL lets
# its holder write exactly the object we already agreed to accept, into the
# owner's own folder, until it expires.
UPLOAD_URL_EXPIRES_IN = 3600

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


def endpoint_url() -> str:
    """The account's S3 endpoint. One home, so the client and r2_setup agree."""
    return f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"


def public_url_for(key: str) -> str:
    """Where an object is readable once uploaded — final, directly renderable."""
    return f"{settings.MEDIA_PUBLIC_BASE_URL}/{key}"


def _error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "")


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
                endpoint_url=endpoint_url(),
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
    def get_upload_config(self, actor, upload_type: str, files: list,
                          temp_id: str = None):
        """
        One presigned PUT per entry in `files`, returned in the SAME order the
        client sent them — that positional pairing is how the client maps an
        upload back to the file it picked (and a video to its thumb).

        `files` entries are already validated by the view: content_type is in
        CONTENT_TYPE_EXTENSIONS and the per-type counts/sizes hold.

        `temp_id` re-signs into an EXISTING batch folder (posts/recruitments
        only; ignored for every other type). A client whose URLs expired, or
        were refused, part-way through a batch asks again for just the files it
        has not uploaded yet, and landing them in the SAME folder is what keeps
        the same-folder thumbnail rule true for a pair that straddles the two
        requests. The view has already checked it is a canonical UUID, and the
        folder is built under the actor's own prefix whatever it says.
        """

        # posts/recruitments upload into a batch folder named for a row that
        # does not exist yet; the client posts this id back with the create
        # request.
        if upload_type in TEMP_BATCH_TYPES:
            temp_id = temp_id or new_temp_id()
        else:
            temp_id = None

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
                # just that plus an extension, so a re-upload overwrites in
                # place and the URL never changes.
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

        `expires_at` is read BEFORE signing, so it can only ever be a moment
        early, never late. The client measures the lifetime on its own clock
        from `expires_in` (a phone's clock can be minutes off ours) and falls
        back to this.
        """
        expires_at = int(time.time()) + UPLOAD_URL_EXPIRES_IN

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
            "public_url": public_url_for(key),
            "headers": {
                "Content-Type": content_type,
            },
            "expires_in": UPLOAD_URL_EXPIRES_IN,
            "expires_at": expires_at,
        }

    # -----------------------------------------
    # multipart (large videos)
    # -----------------------------------------
    # The client sends a large video as fixed-size parts, each with its own
    # presigned URL, so a dropped connection or a page iOS suspended costs one
    # part instead of the whole file. The content type is bound HERE, at
    # create time, by us — a part PUT carries no Content-Type at all, so there
    # is nothing on the client's side of a part left to lie about.
    def create_multipart_upload(self, key: str, content_type: str) -> str:
        """Start a multipart upload and return its UploadId. Raises."""
        response = self.client.create_multipart_upload(
            Bucket=settings.R2_BUCKET,
            Key=key,
            ContentType=content_type,
        )
        return response["UploadId"]

    def presign_upload_part(self, key: str, upload_id: str,
                            part_number: int) -> str:
        """
        A presigned PUT for one part. Only `host` is signed — the part number
        and upload id ride in the query string — which is why the browser must
        send NO extra headers on a part.
        """
        return self.client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": settings.R2_BUCKET,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=UPLOAD_URL_EXPIRES_IN,
        )

    def complete_multipart_upload(self, key: str, upload_id: str,
                                  parts: list):
        """
        Stitch the parts into the final object. Raises.

        `parts` is [{"part_number", "etag"}] in ascending order. Each ETag is
        passed through exactly as the browser read it off the part's response,
        quotes included — R2 compares it as given.
        """
        self.client.complete_multipart_upload(
            Bucket=settings.R2_BUCKET,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": part["part_number"], "ETag": part["etag"]}
                    for part in parts
                ]
            },
        )

    def abort_multipart_upload(self, key: str, upload_id: str):
        """
        Best-effort, like delete_file: a client abandoning an upload must never
        see a failure for it. An upload nobody aborts is still cleaned up — R2
        gives every bucket a "Default Multipart Abort Rule" (7 days) — so a
        failure here costs a few days of stored parts, not an orphan.
        """
        if not key or not upload_id:
            return

        try:
            self.client.abort_multipart_upload(
                Bucket=settings.R2_BUCKET,
                Key=key,
                UploadId=upload_id,
            )
        except Exception as e:  # botocore.exceptions.*, network, auth, ...
            logger.error(
                f"R2Service | abort_multipart_upload failed | "
                f"key={key} | {str(e)}"
            )

    # -----------------------------------------
    # bucket configuration (operator tooling)
    # -----------------------------------------
    # Read and written by `python manage.py r2_setup`. Unlike the delete_*
    # methods these RAISE: they are not cleanup on a request path but an
    # operator asking a question, and a swallowed error there would read as
    # "nothing is configured" — the most misleading answer available.
    def head_bucket(self):
        """Proves the credentials and the bucket name in one call. Raises."""
        self.client.head_bucket(Bucket=settings.R2_BUCKET)

    def get_bucket_cors(self) -> list:
        """The bucket's CORS rules, or [] when none are set. Raises otherwise."""
        try:
            response = self.client.get_bucket_cors(Bucket=settings.R2_BUCKET)
        except ClientError as e:
            if _error_code(e) == "NoSuchCORSConfiguration":
                return []
            raise
        return response.get("CORSRules", [])

    def put_bucket_cors(self, rules: list):
        """Replace the bucket's whole CORS policy with `rules`. Raises."""
        self.client.put_bucket_cors(
            Bucket=settings.R2_BUCKET,
            CORSConfiguration={"CORSRules": rules},
        )

    def get_bucket_lifecycle_rules(self) -> list:
        """
        The bucket's lifecycle rules, or [] when none are set. READ-ONLY on
        purpose — nothing in code writes lifecycle rules (see r2_setup).
        """
        try:
            response = self.client.get_bucket_lifecycle_configuration(
                Bucket=settings.R2_BUCKET
            )
        except ClientError as e:
            if _error_code(e) == "NoSuchLifecycleConfiguration":
                return []
            raise
        return response.get("Rules", [])

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
