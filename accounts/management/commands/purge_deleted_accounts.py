"""
The second half of account deletion: permanently purge accounts whose 30 days
are up.

``confirm_account_deletion`` only deactivates and stamps ``deletion_requested_at``.
This command is what actually destroys the data, and it selects on BOTH
columns — ``is_active=False`` AND a non-NULL ``deletion_requested_at`` older
than the window. That pair is what keeps the other two meanings of
``is_active=False`` safe: an unverified signup and a staff suspension both leave
the timestamp NULL and can never be swept up here.

Schedule it DAILY on Render as a cron job:

    python manage.py purge_deleted_accounts

Run it by hand with a shorter window to see what it would do:

    python manage.py purge_deleted_accounts --dry-run
    ACCOUNT_PURGE_AFTER_DAYS=0 python manage.py purge_deleted_accounts

WHAT HAPPENS TO A USER'S DATA — the three buckets, and why each is what it is:

  HARD DELETE — rows that are purely theirs, or that only describe a graph edge
  nobody else's record depends on: follows in both directions, saved posts,
  their reactions, notifications they received AND notifications they triggered,
  FCM device tokens, their cached OTP. None of these mean anything once the
  person is gone, and a follow edge pointing at a tombstone would keep inflating
  somebody else's follower count.

  SOFT DELETE — their posts and comments, through the same ``is_deleted`` flag
  the app's own delete buttons use. Hard-deleting a comment would cascade to its
  replies and blow a hole in a conversation other people are still having; the
  soft flag removes it from every read path and leaves the thread intact.

  KEEP, pointing at the anonymized shell — legal acceptances, moderation
  reports filed by or about them, recruitment applications, career and
  achievement rows with their org-side verifications. These are OTHER actors'
  records, or ours: a club's hiring pipeline and a moderator's case file must
  not develop holes because a user left. They survive automatically, because the
  User row is never deleted — only emptied.

The User row itself is ANONYMIZED rather than deleted. Deleting it would cascade
through every FK above and take the "keep" bucket with it.

IDEMPOTENT. A purged row is recognised by its tombstone email and skipped, so a
second run the same day (or a retried cron) changes nothing and reports zero.
"""

import logging
import os
import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from accounts.models import User, UserProfile
from connections.models import Follow
from notifications.models import Notification, UserFCMToken
from organization.models import OrganizationProfile
from posts.models import Comment, Like, Post, SavedPost
from usernames.services.username_service import UsernameService
from utils.cache import cache_delete
from utils.cache_keys import CacheKeys

logger = logging.getLogger(__name__)

DEFAULT_PURGE_AFTER_DAYS = 30

# The anonymized display name every purged profile ends up with.
ANON_NAME = "Deleted User"

# The tombstone address a purged account carries, and the marker this command
# recognises its own previous work by.
#
# NOT NULL, which is what the spec asked for and what the data deserves —
# ``User`` carries a CHECK constraint (``user_email_or_phone_required``) that a
# row must have an email OR a phone, so nulling both is rejected by the database.
# Relaxing that constraint would weaken a real invariant on every LIVE account to
# tidy up dead ones. A per-user random address on the reserved ``.invalid`` TLD
# (RFC 2606 — it can never resolve or receive mail) holds no personal data, keeps
# the unique index happy, and satisfies the constraint.
ANON_EMAIL_DOMAIN = "deleted.invalid"


def _anon_email():
    return f"deleted-{uuid.uuid4().hex}@{ANON_EMAIL_DOMAIN}"


def purge_after_days():
    """The window, from the environment. Zero is legal — it purges everything
    already confirmed, which is what the manual verification run wants."""
    raw = os.getenv("ACCOUNT_PURGE_AFTER_DAYS")
    if raw is None or raw == "":
        return DEFAULT_PURGE_AFTER_DAYS
    return int(raw)


def already_purged(user):
    """True when a previous run has emptied this row."""
    return str(user.email or "").endswith(f"@{ANON_EMAIL_DOMAIN}")


