"""
Server-side media processing (Media Upload v2) — CLAUDE.md, "Server-side media
processing".

    signing a `source`        /user/get/upload/signature, kind "source"
    multipart for a source    /user/upload/multipart/create, a `.src.` key
    queueing the conversion   POST /media/jobs, GET /media/jobs/<id>
    the worker                MediaProcessingService.run (ffmpeg mocked at the
                              module's subprocess helper; one real-ffmpeg test
                              that skips when ffmpeg is not installed)
    the sweeper               MediaSweepService.sweep
    held posts / highlights   POST /posts/create and /highlights/create with a
                              media_job_id → 200/201 or 202 + PendingPublication,
                              then PublicationService.try_publish
    the author's endpoints    /media/publications (+ cancel, dismiss)
    the failure deep link     publication_failed → deeplink_service

No network anywhere: signing runs a REAL R2Service against fake credentials
(presigning is local computation), every other storage call is a MagicMock,
the websocket helper (`safe_group_send`) is patched where the media services
import it, and every dispatch (`enqueue`) is patched where it is called.

Job states are written straight to the test DB — a job that is "ready" here is
a row with outputs, not a worker run — except in the worker tests, which run
the worker.
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from botocore.exceptions import EndpointConnectionError
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import worker_ready
from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from kombu import Queue
from PIL import Image
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.accounts.models import User, UserProfile
from apps.accounts.views.user_upload_multipart_views import MULTIPART_PART_SIZE
from apps.connections.models import Follow
from apps.highlights.models import Highlight
from apps.legal.testing import accept_current_terms
from apps.media.constants import (
    ACTOR_NO_LONGER_VALID,
    ACTOR_NO_LONGER_VALID_MESSAGE,
    JOB_ALREADY_USED_MESSAGE,
    MB,
    OUTPUT_CACHE_CONTROL,
    PROCESSING_TIMEOUT,
    PROCESSOR_UNAVAILABLE,
    PUBLISH_REJECTED,
    SERVER_PROCESSING_UNAVAILABLE,
    SOURCE_EXTENSIONS,
    SOURCE_MISSING,
    SOURCE_NOT_PROCESSED_MESSAGE,
    SOURCE_TOO_LARGE,
    SOURCE_UNREADABLE,
    STUCK_GRACE_SECONDS,
    VIDEO_TOO_LONG,
    error_message,
)
from apps.media.models import MediaJob, PendingPublication
from apps.media.processing import video
from apps.media.processing.base import SourceFetchError
from apps.media.services.media_job_service import MediaJobService
from apps.media.services.media_processing_service import (
    WORK_DIR_PREFIX,
    MediaProcessingService,
)
from apps.media.services.media_sweep_service import MediaSweepService
from apps.media.services.publication_service import PublicationService
from apps.media.signals import media_job_finished
from apps.media.tasks import process_media_job
from apps.messaging.models import Message
from apps.messaging.services.conversation_service import ConversationService
from apps.notifications.models import Notification
from apps.notifications.services.deeplink_service import build_notification_url
from apps.notifications.services.notification_service import NotificationService
from apps.organization.models import (
    Organization,
    OrganizationMember,
    OrganizationProfile,
)
from apps.posts.models import Post, PostHashtag, PostMedia, PostMention
from apps.recruitments.services.recruitment_service import RecruitmentService
from core.actor import Actor
from core.celery import app as celery_app
from services.storage.paths import is_source_key, sibling_key
from shared.tasks import delete_keys
from utils.background_jobs import WORKER_LAST_SEEN_KEY

SIGNATURE_URL = "/user/get/upload/signature"
MULTIPART_CREATE_URL = "/user/upload/multipart/create"
CAPABILITIES_URL = "/media/capabilities"
JOBS_URL = "/media/jobs"
PUBLICATIONS_URL = "/media/publications"
POST_CREATE_URL = "/posts/create"
HIGHLIGHT_CREATE_URL = "/highlights/create"

MEDIA_BASE = "https://media.goatza.test"

# The signature tests run a real R2Service: these are enough for botocore to
# sign a URL locally, and never reach a network.
FAKE_R2 = {
    "FILE_STORAGE_PROVIDER": "r2",
    "R2_ACCOUNT_ID": "testaccount",
    "R2_ACCESS_KEY_ID": "test-access-key",
    "R2_SECRET_ACCESS_KEY": "test-secret-key",
    "R2_BUCKET": "test-bucket",
    "R2_UPLOAD_GATEWAY_URL": "",
    "MEDIA_PUBLIC_BASE_URL": MEDIA_BASE,
    "PASSWORD_HASHERS": ["django.contrib.auth.hashers.MD5PasswordHasher"],
}

# Every test starts with processing OFF and Celery OFF, whatever the local
# .env says: with Celery off an unpatched dispatch runs inline (or is
# skipped) instead of dialling a broker.
BASE_SETTINGS = {
    **FAKE_R2,
    "MEDIA_PROCESSING_ENABLED": False,
    "CELERY_ENABLED": False,
}

# server_processing_available(): the flag, Celery, and a worker that is not
# known to be dead (an empty cache reads "unknown", which counts as alive).
# Only used where every enqueue the code under test reaches is patched.
PROCESSING_ON = {"MEDIA_PROCESSING_ENABLED": True, "CELERY_ENABLED": True}

# Patch targets — where each module looks the name up.
JOB_SEND = "apps.media.services.media_job_service.safe_group_send"
PUBLICATION_SEND = "apps.media.services.publication_service.safe_group_send"
JOB_STORAGE = "apps.media.services.media_job_service.get_storage_service"
JOB_ENQUEUE = "apps.media.services.media_job_service.enqueue"
MULTIPART_STORAGE = (
    "apps.accounts.views.user_upload_multipart_views.get_storage_service"
)
WORKER_STORAGE = (
    "apps.media.services.media_processing_service.get_storage_service"
)
WORKER_ENQUEUE = "apps.media.services.media_processing_service.enqueue"
SWEEP_STORAGE = "apps.media.services.media_sweep_service.get_storage_service"
SWEEP_ENQUEUE = "apps.media.services.media_sweep_service.enqueue"
FFMPEG_RUN = "apps.media.processing.video._run"
FFMPEG_ON_PATH = "apps.media.processing.video.tools_available"

QUEUED = MediaJob.Status.QUEUED
PROCESSING = MediaJob.Status.PROCESSING
READY = MediaJob.Status.READY
FAILED = MediaJob.Status.FAILED
VIDEO = MediaJob.Kind.VIDEO
IMAGE = MediaJob.Kind.IMAGE

TOO_LONG_FOR_POSTS = "This video is longer than 5 minutes — trim it and try again"

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def media_url(key):
    return f"{MEDIA_BASE}/{key}"


def source_file(content_type, size_bytes):
    return {"kind": "source", "content_type": content_type,
            "size_bytes": size_bytes}


READY_VIDEO = {"kind": "video", "content_type": "video/mp4",
               "size_bytes": 20 * MB}
READY_IMAGE = {"kind": "image", "content_type": "image/webp",
               "size_bytes": 800_000}
THUMB = {"kind": "thumb", "content_type": "image/webp", "size_bytes": 40_000}


def jpeg_bytes(width, height, *, exif=None):
    buffer = io.BytesIO()
    kwargs = {"exif": exif} if exif is not None else {}
    Image.new("RGB", (width, height), (30, 120, 200)).save(
        buffer, "JPEG", quality=90, **kwargs
    )
    return buffer.getvalue()


@override_settings(**BASE_SETTINGS)
class MediaTestCase(APITestCase):
    """
    The cast: a player (`author`) who owns `org` too, and a `rival` player.
    Websocket sends from the media services are patched for every test.
    """

    def setUp(self):
        cache.clear()

        self.author = self._user("author")
        self.rival = self._user("rival")

        self.org = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB,
        )
        OrganizationProfile.objects.create(organization=self.org)
        OrganizationMember.objects.create(
            organization=self.org, user=self.author,
            role=OrganizationMember.Role.OWNER,
        )

        self.job_send = self._patch(JOB_SEND, return_value=True)
        self.publication_send = self._patch(PUBLICATION_SEND, return_value=True)

        self.client.force_authenticate(user=self.author)

    # ── factories ────────────────────────────────────────────────

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def _user(self, username, role=User.Role.PLAYER):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username, role=role,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=username.title())
        return user

    def _org_headers(self, org=None):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str((org or self.org).id),
        }

    @staticmethod
    def _folder(owner, upload_type="posts"):
        """The folder the signature endpoint would sign into for `owner`."""
        if isinstance(owner, Organization):
            root = f"organizations/{owner.id}"
        else:
            root = f"users/{owner.id}"
        if upload_type in ("posts", "recruitments"):
            return f"{root}/{upload_type}/{uuid.uuid4()}"
        if upload_type == "chat":
            return f"chat/{root}"
        return f"{root}/{upload_type}"

    @staticmethod
    def _outputs(folder, kind, duration):
        """What the worker writes for a ready job: siblings of the source."""
        output_key = f"{folder}/{uuid.uuid4()}" + (
            ".mp4" if kind == VIDEO else ".jpg"
        )
        thumbnail_key = f"{folder}/{uuid.uuid4()}.jpg"
        return {
            "output_key": output_key,
            "output_url": media_url(output_key),
            "thumbnail_key": thumbnail_key,
            "thumbnail_url": media_url(thumbnail_key),
            "width": 1280 if kind == VIDEO else 2560,
            "height": 720 if kind == VIDEO else 1707,
            "duration": duration if kind == VIDEO else None,
            "size_bytes": 6 * MB if kind == VIDEO else 900_000,
        }

    def _job(self, owner=None, *, status=QUEUED, kind=VIDEO,
             purpose="posts", upload_type="posts", content_type=None,
             duration=12, error_code=VIDEO_TOO_LONG, **fields):
        """A MediaJob row in the given state, written straight to the DB."""
        owner = owner or self.author
        is_org = isinstance(owner, Organization)
        content_type = content_type or (
            "video/quicktime" if kind == VIDEO else "image/heic"
        )
        folder = self._folder(owner, upload_type)
        now = timezone.now()

        values = {
            "owner_user": None if is_org else owner,
            "owner_org": owner if is_org else None,
            "created_by": self.author if is_org else owner,
            "purpose": purpose,
            "upload_type": upload_type,
            "kind": kind,
            "status": status,
            "source_key": (
                f"{folder}/{uuid.uuid4()}.src."
                f"{SOURCE_EXTENSIONS[content_type]}"
            ),
            "source_content_type": content_type,
            "source_size_bytes": 40 * MB,
        }
        if status == PROCESSING:
            values.update(started_at=now, attempts=1)
        elif status == READY:
            values.update(
                self._outputs(folder, kind, duration),
                started_at=now, finished_at=now, attempts=1,
                source_deleted_at=now,
            )
        elif status == FAILED:
            values.update(
                error_code=error_code, started_at=now, finished_at=now,
                attempts=1,
            )
        values.update(fields)
        return MediaJob.objects.create(**values)

    def _make_ready(self, job, duration=12):
        """The worker's outcome, without the worker (and without finish())."""
        folder = job.source_key.rpartition("/")[0]
        for field, value in self._outputs(folder, job.kind, duration).items():
            setattr(job, field, value)
        job.status = READY
        job.finished_at = timezone.now()
        job.save()
        return job

    def _make_failed(self, job, error_code=VIDEO_TOO_LONG):
        job.status = FAILED
        job.error_code = error_code
        job.finished_at = timezone.now()
        job.save()
        return job

    def _held_post(self, job, owner=None, content="held post"):
        """A PendingPublication for a post, as the 202 path stores it."""
        owner = owner or self.author
        is_org = isinstance(owner, Organization)
        publication = PendingPublication.objects.create(
            owner_user=None if is_org else owner,
            owner_org=owner if is_org else None,
            created_by=self.author if is_org else owner,
            target=PendingPublication.Target.POST,
            payload={
                "content": content,
                "post_type": Post.PostType.NORMAL,
                "visibility": Post.Visibility.PUBLIC,
                "sport_id": None,
                "media": [{"media_job_id": str(job.id), "order": 0}],
                "location": {},
            },
        )
        publication.jobs.set([job])
        return publication

    def _publication(self, owner=None, *, status=PendingPublication.Status.PROCESSING,
                     target=PendingPublication.Target.POST, content="caption"):
        owner = owner or self.author
        is_org = isinstance(owner, Organization)
        return PendingPublication.objects.create(
            owner_user=None if is_org else owner,
            owner_org=owner if is_org else None,
            created_by=self.author if is_org else owner,
            target=target,
            payload={"content": content, "media": []},
            status=status,
            error_code=VIDEO_TOO_LONG
            if status == PendingPublication.Status.FAILED else "",
            error_message=TOO_LONG_FOR_POSTS
            if status == PendingPublication.Status.FAILED else "",
        )

    def _errors(self, resp):
        return resp.data["data"]["errors"]


