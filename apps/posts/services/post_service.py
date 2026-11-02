import logging

from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest
from apps.posts.models import Comment, Post, PostMedia
from apps.connections.services.follow_services import FollowService
from apps.media.constants import JOB_ALREADY_USED_MESSAGE
from apps.media.models import MediaJob
from apps.media.services.media_job_service import MediaJobService
from apps.notifications.services.notification_service import NotificationService
from apps.posts.services.post_content_service import sync_post_content
from apps.sports.models import Sport
from core.actor import Actor
from core.constant import TYPE_ORGANIZATION, TYPE_USER
from services.location.location_service import LocationService
from services.storage.metadata import (
    MAX_IMAGE_BYTES,
    MAX_POST_VIDEO_DURATION,
    MAX_VIDEO_BYTES,
    clamp_dimensions,
    clamp_duration,
    clamp_size_bytes,
)
from services.storage.validators import (
    allowed_image_extensions,
    allowed_video_extensions,
    validate_media,
    validate_thumbnail,
)
from shared.models import Location
from shared.tasks import delete_folder
from utils.background_jobs import enqueue

logger = logging.getLogger(__name__)

MAX_POST_MEDIA = 10


class PostCreateError(Exception):
    """
    A refused post create, carrying exactly the response the view used to
    build inline: ``response_data(False, message=..., error=..., status_code=...)``.
    Most refusals are a message; the storage/location ones were always an
    empty message with the reason in ``error``, and still are.
    """

    def __init__(self, message="", status_code=400, error=None):
        super().__init__(message or error or "")
        self.message = message
        self.status_code = status_code
        self.error = error


def notify_new_mentions(post, actor, added_mentions):
    """
    Fire MENTION notifications for the targets a content sync just added.

    Mention ROWS are created regardless of who can see the post — only the
    NOTIFICATION is gated. A FOLLOWERS-only post notifies a mentioned actor
    only when that actor follows the author, otherwise the push would advertise
    a post they cannot open.

    Runs after commit, so a notification or FCM failure can never fail or roll
    back the post itself (same pattern as ApplicationService.apply).
    """
    if not added_mentions:
        return

    actor_user = actor.user if actor.is_user else None
    actor_org = actor.organization if actor.is_org else None

    author_id = post.author_user_id or post.author_org_id
    author_type = TYPE_USER if post.author_user_id else TYPE_ORGANIZATION

    def _can_notify(kind, target) -> bool:
        if post.visibility == Post.Visibility.PUBLIC:
            return True

        # MENTION_VISIBILITY: anything that is neither PUBLIC nor FOLLOWERS is
        # treated as not-notifiable. A future, more private mode has to opt in
        # here deliberately rather than inherit "notify" by falling through.
        if post.visibility != Post.Visibility.FOLLOWERS:
            return False

        # FollowService.get_relationship covers all four user/org edges, so no
        # actor-pair combination has to be excluded. `is_following` is read
        # from the MENTIONED actor's side: do they follow the author?
        viewer = Actor(
            actor_type="user" if kind == "user" else "organization",
            user=target if kind == "user" else None,
            organization=target if kind == "org" else None,
        )
        relationship = FollowService.get_relationship(viewer, author_id, author_type)
        return relationship["is_following"]

    def _notify():
        for kind, target in added_mentions:
            try:
                if not _can_notify(kind, target):
                    continue
                NotificationService.mention(
                    actor_user=actor_user,
                    actor_org=actor_org,
                    post=post,
                    mentioned_user=target if kind == "user" else None,
                    mentioned_org=target if kind == "org" else None,
                )
            except Exception as exc:
                logger.warning(
                    "notify_new_mentions | notification failed | "
                    f"post_id={post.id} | target={kind}:{target.id} | {exc}"
                )

    transaction.on_commit(_notify)


