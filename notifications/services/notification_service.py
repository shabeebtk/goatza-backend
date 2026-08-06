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

# Copy for shared-content messages, keyed by what was shared. Shared with
# grouping_service so in-app + push text can't drift.
MESSAGE_SHARE_NOUN = {
    "post": "a post",
    "recruitment": "a recruitment",
}

# Copy for an org's decision on a career entry, keyed by notification type.
# `verb` attaches to the org name ("{Org} verified your career entry"), `body`
# is the fallback line when the org gave no reason. Rejected copy stays gentle —
# the entry is not deleted, it just goes back to being the player's own claim.
# Shared with grouping_service so in-app + push text can't drift.
CAREER_DECISION_COPY = {
    "career_verified": {
        "verb": "verified your career entry ✅",
        "body": "is now verified on your profile",
    },
    "career_rejected": {
        "verb": "could not verify your career entry",
        "body": "is still shown as self-reported",
    },
}

# Copy for an org's decision on an achievement, keyed by notification type.
# Same shape and tone as CAREER_DECISION_COPY — a separate map rather than more
# keys in that one, so neither domain's copy can be changed by accident while
# editing the other. Rejected copy stays gentle for the same reason: the award is
# not deleted, it just goes back to being the owner's own claim.
# Shared with grouping_service so in-app + push text can't drift.
ACHIEVEMENT_DECISION_COPY = {
    "achievement_verified": {
        "verb": "verified your achievement ✅",
        "body": "is now verified on your profile",
    },
    "achievement_rejected": {
        "verb": "could not verify your achievement",
        "body": "is still shown as self-reported",
    },
}

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

    elif notification.type == Notification.Type.MENTION:
        post_text = getattr(notification.post, "content", "") or ""
        short_text = post_text[:60] + "..." if len(post_text) > 60 else post_text
        title = f"{actor_name} mentioned you in a post"
        body = short_text or "Tap to view"
        # Same shape the like/comment pushes use, so the service worker's
        # existing post deep-link handling covers this type with no change.
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

    elif notification.type == Notification.Type.MESSAGE:
        conversation_id = notification.data.get("conversation_id", "")
        shared_kind = notification.data.get("shared_kind", "")
        noun = MESSAGE_SHARE_NOUN.get(shared_kind, "something")
        title = f"{actor_name} shared {noun} with you"
        # The note is the sender's own caption — safe to surface, and it's what
        # makes the push worth opening. Falls back to a generic nudge.
        body = notification.data.get("note", "") or "Tap to view"
        url = f"/messages/{conversation_id}"

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

    elif notification.type == Notification.Type.CAREER_VERIFICATION_REQUEST:
        entry_title = notification.data.get("entry_title", "a career entry")
        title = f"{actor_name} listed you on their career"
        body = f"{entry_title} — tap to verify or reject"
        url = (
            f"/organization/admin/{notification.recipient_org_id}"
            f"/career-verifications"
        )

    elif notification.type == Notification.Type.ACHIEVEMENT_VERIFICATION_REQUEST:
        achievement_title = notification.data.get(
            "achievement_title", "an achievement"
        )
        title = f"{actor_name} credited you with an achievement"
        body = f"{achievement_title} — tap to verify or reject"
        # The review screen is one page with a domain tab, not two routes —
        # /achievement-verifications does not exist and a push landing there
        # would 404. The service worker navigates to this URL verbatim.
        url = (
            f"/organization/admin/{notification.recipient_org_id}"
            f"/verifications?tab=achievements"
        )

    elif notification.type == Notification.Type.CAREER_ADD_PROMPT:
        recruitment_title = getattr(
            notification.recruitment, "title", "a recruitment"
        )
        application_id = notification.data.get("application_id", "")
        recruitment_id = (
            notification.recruitment_id
            or notification.data.get("recruitment_id", "")
        )
        # Deliberately different from the "selected you 🎉" status push that
        # goes out with it: that one announces the result, this one is the
        # call to action.
        title = f"Add {actor_name} to your career"
        body = f"You were selected for {recruitment_title} — add it to your profile"
        url = f"/recruitments/{recruitment_id}?addToCareer={application_id}"

    elif notification.type in (
        Notification.Type.CAREER_VERIFIED,
        Notification.Type.CAREER_REJECTED,
    ):
        entry_title = notification.data.get("entry_title", "Your career entry")
        copy = CAREER_DECISION_COPY[notification.type]
        # The org's own words when they gave a reason, the stock line otherwise.
        reason = notification.data.get("reason", "")
        title = f"{actor_name} {copy['verb']}"
        body = reason or f"{entry_title} {copy['body']}"
        url = (
            f"/profile/{notification.data.get('owner_username', '')}"
            f"?tab=career"
        )

    elif notification.type in (
        Notification.Type.ACHIEVEMENT_VERIFIED,
        Notification.Type.ACHIEVEMENT_REJECTED,
    ):
        achievement_title = notification.data.get(
            "achievement_title", "Your achievement"
        )
        copy = ACHIEVEMENT_DECISION_COPY[notification.type]
        # The org's own words when they gave a reason, the stock line otherwise.
        reason = notification.data.get("reason", "")
        title = f"{actor_name} {copy['verb']}"
        body = reason or f"{achievement_title} {copy['body']}"
        # A fragment, not `?tab=`: the profile page has no tab router, it has an
        # `id="achievements"` section, and the fragment is what actually scrolls
        # the owner to the award that changed.
        url = (
            f"/profile/{notification.data.get('owner_username', '')}"
            f"#achievements"
        )

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
    # MENTION
    # ──────────────────────────────────────────
    @staticmethod
    def mention(
        actor_user=None, actor_org=None, post=None,
        mentioned_user=None, mentioned_org=None,
    ):
        """
        Notify a user or org that the post's author named them in the body.

        Works for all four actor/target combinations. Callers pass only the
        NEWLY added mentions (see post_content_service.sync_post_content), so
        editing a post never re-notifies someone who was already tagged.

        Deduplicated per (post, target): a mention can't repeat within a post —
        the unique constraints on PostMention guarantee it — so this only ever
        guards against a retried write, never a legitimate second mention.
        """
        if post is None:
            return

        def _is_self() -> bool:
            """True when the mention target IS the author actor."""
            if actor_user and mentioned_user and actor_user.id == mentioned_user.id:
                return True
            if actor_org and mentioned_org and actor_org.id == mentioned_org.id:
                return True
            return False

        if _is_self():
            return

        target_key = (
            f"user:{mentioned_user.id}" if mentioned_user
            else f"org:{mentioned_org.id}"
        )
        dedup_key = f"mention:{post.id}:{target_key}"

        if Notification.objects.filter(dedup_key=dedup_key).exists():
            return

        notification = Notification.objects.create(
            type=Notification.Type.MENTION,
            # One post mentions one target at most once, so this group can
            # never hold more than a single row for a given recipient — it
            # exists to keep the grouped list's keying uniform, not to count.
            group_key=f"mention:post:{post.id}",
            dedup_key=dedup_key,
            post=post,
            data={"post_id": str(post.id)},
            **NotificationService._actor_kwargs(actor_user, actor_org),
            **NotificationService._recipient_kwargs(mentioned_user, mentioned_org),
        )
        _dispatch(notification)

    # ──────────────────────────────────────────
    # MESSAGE SHARE
    # ──────────────────────────────────────────
    @staticmethod
    def message_share(
        message,
        recipient_user=None,
        recipient_org=None,
    ):
        """
        Notify the other side of a conversation that content was shared with
        them.

        Grouped per conversation, so a burst of shares into one thread collapses
        into a single "X shared 3 things with you" entry rather than three.
        Deduplicated per message, so a retried send can never double-notify.

        Unlike recruitment applies there is no push throttle: a share is a
        deliberate 1:1 message, and every message pushes today (see
        MessageService._trigger_push). Grouping only affects the in-app list.
        """
        conversation_id = message.conversation_id
        dedup_key = f"message_share:{message.id}"

        if Notification.objects.filter(dedup_key=dedup_key).exists():
            return

        shared_kind = (
            "post"
            if message.message_type == "shared_post"
            else "recruitment"
        )

        notification = Notification.objects.create(
            type=Notification.Type.MESSAGE,
            group_key=f"message_share:conversation:{conversation_id}",
            dedup_key=dedup_key,
            # Deliberately NOT setting the post/recruitment FK. The grouped
            # notification list renders those with PostMiniSerializer, which
            # applies no visibility check — safe for likes/comments (the
            # recipient IS the author) but not here, where the recipient may
            # not be allowed to see what was shared. The notification only says
            # "X shared something"; the visibility-checked preview lives on the
            # message. Ids stay in `data` for deep-linking.
            data={
                "conversation_id": str(conversation_id),
                "message_id": str(message.id),
                "shared_kind": shared_kind,
                "shared_id": str(
                    message.shared_post_id or message.shared_recruitment_id
                ),
                "note": message.content[:120],
            },
            **NotificationService._actor_kwargs(
                message.sender_user, message.sender_org
            ),
            **NotificationService._recipient_kwargs(
                recipient_user, recipient_org
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

    # ──────────────────────────────────────────
    # CAREER ADD PROMPT (org selection → player)
    # ──────────────────────────────────────────
    @staticmethod
    def career_add_prompt(actor_org, recipient_user, recruitment, application_id):
        """
        Nudge a selected applicant to turn the result into a career entry.

        Sent ALONGSIDE the ordinary "selected" status notification, not instead
        of it: one announces the outcome, this one is the call to action, and
        they deep-link to different things.

        Deduplicated per application, so an org that flips an application out of
        and back into ``selected`` prompts once and not once per flip — by then
        the player has either added the entry or chosen not to.

        Ungrouped (no group_key): being selected is a singular event and should
        never collapse into a count.
        """
        dedup_key = f"career_add_prompt:{application_id}"

        if Notification.objects.filter(dedup_key=dedup_key).exists():
            return

        notification = Notification.objects.create(
            type=Notification.Type.CAREER_ADD_PROMPT,
            dedup_key=dedup_key,
            recruitment=recruitment,
            data={
                "application_id": str(application_id),
                "recruitment_id": str(recruitment.id),
                "recruitment_title": recruitment.title,
                "organization_name": recruitment.organization.name,
            },
            **NotificationService._actor_kwargs(actor_org=actor_org),
            **NotificationService._recipient_kwargs(recipient_user=recipient_user),
        )
        _dispatch(notification)

    # ──────────────────────────────────────────
    # CAREER VERIFICATION (player → org)
    # ──────────────────────────────────────────
    @staticmethod
    def career_verification_request(actor_user, career_entry):
        """
        Tell an organization that someone has listed them on their career and
        is waiting for a decision.

        Fired every time an entry *enters* pending for this org — first claim,
        a material edit that invalidated an existing verification, or a re-tag
        onto a different club. An entry that was already pending and stays
        pending does not re-notify: nothing changed for the reviewer.

        Grouped per org, so a club that gets listed by half a squad sees one
        "A, B and 12 others listed you on their career" entry rather than
        fifteen. Not deduplicated — a re-request after an edit is a genuinely
        new decision to make.

        Recipients are the org's OWNER/ADMIN members (``_get_recipient_users``)
        — exactly the roles allowed to act on the request.
        """
        recipient_org = career_entry.organization

        if recipient_org is None:
            return

        notification = Notification.objects.create(
            type=Notification.Type.CAREER_VERIFICATION_REQUEST,
            group_key=f"career_verification:org:{recipient_org.id}",
            career_entry=career_entry,
            data={
                "career_entry_id": str(career_entry.id),
                "entry_title": career_entry.title,
                "organization_name": career_entry.organization_name,
            },
            **NotificationService._actor_kwargs(actor_user=actor_user),
            **NotificationService._recipient_kwargs(recipient_org=recipient_org),
        )
        _dispatch(notification)

    # ──────────────────────────────────────────
    # ACHIEVEMENT VERIFICATION (player → org)
    # ──────────────────────────────────────────
    @staticmethod
    def achievement_verification_request(actor_user, achievement):
        """
        Tell an organization that someone has credited them with issuing an
        award and is waiting for a decision.

        Fired every time an achievement *enters* pending for this org — first
        claim, a material edit that invalidated an existing verification, or a
        re-link onto a different org. One that was already pending and stays
        pending does not re-notify: nothing changed for the reviewer.

        Grouped per org, exactly like the career request, so a federation that
        hands out fifty medals at one tournament sees one "A, B and 48 others
        credited you with an achievement" entry rather than fifty. Not
        deduplicated — a re-request after an edit is a genuinely new decision to
        make.

        Recipients are the org's OWNER/ADMIN members (``_get_recipient_users``)
        — exactly the roles allowed to act on the request.
        """
        recipient_org = achievement.awarded_by

        if recipient_org is None:
            return

        notification = Notification.objects.create(
            type=Notification.Type.ACHIEVEMENT_VERIFICATION_REQUEST,
            group_key=f"achievement_verification:org:{recipient_org.id}",
            achievement=achievement,
            data={
                "achievement_id": str(achievement.id),
                "achievement_title": achievement.title,
                "organization_name": achievement.awarded_by_name,
            },
            **NotificationService._actor_kwargs(actor_user=actor_user),
            **NotificationService._recipient_kwargs(recipient_org=recipient_org),
        )
        _dispatch(notification)

    # ──────────────────────────────────────────
    # ACHIEVEMENT DECISION (org → owner)
    # ──────────────────────────────────────────
    @staticmethod
    def achievement_verified(actor_org, achievement):
        """Tell the owner their award was confirmed by the issuing org."""
        NotificationService._achievement_decision(
            Notification.Type.ACHIEVEMENT_VERIFIED,
            actor_org,
            achievement,
        )

    @staticmethod
    def achievement_rejected(actor_org, achievement, reason=""):
        """
        Tell the owner the org would not confirm their award.

        ``reason`` is the org's own short note. It is carried on the
        notification only — the achievement itself keeps no rejection text — so
        this payload is the single place that copy exists.
        """
        NotificationService._achievement_decision(
            Notification.Type.ACHIEVEMENT_REJECTED,
            actor_org,
            achievement,
            reason=reason,
        )

    @staticmethod
    def _achievement_decision(notification_type, actor_org, achievement, reason=""):
        """
        Shared write for both decisions. One notification per decision (no
        dedup) and no group_key, so an org that verifies then later rejects
        leaves both rows in the owner's list rather than collapsing them.
        """
        owner = achievement.user

        notification = Notification.objects.create(
            type=notification_type,
            achievement=achievement,
            data={
                "achievement_id": str(achievement.id),
                "achievement_title": achievement.title,
                "organization_name": achievement.awarded_by_name,
                # The deep link is to the *recipient's* profile, not the
                # actor's — the org decided, but the award lives on the owner.
                "owner_username": owner.username or "",
                "reason": (reason or "").strip(),
            },
            **NotificationService._actor_kwargs(actor_org=actor_org),
            **NotificationService._recipient_kwargs(recipient_user=owner),
        )
        _dispatch(notification)

    # ──────────────────────────────────────────
    # CAREER DECISION (org → player)
    # ──────────────────────────────────────────
    @staticmethod
    def career_verified(actor_org, career_entry):
        """Tell the player their claim was confirmed by the club."""
        NotificationService._career_decision(
            Notification.Type.CAREER_VERIFIED,
            actor_org,
            career_entry,
        )

    @staticmethod
    def career_rejected(actor_org, career_entry, reason=""):
        """
        Tell the player the club would not confirm their claim.

        ``reason`` is the org's own short note. It is carried on the
        notification only — the entry itself keeps no rejection text — so this
        payload is the single place that copy exists.
        """
        NotificationService._career_decision(
            Notification.Type.CAREER_REJECTED,
            actor_org,
            career_entry,
            reason=reason,
        )

    @staticmethod
    def _career_decision(notification_type, actor_org, career_entry, reason=""):
        """
        Shared write for both decisions. One notification per decision (no
        dedup) and no group_key, so a club that verifies then later rejects
        leaves both rows in the player's list rather than collapsing them.
        """
        owner = career_entry.user

        notification = Notification.objects.create(
            type=notification_type,
            career_entry=career_entry,
            data={
                "career_entry_id": str(career_entry.id),
                "entry_title": career_entry.title,
                "organization_name": career_entry.organization_name,
                # The deep link is to the *recipient's* profile, not the
                # actor's — the org decided, but the entry lives on the player.
                "owner_username": owner.username or "",
                "reason": (reason or "").strip(),
            },
            **NotificationService._actor_kwargs(actor_org=actor_org),
            **NotificationService._recipient_kwargs(recipient_user=owner),
        )
        _dispatch(notification)