# =====================================================================
# Signing a source
# =====================================================================

class SourceSignatureTests(MediaTestCase):
    """
    kind "source" on the ordinary signature request: `<uuid>.src.<ext>`,
    the SOURCE caps, no thumb, every count/exclusive rule unchanged.
    """

    def _sign(self, files, upload_type="posts", **extra):
        body = {"type": upload_type, "files": files, **extra}
        return self.client.post(SIGNATURE_URL, body, format="json")

    def _assert_unavailable(self, resp):
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["data"]["code"], SERVER_PROCESSING_UNAVAILABLE)
        self.assertIn("files", self._errors(resp))

    @override_settings(MEDIA_PROCESSING_ENABLED=False, CELERY_ENABLED=True)
    def test_a_source_is_refused_while_processing_is_off(self):
        resp = self._sign([source_file("video/quicktime", 50 * MB)])

        self._assert_unavailable(resp)

    @override_settings(MEDIA_PROCESSING_ENABLED=True, CELERY_ENABLED=False)
    def test_a_source_is_refused_without_celery(self):
        # A job only ever runs on a worker; with Celery off there is none.
        resp = self._sign([source_file("video/quicktime", 50 * MB)])

        self._assert_unavailable(resp)

    @override_settings(**PROCESSING_ON)
    def test_a_source_is_refused_when_the_worker_heartbeat_is_stale(self):
        cache.set(
            WORKER_LAST_SEEN_KEY,
            (timezone.now() - timedelta(hours=1)).isoformat(),
            None,
        )

        resp = self._sign([source_file("video/quicktime", 50 * MB)])

        self._assert_unavailable(resp)

    @override_settings(**PROCESSING_ON)
    def test_a_video_source_is_signed_as_a_src_key_with_the_source_cap(self):
        # 150 MB: over the 80 MB ready-video cap, under the 300 MB source cap.
        resp = self._sign([source_file("video/quicktime", 150 * MB)])

        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.data["data"]
        self.assertEqual(len(data["uploads"]), 1)
        key = data["uploads"][0]["key"]
        folder = f"users/{self.author.id}/posts/{data['temp_post_id']}/"
        self.assertTrue(key.startswith(folder), key)
        self.assertIn(".src.", key)
        self.assertTrue(key.endswith(".src.mov"), key)
        stem = key[len(folder):].split(".src.")[0]
        self.assertEqual(str(uuid.UUID(stem)), stem)
        # Signed like any PUT, content type bound.
        self.assertIn("X-Amz-Signature=", data["uploads"][0]["upload_url"])

    @override_settings(**PROCESSING_ON)
    def test_an_image_source_takes_the_source_image_cap(self):
        # 20 MB HEIC: refused as a ready image (5 MB), fine as a source.
        resp = self._sign([source_file("image/heic", 20 * MB)])

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(
            resp.data["data"]["uploads"][0]["key"].endswith(".src.heic")
        )

    @override_settings(**PROCESSING_ON)
    def test_a_source_over_its_cap_or_of_an_unknown_type_is_refused(self):
        cases = [
            (source_file("video/quicktime", 301 * MB), "size_bytes"),
            (source_file("image/heic", 26 * MB), "size_bytes"),
            (source_file("application/pdf", 1 * MB), "content_type"),
        ]
        for file, field in cases:
            with self.subTest(file=file):
                resp = self._sign([file])

                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertIn(field, self._errors(resp))

    @override_settings(**PROCESSING_ON)
    def test_a_source_needs_no_thumb_and_a_thumb_beside_one_is_a_stray(self):
        # A ready video must ship its poster; a source's is made by the worker.
        ready_alone = self._sign([READY_VIDEO])
        self.assertEqual(ready_alone.status_code, 400, ready_alone.data)
        self.assertEqual(
            ready_alone.data["message"],
            "A video upload requires exactly one thumbnail",
        )

        source_alone = self._sign([source_file("video/quicktime", 50 * MB)])
        self.assertEqual(source_alone.status_code, 200, source_alone.data)

        with_thumb = self._sign([source_file("video/quicktime", 50 * MB), THUMB])
        self.assertEqual(with_thumb.status_code, 400, with_thumb.data)
        self.assertEqual(
            with_thumb.data["message"],
            "A thumbnail must accompany the file it belongs to",
        )

    @override_settings(**PROCESSING_ON)
    def test_a_source_counts_as_the_image_or_video_it_is(self):
        video_source = source_file("video/quicktime", 50 * MB)
        image_source = source_file("image/heic", 4 * MB)
        cases = [
            ("posts", [video_source, image_source],
             "Send images or a video, not both"),
            ("posts", [video_source, READY_VIDEO, THUMB],
             "Only one video per upload"),
            ("posts", [image_source] * 11, "Invalid count (1-10 allowed)"),
            ("chat", [image_source, READY_IMAGE], "Only one image per upload"),
        ]
        for upload_type, files, message in cases:
            with self.subTest(upload_type=upload_type, message=message):
                resp = self._sign(files, upload_type=upload_type)

                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertEqual(resp.data["message"], message)

    @override_settings(**PROCESSING_ON)
    def test_sources_and_ready_files_mix_in_one_gallery_in_request_order(self):
        resp = self._sign([READY_IMAGE, THUMB, source_file("image/heic", 4 * MB)])

        self.assertEqual(resp.status_code, 200, resp.data)
        keys = [upload["key"] for upload in resp.data["data"]["uploads"]]
        self.assertTrue(keys[0].endswith(".webp"))
        self.assertNotIn(".src.", keys[0])
        self.assertTrue(keys[2].endswith(".src.heic"))
        # One batch folder for all three.
        self.assertEqual(len({key.rpartition("/")[0] for key in keys}), 1)

    @override_settings(**PROCESSING_ON)
    def test_highlights_sign_a_video_source_and_refuse_an_image_one(self):
        video = self._sign(
            [source_file("video/quicktime", 50 * MB)], upload_type="highlights"
        )
        self.assertEqual(video.status_code, 200, video.data)
        self.assertTrue(
            video.data["data"]["uploads"][0]["key"].startswith(
                f"users/{self.author.id}/highlights/"
            )
        )

        image = self._sign(
            [source_file("image/heic", 4 * MB)], upload_type="highlights"
        )
        self.assertEqual(image.status_code, 400, image.data)
        self.assertEqual(
            image.data["message"],
            "Images are not allowed for this upload type",
        )

    @override_settings(**PROCESSING_ON)
    def test_a_type_the_worker_does_not_serve_refuses_a_source(self):
        resp = self._sign(
            [source_file("image/heic", 4 * MB)], upload_type="profile"
        )

        self._assert_unavailable(resp)


class CapabilitiesTests(MediaTestCase):

    @override_settings(MEDIA_PROCESSING_ENABLED=False, CELERY_ENABLED=True)
    def test_off_while_the_flag_is_off(self):
        resp = self.client.get(CAPABILITIES_URL)

        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.data["data"]
        self.assertFalse(data["server_processing"])
        self.assertEqual(data["limits"]["source_video_bytes"], 300 * MB)
        self.assertEqual(data["limits"]["source_image_bytes"], 25 * MB)
        self.assertEqual(data["limits"]["max_duration_seconds"]["highlights"], 90)
        self.assertEqual(data["limits"]["max_duration_seconds"]["posts"], 300)

    @override_settings(**PROCESSING_ON)
    def test_on_with_the_flag_celery_and_an_unknown_heartbeat(self):
        resp = self.client.get(CAPABILITIES_URL)

        self.assertTrue(resp.data["data"]["server_processing"])


# =====================================================================
# Multipart create for a source
# =====================================================================

def fake_multipart_storage():
    storage = MagicMock()
    storage.create_multipart_upload.return_value = "upload-123"
    storage.presign_upload_part.side_effect = (
        lambda key, upload_id, number:
        f"https://r2.test/{key}?partNumber={number}&uploadId={upload_id}"
    )
    return storage


class MultipartSourceTests(MediaTestCase):

    def _source_key(self, ext="mov"):
        return f"{self._folder(self.author)}/{uuid.uuid4()}.src.{ext}"

    def _create(self, storage, **overrides):
        body = {
            "type": "posts",
            "key": self._source_key(),
            "content_type": "video/quicktime",
            "size_bytes": 150 * MB,
        }
        body.update(overrides)
        with patch(MULTIPART_STORAGE, return_value=storage):
            return self.client.post(MULTIPART_CREATE_URL, body, format="json")

    @override_settings(**PROCESSING_ON)
    def test_a_source_key_and_type_start_an_upload_with_the_source_cap(self):
        storage = fake_multipart_storage()
        key = self._source_key()

        # 150 MB is past the 80 MB ready-video cap: only a source gets this.
        resp = self._create(storage, key=key, size_bytes=150 * MB)

        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.data["data"]
        self.assertEqual(data["key"], key)
        self.assertEqual(data["public_url"], media_url(key))
        self.assertEqual(data["part_size"], MULTIPART_PART_SIZE)
        # 150 MB in 8 MB parts: 18 full + 1.
        self.assertEqual(len(data["parts"]), 19)
        storage.create_multipart_upload.assert_called_once_with(
            key, "video/quicktime"
        )

    @override_settings(**PROCESSING_ON)
    def test_the_key_must_carry_the_extension_its_content_type_is_stored_under(self):
        storage = fake_multipart_storage()

        resp = self._create(storage, key=self._source_key("mp4"))

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("key", self._errors(resp))
        storage.create_multipart_upload.assert_not_called()

    @override_settings(**PROCESSING_ON)
    def test_another_users_source_key_is_refused(self):
        storage = fake_multipart_storage()
        key = f"{self._folder(self.rival)}/{uuid.uuid4()}.src.mov"

        resp = self._create(storage, key=key)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("key", self._errors(resp))
        storage.create_multipart_upload.assert_not_called()

    @override_settings(**PROCESSING_ON)
    def test_a_source_over_the_source_cap_is_refused(self):
        storage = fake_multipart_storage()

        resp = self._create(storage, size_bytes=301 * MB)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("size_bytes", self._errors(resp))
        storage.create_multipart_upload.assert_not_called()

    @override_settings(MEDIA_PROCESSING_ENABLED=False, CELERY_ENABLED=True)
    def test_a_source_is_refused_while_processing_is_off(self):
        storage = fake_multipart_storage()

        resp = self._create(storage)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["data"]["code"], SERVER_PROCESSING_UNAVAILABLE)
        storage.create_multipart_upload.assert_not_called()