class Command(BaseCommand):
    help = (
        "Permanently purge accounts deactivated by their owner more than "
        "ACCOUNT_PURGE_AFTER_DAYS (default 30) days ago."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be purged without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        days = purge_after_days()
        cutoff = timezone.now() - timedelta(days=days)

        queryset = (
            User.objects
            .filter(
                is_active=False,
                deletion_requested_at__isnull=False,
                deletion_requested_at__lte=cutoff,
            )
            .select_related("profile")
            .order_by("deletion_requested_at")
        )

        self.stdout.write(self.style.WARNING(
            f"Purging accounts deleted before {cutoff.isoformat()} "
            f"(ACCOUNT_PURGE_AFTER_DAYS={days})"
            f"{' — dry run' if dry_run else ''}..."
        ))

        purged = 0
        skipped = 0
        failed = 0

        for user in queryset:
            if already_purged(user):
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(
                    f"  would purge {user.id} "
                    f"(requested {user.deletion_requested_at.isoformat()})"
                )
                purged += 1
                continue

            try:
                counts = self._purge(user)
            except Exception as e:
                # One bad row must not abort the night's run. Its transaction
                # has rolled back, so the account is untouched and the next run
                # picks it up again.
                failed += 1
                logger.error(
                    f"[PURGE] failed user={user.id} "
                    f"| {type(e).__name__}: {e}"
                )
                self.stdout.write(self.style.ERROR(
                    f"  FAILED {user.id}: {type(e).__name__}: {e}"
                ))
                continue

            purged += 1
            logger.info(
                f"[PURGE] user={user.id} "
                f"requested_at={user.deletion_requested_at.isoformat()} "
                f"follows={counts['follows']} saved={counts['saved']} "
                f"likes={counts['likes']} notifications={counts['notifications']} "
                f"fcm={counts['fcm']} posts_soft={counts['posts']} "
                f"comments_soft={counts['comments']}"
            )
            self.stdout.write(f"  purged {user.id}")

        summary = (
            f"Done. purged={purged}, already_purged={skipped}, failed={failed}"
            f"{' (dry-run — no writes)' if dry_run else ''}."
        )
        self.stdout.write(
            self.style.SUCCESS(summary) if not failed
            else self.style.WARNING(summary)
        )

    # ------------------------------------------------------------------ #
    # ONE ACCOUNT
    # ------------------------------------------------------------------ #
    @transaction.atomic
    def _purge(self, user):
        """
        Everything for one account, in one transaction. Either the whole row is
        emptied or none of it is — a half-purged account is worse than an
        unpurged one, because nothing would ever come back to finish it.
        """
        counts = {}

        # ── HARD DELETE ────────────────────────────────────────────
        # The follow graph, both directions. Their following edges AND the
        # edges pointing at them — a follower row aimed at a tombstone would
        # keep counting toward somebody else's follower total.
        #
        # The DENORMALIZED COUNTERS on the other side of each edge have to come
        # down with it. Nothing else will do it: FollowService.unfollow owns
        # that arithmetic and is never called here, so deleting the rows alone
        # would leave every account this person followed permanently one
        # follower heavier than it really is.
        self._decrement_follow_counters(user)

        deleted_follows, _ = Follow.objects.filter(
            Q(follower_user=user) | Q(following_user=user)
        ).delete()
        counts["follows"] = deleted_follows

        counts["saved"] = SavedPost.objects.filter(user=user).delete()[0]

        # Same story for reactions: likes_count and the likes_breakdown map are
        # maintained by the like endpoint, not by a cascade, so they are
        # corrected here before the rows go.
        counts["likes"] = self._remove_likes(user)

        # Both halves: what they were told, and what they caused somebody else
        # to be told. A "X liked your post" row naming a deleted account is a
        # notification whose deep link goes nowhere.
        counts["notifications"] = Notification.objects.filter(
            Q(recipient_user=user) | Q(actor_user=user)
        ).delete()[0]

        counts["fcm"] = UserFCMToken.objects.filter(user=user).delete()[0]

        # OTPs live in the cache, not in a table (utils.otp_validation), so
        # "delete the OTP rows" is deleting these keys — before the email is
        # cleared below, since the key is built from it.
        if user.email:
            cache_delete(CacheKeys.email_otp(user.email))
            cache_delete(CacheKeys.email_otp(user.email, "account_delete"))

        # ── SOFT DELETE ────────────────────────────────────────────
        # The app's own delete flag, so every read path that already respects
        # it (feed, explore, search, saved, profile, public profile) hides
        # these without another filter. Their replies stay intact.
        counts["posts"] = Post.objects.filter(
            author_user=user, is_deleted=False
        ).update(is_deleted=True)

        counts["comments"] = self._soft_delete_comments(user)

        # ── ANONYMIZE ──────────────────────────────────────────────
        # The handle goes back to the shared namespace FIRST, while
        # user.username still holds it — release() reads the column to know
        # which cache keys to bust.
        UsernameService.release(user=user)

        user.email = _anon_email()
        user.phone = None
        user.username = None
        user.set_unusable_password()
        user.save(update_fields=[
            "email", "phone", "username", "password", "updated_at",
        ])

        profile = getattr(user, "profile", None)
        if profile is not None:
            profile.name = ANON_NAME
            profile.headline = ""
            profile.about = ""
            profile.profile_photo = ""
            profile.profile_photo_public_id = ""
            profile.cover_photo = ""
            profile.cover_photo_public_id = ""
            profile.gender = ""
            profile.birthdate = None
            profile.height_cm = None
            profile.weight_kg = None
            profile.location = None
            profile.location_name = ""
            profile.city = ""
            profile.country_code = ""
            profile.latitude = None
            profile.longitude = None
            profile.save(update_fields=[
                "name", "headline", "about",
                "profile_photo", "profile_photo_public_id",
                "cover_photo", "cover_photo_public_id",
                "gender", "birthdate", "height_cm", "weight_kg",
                "location", "location_name", "city", "country_code",
                "latitude", "longitude", "updated_at",
            ])

        return counts

    # ------------------------------------------------------------------ #
    # COUNTER MAINTENANCE
    #
    # Every denormalized counter this purge would otherwise leave wrong. The
    # arithmetic mirrors the services that own each one — FollowService.unfollow,
    # the like endpoint in posts/views/like_views.py, and
    # PostService.moderator_delete_comment — and every decrement is floored at
    # zero with Greatest(), because a counter that has already drifted must not
    # be driven negative by this job.
    # ------------------------------------------------------------------ #

    @staticmethod
    def _decrement_follow_counters(user):
        """Take this user off the counters of everyone on either side of them."""
        # Who they follow: each loses a follower.
        followed_user_ids = list(
            Follow.objects.filter(follower_user=user, following_user__isnull=False)
            .values_list("following_user_id", flat=True)
        )
        followed_org_ids = list(
            Follow.objects.filter(follower_user=user, following_org__isnull=False)
            .values_list("following_org_id", flat=True)
        )
        # Who follows them: each loses a following.
        follower_user_ids = list(
            Follow.objects.filter(following_user=user, follower_user__isnull=False)
            .values_list("follower_user_id", flat=True)
        )
        follower_org_ids = list(
            Follow.objects.filter(following_user=user, follower_org__isnull=False)
            .values_list("follower_org_id", flat=True)
        )

        if followed_user_ids:
            UserProfile.objects.filter(user_id__in=followed_user_ids).update(
                followers_count=Greatest(F("followers_count") - 1, Value(0))
            )
        if followed_org_ids:
            OrganizationProfile.objects.filter(
                organization_id__in=followed_org_ids
            ).update(followers_count=Greatest(F("followers_count") - 1, Value(0)))

        if follower_user_ids:
            UserProfile.objects.filter(user_id__in=follower_user_ids).update(
                following_count=Greatest(F("following_count") - 1, Value(0))
            )
        if follower_org_ids:
            OrganizationProfile.objects.filter(
                organization_id__in=follower_org_ids
            ).update(following_count=Greatest(F("following_count") - 1, Value(0)))

        # A CONNECTION is a mutual user-to-user follow, so it is exactly the
        # intersection of the two user lists. Only the surviving side needs
        # fixing; this user's own counters are about to stop meaning anything.
        mutual_ids = set(followed_user_ids) & set(follower_user_ids)
        if mutual_ids:
            UserProfile.objects.filter(user_id__in=mutual_ids).update(
                connections_count=Greatest(F("connections_count") - 1, Value(0))
            )

    @staticmethod
    def _remove_likes(user):
        """
        Delete their reactions and repair each affected post's counters.

        ``likes_breakdown`` is a JSON map of reaction type -> count, so it
        cannot be fixed with an F() expression; the posts are walked in Python.
        One person's reactions are bounded by what they could physically tap.
        """
        rows = list(
            Like.objects.filter(user=user).values_list("post_id", "type")
        )
        if not rows:
            return 0

        per_post = {}
        for post_id, like_type in rows:
            per_post.setdefault(post_id, []).append(like_type)

        posts = Post.objects.filter(id__in=per_post).only(
            "id", "likes_count", "likes_breakdown"
        )
        for post in posts:
            types = per_post[post.id]
            breakdown = post.likes_breakdown or {}
            for like_type in types:
                breakdown[like_type] = max(0, breakdown.get(like_type, 1) - 1)
            post.likes_count = max(0, post.likes_count - len(types))
            post.likes_breakdown = breakdown
            post.save(update_fields=["likes_count", "likes_breakdown"])

        return Like.objects.filter(user=user).delete()[0]

    @staticmethod
    def _soft_delete_comments(user):
        """
        Soft-delete their comments and repair the two counters that hang off
        them: the post's ``comments_count`` and, for a reply, its parent's
        ``reply_count``.

        Their top-level comments do NOT cascade to other people's replies —
        unlike PostService.moderator_delete_comment, which is the takedown of
        one whole thread. Those replies are somebody else's words and belong in
        the KEEP bucket; the parent simply renders as removed, which is a state
        the app already handles.
        """
        rows = list(
            Comment.objects.filter(user=user, is_deleted=False)
            .values_list("id", "post_id", "parent_id")
        )
        if not rows:
            return 0

        per_post = {}
        parent_ids = []
        for _, post_id, parent_id in rows:
            per_post[post_id] = per_post.get(post_id, 0) + 1
            if parent_id is not None:
                parent_ids.append(parent_id)

        Comment.objects.filter(id__in=[r[0] for r in rows]).update(is_deleted=True)

        for post_id, removed in per_post.items():
            Post.objects.filter(id=post_id).update(
                comments_count=Greatest(F("comments_count") - removed, Value(0))
            )

        # One decrement per reply, so a parent that lost three of them drops by
        # three rather than by one.
        for parent_id in parent_ids:
            Comment.objects.filter(id=parent_id).update(
                reply_count=Greatest(F("reply_count") - 1, Value(0))
            )

        return len(rows)
