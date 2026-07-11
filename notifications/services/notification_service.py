from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from notifications.models import Notification
from notifications.services.fcm_service import FCMService
from accounts.models import User
from organization.models import OrganizationMember

# A single recruitment can attract hundreds of applies; only one push per
# recruitment is sent within this window (extra rows are still saved so the
# in-app grouped notification keeps counting).
RECRUITMENT_APPLICATION_PUSH_WINDOW = timedelta(minutes=10)

# Copy for org-initiated application status changes, keyed by the new status.
# `verb` attaches to the org name ("{Org} shortlisted your application"); `body`
# attaches to "Your application for {title} …". Rejected copy stays gentle.
# Shared with grouping_service so in-app + push text can't drift.
RECRUITMENT_STATUS_COPY = {
    "reviewing":   {"verb": "is reviewing your application", "body": "is now being reviewed"},
    "shortlisted": {"verb": "shortlisted your application",  "body": "was shortlisted"},
    "invited":     {"verb": "invited you to the trial",      "body": "has an invitation for you"},
    "selected":    {"verb": "selected you 🎉",               "body": "— you've been selected 🎉"},
    "rejected":    {"verb": "reviewed your application",      "body": "was not selected this time"},
}
RECRUITMENT_STATUS_COPY_DEFAULT = {"verb": "updated your application", "body": "was updated"}

def _resolve_actor_display(notification: "Notification"):
    """
    Return (name, username, avatar) for whichever actor
    (user or org) triggered the notification.
    """
    if notification.actor_user:
        actor = notification.actor_user
        profile = getattr(actor, "profile", None)
        name = profile.name if profile else (actor.username or str(actor.id))
        avatar = profile.profile_photo if profile else ""
        username = actor.username or ""
    else:
        actor = notification.actor_org
        profile = getattr(actor, "profile", None)   # OrganizationProfile
        name = actor.name or actor.username
        avatar = profile.logo if profile else ""
        username = actor.username or ""

    return name, username, avatar


def _get_recipient_users(notification: "Notification") -> list:
    """
    Return a list of User objects that should receive the push.
    - recipient_user  → [recipient_user]
    - recipient_org   → members with OWNER / ADMIN role
    Returns [] if nobody to notify.
    """
    if notification.recipient_user:
        return [notification.recipient_user]

    if notification.recipient_org:
        member_user_ids = OrganizationMember.objects.filter(
            organization=notification.recipient_org,
            role__in=[
                OrganizationMember.Role.OWNER,
                OrganizationMember.Role.ADMIN,
            ]
        ).values_list("user_id", flat=True)

        return list(User.objects.filter(id__in=member_user_ids, is_active=True))

    return []


def build_notification_payload(notification: "Notification") -> dict:
    """
    Build the FCM / websocket payload for any actor type.
    """
    actor_name, actor_username, actor_avatar = _resolve_actor_display(notification)

    title = "Goatza"
    body = "Tap to view"
    url = "/"

    if notification.type == Notification.Type.LIKE:
        title = f"{actor_name} liked your post"
        body = "Tap to view"
        url = f"/post/{notification.post_id}"

    elif notification.type == Notification.Type.COMMENT:
        comment_text = getattr(notification.comment, "comment", "")   # field is `comment`, not `content`
        short_comment = (
            comment_text[:60] + "..." if len(comment_text) > 60 else comment_text
        )
        if notification.group_key and notification.group_key.startswith("reply:"):
            title = f"{actor_name} replied to your comment"
        else:
            title = f"{actor_name} commented on your post"
        body = short_comment or "Tap to view"
        url = f"/post/{notification.post_id}"

    elif notification.type == Notification.Type.FOLLOW:
        title = f"{actor_name} started following you"
        body = "Tap to view profile"
        url = f"/profile/{actor_username}"

    elif notification.type == Notification.Type.FOLLOW_BACK:
        title = f"{actor_name} followed you back"
        body = "Tap to view profile"
        url = f"/profile/{actor_username}"

    elif notification.type == Notification.Type.RECRUITMENT_APPLICATION:
        recruitment_title = notification.data.get(
            "recruitment_title", "your recruitment"
        )
        recruitment_id = (
            notification.recruitment_id
            or notification.data.get("recruitment_id", "")
        )
        title = f"{actor_name} applied to {recruitment_title}"
        body = "Tap to view applicants"
        url = (
            f"/organization/admin/{notification.recipient_org_id}"
            f"/recruitments/{recruitment_id}?tab=applicants"
        )

    elif notification.type == Notification.Type.RECRUITMENT_APPLICATION_STATUS:
        to_status = notification.data.get("to_status", "")
        recruitment_title = getattr(
            notification.recruitment, "title", "your recruitment"
        )
        recruitment_id = (
            notification.recruitment_id
            or notification.data.get("recruitment_id", "")
        )
        copy = RECRUITMENT_STATUS_COPY.get(
            to_status, RECRUITMENT_STATUS_COPY_DEFAULT
        )
        title = f"{actor_name} {copy['verb']}"
        body = f"Your application for {recruitment_title} {copy['body']}"
        url = f"/recruitments/{recruitment_id}"

    return {
        "type": notification.type,
        "notification_id": str(notification.id),

        # actor
        "actor_name": actor_name,
        "actor_username": actor_username,
        "actor_avatar": actor_avatar,
        "actor_initials": actor_name[:2].upper() if actor_name else "??",

        # target
        "target_id": str(notification.post_id or ""),
        # recruitment deep-link id (empty for non-recruitment types)
        "recruitment_id": str(notification.recruitment_id or ""),

        # push content
        "title": title,
        "body": body,
        "url": url,

        # grouping
        "group_key": notification.group_key or "",
    }