# =====================================================================
# POST /media/jobs  and  GET /media/jobs/<id>
# =====================================================================

@override_settings(**PROCESSING_ON)
class JobEndpointTestCase(MediaTestCase):
    """POST /media/jobs with R2's HEAD and the dispatch mocked."""

    def setUp(self):
        super().setUp()
        self.storage = MagicMock()
        self.storage.head_object.return_value = {
            "size_bytes": 50 * MB, "content_type": "video/quicktime",
        }
        self._patch(JOB_STORAGE, return_value=self.storage)
        self.enqueue = self._patch(JOB_ENQUEUE)

    def _key(self, owner=None, ext="mov", upload_type="posts"):
        folder = self._folder(owner or self.author, upload_type)
        return f"{folder}/{uuid.uuid4()}.src.{ext}"

    def _post(self, key, purpose="posts", upload_type="posts", headers=None,
              **extra):
        body = {"key": key, "purpose": purpose, "upload_type": upload_type,
                **extra}
        return self.client.post(JOBS_URL, body, format="json",
                                **(headers or {}))


class MediaJobCreateTests(JobEndpointTestCase):

    def test_a_source_is_queued_and_dispatched_with_skip(self):
        key = self._key()

        resp = self._post(key)

        self.assertEqual(resp.status_code, 201, resp.data)
        payload = resp.data["data"]["job"]
        job = MediaJob.objects.get()
        self.assertEqual(payload["id"], str(job.id))
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["kind"], "video")
        self.assertEqual(payload["purpose"], "posts")
        self.assertIsNone(payload["error_code"])
        # Kind and size from R2's stored copy, not the client.
        self.assertEqual(job.kind, VIDEO)
        self.assertEqual(job.source_content_type, "video/quicktime")
        self.assertEqual(job.source_size_bytes, 50 * MB)
        self.assertEqual(job.owner_user, self.author)
        self.assertIsNone(job.owner_org)
        self.assertEqual(job.created_by, self.author)
        self.storage.head_object.assert_called_once_with(key)

        # Never inline: a web dyno has no ffmpeg.
        self.enqueue.assert_called_once()
        call = self.enqueue.call_args
        self.assertIs(call.args[0], process_media_job)
        self.assertEqual(call.args[1], (str(job.id),))
        self.assertEqual(call.kwargs["fallback"], "skip")

    def test_it_is_idempotent_on_the_source_key(self):
        key = self._key()

        first = self._post(key)
        second = self._post(key)

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(
            first.data["data"]["job"]["id"], second.data["data"]["job"]["id"]
        )
        self.assertEqual(MediaJob.objects.count(), 1)
        # The second post neither re-checks R2 nor re-dispatches.
        self.storage.head_object.assert_called_once()
        self.enqueue.assert_called_once()

    def test_another_actors_key_is_refused(self):
        resp = self._post(self._key(owner=self.rival))

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("key", self._errors(resp))
        self.storage.head_object.assert_not_called()
        self.enqueue.assert_not_called()
        self.assertFalse(MediaJob.objects.exists())

    def test_a_key_that_is_not_a_source_is_refused(self):
        ready_key = f"{self._folder(self.author)}/{uuid.uuid4()}.mp4"

        resp = self._post(ready_key)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("key", self._errors(resp))
        self.storage.head_object.assert_not_called()

    def test_a_missing_object_is_source_missing(self):
        self.storage.head_object.return_value = None

        resp = self._post(self._key())

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["data"]["code"], SOURCE_MISSING)
        self.assertFalse(MediaJob.objects.exists())
        self.enqueue.assert_not_called()

    def test_an_object_over_the_source_cap_is_source_too_large(self):
        # The PUT bound the type, not the length: R2's size is checked again.
        self.storage.head_object.return_value = {
            "size_bytes": 301 * MB, "content_type": "video/quicktime",
        }

        resp = self._post(self._key())

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["data"]["code"], SOURCE_TOO_LARGE)
        self.assertFalse(MediaJob.objects.exists())

    def test_an_object_of_no_source_type_is_unreadable(self):
        self.storage.head_object.return_value = {
            "size_bytes": 1 * MB, "content_type": "application/pdf",
        }

        # The extension is the fallback, and "pdf" is no source either.
        resp = self._post(self._key(ext="pdf"))

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["data"]["code"], SOURCE_UNREADABLE)

    def test_a_highlight_refuses_an_image_source_at_job_creation(self):
        # A highlight's source is SIGNED as "posts", so signing cannot see
        # the purpose — the job endpoint is where a photo is refused for it.
        self.storage.head_object.return_value = {
            "size_bytes": 4 * MB, "content_type": "image/heic",
        }

        resp = self._post(self._key(ext="heic"), purpose="highlights")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(
            resp.data["message"], "Images are not allowed for this upload type"
        )
        self.assertIn("purpose", self._errors(resp))
        self.assertFalse(MediaJob.objects.exists())

    def test_a_highlight_video_source_signed_as_posts_is_queued(self):
        resp = self._post(self._key(), purpose="highlights")

        self.assertEqual(resp.status_code, 201, resp.data)
        job = MediaJob.objects.get()
        self.assertEqual(job.purpose, "highlights")
        self.assertEqual(job.upload_type, "posts")

    @override_settings(MEDIA_PROCESSING_ENABLED=False)
    def test_refused_while_processing_is_off(self):
        resp = self._post(self._key())

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["data"]["code"], SERVER_PROCESSING_UNAVAILABLE)
        self.storage.head_object.assert_not_called()

    def test_an_org_actor_queues_a_job_owned_by_the_org(self):
        key = self._key(owner=self.org)

        resp = self._post(key, headers=self._org_headers())

        self.assertEqual(resp.status_code, 201, resp.data)
        job = MediaJob.objects.get()
        self.assertEqual(job.owner_org, self.org)
        self.assertIsNone(job.owner_user)
        self.assertEqual(job.created_by, self.author)

        # Owner only: the org sees it, the same human acting personally not.
        as_org = self.client.get(f"{JOBS_URL}/{job.id}", **self._org_headers())
        self.assertEqual(as_org.status_code, 200, as_org.data)
        as_user = self.client.get(f"{JOBS_URL}/{job.id}")
        self.assertEqual(as_user.status_code, 404, as_user.data)

    def test_job_detail_is_the_owners_alone(self):
        job = self._job(status=READY)

        mine = self.client.get(f"{JOBS_URL}/{job.id}")
        self.assertEqual(mine.status_code, 200, mine.data)
        payload = mine.data["data"]["job"]
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["file_url"], job.output_url)
        self.assertEqual(payload["public_id"], job.output_key)
        self.assertEqual(payload["thumbnail_url"], job.thumbnail_url)
        self.assertNotIn("error_detail", payload)

        self.client.force_authenticate(user=self.rival)
        theirs = self.client.get(f"{JOBS_URL}/{job.id}")
        self.assertEqual(theirs.status_code, 404, theirs.data)


# =====================================================================
# The worker
# =====================================================================

class FakeFfmpeg:
    """
    Stands in for apps.media.processing.video._run — the one place the
    converter starts ffprobe / ffmpeg. ffprobe answers from canned JSON (the
    source, or the output it "made"); ffmpeg writes a file at its last
    argument, which is where the converter puts the output path.
    """

    def __init__(self, *, source_duration, fps="60/1", audio=True,
                 out_size=(1280, 720), out_duration=None,
                 video_bytes=4 * MB, poster_bytes=60_000):
        streams = [{
            "index": 0, "codec_type": "video", "width": 1920, "height": 1080,
            "avg_frame_rate": fps,
        }]
        if audio:
            streams.append({"index": 1, "codec_type": "audio"})
        self.source_info = {
            "streams": streams,
            "format": {"duration": str(source_duration)},
        }
        self.output_info = {
            "streams": [{
                "index": 0, "codec_type": "video",
                "width": out_size[0], "height": out_size[1],
            }],
            "format": {
                "duration": str(out_duration if out_duration is not None
                                else source_duration),
            },
        }
        self.video_bytes = video_bytes
        self.poster_bytes = poster_bytes
        self.calls = []

    def __call__(self, cmd, deadline, *, limit=None):
        self.calls.append(list(cmd))
        target = cmd[-1]

        if cmd[0] == settings.FFPROBE_BIN:
            info = self.source_info \
                if os.path.basename(target).startswith("source.") \
                else self.output_info
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps(info).encode(), b""
            )

        size = self.video_bytes if target.endswith(".mp4") \
            else self.poster_bytes
        with open(target, "wb") as handle:
            handle.write(b"\0" * size)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    @property
    def encodes(self):
        return [cmd for cmd in self.calls
                if cmd[0] == settings.FFMPEG_BIN and cmd[-1].endswith(".mp4")]


class WorkerTestCase(MediaTestCase):
    """
    MediaProcessingService.run against a mocked bucket. The converters run
    for real; for video, ffmpeg/ffprobe are FakeFfmpeg (the real-ffmpeg tests
    run the binaries when they are installed).
    """

    def setUp(self):
        super().setUp()
        self.work_dir = tempfile.mkdtemp(prefix="media-tests-")
        self.addCleanup(shutil.rmtree, self.work_dir, True)
        work_dir_override = override_settings(MEDIA_WORK_DIR=self.work_dir)
        work_dir_override.enable()
        self.addCleanup(work_dir_override.disable)

        self.uploads = {}
        self.storage = MagicMock()
        self.storage.upload_file.side_effect = self._record_upload
        self._patch(WORKER_STORAGE, return_value=self.storage)
        self.enqueue = self._patch(WORKER_ENQUEUE)

        self.finished = []
        media_job_finished.connect(
            self._on_finished, dispatch_uid="media-tests-finished"
        )
        self.addCleanup(
            media_job_finished.disconnect,
            dispatch_uid="media-tests-finished",
        )

    # ── plumbing ─────────────────────────────────────────────────

    def _on_finished(self, sender, job, **kwargs):
        self.finished.append((job.id, job.status))

    def _record_upload(self, path, key, content_type, cache_control):
        with open(path, "rb") as handle:
            data = handle.read()
        self.uploads[key] = {
            "data": data,
            "content_type": content_type,
            "cache_control": cache_control,
        }

    def _serve_source(self, data, content_type):
        self.storage.head_object.return_value = {
            "size_bytes": len(data), "content_type": content_type,
        }

        def download(key, path):
            with open(path, "wb") as handle:
                handle.write(data)

        self.storage.download_to_file.side_effect = download

    def _uploaded_image(self, key):
        return Image.open(io.BytesIO(self.uploads[key]["data"]))

    def _run(self, job):
        MediaProcessingService.run(str(job.id))
        job.refresh_from_db()
        return job

    def _assert_finish_sent(self, job, status):
        self.job_send.assert_called_once()
        group, message = self.job_send.call_args.args
        self.assertEqual(group, f"user_notifications_{self.author.id}")
        self.assertEqual(message["type"], "notification_message")
        self.assertEqual(message["notification_type"], "media_job_updated")
        self.assertEqual(message["job"]["id"], str(job.id))
        self.assertEqual(message["job"]["status"], status)
        self.assertEqual(self.finished, [(job.id, status)])


