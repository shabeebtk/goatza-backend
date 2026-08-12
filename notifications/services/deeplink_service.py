"""
Notification deep links — the single place a notification's destination is
decided.

The push payload (``notification_service``), the in-app list
(``grouping_service``) and the foreground toast all read the URL from here, so
a tapped row, a background push and a toast action can never disagree about
where a notification goes.

This module imports neither of those two on purpose: both import it, and they
already share copy constants in one direction only. Keeping this a leaf is what
stops that becoming a cycle.

**The recipient's route space is resolved before anything else.** The web client
reads the URL to decide which actor the person is acting as
(``syncActorFromPath``), so an org recipient's link that escapes
``/organization/admin/<id>/…`` doesn't merely open the wrong page — it silently
switches them out of their organization.

Every path below is a real Next.js app-router route.
"""

from notifications.models import Notification


def _admin_base(org_id) -> str:
    """The org-admin route prefix for an org recipient, "" for a user."""
    return f"/organization/admin/{org_id}" if org_id else ""


def build_conversation_url(conversation_id, recipient_org_id=None) -> str:
    """
    A chat thread, in the recipient's route space.

    ``/messages/<id>`` is NOT this route: ``/messages/[username]`` is
    MessageResolver, which looks a person up by username — handing it a
    conversation UUID resolves nothing. The thread lives at
    ``/messages/chat/<id>``.

    Shared with ``messaging.services.message_service`` so an ordinary chat push
    and a share notification land on the same screen.
    """
    base = _admin_base(recipient_org_id)

    if not conversation_id:
        return f"{base}/notifications"

    return f"{base}/messages/chat/{conversation_id}"


def _owner_profile_url(data, anchor, fallback) -> str:
    """
    A career / achievement decision lands on the OWNER's profile, not on the
    org that decided — the entry lives on the player.

    A fragment, not ``?tab=``: the profile page has no tab router, it has
    ``id="career"`` / ``id="achievements"`` sections, and the fragment is what
    scrolls the owner to the row that changed.
    """
    owner_username = data.get("owner_username", "")

    return f"/profile/{owner_username}{anchor}" if owner_username else fallback


def build_notification_url(notification) -> str:
    """
    The URL a notification should open, for any type and either recipient shape.

    Falls back to the recipient's own notifications list — never "/" — whenever
    the target id is missing, so a deleted post or a half-written payload lands
    somewhere real instead of on ``/posts/None``.
    """
    org_id = notification.recipient_org_id          # None for user recipients
    base = _admin_base(org_id)
    fallback = f"{base}/notifications"

    data = notification.data or {}
    ntype = notification.type

    # ── FOLLOW ────────────────────────────────────────────────────
    if ntype in (Notification.Type.FOLLOW, Notification.Type.FOLLOW_BACK):
        if notification.actor_org_id:
            actor_username = str(notification.actor_org.username or "")
            actor_is_org = True
        else:
            actor_username = notification.actor_user.username if notification.actor_user else ""
            actor_is_org = False

        if not actor_username:
            return fallback

        if base:
            # Inside the admin space a person and a club are two different
            # routes, not one /profile/<username> that guesses.
            kind = "org" if actor_is_org else "user"
            return f"{base}/profile/{kind}/{actor_username}"

        return (
            f"/organization/profile/{actor_username}" if actor_is_org
            else f"/profile/{actor_username}"
        )

    # ── POST INTERACTIONS ─────────────────────────────────────────
    if ntype in (
        Notification.Type.LIKE,
        Notification.Type.COMMENT,
        Notification.Type.MENTION,
    ):
        post_id = notification.post_id or data.get("post_id", "")
        # Plural. /post/<id> is not a route and 404s on every one of these.
        return f"{base}/posts/{post_id}" if post_id else fallback

    # ── MESSAGE ───────────────────────────────────────────────────
    if ntype == Notification.Type.MESSAGE:
        return build_conversation_url(data.get("conversation_id", ""), org_id)

    # ── RECRUITMENT ───────────────────────────────────────────────
    if ntype == Notification.Type.RECRUITMENT_APPLICATION:
        recruitment_id = notification.recruitment_id or data.get("recruitment_id", "")
        return (
            f"{base}/recruitments/{recruitment_id}?tab=applicants"
            if recruitment_id else fallback
        )

    if ntype == Notification.Type.RECRUITMENT_APPLICATION_STATUS:
        recruitment_id = notification.recruitment_id or data.get("recruitment_id", "")
        return f"{base}/recruitments/{recruitment_id}" if recruitment_id else fallback

    if ntype == Notification.Type.CAREER_ADD_PROMPT:
        # An action, not a destination: the in-app row opens CareerAddPromptSheet
        # in place, so the notifications list is exactly where the working
        # action lives. (The old ?addToCareer=<id> param was read by nothing.)
        return fallback

    # ── VERIFICATION QUEUES (org side) ────────────────────────────
    if ntype == Notification.Type.CAREER_VERIFICATION_REQUEST:
        # /career-verifications does not exist — one review page, two tabs.
        return f"{base}/verifications" if base else fallback

    if ntype == Notification.Type.ACHIEVEMENT_VERIFICATION_REQUEST:
        return f"{base}/verifications?tab=achievements" if base else fallback

    # ── VERIFICATION DECISIONS (owner side) ───────────────────────
    if ntype in (Notification.Type.CAREER_VERIFIED, Notification.Type.CAREER_REJECTED):
        return _owner_profile_url(data, "#career", fallback)

    if ntype in (
        Notification.Type.ACHIEVEMENT_VERIFIED,
        Notification.Type.ACHIEVEMENT_REJECTED,
    ):
        return _owner_profile_url(data, "#achievements", fallback)

    return fallback
