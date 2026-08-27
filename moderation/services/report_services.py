"""
Reporting — the write side.

Three rules separate this from every other write service in the app, and each
one is load-bearing:

  * NO BLOCK GUARD, anywhere. Every other write path in the codebase calls
    ``require_not_blocked`` first. Reporting must not: the single most likely
    thing a person does after blocking a harasser is report them, and a guard
    here would refuse exactly that. Blocking and reporting are the two halves
    of the same gesture, so a block in either direction is irrelevant to this
    service.

  * DEDUP IS A SUCCESS, not an error. A second open report on the same target
    by the same reporter returns the FIRST one with ``already_reported=True``
    and the endpoint still answers 200. The client's report sheet is fire-and-
    forget — a 400 on the second tap tells the reporter their report failed,
    which is both false and discouraging.

  * NOTHING IS EVER AUTO-HIDDEN. No count, no category, no number of distinct
    reporters removes or hides content here (spec §2.4.6). Reports only ever
    raise ``is_priority`` so a human sees them sooner. Anything that hides
    content on a threshold is a brigading tool.

The other subtlety is the SNAPSHOT. ``content_snapshot`` is captured at report
time and never updated, so an author who edits or deletes the reported content
afterwards cannot rewrite what a moderator sees. That is also why the model's
target FKs are SET_NULL — the evidence outlives the row.
"""

import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from core.constant import TYPE_ORGANIZATION, TYPE_USER
from messaging.models import ConversationParticipant
from moderation.models import (
    OPEN_REPORT_STATUSES,
    Report,
    ReportAction,
    ReportCategory,
    ReportStatus,
)
from utils.emails import send_email_async

logger = logging.getLogger(__name__)


# The ONE message every "you cannot report this" path returns.
#
# Byte-identical on purpose: a message you are not a participant of and a
# message id that never existed must be indistinguishable, or the endpoint
# becomes an oracle for probing whether a given message id is real.
TARGET_NOT_FOUND = "Not found"

# Target types accepted on the wire, mapped to the service kwarg each resolves
# into. Also the serializer's ChoiceField source, so the two cannot drift.
TARGET_TYPES = {
    TYPE_USER: "target_user",
    TYPE_ORGANIZATION: "target_org",
    "post": "target_post",
    "comment": "target_comment",
    "message": "target_message",
    "recruitment": "target_recruitment",
}

# Service kwarg -> the model column it writes.
_TARGET_COLUMNS = {
    "target_user": "reported_user",
    "target_org": "reported_org",
    "target_post": "reported_post",
    "target_comment": "reported_comment",
    "target_message": "reported_message",
    "target_recruitment": "reported_recruitment",
}