class MediaWorkerTests(WorkerTestCase):

    # ── video ────────────────────────────────────────────────────

    def test_a_video_past_its_cap_fails_video_too_long(self):
        job = self._job(status=QUEUED)
        self._serve_source(b"\0" * 1024, "video/quicktime")
        ffmpeg = FakeFfmpeg(source_duration=400.0)

        with patch(FFMPEG_ON_PATH, return_value=True), \
                patch(FFMPEG_RUN, side_effect=ffmpeg):
            job = self._run(job)

        self.assertEqual(job.status, FAILED)
        self.assertEqual(job.error_code, VIDEO_TOO_LONG)
        self.assertIsNotNone(job.finished_at)
        # Refused on the probe: nothing encoded, uploaded or deleted.
        self.assertEqual(ffmpeg.encodes, [])
        self.storage.upload_file.assert_not_called()
        self.enqueue.assert_not_called()
        self.assertIsNone(job.source_deleted_at)
        self._assert_finish_sent(job, FAILED)
        self.assertEqual(
            self.job_send.call_args.args[1]["job"]["error_message"],
            TOO_LONG_FOR_POSTS,
        )

    def test_a_highlight_past_90_seconds_names_its_own_cap(self):
        job = self._job(status=QUEUED, purpose="highlights")
        self._serve_source(b"\0" * 1024, "video/quicktime")

        with patch(FFMPEG_ON_PATH, return_value=True), \
                patch(FFMPEG_RUN, side_effect=FakeFfmpeg(source_duration=95.0)):
            job = self._run(job)

        self.assertEqual(job.error_code, VIDEO_TOO_LONG)
        self.assertEqual(
            error_message(job.error_code, job.purpose),
            "This video is longer than 90 seconds — trim it and try again",
        )

    def test_a_ready_video_is_written_beside_its_source_and_announced(self):
        job = self._job(status=QUEUED)
        source_key = job.source_key
        self._serve_source(b"\0" * 2048, "video/quicktime")
        ffmpeg = FakeFfmpeg(source_duration=12.4, video_bytes=3 * MB)

        with patch(FFMPEG_ON_PATH, return_value=True), \
                patch(FFMPEG_RUN, side_effect=ffmpeg):
            job = self._run(job)

        self.assertEqual(job.status, READY, job.error_detail)
        self.assertEqual(job.attempts, 1)

        # Outputs are fresh uuid keys in the SOURCE's folder.
        folder = source_key.rpartition("/")[0]
        self.assertEqual(job.output_key.rpartition("/")[0], folder)
        self.assertEqual(job.thumbnail_key.rpartition("/")[0], folder)
        self.assertTrue(job.output_key.endswith(".mp4"))
        self.assertTrue(job.thumbnail_key.endswith(".jpg"))
        self.assertNotIn(".src.", job.output_key)
        self.assertEqual(job.output_url, media_url(job.output_key))
        self.assertEqual(job.thumbnail_url, media_url(job.thumbnail_key))

        # Measured from the OUTPUT.
        self.assertEqual((job.width, job.height), (1280, 720))
        self.assertEqual(job.duration, 12)
        self.assertEqual(job.size_bytes, 3 * MB)

        # Both uploaded, immutable.
        self.assertEqual(
            self.uploads[job.output_key]["content_type"], "video/mp4"
        )
        self.assertEqual(
            self.uploads[job.thumbnail_key]["content_type"], "image/jpeg"
        )
        for upload in self.uploads.values():
            self.assertEqual(upload["cache_control"], OUTPUT_CACHE_CONTROL)

        # The client's target: H.264, metadata dropped, ≤30 fps, AAC.
        self.assertEqual(len(ffmpeg.encodes), 1)
        encode = ffmpeg.encodes[0]
        self.assertEqual(encode[encode.index("-c:v") + 1], "libx264")
        self.assertEqual(encode[encode.index("-c:a") + 1], "aac")
        self.assertEqual(encode[encode.index("-map_metadata") + 1], "-1")
        self.assertIn("fps=30", encode[encode.index("-vf") + 1])
        self.assertIn("+faststart", encode)

        # The source is deleted once ready.
        self.enqueue.assert_called_once_with(delete_keys, ([source_key],))
        self.assertIsNotNone(job.source_deleted_at)

        self._assert_finish_sent(job, READY)
        self.assertEqual(
            self.job_send.call_args.args[1]["job"]["file_url"], job.output_url
        )

    @skipUnless(HAS_FFMPEG, "ffmpeg / ffprobe are not installed")
    def test_real_ffmpeg_converts_a_clip_to_the_client_target(self):
        clip = os.path.join(self.work_dir, "clip.mov")
        subprocess.run(
            [
                shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
                "-y",
                "-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "mpeg4", "-c:a", "aac", "-shortest", clip,
            ],
            check=True, timeout=120,
        )
        with open(clip, "rb") as handle:
            self._serve_source(handle.read(), "video/quicktime")
        job = self._job(status=QUEUED)

        job = self._run(job)

        self.assertEqual(job.status, READY, job.error_detail)
        self.assertEqual((job.width, job.height), (640, 360))
        self.assertEqual(job.duration, 2)

        output = os.path.join(self.work_dir, "uploaded.mp4")
        with open(output, "wb") as handle:
            handle.write(self.uploads[job.output_key]["data"])
        probed = json.loads(subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-print_format", "json",
             "-show_streams", output],
            check=True, capture_output=True, timeout=60,
        ).stdout)
        streams = {stream["codec_type"]: stream for stream in probed["streams"]}
        self.assertEqual(streams["video"]["codec_name"], "h264")
        self.assertEqual(streams["video"]["pix_fmt"], "yuv420p")
        self.assertEqual(streams["audio"]["codec_name"], "aac")

        poster = self._uploaded_image(job.thumbnail_key)
        self.assertEqual(poster.format, "JPEG")
        self.assertLessEqual(max(poster.size), 1080)
        self.assertLessEqual(
            len(self.uploads[job.thumbnail_key]["data"]), 900 * 1024
        )

    # ── image ────────────────────────────────────────────────────

    def test_a_photo_becomes_a_2560_jpeg_and_a_640_thumb(self):
        job = self._job(status=QUEUED, kind=IMAGE, content_type="image/jpeg")
        self._serve_source(jpeg_bytes(3000, 2000), "image/jpeg")

        job = self._run(job)

        self.assertEqual(job.status, READY, job.error_detail)
        self.assertTrue(job.output_key.endswith(".jpg"))
        self.assertEqual(
            job.output_key.rpartition("/")[0],
            job.source_key.rpartition("/")[0],
        )

        full = self._uploaded_image(job.output_key)
        thumb = self._uploaded_image(job.thumbnail_key)
        self.assertEqual(full.format, "JPEG")
        self.assertEqual(max(full.size), 2560)
        self.assertEqual(max(thumb.size), 640)
        self.assertEqual((job.width, job.height), full.size)
        self.assertIsNone(job.duration)
        self.assertEqual(
            job.size_bytes, len(self.uploads[job.output_key]["data"])
        )
        self.assertLessEqual(job.size_bytes, 5 * MB)
        self.assertEqual(
            self.uploads[job.output_key]["content_type"], "image/jpeg"
        )
        self.enqueue.assert_called_once_with(delete_keys, ([job.source_key],))
        self._assert_finish_sent(job, READY)

    def test_a_photo_is_turned_upright_and_loses_its_exif(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation: rotate 90° CW to display.
        job = self._job(status=QUEUED, kind=IMAGE, content_type="image/jpeg")
        self._serve_source(
            jpeg_bytes(3000, 2000, exif=exif.tobytes()), "image/jpeg"
        )

        job = self._run(job)

        self.assertEqual(job.status, READY, job.error_detail)
        full = self._uploaded_image(job.output_key)
        # Landscape pixels, portrait display → portrait output.
        self.assertGreater(full.height, full.width)
        self.assertEqual(full.height, 2560)
        self.assertNotIn(0x0112, full.getexif())

    def test_a_heic_photo_is_converted(self):
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            buffer = io.BytesIO()
            Image.new("RGB", (1200, 800), (200, 40, 40)).save(
                buffer, format="HEIF"
            )
        except Exception as exc:  # no HEVC encoder in this pillow-heif build
            self.skipTest(f"pillow-heif cannot write HEIC here: {exc}")
        job = self._job(status=QUEUED, kind=IMAGE, content_type="image/heic")
        self._serve_source(buffer.getvalue(), "image/heic")

        job = self._run(job)

        self.assertEqual(job.status, READY, job.error_detail)
        full = self._uploaded_image(job.output_key)
        self.assertEqual(full.format, "JPEG")
        self.assertEqual(full.size, (1200, 800))  # never upscaled
        self.assertEqual(max(self._uploaded_image(job.thumbnail_key).size), 640)

    def test_a_file_pillow_cannot_read_fails_source_unreadable(self):
        job = self._job(status=QUEUED, kind=IMAGE, content_type="image/jpeg")
        self._serve_source(b"definitely not a jpeg", "image/jpeg")

        job = self._run(job)

        self.assertEqual(job.status, FAILED)
        self.assertEqual(job.error_code, SOURCE_UNREADABLE)
        self.storage.upload_file.assert_not_called()
        self._assert_finish_sent(job, FAILED)

    # ── the gates around the work ────────────────────────────────

    def test_a_source_gone_from_the_bucket_fails_source_missing(self):
        job = self._job(status=QUEUED, kind=IMAGE, content_type="image/jpeg")
        self.storage.head_object.return_value = None

        job = self._run(job)

        self.assertEqual(job.error_code, SOURCE_MISSING)
        self.storage.download_to_file.assert_not_called()

    def test_a_failed_download_hands_the_job_back_and_raises_for_a_retry(self):
        job = self._job(status=QUEUED, kind=IMAGE, content_type="image/jpeg")
        self._serve_source(jpeg_bytes(100, 100), "image/jpeg")
        self.storage.download_to_file.side_effect = EndpointConnectionError(
            endpoint_url="https://r2.test"
        )

        with self.assertRaises(SourceFetchError):
            MediaProcessingService.run(str(job.id))

        job.refresh_from_db()
        self.assertEqual(job.status, QUEUED)
        self.job_send.assert_not_called()
        self.assertEqual(self.finished, [])

    def test_a_finished_job_is_left_alone(self):
        job = self._job(status=READY)

        MediaProcessingService.run(str(job.id))

        self.storage.head_object.assert_not_called()
        self.job_send.assert_not_called()


# =====================================================================
# The sweeper
# =====================================================================

class MediaSweepTests(MediaTestCase):

    def setUp(self):
        super().setUp()
        self.enqueue = self._patch(SWEEP_ENQUEUE)
        self.storage = MagicMock()
        self._patch(SWEEP_STORAGE, return_value=self.storage)

    def _age(self, job, **fields):
        MediaJob.objects.filter(id=job.id).update(**fields)
        job.refresh_from_db()
        return job

    def _dispatched_ids(self):
        ids = []
        for call in self.enqueue.call_args_list:
            if call.args[0] is process_media_job:
                self.assertEqual(call.kwargs["fallback"], "skip")
                ids.extend(call.args[1])
        return ids

    def test_a_stale_queued_job_is_dispatched_again(self):
        stale = self._age(
            self._job(status=QUEUED),
            updated_at=timezone.now() - timedelta(minutes=5),
        )
        self._job(status=QUEUED)  # just queued: left alone
        before = stale.updated_at

        result = MediaSweepService.sweep()

        self.assertEqual(result["redispatched"], 1)
        self.assertEqual(self._dispatched_ids(), [str(stale.id)])
        stale.refresh_from_db()
        # Bumped, so it is re-sent once per two minutes, not every sweep.
        self.assertGreater(stale.updated_at, before)

        self.enqueue.reset_mock()
        MediaSweepService.sweep()
        self.assertEqual(self._dispatched_ids(), [])

    def test_a_dead_run_is_requeued_or_timed_out_once_out_of_attempts(self):
        long_ago = timezone.now() - timedelta(
            seconds=settings.MEDIA_JOB_TIME_LIMIT + STUCK_GRACE_SECONDS + 60
        )
        retry = self._age(
            self._job(status=PROCESSING), started_at=long_ago, attempts=1
        )
        exhausted = self._age(
            self._job(status=PROCESSING), started_at=long_ago, attempts=3
        )
        live = self._job(status=PROCESSING)  # started just now

        result = MediaSweepService.sweep()

        self.assertEqual(result["requeued"], 1)
        self.assertEqual(result["timed_out"], 1)

        retry.refresh_from_db()
        self.assertEqual(retry.status, QUEUED)
        self.assertEqual(self._dispatched_ids(), [str(retry.id)])

        exhausted.refresh_from_db()
        self.assertEqual(exhausted.status, FAILED)
        self.assertEqual(exhausted.error_code, PROCESSING_TIMEOUT)
        self.assertIsNotNone(exhausted.finished_at)
        # finish(): the owner is told.
        self.job_send.assert_called_once()
        message = self.job_send.call_args.args[1]
        self.assertEqual(message["notification_type"], "media_job_updated")
        self.assertEqual(message["job"]["id"], str(exhausted.id))
        self.assertEqual(message["job"]["error_code"], PROCESSING_TIMEOUT)

        live.refresh_from_db()
        self.assertEqual(live.status, PROCESSING)

    def test_a_day_old_failed_source_is_deleted_and_asked_about_once(self):
        failed = self._age(
            self._job(status=FAILED),
            finished_at=timezone.now() - timedelta(hours=25),
        )
        self._job(status=FAILED)  # failed just now: kept for a look
        self.storage.head_object.return_value = {
            "size_bytes": 1, "content_type": "video/quicktime",
        }

        result = MediaSweepService.sweep()

        self.assertEqual(result["sources_deleted"], 1)
        self.storage.head_object.assert_called_once_with(failed.source_key)
        self.enqueue.assert_any_call(delete_keys, ([failed.source_key],))
        failed.refresh_from_db()
        self.assertIsNotNone(failed.source_deleted_at)

        self.storage.reset_mock()
        MediaSweepService.sweep()
        self.storage.head_object.assert_not_called()

    def test_a_publication_whose_signal_was_missed_is_published(self):
        job = self._job(status=PROCESSING)
        publication = self._held_post(job, content="late one")
        self._make_ready(job)  # no finish(): the signal was lost

        result = MediaSweepService.sweep()

        self.assertEqual(result["publications_settled"], 1)
        publication.refresh_from_db()
        self.assertEqual(publication.status, PendingPublication.Status.PUBLISHED)
        self.assertTrue(
            Post.objects.filter(id=publication.result_id, content="late one")
            .exists()
        )

    def test_a_publication_waiting_two_hours_times_out_and_tells_the_author(self):
        job = self._job(status=PROCESSING)
        publication = self._held_post(job)
        PendingPublication.objects.filter(id=publication.id).update(
            created_at=timezone.now() - timedelta(hours=3)
        )

        result = MediaSweepService.sweep()

        self.assertEqual(result["publications_expired"], 1)
        publication.refresh_from_db()
        self.assertEqual(publication.status, PendingPublication.Status.FAILED)
        self.assertEqual(publication.error_code, PROCESSING_TIMEOUT)
        self.assertTrue(
            Notification.objects.filter(
                type=Notification.Type.PUBLICATION_FAILED,
                recipient_user=self.author,
            ).exists()
        )


# =====================================================================
# Posts: 200 when ready, 202 while converting, published by the server
# =====================================================================

class PostPublicationTests(MediaTestCase):

    def _create(self, body, org=None):
        headers = self._org_headers(org) if org is not None else {}
        return self.client.post(POST_CREATE_URL, body, format="json", **headers)

    def _job_item(self, job, order=0):
        return {"media_job_id": str(job.id), "order": order}

    def test_a_request_without_jobs_is_answered_exactly_as_before(self):
        # Refactor regression: CreatePostAPIView's body now lives in
        # PostService.validate_create_payload / create_post.
        folder = self._folder(self.author)
        video_key = f"{folder}/{uuid.uuid4()}.mp4"
        thumb_key = f"{folder}/{uuid.uuid4()}.jpg"

        resp = self._create({
            "content": "match day",
            "media": [{
                "file_url": media_url(video_key),
                "public_id": video_key,
                "thumbnail_url": media_url(thumb_key),
                "media_type": "video",
                "duration": 20, "width": 1280, "height": 720,
                "size_bytes": 5 * MB,
                "order": 0,
            }],
        })

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["message"], "Post created successfully")
        post = Post.objects.get()
        self.assertEqual(resp.data["data"], {"post_id": str(post.id)})
        media = PostMedia.objects.get(post=post)
        self.assertEqual(media.file_url, media_url(video_key))
        self.assertEqual(media.public_id, video_key)
        self.assertEqual(media.thumbnail_url, media_url(thumb_key))
        self.assertEqual(media.duration, 20)
        self.assertFalse(PendingPublication.objects.exists())

    def test_a_text_post_is_answered_exactly_as_before(self):
        resp = self._create({"content": "just words"})

        self.assertEqual(resp.status_code, 200, resp.data)
        post = Post.objects.get()
        self.assertEqual(resp.data["data"], {"post_id": str(post.id)})

    def test_a_ready_job_is_posted_in_the_request_from_its_outputs(self):
        job = self._job(status=READY, duration=14)

        resp = self._create({"content": "ready", "media": [self._job_item(job)]})

        self.assertEqual(resp.status_code, 200, resp.data)
        post = Post.objects.get()
        self.assertEqual(resp.data["data"], {"post_id": str(post.id)})
        media = PostMedia.objects.get(post=post)
        self.assertEqual(media.file_url, job.output_url)
        self.assertEqual(media.public_id, job.output_key)
        self.assertEqual(media.thumbnail_url, job.thumbnail_url)
        self.assertEqual(media.media_type, PostMedia.MediaType.VIDEO)
        self.assertEqual((media.width, media.height), (job.width, job.height))
        self.assertEqual(media.duration, 14)
        self.assertEqual(media.size_bytes, job.size_bytes)
        self.assertFalse(PendingPublication.objects.exists())

    def test_a_converting_job_is_held_with_a_202_and_no_post(self):
        job = self._job(status=PROCESSING)

        resp = self._create(
            {"content": "wait for it", "media": [self._job_item(job)]}
        )

        self.assertEqual(resp.status_code, 202, resp.data)
        self.assertEqual(resp.data["message"], "Your post is processing")
        pending = resp.data["data"]["pending_publication"]
        self.assertEqual(pending["target"], "post")
        self.assertEqual(pending["status"], "processing")
        self.assertIsNone(pending["result_id"])
        self.assertEqual(pending["jobs"], [
            {"id": str(job.id), "status": "processing"}
        ])
        self.assertEqual(pending["preview"]["content"], "wait for it")
        self.assertEqual(pending["preview"]["media_type"], "video")

        self.assertFalse(Post.objects.exists())
        publication = PendingPublication.objects.get()
        self.assertEqual(pending["id"], str(publication.id))
        self.assertEqual(publication.owner_user, self.author)
        self.assertEqual(publication.created_by, self.author)
        self.assertEqual(list(publication.jobs.all()), [job])
        self.assertEqual(publication.payload["content"], "wait for it")
        self.assertEqual(publication.payload["media"], [self._job_item(job)])

        # Announced on the author's socket.
        group, message = self.publication_send.call_args.args
        self.assertEqual(group, f"user_notifications_{self.author.id}")
        self.assertEqual(message["notification_type"], "publication_updated")
        self.assertEqual(message["publication"]["id"], str(publication.id))

    def test_a_ready_job_publishes_the_post_from_its_outputs_with_mentions(self):
        rahul = self._user("rahul10")
        job = self._job(status=PROCESSING)
        resp = self._create({
            "content": "Screamer for @rahul10 #golazo",
            "media": [self._job_item(job)],
        })
        self.assertEqual(resp.status_code, 202, resp.data)
        publication = PendingPublication.objects.get()
        self._make_ready(job, duration=21)

        with self.captureOnCommitCallbacks(execute=True):
            status = PublicationService.try_publish(publication)

        self.assertEqual(status, PendingPublication.Status.PUBLISHED)
        publication.refresh_from_db()
        post = Post.objects.get()
        self.assertEqual(publication.result_id, post.id)
        self.assertIsNotNone(publication.published_at)
        self.assertEqual(post.author_user, self.author)
        self.assertEqual(post.content, "Screamer for @rahul10 #golazo")

        media = PostMedia.objects.get(post=post)
        self.assertEqual(media.file_url, job.output_url)
        self.assertEqual(media.public_id, job.output_key)
        self.assertEqual(media.thumbnail_url, job.thumbnail_url)
        self.assertEqual(media.duration, 21)

        # Today's create path: mention + hashtag rows, mention notification.
        self.assertTrue(
            PostMention.objects.filter(post=post, mentioned_user=rahul).exists()
        )
        self.assertTrue(
            PostHashtag.objects.filter(post=post, hashtag__name="golazo")
            .exists()
        )
        self.assertEqual(
            Notification.objects.filter(
                type=Notification.Type.MENTION, recipient_user=rahul,
                post=post,
            ).count(),
            1,
        )
        # A success is no notification of its own.
        self.assertFalse(Notification.objects.filter(
            type=Notification.Type.PUBLICATION_FAILED
        ).exists())

        # Settled: a second try (another job's signal, the sweeper) is a no-op.
        self.assertIsNone(PublicationService.try_publish(publication))
        self.assertEqual(Post.objects.count(), 1)

    def test_a_publication_still_waiting_on_a_job_does_nothing(self):
        job = self._job(status=PROCESSING)
        publication = self._held_post(job)

        self.assertIsNone(PublicationService.try_publish(publication))

        publication.refresh_from_db()
        self.assertEqual(publication.status, PendingPublication.Status.PROCESSING)
        self.assertFalse(Post.objects.exists())

    def test_a_failed_job_fails_the_publication_and_notifies_the_author(self):
        job = self._job(status=PROCESSING)
        self._create({"content": "doomed", "media": [self._job_item(job)]})
        publication = PendingPublication.objects.get()
        self._make_failed(job, VIDEO_TOO_LONG)

        status = PublicationService.try_publish(publication)

        self.assertEqual(status, PendingPublication.Status.FAILED)
        publication.refresh_from_db()
        self.assertEqual(publication.error_code, VIDEO_TOO_LONG)
        self.assertEqual(publication.error_message, TOO_LONG_FOR_POSTS)
        self.assertFalse(Post.objects.exists())

        notification = Notification.objects.get(
            type=Notification.Type.PUBLICATION_FAILED
        )
        self.assertEqual(notification.recipient_user, self.author)
        self.assertIsNone(notification.recipient_org)
        self.assertIsNone(notification.actor_user)
        self.assertIsNone(notification.actor_org)
        self.assertEqual(notification.data["publication_id"], str(publication.id))
        self.assertEqual(notification.data["target"], "post")
        self.assertEqual(notification.data["error_code"], VIDEO_TOO_LONG)
        self.assertEqual(notification.data["error_message"], TOO_LONG_FOR_POSTS)
        self.assertEqual(notification.data["owner_username"], "author")

        # The socket heard about the failure too.
        message = self.publication_send.call_args.args[1]
        self.assertEqual(message["publication"]["status"], "failed")

    def test_an_org_post_whose_author_left_the_org_is_not_published(self):
        job = self._job(self.org, status=PROCESSING)
        resp = self._create(
            {"content": "club news", "media": [self._job_item(job)]},
            org=self.org,
        )
        self.assertEqual(resp.status_code, 202, resp.data)
        publication = PendingPublication.objects.get()
        self.assertEqual(publication.owner_org, self.org)
        self.assertIsNone(publication.owner_user)
        self.assertEqual(publication.created_by, self.author)

        OrganizationMember.objects.filter(
            organization=self.org, user=self.author
        ).delete()
        self._make_ready(job)

        status = PublicationService.try_publish(publication)

        self.assertEqual(status, PendingPublication.Status.FAILED)
        publication.refresh_from_db()
        self.assertEqual(publication.error_code, ACTOR_NO_LONGER_VALID)
        self.assertEqual(publication.error_message, ACTOR_NO_LONGER_VALID_MESSAGE)
        self.assertFalse(Post.objects.exists())
        notification = Notification.objects.get(
            type=Notification.Type.PUBLICATION_FAILED
        )
        self.assertEqual(notification.recipient_org, self.org)
        self.assertIsNone(notification.recipient_user)

    def test_an_org_post_is_published_as_the_org_while_its_author_is_a_member(self):
        job = self._job(self.org, status=PROCESSING)
        self._create(
            {"content": "club news", "media": [self._job_item(job)]},
            org=self.org,
        )
        publication = PendingPublication.objects.get()
        self._make_ready(job)

        status = PublicationService.try_publish(publication)

        self.assertEqual(status, PendingPublication.Status.PUBLISHED)
        post = Post.objects.get()
        self.assertEqual(post.author_org, self.org)
        self.assertIsNone(post.author_user)

    def test_a_job_item_is_refused_like_any_bad_media(self):
        theirs = self._job(self.rival, status=READY)
        wrong_purpose = self._job(status=READY, purpose="highlights")
        cases = [
            ("another actor's job", str(theirs.id)),
            ("a job for another purpose", str(wrong_purpose.id)),
            ("a malformed id", "not-a-uuid"),
            ("an unknown id", str(uuid.uuid4())),
        ]
        for label, job_id in cases:
            with self.subTest(label):
                resp = self._create({
                    "content": "x",
                    "media": [{"media_job_id": job_id, "order": 0}],
                })

                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertEqual(resp.data["message"], "Invalid media at index 0")

        self.assertFalse(Post.objects.exists())
        self.assertFalse(PendingPublication.objects.exists())

    def test_a_failed_job_is_refused_with_its_own_sentence(self):
        job = self._job(status=FAILED, error_code=VIDEO_TOO_LONG)

        resp = self._create({"content": "x", "media": [self._job_item(job)]})

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["message"], TOO_LONG_FOR_POSTS)

    def test_the_mix_rule_holds_for_jobs_still_converting(self):
        image_job = self._job(status=PROCESSING, kind=IMAGE)
        video_job = self._job(status=PROCESSING, kind=VIDEO)

        resp = self._create({
            "content": "x",
            "media": [self._job_item(image_job, 0), self._job_item(video_job, 1)],
        })

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["message"], "Cannot mix images and video")
        self.assertFalse(PendingPublication.objects.exists())