class PostService:

    # =================================================================
    # CREATE
    # =================================================================
    #
    # Two halves, so a post whose media is still converting can be checked
    # NOW and created LATER through the very same code: CreatePostAPIView
    # runs both for a ready post, and PublicationService.try_publish runs
    # both again once the media is ready (apps/media).

    @staticmethod
    def validate_create_payload(actor, user, data, *, publication_id=None) -> dict:
        """
        Check a create body. Returns the cleaned post, or raises
        PostCreateError with exactly the message the endpoint has always sent.

        ``publication_id`` — set only by PublicationService.try_publish — is
        the held publication this body belongs to, so its own jobs do not read
        as already used.

        A media item is either a ready upload (``file_url`` / ``public_id`` /
        ``thumbnail_url`` / …) or ``{"media_job_id", "order"}`` — a server-side
        conversion (apps/media). A READY job's outputs replace the item and it
        is checked like any other; a job still converting is checked for
        ownership and counted by kind, and is returned in ``pending_jobs`` —
        the caller holds the post as a PendingPublication instead of creating
        it. Every non-media field is fully checked either way.

        Returns ``{"content", "post_type", "visibility", "sport",
        "location_data", "media", "jobs", "pending_jobs", "payload"}``;
        ``payload`` is the validated body with media as written, for a
        PendingPublication to re-run through here.
        """
        content = (data.get("content") or "").strip()
        post_type = data.get("post_type", Post.PostType.NORMAL)
        visibility = data.get("visibility", Post.Visibility.PUBLIC)
        sport_id = data.get("sport_id")
        media_list = data.get("media", [])
        location_data = data.get("location", {})

        # -------------------------
        # BASIC VALIDATIONS
        # -------------------------

        if not content and not media_list:
            raise PostCreateError("Post cannot be empty")

        if post_type not in Post.PostType.values:
            raise PostCreateError("Invalid post_type")

        if visibility not in Post.Visibility.values:
            raise PostCreateError("Invalid visibility")

        # location validation
        # A place is identified by its id, so a payload carrying one is
        # valid with or without coordinates — LocationService resolves it
        # against the row it already has. Coordinates are only REQUIRED for
        # a place nothing has ever seen. Note the `is None` test: latitude 0
        # is a real coordinate, and a falsy check rejected the equator.
        if location_data:
            if not isinstance(location_data, dict):
                raise PostCreateError("Invalid location format")

            has_coords = (
                location_data.get("latitude") is not None
                and location_data.get("longitude") is not None
            )
            if not has_coords and not location_data.get("external_id"):
                raise PostCreateError(
                    "Location must include latitude and longitude"
                )

        # Sport validation
        sport = None
        if sport_id:
            sport = Sport.objects.filter(id=sport_id).only("id").first()
            if not sport:
                raise PostCreateError("Invalid sport_id")

        # -------------------------
        # MEDIA VALIDATION
        # -------------------------

        if not isinstance(media_list, list):
            raise PostCreateError("media must be a list")

        if len(media_list) > MAX_POST_MEDIA:
            raise PostCreateError("Maximum 10 media allowed")

        image_count = 0
        video_count = 0
        cleaned_media = []
        jobs = []
        pending_jobs = []
        org = actor.organization if actor.is_org else None

        for idx, media in enumerate(media_list):

            if not isinstance(media, dict):
                raise PostCreateError(f"Invalid media at index {idx}")

            if media.get("media_job_id") not in (None, ""):
                job = MediaJobService.resolve_for_attach(
                    actor, media.get("media_job_id"), purpose="posts"
                )
                # Not found and not yours read the same: another actor's
                # job id is never confirmed to exist.
                if job is None:
                    raise PostCreateError(f"Invalid media at index {idx}")

                if job.status == MediaJob.Status.FAILED:
                    raise PostCreateError(
                        MediaJobService.job_error_message(job)
                    )

                # One job, one post: a retry after a lost 202 must not hold
                # or create a second post of the same media.
                if MediaJobService.in_use(job, publication_id=publication_id):
                    raise PostCreateError(JOB_ALREADY_USED_MESSAGE)

                jobs.append(job)

                if job.status != MediaJob.Status.READY:
                    # Still converting. Its kind is known already, so the
                    # count and mix rules below hold now, not minutes later.
                    if job.kind == MediaJob.Kind.IMAGE:
                        image_count += 1
                    else:
                        video_count += 1

                    order = media.get("order", idx)
                    if order < 0:
                        raise PostCreateError("Invalid media order")

                    pending_jobs.append(job)
                    continue

                # Ready: the job's outputs ARE the item from here on.
                media = {
                    **MediaJobService.attach_fields(job),
                    "order": media.get("order", idx),
                }
            else:
                media = dict(media)

            file_url = media.get("file_url")
            media_type = media.get("media_type")
            public_id = media.get("public_id")

            if not file_url or not media_type or not public_id:
                raise PostCreateError(f"Missing fields at index {idx}")

            # -------------------------
            # TYPE COUNT
            # -------------------------
            if media_type == PostMedia.MediaType.IMAGE:
                image_count += 1
            elif media_type == PostMedia.MediaType.VIDEO:
                video_count += 1
            else:
                raise PostCreateError(f"Invalid media_type at index {idx}")

            # -------------------------
            # MEDIA URL VALIDATION
            # -------------------------
            # Provider-aware (see services/storage/validators.py): the URL
            # must come from our storage, carry an allowed extension, sit
            # under this actor's own prefix, and its key must match the
            # public_id sent alongside it.
            thumbnail_url = (media.get("thumbnail_url") or "").strip()

            try:
                if media_type == PostMedia.MediaType.IMAGE:
                    validate_media(
                        user,
                        file_url,
                        public_id,
                        org=org,
                        allowed_extensions=allowed_image_extensions()
                    )

                    # Optional for an image — a cheap preview for the feed.
                    if thumbnail_url:
                        validate_thumbnail(
                            user,
                            thumbnail_url,
                            parent_key=public_id,
                            org=org,
                        )

                elif media_type == PostMedia.MediaType.VIDEO:
                    validate_media(
                        user,
                        file_url,
                        public_id,
                        org=org,
                        allowed_extensions=allowed_video_extensions()
                    )

                    # REQUIRED for a video. The browser captures it while
                    # encoding (or the media worker makes it), so without one
                    # the feed has a black tile until the clip buffers.
                    if not thumbnail_url:
                        raise PostCreateError(
                            f"thumbnail_url required at index {idx}"
                        )

                    validate_thumbnail(
                        user,
                        thumbnail_url,
                        parent_key=public_id,
                        org=org,
                    )

            except ValueError as ve:
                raise PostCreateError(error=str(ve))

            # Order validation
            order = media.get("order", idx)
            if order < 0:
                raise PostCreateError("Invalid media order")
            media["order"] = order

            # -------------------------
            # CLIENT-REPORTED METADATA
            # -------------------------
            # For a browser-made file there is nobody left to ask: the object
            # is the exact bytes it uploaded, so width/height/duration/size
            # arrive with the attach request. They are cosmetic (layout and a
            # duration badge), so anything out of range becomes NULL rather
            # than a 400 — the client already renders older rows that have
            # none. A job's measured values pass through the same clamps.
            media["width"], media["height"] = clamp_dimensions(
                media.get("width"), media.get("height")
            )
            media["thumbnail_url"] = thumbnail_url

            if media_type == PostMedia.MediaType.VIDEO:
                media["duration"] = clamp_duration(
                    media.get("duration"), MAX_POST_VIDEO_DURATION
                )
                media["size_bytes"] = clamp_size_bytes(
                    media.get("size_bytes"), MAX_VIDEO_BYTES
                )
            else:
                # An image has no duration; a client that sends one is
                # confused, and storing it would print a badge on a photo.
                media["duration"] = None
                media["size_bytes"] = clamp_size_bytes(
                    media.get("size_bytes"), MAX_IMAGE_BYTES
                )

            cleaned_media.append(media)

        # -------------------------
        # MEDIA RULES
        # -------------------------

        if image_count > 10:
            raise PostCreateError("Max 10 images allowed")

        if video_count > 1:
            raise PostCreateError("Only one video allowed")

        if video_count >= 1 and image_count >= 1:
            raise PostCreateError("Cannot mix images and video")

        # A post that will be created minutes from now must not fail then on a
        # location it could have been told about now. A ready post resolves
        # its location in create_post, exactly as before.
        if pending_jobs and location_data:
            PostService._check_location(location_data)

        return {
            "content": content,
            "post_type": post_type,
            "visibility": visibility,
            "sport": sport,
            "location_data": location_data,
            "media": cleaned_media,
            "jobs": jobs,
            "pending_jobs": pending_jobs,
            "payload": {
                "content": content,
                "post_type": post_type,
                "visibility": visibility,
                "sport_id": sport_id,
                "media": media_list,
                "location": location_data,
            },
        }

    @staticmethod
    def _check_location(location_data):
        """
        Would ``LocationService.get_or_create_location`` accept this? Asked
        without writing a row — a manual place has no external id, so
        resolving it now and again at publish would store it twice.
        """
        try:
            data = LocationService.normalize_data(location_data)
            if LocationService.has_coords(data["latitude"], data["longitude"]):
                return
            if data["external_id"] and Location.objects.filter(
                provider=data["provider"], external_id=data["external_id"]
            ).exists():
                return
            LocationService.validate(data["latitude"], data["longitude"])
        except (AttributeError, TypeError, ValueError) as exc:
            raise PostCreateError(error=str(exc))

    @staticmethod
    def create_post(actor, user, cleaned) -> Post:
        """
        Create a post from ``validate_create_payload``'s result, every media
        item ready. One transaction: the location, the post, its media, and
        the hashtag + mention rows derived from the body — so a post is never
        visible without them. Mention notifications go out after commit.
        """
        with transaction.atomic():
            location = None
            denorm = {}
            if cleaned["location_data"]:
                try:
                    location = LocationService.get_or_create_location(
                        cleaned["location_data"]
                    )
                except ValueError as ve:
                    raise PostCreateError(error=str(ve))

                denorm = LocationService.build_denormalized(location)

            post = Post.objects.create(
                author_user=actor.user if actor.is_user else None,
                author_org=actor.organization if actor.is_org else None,
                content=cleaned["content"],
                post_type=cleaned["post_type"],
                visibility=cleaned["visibility"],
                sport=cleaned["sport"],

                # LOCATION DATA - (for performance storing denormalized way)
                location=location,
                location_name=denorm.get("location_name", ""),
                city=denorm.get("city", ""),
                country_code=denorm.get("country_code", ""),
                latitude=denorm.get("latitude"),
                longitude=denorm.get("longitude"),
            )

            media_objs = [
                PostMedia(
                    post=post,
                    file_url=media.get("file_url"),
                    public_id=media.get("public_id"),
                    media_type=media.get("media_type"),
                    thumbnail_url=media.get("thumbnail_url", ""),
                    duration=media.get("duration"),
                    width=media.get("width"),
                    height=media.get("height"),
                    size_bytes=media.get("size_bytes"),
                    order=media["order"],
                )
                for media in cleaned["media"]
            ]

            if media_objs:
                PostMedia.objects.bulk_create(media_objs)

            # Hashtag + mention rows are derived from the body — inside the
            # same transaction, so a post is never visible without them.
            added_mentions = sync_post_content(post)
            notify_new_mentions(post, actor, added_mentions)

        return post

    # =================================================================
    # DELETE
    # =================================================================

    @staticmethod
    def delete_post(post_id, actor):
        # get post
        if actor.is_user:
            post = Post.objects.filter(
                id=post_id,
                author_user=actor.user
            ).first()

        elif actor.is_org:
            post = Post.objects.filter(
                id=post_id,
                author_org=actor.organization
            ).first()
        else:
            return False, None

        if not post:
            return False, None

        try:
            with transaction.atomic():

                #  get first media to extract folder
                first_media = post.media.first()

                if first_media and first_media.public_id:
                    folder_path = "/".join(first_media.public_id.split("/")[:-1])

                    # AFTER COMMIT, which it was not before: the sweep used to
                    # run here, inside the atomic block, so a transaction that
                    # rolled back below left the post in the database with its
                    # media already deleted from R2. enqueue's on_commit
                    # default is what fixes that — the job is published only if
                    # this block commits. No try/except: enqueue never raises
                    # into its caller.
                    enqueue(delete_folder, (folder_path,))

                #  delete DB
                deleted_count, _ = post.delete()

                return deleted_count > 0, None

        except Exception as e:
            print(f"Delete post failed: {e}")
            return False, None

    # =================================================================
    # MODERATION
    # =================================================================
    #
    # Additive: nothing above this line changes. These are the paths the
    # moderation admin calls, and they differ from the owner paths on purpose.

    @staticmethod
    def moderator_delete_post(post):
        """
        Take a post down as a moderator. Returns True if it moved.

        SOFT delete, unlike ``delete_post``, and the difference is deliberate.
        The owner path hard-deletes the row AND sweeps the post's R2 folder,
        which is right when somebody removes their own photo. It is wrong here
        twice over: the report's evidence would be destroyed along with the
        content, and a wrong call could never be undone.

        So this sets ``is_deleted`` — the flag every read path already filters
        on (feed, search, saved posts, the public profile) — and leaves the
        media objects in the bucket. Reversing a takedown is one boolean.
        """
        if post.is_deleted:
            return False

        post.is_deleted = True
        post.save(update_fields=["is_deleted"])

        return True

    @staticmethod
    def moderator_delete_comment(comment):
        """
        Take a comment down as a moderator, cascading to its replies and
        fixing the denormalized counters — the same mechanics the owner path in
        DeleteCommentAPIView runs, minus the author/post-owner permission gate.

        NOTE: those mechanics are implemented in the view, not in a service, so
        this is a second copy of them. The two must move together; extracting
        the view's block into this method is the obvious follow-up, kept out of
        this change so a moderation feature does not quietly alter the comment
        endpoint.
        """
        if comment.is_deleted:
            return False

        with transaction.atomic():
            if comment.parent_id is None:
                reply_ids = list(
                    Comment.objects
                    .filter(parent_id=comment.id, is_deleted=False)
                    .values_list("id", flat=True)
                )
                removed = 1 + len(reply_ids)

                if reply_ids:
                    Comment.objects.filter(id__in=reply_ids).update(is_deleted=True)

                comment.is_deleted = True
                comment.save(update_fields=["is_deleted"])
            else:
                removed = 1
                comment.is_deleted = True
                comment.save(update_fields=["is_deleted"])

                Comment.objects.filter(id=comment.parent_id).update(
                    reply_count=Greatest(F("reply_count") - 1, Value(0))
                )

            Post.objects.filter(id=comment.post_id).update(
                comments_count=Greatest(F("comments_count") - removed, Value(0))
            )

        return True