def _dispatch(notification: "Notification") -> None:
    """
    Build payload and fan out push to all relevant recipient users.
    Wraps FCMService so callers don't need to think about user vs org.
    """
    payload = build_notification_payload(notification)
    for user in _get_recipient_users(notification):
        FCMService.send_to_user(user, payload)


# ─────────────────────────────────────────────
# SERVICE
# ─────────────────────────────────────────────

class NotificationService:

    @staticmethod
    def _actor_kwargs(actor_user=None, actor_org=None) -> dict:
        return {"actor_user": actor_user, "actor_org": actor_org}

    @staticmethod
    def _recipient_kwargs(recipient_user=None, recipient_org=None) -> dict:
        return {"recipient_user": recipient_user, "recipient_org": recipient_org}

    # ──────────────────────────────────────────
    # FOLLOW
    # ──────────────────────────────────────────
    @staticmethod
    def follow(
        actor_user=None, actor_org=None,
        target_user=None, target_org=None,
    ):
        """
        Notify the follow target. Deduplicated per actor→target pair.
        Supports all four combinations: user→user, user→org, org→user, org→org.
        """
        actor_id = actor_user.id if actor_user else f"org_{actor_org.id}"
        target_id = target_user.id if target_user else f"org_{target_org.id}"
        dedup_key = f"follow:{actor_id}:{target_id}"

        if Notification.objects.filter(dedup_key=dedup_key).exists():
            return

        notification = Notification.objects.create(
            type=Notification.Type.FOLLOW,
            dedup_key=dedup_key,
            group_key=dedup_key,
            **NotificationService._actor_kwargs(actor_user, actor_org),
            **NotificationService._recipient_kwargs(target_user, target_org),
        )
        _dispatch(notification)

    @staticmethod
    def follow_back(
        actor_user=None, actor_org=None,
        target_user=None, target_org=None,
    ):
        """
        Notify the original follower that the target followed back.
        Deduplicated per actor→target pair.
        """
        actor_id = actor_user.id if actor_user else f"org_{actor_org.id}"
        target_id = target_user.id if target_user else f"org_{target_org.id}"
        dedup_key = f"follow_back:{actor_id}:{target_id}"

        if Notification.objects.filter(dedup_key=dedup_key).exists():
            return

        notification = Notification.objects.create(
            type=Notification.Type.FOLLOW_BACK,
            dedup_key=dedup_key,
            group_key=dedup_key,
            **NotificationService._actor_kwargs(actor_user, actor_org),
            **NotificationService._recipient_kwargs(target_user, target_org),
        )
        _dispatch(notification)

    # ──────────────────────────────────────────
    # LIKE
    # ──────────────────────────────────────────
    @staticmethod
    def like(actor_user=None, actor_org=None, post=None):
        """
        Notify the post author when someone likes their post.
        Skips self-likes for both user and org actors.
        """
        # Self-like guard — covers all four actor/author combos
        if actor_user and post.author_user_id == actor_user.id:
            return
        if actor_org and post.author_org_id == actor_org.id:
            return

        notification = Notification.objects.create(
            type=Notification.Type.LIKE,
            group_key=f"like:post:{post.id}",
            post=post,
            data={"post_id": str(post.id)},
            **NotificationService._actor_kwargs(actor_user, actor_org),
            **NotificationService._recipient_kwargs(post.author_user, post.author_org),
        )
        _dispatch(notification)

    # ──────────────────────────────────────────
    # COMMENT
    # ──────────────────────────────────────────
    @staticmethod
    def comment(actor_user=None, actor_org=None, comment=None):
        """
        Notify:
          1. The post author (unless they are the commenter).
          2. The reply target's author if this is a reply (unless already notified).

        Works for all combinations of user/org actor and user/org post author.
        """
        post = comment.post
        notified_recipient_ids: set[str] = set()  # tracks "user:{id}" / "org:{id}" keys

        def _already_notified(user=None, org=None) -> bool:
            key = f"user:{user.id}" if user else f"org:{org.id}"
            if key in notified_recipient_ids:
                return True
            notified_recipient_ids.add(key)
            return False

        def _is_self(user=None, org=None) -> bool:
            """True when the recipient IS the actor (don't self-notify)."""
            if actor_user and user and actor_user.id == user.id:
                return True
            if actor_org and org and actor_org.id == org.id:
                return True
            return False

        # ── 1. POST OWNER ────────────────────────────────────────────────
        post_owner_user = post.author_user
        post_owner_org = post.author_org

        if not _is_self(post_owner_user, post_owner_org):
            notification = Notification.objects.create(
                type=Notification.Type.COMMENT,
                group_key=f"comment:post:{post.id}",
                post=post,
                comment=comment,
                **NotificationService._actor_kwargs(actor_user, actor_org),
                **NotificationService._recipient_kwargs(post_owner_user, post_owner_org),
            )
            _dispatch(notification)
            _already_notified(post_owner_user, post_owner_org)   # mark as done

        # ── 2. REPLY TARGET ──────────────────────────────────────────────
        if comment.reply_to:
            reply_target_user = comment.reply_to.user
            reply_target_org = comment.reply_to.organization

            if (
                not _is_self(reply_target_user, reply_target_org)
                and not _already_notified(reply_target_user, reply_target_org)
            ):
                notification = Notification.objects.create(
                    type=Notification.Type.COMMENT,
                    group_key=f"reply:comment:{comment.reply_to.id}",
                    post=post,
                    comment=comment,
                    **NotificationService._actor_kwargs(actor_user, actor_org),
                    **NotificationService._recipient_kwargs(
                        reply_target_user, reply_target_org
                    ),
                )
                _dispatch(notification)

    # ──────────────────────────────────────────
    # RECRUITMENT APPLICATION
    # ──────────────────────────────────────────
    @staticmethod
    def recruitment_application(actor_user, recruitment):
        """
        Notify the owning org that a player applied to a recruitment.

        In-app rows are grouped per recruitment (group_key) so many applies
        collapse into one "X, Y and N others applied to …" entry. Push is
        throttled: only the first apply within a 10-minute window pushes —
        later applies still save a row (for the grouped count) but skip the
        push, so a popular recruitment can't storm the org's devices.
        """
        recipient_org = recruitment.organization
        group_key = f"application:recruitment:{recruitment.id}"
        dedup_key = f"application:{actor_user.id}:{recruitment.id}"

        # Idempotent — re-apply is blocked upstream, but never double-notify.
        if Notification.objects.filter(dedup_key=dedup_key).exists():
            return

        # Decide the push BEFORE inserting this row: was there already a row for
        # this recruitment in the throttle window? (This row is always saved.)
        window_start = timezone.now() - RECRUITMENT_APPLICATION_PUSH_WINDOW
        recently_pushed = (
            Notification.objects
            .filter(group_key=group_key, created_at__gte=window_start)
            .exists()
        )

        notification = Notification.objects.create(
            type=Notification.Type.RECRUITMENT_APPLICATION,
            group_key=group_key,
            dedup_key=dedup_key,
            recruitment=recruitment,
            data={
                "recruitment_id": str(recruitment.id),
                "recruitment_title": recruitment.title,
            },
            **NotificationService._actor_kwargs(actor_user=actor_user),
            **NotificationService._recipient_kwargs(recipient_org=recipient_org),
        )

        if not recently_pushed:
            _dispatch(notification)

    # ──────────────────────────────────────────
    # RECRUITMENT APPLICATION STATUS (org → applicant)
    # ──────────────────────────────────────────
    @staticmethod
    def recruitment_application_status(
        actor_org, recipient_user, recruitment, to_status, application_id
    ):
        """
        Notify the applicant that the org changed their application status.

        One notification per change (no dedup) — flip a status twice and the
        player gets two rows. group_key is left empty so grouping renders each
        as its own entry (ungrouped).
        """
        notification = Notification.objects.create(
            type=Notification.Type.RECRUITMENT_APPLICATION_STATUS,
            recruitment=recruitment,
            data={
                "application_id": str(application_id),
                "recruitment_id": str(recruitment.id),
                "recruitment_title": recruitment.title,
                "to_status": to_status,
            },
            **NotificationService._actor_kwargs(actor_org=actor_org),
            **NotificationService._recipient_kwargs(recipient_user=recipient_user),
        )
        _dispatch(notification)