# =====================================================================
# Highlights
# =====================================================================

class HighlightPublicationTests(MediaTestCase):

    def _create(self, job, title="worldie"):
        return self.client.post(
            HIGHLIGHT_CREATE_URL,
            {"media_job_id": str(job.id), "title": title},
            format="json",
        )

    def _highlight_job(self, **kwargs):
        # The client signs a highlight's source as "posts".
        return self._job(purpose="highlights", upload_type="posts", **kwargs)

    def test_a_ready_job_over_90_seconds_is_refused(self):
        job = self._highlight_job(status=READY, duration=95)

        resp = self._create(job)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(
            resp.data["message"],
            "Highlights can be at most 90 seconds long. This clip is 95 "
            "seconds.",
        )
        self.assertFalse(Highlight.objects.exists())
        self.assertFalse(PendingPublication.objects.exists())

    def test_a_ready_job_is_added_at_once_with_a_201(self):
        job = self._highlight_job(status=READY, duration=30)

        resp = self._create(job)

        self.assertEqual(resp.status_code, 201, resp.data)
        highlight = Highlight.objects.get()
        self.assertEqual(highlight.file_url, job.output_url)
        self.assertEqual(highlight.public_id, job.output_key)
        self.assertEqual(highlight.thumbnail_url, job.thumbnail_url)
        self.assertEqual(highlight.duration, 30)
        self.assertFalse(PendingPublication.objects.exists())

    def test_a_posts_job_is_not_a_highlight(self):
        job = self._job(status=READY, purpose="posts")

        resp = self._create(job)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["message"], "Invalid media.")

    def test_a_converting_job_is_held_and_added_when_the_job_finishes(self):
        job = self._highlight_job(status=PROCESSING)

        resp = self._create(job, title="worldie")

        self.assertEqual(resp.status_code, 202, resp.data)
        self.assertEqual(resp.data["message"], "Your highlight is processing")
        pending = resp.data["data"]["pending_publication"]
        self.assertEqual(pending["target"], "highlight")
        self.assertEqual(pending["preview"]["content"], "worldie")
        self.assertFalse(Highlight.objects.exists())
        publication = PendingPublication.objects.get()

        # What the worker does: outputs saved, then finish() — the socket
        # event and media_job_finished, whose receiver publishes the clip.
        self._make_ready(job, duration=30)
        MediaJobService.finish(job)

        publication.refresh_from_db()
        self.assertEqual(publication.status, PendingPublication.Status.PUBLISHED)
        highlight = Highlight.objects.get()
        self.assertEqual(publication.result_id, highlight.id)
        self.assertEqual(highlight.user, self.author)
        self.assertEqual(highlight.title, "worldie")
        self.assertEqual(highlight.file_url, job.output_url)
        self.assertEqual(highlight.public_id, job.output_key)
        self.assertEqual(highlight.thumbnail_url, job.thumbnail_url)
        self.assertEqual(highlight.duration, 30)

    def test_a_held_clip_that_comes_out_over_90_seconds_is_rejected(self):
        job = self._highlight_job(status=PROCESSING)
        self.assertEqual(self._create(job).status_code, 202)
        publication = PendingPublication.objects.get()

        self._make_ready(job, duration=120)
        MediaJobService.finish(job)

        publication.refresh_from_db()
        self.assertEqual(publication.status, PendingPublication.Status.FAILED)
        self.assertEqual(publication.error_code, PUBLISH_REJECTED)
        self.assertIn("at most 90 seconds", publication.error_message)
        self.assertFalse(Highlight.objects.exists())
        notification = Notification.objects.get(
            type=Notification.Type.PUBLICATION_FAILED
        )
        self.assertEqual(notification.data["target"], "highlight")