class ReportService:

    # Categories that jump the queue on their own and are the only ones that
    # generate mail. Everything else waits its turn in the admin list — a
    # spam report at 3am is not worth a notification, a minor-safety one is.
    SEVERE_CATEGORIES = (
        ReportCategory.MINOR_SAFETY,
        ReportCategory.SELF_HARM,
        ReportCategory.VIOLENCE,
    )

    # Distinct reporter identities on one target that make it priority
    # regardless of category. Distinct IDENTITIES, not rows — the model's
    # partial uniques already cap one open report per identity, so this cannot
    # be inflated by one person tapping twice.
    PRIORITY_REPORTER_THRESHOLD = 3

    # Free text is optional and advisory. Capped because it lands in an admin
    # list and in an email body.
    MAX_DETAILS_LENGTH = 2000

    # =================================================================
    # PUBLIC ENTRY POINT
    # =================================================================

    @staticmethod
    def create(
        actor,
        *,
        category,
        details="",
        target_user=None,
        target_org=None,
        target_post=None,
        target_comment=None,
        target_message=None,
        target_recruitment=None,
    ):
        """
        actor: request.actor
        Exactly one target_* required.

        Returns ``(True, {...})`` on success — including the dedup path, where
        the payload carries ``already_reported=True`` and the id of the report
        that already exists. Returns ``(False, message)`` for the answers a
        reporter can act on ("Cannot report yourself"), and raises NotFound
        with a generic message for everything that must not be confirmed.
        """
        targets = {
            "target_user": target_user,
            "target_org": target_org,
            "target_post": target_post,
            "target_comment": target_comment,
            "target_message": target_message,
            "target_recruitment": target_recruitment,
        }

        given = {key: value for key, value in targets.items() if value is not None}

        error = ReportService._validate(actor, category, given)
        if error:
            return False, error

        kwarg, target = next(iter(given.items()))
        column = _TARGET_COLUMNS[kwarg]

        # NO BLOCK GUARD HERE — see the module docstring. This is the one write
        # path in the app that must work between blocked parties.

        existing = ReportService._open_report_by(actor, column, target)
        if existing:
            return True, ReportService._payload(existing, already_reported=True)

        return ReportService._create_report(
            actor=actor,
            column=column,
            target=target,
            category=category,
            details=(details or "").strip()[:ReportService.MAX_DETAILS_LENGTH],
        )

    # =================================================================
    # VALIDATION
    # =================================================================

    @staticmethod
    def _validate(actor, category, given):
        """
        Returns an error string, or None when the report may proceed.

        String outcomes rather than exceptions for the same reason
        BlockService._validate_target uses them: "you cannot report yourself"
        is a normal answer to a normal request, not a fault. The paths that
        must NOT be confirmed raise NotFound instead.
        """
        if actor is None:
            return "Authentication required"

        if len(given) != 1:
            return "Exactly one target is required"

        if category not in ReportCategory.values:
            return "Invalid category"

        kwarg, target = next(iter(given.items()))

        if ReportService._is_self(actor, kwarg, target):
            return "You cannot report your own content"

        ReportService._require_visible(actor, kwarg, target)

        return None

    @staticmethod
    def _is_self(actor, kwarg, target):
        """
        Is the acting identity the thing being reported, or its author?

        Identity, not membership: acting as yourself you may report a post your
        club published, and acting as the club you may not. The actor headers
        decide who is speaking, and that is the identity this rule applies to —
        same split BlockService draws.
        """
        actor_user_id = actor.user.id if actor.is_user else None
        actor_org_id = actor.organization.id if actor.is_org else None

        def is_author(user_id, org_id):
            return (
                (actor_user_id is not None and user_id == actor_user_id) or
                (actor_org_id is not None and org_id == actor_org_id)
            )

        if kwarg == "target_user":
            return is_author(target.id, None)

        if kwarg == "target_org":
            return is_author(None, target.id)

        if kwarg == "target_post":
            return is_author(target.author_user_id, target.author_org_id)

        if kwarg == "target_comment":
            return is_author(target.user_id, target.organization_id)

        if kwarg == "target_message":
            return is_author(target.sender_user_id, target.sender_org_id)

        # Recruitments are org-owned only — there is no user-authored variant.
        return is_author(None, target.organization_id)

    @staticmethod
    def _require_visible(actor, kwarg, target):
        """
        Raise NotFound — always with the same generic string — for a target
        this reporter must not be told about.

        Soft-deleted post / comment / recruitment: already gone from every read
        surface, so accepting a report on one would confirm it once existed.

        MESSAGES are deliberately exempt from the soft-delete rule and get a
        PARTICIPANT check instead. A harasser deleting their message the moment
        after it lands is the exact case reporting exists for, so a deleted
        message stays reportable — but only by someone who was in the thread.
        """
        if kwarg in ("target_post", "target_comment", "target_recruitment"):
            if target.is_deleted:
                raise NotFound(TARGET_NOT_FOUND)
            return

        if kwarg == "target_message":
            if actor.is_user:
                side = {"user": actor.user}
            else:
                side = {"org": actor.organization}

            in_thread = ConversationParticipant.objects.filter(
                conversation_id=target.conversation_id,
                **side,
            ).exists()

            if not in_thread:
                raise NotFound(TARGET_NOT_FOUND)

    # =================================================================
    # ACTOR / TARGET PLUMBING
    # =================================================================

    @staticmethod
    def _actor_filters(actor):
        """``{"reporter_user": <User>}`` or ``{"reporter_org": <Organization>}``."""
        if actor.is_user:
            return {"reporter_user": actor.user}
        return {"reporter_org": actor.organization}

    @staticmethod
    def _identity_of(actor):
        return actor.user if actor.is_user else actor.organization

    @staticmethod
    def _open_report_by(actor, column, target):
        """This reporter's still-open report on this target, or None."""
        return Report.objects.filter(
            **ReportService._actor_filters(actor),
            **{column: target},
            status__in=OPEN_REPORT_STATUSES,
        ).first()

    @staticmethod
    def _payload(report, *, already_reported):
        return {
            "report_id": str(report.id),
            "already_reported": already_reported,
            "status": report.status,
            "is_priority": report.is_priority,
        }

    # =================================================================
    # WRITE
    # =================================================================

    @staticmethod
    @transaction.atomic
    def _create_report(actor, column, target, category, details):
        severe = category in ReportService.SEVERE_CATEGORIES

        try:
            report = Report.objects.create(
                **ReportService._actor_filters(actor),
                **{column: target},
                category=category,
                details=details,
                content_snapshot=ReportService._snapshot(column, target),
                is_priority=severe,
            )
        except IntegrityError:
            # Lost the race against a concurrent duplicate — the partial unique
            # (uniq_open_report_<reporter>_<target>) is the real dedup, the
            # pre-check above is only the cheap path. Re-read and answer the
            # same way the pre-check would have.
            existing = ReportService._open_report_by(actor, column, target)

            if existing is None:
                raise

            return True, ReportService._payload(existing, already_reported=True)

        # Counted AFTER the insert so this report is part of the total.
        if not severe:
            ReportService._apply_reporter_threshold(report, column, target)

        if severe:
            # After COMMIT: send_email_async only spawns a thread, so a mail
            # queued inside the transaction can describe a report that then
            # rolls back and links to an admin page that 404s.
            transaction.on_commit(lambda: ReportService._alert(report))

        logger.info(
            "[REPORT] reporter=%s target=%s:%s category=%s priority=%s",
            ReportService._identity_of(actor).id,
            column,
            target.id,
            category,
            report.is_priority,
        )

        return True, ReportService._payload(report, already_reported=False)

    @staticmethod
    def _apply_reporter_threshold(report, column, target):
        """
        Raise priority on the whole pile once enough DISTINCT identities have
        an open report on one target.

        Counts identity pairs, not rows, and flips every open sibling too — a
        moderator sorting by priority must see the pile, not just the report
        that happened to cross the line.
        """
        open_on_target = Report.objects.filter(
            **{column: target},
            status__in=OPEN_REPORT_STATUSES,
        )

        reporters = open_on_target.values(
            "reporter_user_id", "reporter_org_id"
        ).distinct().count()

        if reporters < ReportService.PRIORITY_REPORTER_THRESHOLD:
            return

        open_on_target.filter(is_priority=False).update(is_priority=True)

        report.is_priority = True

        logger.info(
            "[REPORT] target=%s:%s reached %s distinct reporters, pile marked priority",
            column, target.id, reporters,
        )

    # =================================================================
    # SNAPSHOT
    # =================================================================
    #
    # Every helper returns JSON primitives only — uuid and datetime are
    # stringified at the edge, because JSONField serializes with the stdlib
    # encoder and would otherwise raise at write time.

    @staticmethod
    def _snapshot(column, target):
        builders = {
            "reported_user": ReportService._snapshot_user,
            "reported_org": ReportService._snapshot_org,
            "reported_post": ReportService._snapshot_post,
            "reported_comment": ReportService._snapshot_comment,
            "reported_message": ReportService._snapshot_message,
            "reported_recruitment": ReportService._snapshot_recruitment,
        }

        try:
            return builders[column](target)
        except Exception as e:
            # A snapshot is evidence, not a precondition. A missing profile row
            # or an unexpected shape must never be the reason a report of a
            # child-safety issue fails to land.
            logger.error("[REPORT] snapshot failed for %s | %s", column, str(e))
            return {"type": column, "id": str(target.id), "snapshot_error": str(e)}

    @staticmethod
    def _iso(value):
        return value.isoformat() if value else None

    @staticmethod
    def _author(user, org):
        """The dual-actor author of a piece of content, as flat primitives."""
        if user:
            return {
                "type": TYPE_USER,
                "id": str(user.id),
                "username": user.username or "",
                "name": getattr(getattr(user, "profile", None), "name", "") or "",
            }

        if org:
            return {
                "type": TYPE_ORGANIZATION,
                "id": str(org.id),
                "username": org.username or "",
                "name": org.name or "",
            }

        return None

    @staticmethod
    def _snapshot_user(user):
        profile = getattr(user, "profile", None)

        return {
            "type": TYPE_USER,
            "id": str(user.id),
            "username": user.username or "",
            "name": getattr(profile, "name", "") or "",
            "bio": getattr(profile, "about", "") or "",
            "avatar_url": getattr(profile, "profile_photo", "") or "",
            "role": user.role,
            "created_at": ReportService._iso(user.created_at),
        }

    @staticmethod
    def _snapshot_org(org):
        profile = getattr(org, "profile", None)

        return {
            "type": TYPE_ORGANIZATION,
            "id": str(org.id),
            "username": org.username or "",
            "name": org.name or "",
            "bio": getattr(profile, "description", "") or "",
            "avatar_url": getattr(profile, "logo", "") or "",
            "org_type": org.type,
        }

    @staticmethod
    def _snapshot_post(post):
        media = [
            {
                "url": item.file_url,
                "media_type": item.media_type,
                "thumbnail_url": item.thumbnail_url or "",
            }
            for item in post.media.all()
        ]

        return {
            "type": "post",
            "id": str(post.id),
            "content": post.content or "",
            "media": media,
            "author": ReportService._author(post.author_user, post.author_org),
            "visibility": post.visibility,
            "created_at": ReportService._iso(post.created_at),
        }

    @staticmethod
    def _snapshot_comment(comment):
        return {
            "type": "comment",
            "id": str(comment.id),
            "text": comment.comment or "",
            "author": ReportService._author(comment.user, comment.organization),
            "post_id": str(comment.post_id),
            "parent_comment_id": str(comment.parent_id) if comment.parent_id else None,
            "created_at": ReportService._iso(comment.created_at),
        }

    @staticmethod
    def _snapshot_message(message):
        return {
            "type": "message",
            "id": str(message.id),
            "text": message.content or "",
            "media_url": message.media_url or "",
            "message_type": message.message_type,
            "sender": ReportService._author(message.sender_user, message.sender_org),
            "conversation_id": str(message.conversation_id),
            "created_at": ReportService._iso(message.created_at),
        }

    @staticmethod
    def _snapshot_recruitment(recruitment):
        excerpt = (recruitment.short_description or recruitment.description or "")[:300]

        return {
            "type": "recruitment",
            "id": str(recruitment.id),
            "title": recruitment.title or "",
            "excerpt": excerpt,
            "organization": ReportService._author(None, recruitment.organization),
            "status": recruitment.status,
            "created_at": ReportService._iso(recruitment.created_at),
        }

    # =================================================================
    # ALERT
    # =================================================================

    @staticmethod
    def admin_url(report_id):
        """
        Deep link to the report's admin page.

        Relative when SITE_ADMIN_BASE_URL is unset, so a local run still gets a
        clickable path rather than a broken absolute URL to production.
        """
        base = (getattr(settings, "SITE_ADMIN_BASE_URL", "") or "").rstrip("/")
        path = f"/admin/moderation/report/{report_id}/change/"

        return f"{base}{path}" if base else path

    @staticmethod
    def _target_label(report):
        """A one-line description of what was reported, for the mail body."""
        snapshot = report.content_snapshot or {}
        kind = snapshot.get("type", "content")

        handle = (
            snapshot.get("username")
            or (snapshot.get("author") or {}).get("username")
            or (snapshot.get("sender") or {}).get("username")
            or (snapshot.get("organization") or {}).get("username")
            or ""
        )

        label = f"{kind} {snapshot.get('id', '')}".strip()

        return f"{label} (@{handle})" if handle else label

    @staticmethod
    def _alert(report):
        """
        Mail a severe report to the moderation inbox. Best effort, always.

        Wrapped whole rather than just around the send, for the same reason
        PlayerSignupService._notify is: ``send_email_async`` only spawns a
        thread, so the body formatting is what actually runs in-process here,
        and neither it nor the spawn may ever be the reason a report is lost.
        """
        try:
            recipient = getattr(settings, "MODERATION_ALERT_EMAIL", "")

            if not recipient:
                logger.info(
                    "[REPORT] MODERATION_ALERT_EMAIL is not set, skipping the "
                    "priority alert for report=%s", report.id,
                )
                return

            reporter = ReportService._identity_of_report(report)

            body = "\n".join([
                f"Category:  {report.get_category_display()} ({report.category})",
                f"Target:    {ReportService._target_label(report)}",
                f"Reporter:  {reporter}",
                f"Details:   {report.details or '-'}",
                "",
                f"Review it: {ReportService.admin_url(report.id)}",
            ])

            send_email_async(
                subject=f"[Goatza] Priority report: {report.category}",
                message=body,
                to_email=recipient,
            )

            logger.info("[REPORT] priority alert queued for report=%s", report.id)

        except Exception as e:
            logger.error("[REPORT] alert failed for report=%s | %s", report.id, str(e))

    @staticmethod
    def _identity_of_report(report):
        """The reporter as a readable handle, for the mail body."""
        if report.reporter_user_id:
            return f"@{report.reporter_user.username or report.reporter_user_id} (user)"

        return f"@{report.reporter_org.username or report.reporter_org_id} (organization)"

    # =================================================================
    # STATUS TRANSITIONS
    # =================================================================
    #
    # The admin actions are thin wrappers over these — same layering rule the
    # rest of the repo follows, and the reason it matters here is that the
    # enforcement actions landing next (remove content, warn, suspend) all end
    # by RESOLVING a report. They must resolve it the same way Dismiss does, so
    # the transition lives in one place rather than being retyped per action.
    #
    # Every one of these returns a bool — did this row actually move — because
    # the admin action's message ("3 dismissed, 1 skipped") is the moderator's
    # only feedback on a multi-select, and a silent no-op on an already-closed
    # row reads as success.

    @staticmethod
    def mark_reviewing(report, staff_user):
        """
        PENDING -> REVIEWING, stamping who picked it up.

        Only from PENDING. A row already REVIEWING keeps its original
        ``reviewed_by`` — the person who claimed it first is the one holding
        it, and re-stamping on a second click would quietly reassign the case.
        A resolved row does not reopen: that needs a deliberate edit, not a
        bulk action.
        """
        if report.status != ReportStatus.PENDING:
            return False

        report.status = ReportStatus.REVIEWING
        report.reviewed_by = staff_user
        report.save(update_fields=["status", "reviewed_by"])

        logger.info(
            "[REPORT] %s -> reviewing by %s", report.id, getattr(staff_user, "id", None)
        )

        return True

    @staticmethod
    def dismiss(report, staff_user, note=""):
        """
        Open (PENDING or REVIEWING) -> DISMISSED. Nothing happened to the
        target: ``action_taken`` stays NONE, which is what makes "reviewed and
        found fine" distinguishable from "not looked at yet".

        Auto-resolves the target's other open reports, like every
        enforcement action (spec 2.5): a moderator who looked at the
        content and found it fine has answered every complaint about it,
        not just the row they happened to click. The siblings are stamped
        with the same reviewer, status and action, and a note naming the
        report the decision came from.
        """
        return ReportService._resolve(
            report,
            staff_user,
            status=ReportStatus.DISMISSED,
            action=ReportAction.NONE,
            note=note,
        )

    @staticmethod
    def _resolve(report, staff_user, *, status, action, note=""):
        """
        The one closing transition. Skips a row that is already closed —
        re-resolving would overwrite the first decision's reviewer and
        timestamp with a second one that decided nothing.

        An empty ``note`` LEAVES the existing resolution_note alone rather than
        blanking it: a moderator who typed a note on the detail page and then
        ran the action from the list must not lose it.
        """
        if report.status not in OPEN_REPORT_STATUSES:
            return False

        fields = ["status", "reviewed_by", "reviewed_at", "action_taken"]

        report.status = status
        report.reviewed_by = staff_user
        report.reviewed_at = timezone.now()
        report.action_taken = action

        if note:
            report.resolution_note = note
            fields.append("resolution_note")

        report.save(update_fields=fields)

        siblings = ReportService._resolve_siblings(report, staff_user, status, action)

        logger.info(
            "[REPORT] %s -> %s (action=%s) by %s siblings=%s",
            report.id, status, action, getattr(staff_user, "id", None), siblings,
        )

        return True

    @staticmethod
    def _resolve_siblings(report, staff_user, status, action):
        """
        Close every OTHER open report on the same target with the same outcome.
        Returns how many moved.

        One decision clears the pile (spec §2.5). Without this, removing a post
        that fifty people reported leaves forty-nine rows in the queue that a
        moderator has to click through to reach real work, and every one they
        open shows content that is already gone.

        Their note names the report the decision came from, so the audit trail
        survives: any sibling can be traced back to the row a human actually
        looked at.

        A report with NO target (content hard-deleted afterwards) has no
        siblings to find. Matching on "all six columns NULL" would sweep in
        every other orphaned report in the table, which share nothing but their
        emptiness — so those are left alone and resolved one at a time.
        """
        column, target = ReportService._target_of(report)

        if target is None:
            return 0

        return (
            Report.objects
            .filter(**{column: target}, status__in=OPEN_REPORT_STATUSES)
            .exclude(id=report.id)
            .update(
                status=status,
                action_taken=action,
                reviewed_by=staff_user,
                reviewed_at=timezone.now(),
                resolution_note=f"Auto-resolved with report {report.id}",
            )
        )

    # =================================================================
    # ENFORCEMENT
    # =================================================================
    #
    # Four actions, one shape: decide, act on the target, resolve this report,
    # resolve its siblings. Each returns ``(moved: bool, error: str|None)`` —
    # the admin action needs to tell "done" from "skipped" from "you picked the
    # wrong action for this target", and those are three different outcomes.
    #
    # NONE of them hard-deletes anything, and none of them touches the report's
    # content_snapshot. A takedown must stay reversible and its evidence must
    # outlive it, or the queue becomes a place where mistakes are permanent.

    # Which targets each action can act on. Checked before anything is written
    # so a wrong multi-select costs an error message, not a half-applied batch.
    _REMOVABLE = (
        "reported_post",
        "reported_comment",
        "reported_message",
        "reported_recruitment",
    )

    @staticmethod
    @transaction.atomic
    def remove_content(report, staff_user, note=""):
        """
        Soft-delete the reported content through its own app's moderator path.

        Dispatches to PostService / MessageService / RecruitmentService rather
        than writing ``is_deleted`` here, because each type has mechanics this
        service must not reimplement — a comment cascades to its replies and
        decrements two counters, a message moves the conversation's
        last_message pointer and fires the realtime delete event.

        Accounts are not content: a user or org target is refused so the
        moderator reaches for Suspend instead of silently doing nothing.
        """
        column, target = ReportService._target_of(report)

        if column not in ReportService._REMOVABLE:
            return False, (
                "Remove content only applies to a post, comment, message or "
                "recruitment — use a suspend action for an account"
            )

        if target is None:
            return False, "The reported content no longer exists"

        removed = ReportService._soft_delete(column, target)

        if not removed:
            # Already down — most often because a sibling report on the same
            # content was actioned a moment ago. Still resolve THIS report:
            # the outcome the moderator wanted is the outcome that holds.
            logger.info("[REPORT] %s target already removed", report.id)

        ReportService._resolve(
            report,
            staff_user,
            status=ReportStatus.ACTION_TAKEN,
            action=ReportAction.CONTENT_REMOVED,
            note=note,
        )

        return True, None

    @staticmethod
    def _soft_delete(column, target):
        """Hand the target to its own app's moderator takedown."""
        from messaging.services.message_service import MessageService
        from posts.services.post_service import PostService
        from recruitments.services.recruitment_service import RecruitmentService

        if column == "reported_post":
            return PostService.moderator_delete_post(target)

        if column == "reported_comment":
            return PostService.moderator_delete_comment(target)

        if column == "reported_message":
            return MessageService.moderator_delete_message(target)

        return RecruitmentService.moderator_delete_recruitment(target)

    @staticmethod
    @transaction.atomic
    def warn_author(report, staff_user, note=""):
        """
        Tell the account behind the reported thing that it broke the rules.

        Works for all six target types: for content it is the author, for an
        account it is the account itself. The notification carries no category,
        no reporter and no moderator — see
        NotificationService.moderation_warning.
        """
        from notifications.services.notification_service import NotificationService

        column, target = ReportService._target_of(report)

        if target is None:
            return False, "The reported content no longer exists"

        author_user, author_org = ReportService._author_of(column, target)

        if author_user is None and author_org is None:
            return False, "Could not resolve who to warn"

        NotificationService.moderation_warning(
            recipient_user=author_user,
            recipient_org=author_org,
            post=target if column == "reported_post" else None,
            comment=target if column == "reported_comment" else None,
            recruitment=target if column == "reported_recruitment" else None,
        )

        ReportService._resolve(
            report,
            staff_user,
            status=ReportStatus.ACTION_TAKEN,
            action=ReportAction.WARNING_SENT,
            note=note,
        )

        return True, None

    @staticmethod
    @transaction.atomic
    def suspend_user(report, staff_user, note=""):
        """
        Deactivate the reported USER and kill every session they hold.

        ``is_active = False`` alone only stops the NEXT authenticated request
        that re-reads the user — but an access token is valid for 15 minutes
        and a refresh token for 30 days, so a suspension without the blacklist
        is a suspension that starts whenever the offender's current token
        happens to expire. Blacklisting every OutstandingToken closes the
        refresh path immediately; SimpleJWT's CHECK_USER_IS_ACTIVE (on by
        default, not overridden in settings) rejects the still-live access
        token on its next use, and the websocket middleware now does the same.
        """
        column, target = ReportService._target_of(report)

        if target is None:
            return False, "The reported content no longer exists"

        author_user, author_org = ReportService._author_of(column, target)

        if author_user is None:
            if author_org is not None:
                return False, (
                    "That target belongs to an organization — use "
                    "'Suspend organization'"
                )
            return False, "Could not resolve a user account to suspend"

        if author_user.is_staff or author_user.is_superuser:
            # A moderation queue that can lock out staff is a moderation queue
            # one compromised account away from locking out everyone.
            return False, "Staff accounts cannot be suspended from the queue"

        if author_user.is_active:
            author_user.is_active = False
            author_user.save(update_fields=["is_active"])

        killed = ReportService._blacklist_tokens(author_user)

        logger.info(
            "[REPORT] suspended user=%s tokens_blacklisted=%s by=%s",
            author_user.id, killed, getattr(staff_user, "id", None),
        )

        ReportService._resolve(
            report,
            staff_user,
            status=ReportStatus.ACTION_TAKEN,
            action=ReportAction.ACCOUNT_SUSPENDED,
            note=note,
        )

        return True, None

    @staticmethod
    def _blacklist_tokens(user):
        """
        Every refresh token this user holds, revoked. Returns how many.

        get_or_create per token, not bulk_create: a token already blacklisted
        by rotation would raise on the unique, and the point of this call is
        that it always finishes.
        """
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken,
            OutstandingToken,
        )

        killed = 0

        for token in OutstandingToken.objects.filter(user=user):
            _, created = BlacklistedToken.objects.get_or_create(token=token)
            killed += int(created)

        return killed

    @staticmethod
    @transaction.atomic
    def suspend_organization(report, staff_user, note=""):
        """
        Suspend the ORG behind the report — the reported org itself, or the org
        that authored the reported content.

        ``is_suspended``, not ``is_active``: is_active is the org's own
        lifecycle and its owner can flip it, which would make a suspension
        self-clearing. Enforcement lives in core.actor.resolve_actor (nobody
        may act as it), the org read paths (gone from explore, search, its own
        profile) and recruitment_selectors (its listings go with it).

        No token blacklist here, and none is needed: an org holds no tokens.
        Its members keep their personal sessions and lose only the ability to
        speak as the club.
        """
        column, target = ReportService._target_of(report)

        if target is None:
            return False, "The reported content no longer exists"

        author_user, author_org = ReportService._author_of(column, target)

        if author_org is None:
            if author_user is not None:
                return False, (
                    "That target belongs to a user — use 'Suspend user account'"
                )
            return False, "Could not resolve an organization to suspend"

        if not author_org.is_suspended:
            author_org.is_suspended = True
            author_org.save(update_fields=["is_suspended"])

        logger.info(
            "[REPORT] suspended org=%s by=%s",
            author_org.id, getattr(staff_user, "id", None),
        )

        ReportService._resolve(
            report,
            staff_user,
            status=ReportStatus.ACTION_TAKEN,
            action=ReportAction.ACCOUNT_SUSPENDED,
            note=note,
        )

        return True, None

    # =================================================================
    # TARGET RESOLUTION
    # =================================================================

    @staticmethod
    def _target_of(report):
        """``(column, instance)`` for the one non-null target, or ``(None, None)``."""
        for column in _TARGET_COLUMNS.values():
            instance = getattr(report, column, None)
            if instance is not None:
                return column, instance

        return None, None

    @staticmethod
    def _author_of(column, target):
        """
        ``(User|None, Organization|None)`` — the identity accountable for the
        target. An account is its own author; content answers for whoever
        posted it.
        """
        if column == "reported_user":
            return target, None

        if column == "reported_org":
            return None, target

        if column == "reported_post":
            return target.author_user, target.author_org

        if column == "reported_comment":
            return target.user, target.organization

        if column == "reported_message":
            return target.sender_user, target.sender_org

        # Recruitments are org-owned only.
        return None, target.organization