# =====================================================================
# /media/publications
# =====================================================================

class PublicationEndpointTests(MediaTestCase):

    def _ids(self, resp):
        self.assertEqual(resp.status_code, 200, resp.data)
        return {row["id"] for row in resp.data["data"]["publications"]}

    def _cancel(self, publication, **headers):
        return self.client.post(
            f"{PUBLICATIONS_URL}/{publication.id}/cancel", {}, format="json",
            **headers,
        )

    def _dismiss(self, publication, **headers):
        return self.client.post(
            f"{PUBLICATIONS_URL}/{publication.id}/dismiss", {}, format="json",
            **headers,
        )

    def test_the_list_is_the_current_actors_own(self):
        processing = self._publication()
        failed = self._publication(status=PendingPublication.Status.FAILED)
        published = self._publication(
            status=PendingPublication.Status.PUBLISHED
        )
        self._publication(self.rival)
        org_one = self._publication(self.org)

        self.assertEqual(
            self._ids(self.client.get(PUBLICATIONS_URL)),
            {str(processing.id), str(failed.id)},
        )
        self.assertEqual(
            self._ids(self.client.get(PUBLICATIONS_URL, {"status": "published"})),
            {str(published.id)},
        )
        self.assertEqual(
            self._ids(self.client.get(PUBLICATIONS_URL, **self._org_headers())),
            {str(org_one.id)},
        )

    def test_cancel_stops_a_held_post_from_publishing(self):
        job = self._job(status=PROCESSING)
        publication = self._held_post(job)

        resp = self._cancel(publication)

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["data"]["publication"]["status"], "cancelled")
        publication.refresh_from_db()
        self.assertEqual(publication.status, PendingPublication.Status.CANCELLED)
        # Again is fine.
        self.assertEqual(self._cancel(publication).status_code, 200)

        # The job finishing afterwards publishes nothing.
        self._make_ready(job)
        self.assertIsNone(PublicationService.try_publish(publication))
        self.assertFalse(Post.objects.exists())

    def test_cancel_is_refused_once_settled_and_hidden_from_others(self):
        published = self._publication(status=PendingPublication.Status.PUBLISHED)

        late = self._cancel(published)
        self.assertEqual(late.status_code, 400, late.data)
        self.assertEqual(late.data["data"]["code"], "publication_not_processing")

        theirs = self._publication(self.rival)
        self.assertEqual(self._cancel(theirs).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, PendingPublication.Status.PROCESSING)

        # The org's, from the personal actor, is somebody else's too.
        org_one = self._publication(self.org)
        self.assertEqual(self._cancel(org_one).status_code, 404)
        self.assertEqual(
            self._cancel(org_one, **self._org_headers()).status_code, 200
        )

    def test_dismiss_hides_a_failed_one_from_the_list(self):
        failed = self._publication(status=PendingPublication.Status.FAILED)

        resp = self._dismiss(failed)

        self.assertEqual(resp.status_code, 200, resp.data)
        failed.refresh_from_db()
        self.assertIsNotNone(failed.dismissed_at)
        self.assertEqual(self._ids(self.client.get(PUBLICATIONS_URL)), set())

    def test_dismiss_is_for_failed_ones_and_the_owners_alone(self):
        processing = self._publication()
        refused = self._dismiss(processing)
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertEqual(refused.data["data"]["code"], "publication_not_failed")

        theirs = self._publication(
            self.rival, status=PendingPublication.Status.FAILED
        )
        self.assertEqual(self._dismiss(theirs).status_code, 404)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.dismissed_at)


# =====================================================================
# The failure notification's deep link
# =====================================================================

class PublicationFailedDeepLinkTests(MediaTestCase):

    def _notify(self, owner, target):
        publication = self._publication(
            owner, status=PendingPublication.Status.FAILED, target=target
        )
        is_org = isinstance(owner, Organization)
        return NotificationService.publication_failed(
            recipient_user=None if is_org else owner,
            recipient_org=owner if is_org else None,
            publication=publication,
        )

    def test_a_users_post_opens_their_own_posts(self):
        notification = self._notify(self.author, PendingPublication.Target.POST)

        self.assertEqual(notification.data["owner_username"], "author")
        self.assertEqual(
            build_notification_url(notification), "/profile/author/posts"
        )

    def test_a_users_highlight_opens_the_highlights_screen(self):
        notification = self._notify(
            self.author, PendingPublication.Target.HIGHLIGHT
        )

        self.assertEqual(build_notification_url(notification), "/highlights/manage")

    def test_an_org_post_stays_in_the_org_admin_space(self):
        notification = self._notify(self.org, PendingPublication.Target.POST)

        self.assertEqual(
            build_notification_url(notification),
            f"/organization/admin/{self.org.id}/posts",
        )


# =====================================================================
# Fix round — B1: an original never attaches as a finished file
# =====================================================================

PROFILE_MEDIA_URL = "/user/update/profile/cover"
IN_MEMORY_CHANNELS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
}


def object_key(folder, ext, *, source=False):
    """A key in `folder`: `<uuid>.<ext>`, or `<uuid>.src.<ext>` for a source."""
    return f"{folder}/{uuid.uuid4()}" + (f".src.{ext}" if source else f".{ext}")


class SourceKeyNeverAttachesTests(MediaTestCase):
    """
    A `.src.` key sits under the actor's own prefix, so the ownership checks
    alone pass it. Every attach path refuses it — as the file and as the
    thumbnail — and a job's outputs (plain `<uuid>.<ext>`) attach as before.
    """

    def _post_media(self, file_key, thumb_key, media_type="video"):
        return self.client.post(POST_CREATE_URL, {
            "content": "raw upload",
            "media": [{
                "file_url": media_url(file_key),
                "public_id": file_key,
                "thumbnail_url": media_url(thumb_key),
                "media_type": media_type,
                "order": 0,
            }],
        }, format="json")

    def test_a_jobs_outputs_are_never_source_keys(self):
        source_key = self._job().source_key
        self.assertTrue(is_source_key(source_key))
        for extension in (".mp4", ".jpg"):
            self.assertFalse(is_source_key(sibling_key(source_key, extension)))

    def test_posts_refuse_a_source_file_and_a_source_thumbnail(self):
        folder = self._folder(self.author)
        cases = {
            "video file": (object_key(folder, "mp4", source=True),
                           object_key(folder, "jpg"), "video"),
            "image file": (object_key(folder, "jpg", source=True),
                           object_key(folder, "jpg"), "image"),
            "thumbnail": (object_key(folder, "mp4"),
                          object_key(folder, "jpg", source=True), "video"),
        }
        for label, (file_key, thumb_key, media_type) in cases.items():
            with self.subTest(label):
                resp = self._post_media(file_key, thumb_key, media_type)

                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertEqual(resp.data["error"], SOURCE_NOT_PROCESSED_MESSAGE)

        self.assertFalse(Post.objects.exists())

    def test_posts_still_attach_a_jobs_outputs(self):
        job = self._job(status=READY)

        resp = self.client.post(POST_CREATE_URL, {
            "content": "converted",
            "media": [{"media_job_id": str(job.id), "order": 0}],
        }, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(PostMedia.objects.get().public_id, job.output_key)

    def test_highlights_refuse_a_source_file_and_a_source_thumbnail(self):
        folder = self._folder(self.author, "highlights")
        cases = {
            "file": (object_key(folder, "mp4", source=True),
                     object_key(folder, "jpg")),
            "thumbnail": (object_key(folder, "mp4"),
                          object_key(folder, "jpg", source=True)),
        }
        for label, (file_key, thumb_key) in cases.items():
            with self.subTest(label):
                resp = self.client.post(HIGHLIGHT_CREATE_URL, {
                    "file_url": media_url(file_key),
                    "public_id": file_key,
                    "thumbnail_url": media_url(thumb_key),
                }, format="json")

                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertIn(SOURCE_NOT_PROCESSED_MESSAGE, resp.data["message"])

        self.assertFalse(Highlight.objects.exists())

    def test_a_recruitment_refuses_a_source_file_and_a_source_thumbnail(self):
        actor = Actor(
            actor_type="organization",
            organization=self.org,
            organization_member=OrganizationMember.objects.get(
                organization=self.org, user=self.author
            ),
        )
        folder = self._folder(self.org, "recruitments")

        source_video = object_key(folder, "mp4", source=True)
        with self.assertRaises(ValidationError) as refused:
            RecruitmentService._validate_media(actor, [{
                "file_url": media_url(source_video),
                "public_id": source_video,
                "media_type": "video",
            }])
        self.assertEqual(
            str(refused.exception.detail[0]),
            f"media[0]: {SOURCE_NOT_PROCESSED_MESSAGE}",
        )

        # A bad thumbnail is refused in the recruitment's own words.
        plain_video = object_key(folder, "mp4")
        with self.assertRaises(ValidationError) as refused:
            RecruitmentService._validate_media(actor, [{
                "file_url": media_url(plain_video),
                "public_id": plain_video,
                "media_type": "video",
                "thumbnail_url": media_url(
                    object_key(folder, "jpg", source=True)
                ),
            }])
        self.assertEqual(
            str(refused.exception.detail[0]),
            "media[0]: invalid thumbnail source",
        )

        # A recruitments job's outputs pass.
        job = self._job(
            self.org, status=READY, purpose="recruitments",
            upload_type="recruitments",
        )
        RecruitmentService._validate_media(actor, [{
            "file_url": job.output_url,
            "public_id": job.output_key,
            "media_type": "video",
            "thumbnail_url": job.thumbnail_url,
        }])

    def test_a_profile_photo_refuses_a_source_key(self):
        key = object_key(self._folder(self.author), "jpg", source=True)

        resp = self.client.post(PROFILE_MEDIA_URL, {
            "profile_photo": media_url(key),
            "profile_photo_public_id": key,
        }, format="json")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["error"], SOURCE_NOT_PROCESSED_MESSAGE)
        self.author.profile.refresh_from_db()
        self.assertEqual(self.author.profile.profile_photo_public_id or "", "")

    def test_a_profile_photo_still_takes_a_jobs_output(self):
        job = self._job(status=READY, kind=IMAGE)

        resp = self.client.post(PROFILE_MEDIA_URL, {
            "profile_photo": job.output_url,
            "profile_photo_public_id": job.output_key,
        }, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.author.profile.refresh_from_db()
        self.assertEqual(
            self.author.profile.profile_photo_public_id, job.output_key
        )


@override_settings(CHANNEL_LAYERS=IN_MEMORY_CHANNELS)
class ChatSourceKeyTests(MediaTestCase):
    """The chat validators: media and thumbnail alike."""

    def setUp(self):
        super().setUp()
        Follow.objects.create(follower_user=self.author, following_user=self.rival)
        Follow.objects.create(follower_user=self.rival, following_user=self.author)
        self.conversation, _ = ConversationService.get_or_create_conversation(
            actor_user=self.author, target_user=self.rival,
        )
        self.folder = self._folder(self.author, "chat")

    def _send(self, body):
        return self.client.post(
            f"/conversations/{self.conversation.id}/messages/media",
            body, format="json",
        )

    def test_a_chat_video_refuses_a_source_file(self):
        file_key = object_key(self.folder, "mp4", source=True)

        resp = self._send({
            "media_type": "video",
            "media_url": media_url(file_key),
            "media_public_id": file_key,
            "thumbnail_url": media_url(object_key(self.folder, "jpg")),
            "duration_ms": 12_000,
            "size_bytes": 5 * MB,
        })

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["message"], SOURCE_NOT_PROCESSED_MESSAGE)
        self.assertFalse(Message.objects.exists())

    def test_a_chat_thumbnail_refuses_a_source_key(self):
        file_key = object_key(self.folder, "jpg")

        resp = self._send({
            "media_type": "image",
            "media_url": media_url(file_key),
            "media_public_id": file_key,
            "thumbnail_url": media_url(
                object_key(self.folder, "jpg", source=True)
            ),
        })

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["message"], SOURCE_NOT_PROCESSED_MESSAGE)
        self.assertFalse(Message.objects.exists())

    def test_a_chat_jobs_outputs_still_send(self):
        job = self._job(status=READY, purpose="chat", upload_type="chat")

        resp = self._send({
            "media_type": "video",
            "media_url": job.output_url,
            "media_public_id": job.output_key,
            "thumbnail_url": job.thumbnail_url,
            "duration_ms": job.duration * 1000,
            "size_bytes": job.size_bytes,
            "width": job.width,
            "height": job.height,
        })

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Message.objects.get().media_public_id, job.output_key)


# =====================================================================
# Fix round — B2: the duration grace never produces a refused file
# =====================================================================

class DurationGraceTests(WorkerTestCase):
    """
    Over cap + 3 s is VIDEO_TOO_LONG; inside the grace the output is cut to
    the cap (`-t <cap>`) and never reports more than the cap.
    """

    def _convert(self, *, purpose, ffmpeg):
        job = self._job(status=QUEUED, purpose=purpose)
        self._serve_source(b"\0" * 1024, "video/quicktime")
        with patch(FFMPEG_ON_PATH, return_value=True), \
                patch(FFMPEG_RUN, side_effect=ffmpeg):
            return self._run(job)

    @staticmethod
    def _t(ffmpeg):
        encode = ffmpeg.encodes[0]
        return encode[encode.index("-t") + 1]

    def test_a_highlight_inside_the_grace_is_cut_to_90_seconds(self):
        ffmpeg = FakeFfmpeg(source_duration=92.0, out_duration=90.04)

        job = self._convert(purpose="highlights", ffmpeg=ffmpeg)

        self.assertEqual(job.status, READY, job.error_detail)
        self.assertEqual(self._t(ffmpeg), "90")
        self.assertEqual(job.duration, 90)

    def test_a_post_inside_the_grace_is_cut_to_300_seconds(self):
        ffmpeg = FakeFfmpeg(source_duration=302.4, out_duration=300.4)

        job = self._convert(purpose="posts", ffmpeg=ffmpeg)

        self.assertEqual(job.status, READY, job.error_detail)
        self.assertEqual(self._t(ffmpeg), "300")
        self.assertEqual(job.duration, 300)

    def test_a_clip_under_its_cap_is_not_cut(self):
        ffmpeg = FakeFfmpeg(source_duration=60.0)

        job = self._convert(purpose="highlights", ffmpeg=ffmpeg)

        self.assertEqual(job.status, READY, job.error_detail)
        # cap + grace + 1: only a bound on the work, as before.
        self.assertEqual(self._t(ffmpeg), "94")
        self.assertEqual(job.duration, 60)

    def test_over_cap_plus_grace_is_still_too_long(self):
        ffmpeg = FakeFfmpeg(source_duration=93.5)

        job = self._convert(purpose="highlights", ffmpeg=ffmpeg)

        self.assertEqual(job.status, FAILED)
        self.assertEqual(job.error_code, VIDEO_TOO_LONG)
        self.assertEqual(ffmpeg.encodes, [])

    def test_an_untimed_source_never_reports_past_its_cap(self):
        # ffprobe gives no duration ("None" is not a number): no trim, but the
        # output it measures at 92 s is still reported as the cap.
        ffmpeg = FakeFfmpeg(source_duration=None, out_duration=92.0)

        job = self._convert(purpose="highlights", ffmpeg=ffmpeg)

        self.assertEqual(job.status, READY, job.error_detail)
        self.assertEqual(self._t(ffmpeg), "94")
        self.assertEqual(job.duration, 90)

    @skipUnless(HAS_FFMPEG, "ffmpeg / ffprobe are not installed")
    def test_real_ffmpeg_cuts_cap_plus_two_seconds_to_the_cap(self):
        clip = os.path.join(self.work_dir, "long.mp4")
        subprocess.run(
            [
                shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
                "-y", "-f", "lavfi",
                "-i", "testsrc=duration=92:size=320x180:rate=10",
                "-c:v", "mpeg4", clip,
            ],
            check=True, timeout=300,
        )
        out_dir = tempfile.mkdtemp(dir=self.work_dir)

        result = video.process(
            clip, out_dir, max_duration=90, max_bytes=40 * MB,
            deadline=time.monotonic() + 600,
        )

        self.assertEqual(result.duration, 90)
        probed = json.loads(subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-print_format", "json",
             "-show_format", result.output_path],
            check=True, capture_output=True, timeout=60,
        ).stdout)
        self.assertLessEqual(float(probed["format"]["duration"]), 90.5)


# =====================================================================
# Fix round — B3: media has its own queue; a worker that cannot convert
# hands the job back
# =====================================================================

class MediaQueueRoutingTests(SimpleTestCase):

    def test_process_job_is_routed_to_the_media_queue(self):
        router = celery_app.amqp.router

        self.assertEqual(
            router.route({}, "media.process_job")["queue"].name, "media"
        )
        # Everything else stays on the default queue.
        for name in ("media.sweep_jobs", "core.heartbeat", "emails.send"):
            with self.subTest(name):
                self.assertEqual(
                    router.route({}, name)["queue"].name, "celery"
                )


class WorkerRecoveryTests(WorkerTestCase):

    def test_a_worker_without_ffmpeg_requeues_the_job_without_an_attempt(self):
        job = self._job(status=QUEUED)

        with patch(FFMPEG_ON_PATH, return_value=False), \
                self.assertLogs(
                    "apps.media.services.media_processing_service", "ERROR"
                ):
            job = self._run(job)

        self.assertEqual(job.status, QUEUED)
        self.assertEqual(job.attempts, 0)
        self.assertEqual(job.error_code, "")
        self.storage.head_object.assert_not_called()
        # Not finished: nobody is told it failed, nothing is published.
        self.job_send.assert_not_called()
        self.assertEqual(self.finished, [])

    def test_an_image_worker_without_pillow_heif_requeues_too(self):
        job = self._job(status=QUEUED, kind=IMAGE, content_type="image/jpeg")

        with patch(
            "apps.media.processing.image.imaging_available",
            return_value=False,
        ):
            job = self._run(job)

        self.assertEqual((job.status, job.attempts), (QUEUED, 0))

    def test_it_fails_processor_unavailable_once_the_job_is_an_hour_old(self):
        job = self._job(status=QUEUED)
        MediaJob.objects.filter(id=job.id).update(
            created_at=timezone.now() - timedelta(minutes=61)
        )

        with patch(FFMPEG_ON_PATH, return_value=False):
            job = self._run(job)

        self.assertEqual(job.status, FAILED)
        self.assertEqual(job.error_code, PROCESSOR_UNAVAILABLE)
        self._assert_finish_sent(job, FAILED)

    # ── B5: the soft time limit after ready ──────────────────────

    def test_a_soft_time_limit_after_ready_keeps_the_job_ready(self):
        job = self._job(status=QUEUED, kind=IMAGE, content_type="image/jpeg")
        self._serve_source(jpeg_bytes(800, 600), "image/jpeg")
        # Fires in the source-delete dispatch, after mark_ready.
        self.enqueue.side_effect = SoftTimeLimitExceeded()

        job = self._run(job)

        self.assertEqual(job.status, READY)
        self.assertEqual(job.error_code, "")
        self.assertTrue(job.output_url)
        self._assert_finish_sent(job, READY)

    def test_a_soft_time_limit_before_ready_still_times_out(self):
        job = self._job(status=QUEUED, kind=IMAGE, content_type="image/jpeg")
        self._serve_source(jpeg_bytes(800, 600), "image/jpeg")
        self.storage.upload_file.side_effect = SoftTimeLimitExceeded()

        job = self._run(job)

        self.assertEqual(job.status, FAILED)
        self.assertEqual(job.error_code, PROCESSING_TIMEOUT)
        self._assert_finish_sent(job, FAILED)


# =====================================================================
# Fix round — B4: the media worker recovers its own jobs at startup
# =====================================================================

class MediaWorkerReadyTests(MediaTestCase):

    def setUp(self):
        super().setUp()
        self.enqueue = self._patch(SWEEP_ENQUEUE)

        self.work_dir = tempfile.mkdtemp(prefix="media-tests-")
        self.addCleanup(shutil.rmtree, self.work_dir, True)
        work_dir_override = override_settings(MEDIA_WORK_DIR=self.work_dir)
        work_dir_override.enable()
        self.addCleanup(work_dir_override.disable)

        # What a hard-killed run leaves behind, and a folder that is not ours.
        self.leftovers = []
        for name in ("abc123", "def456"):
            path = os.path.join(self.work_dir, f"{WORK_DIR_PREFIX}{name}")
            os.makedirs(path)
            with open(os.path.join(path, "source.mov"), "wb") as handle:
                handle.write(b"\0" * 64)
            self.leftovers.append(path)
        self.unrelated = os.path.join(self.work_dir, "keep-me")
        os.makedirs(self.unrelated)

    @staticmethod
    def _consumer(*queue_names):
        """
        Stands in for the Consumer Celery sends worker_ready with — a plain
        object, because Celery caches receivers per (hashable) sender.
        """
        class FakeConsumer:
            task_consumer = SimpleNamespace(
                queues=[Queue(name) for name in queue_names]
            )

        return FakeConsumer()

    def test_the_media_worker_requeues_interrupted_runs_at_startup(self):
        first = self._job(status=PROCESSING, attempts=1)
        second = self._job(status=PROCESSING, attempts=2)
        queued = self._job(status=QUEUED)
        ready = self._job(status=READY)

        worker_ready.send(sender=self._consumer("media"))

        for job, attempts in ((first, 1), (second, 2)):
            job.refresh_from_db()
            self.assertEqual(job.status, QUEUED)
            self.assertEqual(job.attempts, attempts)  # unchanged
        queued.refresh_from_db()
        ready.refresh_from_db()
        self.assertEqual(queued.status, QUEUED)
        self.assertEqual(ready.status, READY)

        dispatched = set()
        for call in self.enqueue.call_args_list:
            self.assertIs(call.args[0], process_media_job)
            self.assertEqual(call.kwargs["fallback"], "skip")
            dispatched.update(call.args[1])
        self.assertEqual(dispatched, {str(first.id), str(second.id)})

        for path in self.leftovers:
            self.assertFalse(os.path.exists(path), path)
        self.assertTrue(os.path.isdir(self.unrelated))

    def test_a_worker_on_both_queues_recovers_too(self):
        job = self._job(status=PROCESSING)

        worker_ready.send(sender=self._consumer("celery", "media"))

        job.refresh_from_db()
        self.assertEqual(job.status, QUEUED)

    def test_a_default_queue_worker_does_nothing(self):
        job = self._job(status=PROCESSING)

        worker_ready.send(sender=self._consumer("celery"))

        job.refresh_from_db()
        self.assertEqual(job.status, PROCESSING)
        self.enqueue.assert_not_called()
        for path in self.leftovers:
            self.assertTrue(os.path.isdir(path), path)


# =====================================================================
# Fix round — B5: org_id, purpose ↔ upload_type, one job one post
# =====================================================================

class MediaJobGuardTests(JobEndpointTestCase):

    def test_a_malformed_org_id_is_a_400_not_a_500(self):
        resp = self._post(self._key(), org_id="not-a-uuid")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("org_id", self._errors(resp))
        self.storage.head_object.assert_not_called()

        job = self._job(status=READY)
        resp = self.client.get(f"{JOBS_URL}/{job.id}", {"org_id": "not-a-uuid"})

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("org_id", self._errors(resp))

    def test_a_purpose_and_upload_type_that_do_not_pair_are_refused(self):
        for purpose, upload_type in [
            ("posts", "highlights"),
            ("posts", "chat"),
            ("chat", "posts"),
            ("recruitments", "posts"),
            ("highlights", "chat"),
        ]:
            with self.subTest(purpose=purpose, upload_type=upload_type):
                resp = self._post(
                    self._key(upload_type=upload_type),
                    purpose=purpose, upload_type=upload_type,
                )

                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertIn("upload_type", self._errors(resp))

        self.storage.head_object.assert_not_called()
        self.assertFalse(MediaJob.objects.exists())

    def test_a_highlight_signed_as_highlights_is_queued(self):
        resp = self._post(
            self._key(upload_type="highlights"),
            purpose="highlights", upload_type="highlights",
        )

        self.assertEqual(resp.status_code, 201, resp.data)


class OneJobOnePostTests(MediaTestCase):
    """A job is one post or highlight: a retry after a lost 202 is refused."""

    def _post_job(self, job):
        return self.client.post(POST_CREATE_URL, {
            "content": "retry me",
            "media": [{"media_job_id": str(job.id), "order": 0}],
        }, format="json")

    def _highlight_job(self, job):
        return self.client.post(
            HIGHLIGHT_CREATE_URL, {"media_job_id": str(job.id)}, format="json"
        )

    def test_a_held_job_is_refused_a_second_time_and_still_publishes(self):
        job = self._job(status=PROCESSING)
        self.assertEqual(self._post_job(job).status_code, 202)

        again = self._post_job(job)

        self.assertEqual(again.status_code, 400, again.data)
        self.assertEqual(again.data["message"], JOB_ALREADY_USED_MESSAGE)
        self.assertEqual(PendingPublication.objects.count(), 1)

        # Its own publication is not a second use of it.
        self._make_ready(job)
        status = PublicationService.try_publish(PendingPublication.objects.get())
        self.assertEqual(status, PendingPublication.Status.PUBLISHED)

        after = self._post_job(job)
        self.assertEqual(after.status_code, 400, after.data)
        self.assertEqual(after.data["message"], JOB_ALREADY_USED_MESSAGE)
        self.assertEqual(Post.objects.count(), 1)

    def test_a_ready_job_attached_in_the_request_cannot_be_posted_again(self):
        job = self._job(status=READY)
        self.assertEqual(self._post_job(job).status_code, 200)

        again = self._post_job(job)

        self.assertEqual(again.status_code, 400, again.data)
        self.assertEqual(again.data["message"], JOB_ALREADY_USED_MESSAGE)
        self.assertEqual(Post.objects.count(), 1)

    def test_a_cancelled_publication_frees_its_job(self):
        job = self._job(status=PROCESSING)
        self._post_job(job)
        publication = PendingPublication.objects.get()
        cancel = self.client.post(
            f"{PUBLICATIONS_URL}/{publication.id}/cancel", {}, format="json"
        )
        self.assertEqual(cancel.status_code, 200, cancel.data)

        again = self._post_job(job)

        self.assertEqual(again.status_code, 202, again.data)

    def test_a_highlight_job_is_used_once(self):
        held = self._job(status=PROCESSING, purpose="highlights")
        self.assertEqual(self._highlight_job(held).status_code, 202)
        again = self._highlight_job(held)
        self.assertEqual(again.status_code, 400, again.data)
        self.assertEqual(again.data["message"], JOB_ALREADY_USED_MESSAGE)

        ready = self._job(status=READY, purpose="highlights")
        self.assertEqual(self._highlight_job(ready).status_code, 201)
        again = self._highlight_job(ready)
        self.assertEqual(again.status_code, 400, again.data)
        self.assertEqual(again.data["message"], JOB_ALREADY_USED_MESSAGE)
        self.assertEqual(Highlight.objects.count(), 1)
