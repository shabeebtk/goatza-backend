# recruitments/services/announcement_service.py
"""
Announcements: an org speaking to the people on one recruitment.

THE RULE THIS SERVICE IS BUILT AROUND: the request WRITES rows, a cron job
SENDS them. ``create`` writes one ``AnnouncementDelivery`` per recipient per
channel and returns without sending a single notification or email;
``manage.py dispatch_announcements`` drains them.

That is not caution, it is arithmetic. ``utils.emails.send_email_async`` is one
daemon OS thread per email, ``utils.background_jobs.enqueue`` runs inline while
CELERY_ENABLED is off, and the existing bulk fan-out is capped at 100. An
announcement to 340 confirmed players sent from its own request would be 340
OS threads and 340 inline FCM calls on a web dyno. See the
``AnnouncementDelivery`` model docstring.

The audience resolver is ONE function, ``audience_queryset``, shared by the
recipients-count endpoint and the create path — so the number the org is shown
before sending is the number that gets written, and not a second
implementation that agrees with the first until someone edits one of them.
"""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import TruncMinute
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.moderation.models import Block
from apps.organization.models import OrganizationMember
from apps.recruitments.models import (
    AnnouncementDelivery,
    Recruitment,
    RecruitmentAnnouncement,
    RecruitmentApplication,
)

logger = logging.getLogger(__name__)

Audience = RecruitmentAnnouncement.Audience
Channel = AnnouncementDelivery.Channel
State = AnnouncementDelivery.State
Status = RecruitmentApplication.Status


# Who each audience means, as application statuses.
#
#   all_applicants  everyone still in the pipeline. Withdrawn is the one
#                   exclusion: they took themselves off the list.
#   confirmed       everyone who was CALLED to the trial and therefore has a
#                   reason to care where and when it is — including the people
#                   already given a result, because "the venue has moved" is
#                   not news you withhold from someone who was told they did
#                   not make it but is still on the day's list.
#   selected        the squad.
AUDIENCE_STATUSES = {
    Audience.CONFIRMED: (
        Status.TRIAL_CONFIRMED,
        Status.SELECTED,
        Status.NOT_SELECTED,
    ),
    Audience.SELECTED: (Status.SELECTED,),
}

# Every channel an announcement goes out on. `dm` delivers it as a real
# Goatza message; see RecruitmentMessageService for why that is worth doing
# rather than routing people to WhatsApp.
WRITTEN_CHANNELS = (Channel.DM, Channel.NOTIFICATION, Channel.EMAIL)

# A direct message is one channel only. It IS a Goatza message — there is
# no separate in-app row to write, and mailing somebody the same sentence
# again would be two notifications for one thing the org said once.
DIRECT_CHANNELS = (Channel.DM,)

# Announcements one recruitment may send per calendar day in
# RECRUITMENT_TIMEZONE. A cap, not a rate limit: the failure mode this guards
# is an org treating announcements as a chat window, and the cost of that is
# every applicant muting their notifications.
MAX_PER_DAY = 5

# Applicants one direct message may name. The same ceiling the bulk status
# change uses, and for the same reason: it is the tested one.
MAX_BULK_MESSAGE = 100

# Org roles that may speak FOR the organization. A coach reads the pipeline;
# announcing to every applicant is the club's voice.
REVIEWER_ROLES = (
    OrganizationMember.Role.OWNER,
    OrganizationMember.Role.ADMIN,
)


def _local_now():
    return timezone.now().astimezone(ZoneInfo(settings.RECRUITMENT_TIMEZONE))


class AnnouncementService:

    # -----------------------------------------------------------------
    # GATE
    # -----------------------------------------------------------------
    @staticmethod
    def require_reviewer(actor):
        """
        The org-side gate. Returns the OrganizationMember, or raises
        PermissionDenied (→ 403).

        ``resolve_actor`` has already proved the logged-in user is a member of
        the org they claim to act as; this only adds the role rule on top.
        Public because the write views call it BEFORE validating the body: a
        coach should get a 403, not a critique of a payload that was never
        going to be accepted.
        """
        if actor is None or not actor.is_org or actor.organization is None:
            raise PermissionDenied(
                "Switch to your organization account to post an announcement."
            )

        member = actor.organization_member
        if member is None or member.role not in REVIEWER_ROLES:
            raise PermissionDenied(
                "Only organization owners and admins can post announcements."
            )

        return member

    # -----------------------------------------------------------------
    # AUDIENCE — the one resolver
    # -----------------------------------------------------------------
    @staticmethod
    def audience_queryset(recruitment, audience, session=None):
        """
        The applications an announcement goes to. THE one implementation:
        both the recipients-count endpoint and ``create`` call this, so the
        number quoted to the org is the number that is written.

        ``session`` narrows to the people attending ONE date. It only means
        anything in choose_one mode — that is the only mode where an
        application names a date at all — and is ignored otherwise rather
        than emptying the list.
        """
        queryset = RecruitmentApplication.objects.filter(
            recruitment=recruitment
        ).exclude(status=Status.WITHDRAWN)

        statuses = AUDIENCE_STATUSES.get(audience)
        if statuses:
            queryset = queryset.filter(status__in=statuses)

        if (
            session is not None
            and recruitment.session_mode == Recruitment.SessionMode.CHOOSE_ONE
        ):
            queryset = queryset.filter(session=session)

        return queryset

    @staticmethod
    def recipients_count(recruitment, audience, session=None):
        """How many people an announcement would reach, before sending it."""
        return AnnouncementService.audience_queryset(
            recruitment, audience, session
        ).count()

    # -----------------------------------------------------------------
    # BLOCKS
    # -----------------------------------------------------------------
    @staticmethod
    def _blocked_user_ids(organization, user_ids):
        """
        Which of these people are blocked with the posting org, EITHER way.

        Two queries for any audience size, not ``is_blocked`` per recipient:
        that guard is a per-pair lookup and 340 of them inside one request is
        the kind of fan-out this whole design exists to avoid.

        Symmetric, like the guard itself — the org blocking a player and the
        player blocking the org are indistinguishable here, and both mean the
        message does not go.
        """
        if not user_ids:
            return set()

        blocked = set(
            Block.objects.filter(
                blocker_org=organization, blocked_user_id__in=user_ids
            ).values_list("blocked_user_id", flat=True)
        )
        blocked |= set(
            Block.objects.filter(
                blocked_org=organization, blocker_user_id__in=user_ids
            ).values_list("blocker_user_id", flat=True)
        )
        return blocked

    # -----------------------------------------------------------------
    # DAILY CAP
    # -----------------------------------------------------------------
    @staticmethod
    def _check_daily_limit(recruitment):
        """
        At most MAX_PER_DAY announcements per recruitment per calendar day in
        RECRUITMENT_TIMEZONE — the same clock every other date rule here uses.

        Soft-deleted announcements COUNT. They were sent; deleting one takes
        nothing back, so letting a delete buy another send would make the cap
        a formality.

        DIRECT MESSAGES COUNT TOO, against the same budget. They reach the
        same people through the same channel; keeping two separate caps
        would just mean ten sends a day through whichever one had budget
        left, which is the thing the cap exists to stop.
        """
        local_now = _local_now()
        start_of_day = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        sent_today = RecruitmentAnnouncement.objects.filter(
            recruitment=recruitment, created_at__gte=start_of_day
        ).count()

        # One SEND, not one row: a direct message to 40 people is 40
        # deliveries and one thing the org did. Grouped by the minute it
        # was written, which is how a bulk_create's rows arrive.
        sent_today += (
            AnnouncementDelivery.objects
            .filter(
                announcement__isnull=True,
                application__recruitment=recruitment,
                created_at__gte=start_of_day,
            )
            .annotate(minute=TruncMinute("created_at"))
            .values("minute")
            .distinct()
            .count()
        )

        if sent_today < MAX_PER_DAY:
            return

        tomorrow = (start_of_day + timedelta(days=1)).strftime(
            "%d %b"
        )
        raise ValidationError(
            f"You've sent {MAX_PER_DAY} announcements for this trial today. "
            f"You can send more from midnight, {tomorrow}."
        )

    # -----------------------------------------------------------------
    # CREATE — writes rows, sends NOTHING
    # -----------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def create(actor, recruitment, validated_data):
        """
        Write the announcement and its outbox. Nothing is sent here: no
        email, no notification, no enqueue. ``dispatch_announcements`` does
        the sending, and that separation is the point of the feature.

        Everything is one transaction, so an announcement can never exist
        without its deliveries — a half-written outbox would send to half the
        audience and there would be no record of the other half.
        """
        member = AnnouncementService.require_reviewer(actor)
        AnnouncementService._check_daily_limit(recruitment)

        session = validated_data.get("session")

        # A date only means something where an applicant picked one. Stored as
        # null otherwise, rather than kept as decoration the resolver ignores:
        # the row would then claim an audience it did not have.
        if recruitment.session_mode != Recruitment.SessionMode.CHOOSE_ONE:
            session = None

        announcement = RecruitmentAnnouncement.objects.create(
            recruitment=recruitment,
            created_by_member=member,
            title=validated_data["title"],
            body=validated_data["body"],
            audience=validated_data["audience"],
            session=session,
        )

        applications = list(
            AnnouncementService.audience_queryset(
                recruitment, announcement.audience, session
            ).select_related("applicant")
        )

        blocked_ids = AnnouncementService._blocked_user_ids(
            recruitment.organization,
            [application.applicant_id for application in applications],
        )

        rows = []
        for application in applications:
            applicant = application.applicant
            is_blocked = applicant.id in blocked_ids

            for channel in WRITTEN_CHANNELS:
                # A skip is WRITTEN, never omitted, so the delivery summary
                # adds up to recipients_count and "138 of 142" can be read as
                # a fact rather than a discrepancy.
                skipped = is_blocked or (
                    channel == Channel.EMAIL and not applicant.email
                )
                rows.append(
                    AnnouncementDelivery(
                        announcement=announcement,
                        application=application,
                        recipient=applicant,
                        channel=channel,
                        state=State.SKIPPED if skipped else State.PENDING,
                    )
                )

        if rows:
            AnnouncementDelivery.objects.bulk_create(rows)

        # DISTINCT PEOPLE, not delivery rows. One application per
        # (recruitment, applicant) is a DB constraint, so this is the size of
        # the audience that was resolved — and exactly the count of
        # `notification` rows written, which is what makes the summary
        # reconcile.
        announcement.recipients_count = len(applications)
        announcement.save(update_fields=["recipients_count"])

        logger.info(
            "AnnouncementService.create | announcement_id=%s | "
            "recruitment_id=%s | audience=%s | recipients=%s | rows=%s",
            announcement.id, recruitment.id, announcement.audience,
            announcement.recipients_count, len(rows),
        )

        return announcement

    # -----------------------------------------------------------------
    # DIRECT MESSAGES ("message these players")
    # -----------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def create_direct(actor, recruitment, application_ids, body):
        """
        Queue a private message to specific applicants. Writes rows and sends
        NOTHING, exactly like an announcement — same outbox, same drain, same
        daily cap.

        NOT an announcement: no announcement row, nothing pinned, nothing on
        the posting. The org is messaging people, not publishing an update, and
        the two should not be made to look alike.

        EACH PLAYER GETS IT INDIVIDUALLY. One delivery per applicant, one
        direct conversation each — nobody is in a group and nobody sees anybody
        else. The composer says so; this is what makes it true.

        Any org member may send one. Announcing to the whole trial is the
        club's voice and is owner/admin-gated; messaging the four people you
        just watched play is the job of whoever watched them.

        Returns {"queued": n, "skipped": [{"id", "reason"}]}.
        """
        if actor is None or not actor.is_org or actor.organization is None:
            raise PermissionDenied(
                "Switch to your organization account to message applicants."
            )

        if len(application_ids) > MAX_BULK_MESSAGE:
            raise ValidationError(
                f"Cannot message more than {MAX_BULK_MESSAGE} applicants at "
                f"once."
            )

        AnnouncementService._check_daily_limit(recruitment)

        applications = {
            str(application.id): application
            for application in RecruitmentApplication.objects
            .filter(id__in=application_ids, recruitment=recruitment)
            .select_related("applicant")
        }

        blocked_ids = AnnouncementService._blocked_user_ids(
            recruitment.organization,
            [a.applicant_id for a in applications.values()],
        )

        rows = []
        skipped = []
        member = actor.organization_member

        for raw_id in application_ids:
            application = applications.get(str(raw_id))

            if application is None:
                skipped.append({"id": str(raw_id), "reason": "not_found"})
                continue
            if application.status == Status.WITHDRAWN:
                skipped.append({"id": str(raw_id), "reason": "withdrawn"})
                continue

            # A blocked pair is skipped WITHOUT saying so to the org — the
            # same rule the block guard applies everywhere else. The row is
            # still written, so the counts reconcile.
            state = (
                State.SKIPPED
                if application.applicant_id in blocked_ids
                else State.PENDING
            )

            for channel in DIRECT_CHANNELS:
                rows.append(
                    AnnouncementDelivery(
                        announcement=None,
                        application=application,
                        recipient=application.applicant,
                        channel=channel,
                        state=state,
                        direct_body=body,
                        created_by_member=member,
                    )
                )

        if rows:
            AnnouncementDelivery.objects.bulk_create(rows)

        logger.info(
            "AnnouncementService.create_direct | recruitment_id=%s | "
            "queued=%s | skipped=%s",
            recruitment.id, len(rows), len(skipped),
        )

        return {"queued": len(rows), "skipped": skipped}

    # -----------------------------------------------------------------
    # DELETE — soft, and recalls nothing
    # -----------------------------------------------------------------
    @staticmethod
    def delete(actor, announcement):
        """
        Take an announcement off the lists. Returns True if it moved.

        It does NOT unsend anything. Notifications and emails that already
        left are gone, and any PENDING deliveries are left alone rather than
        cancelled — see the note in ``dispatch_announcements``, which skips a
        deleted announcement's pending rows on its next pass. The API says so
        in words so the client can put it in front of the org BEFORE they
        press it.
        """
        AnnouncementService.require_reviewer(actor)

        if announcement.is_deleted:
            return False

        announcement.is_deleted = True
        announcement.save(update_fields=["is_deleted"])
        return True

    # -----------------------------------------------------------------
    # DELIVERY SUMMARY
    # -----------------------------------------------------------------
    @staticmethod
    def delivery_summaries(announcement_ids):
        """
        ``{announcement_id: {"sent": n, "pending": n, "failed": n,
        "skipped": n}}`` for a whole page in ONE grouped query.

        Every key is always present and zero-filled, so the org reads
        "Delivered to 138 of 142" instead of wondering whether a send is stuck
        or the field is simply missing.
        """
        summaries = {
            str(announcement_id): {
                State.SENT: 0,
                State.PENDING: 0,
                State.FAILED: 0,
                State.SKIPPED: 0,
            }
            for announcement_id in announcement_ids
        }

        if not summaries:
            return summaries

        rows = (
            AnnouncementDelivery.objects
            .filter(announcement_id__in=announcement_ids)
            .values("announcement_id", "state")
            .annotate(total=Count("id"))
        )

        for row in rows:
            bucket = summaries.get(str(row["announcement_id"]))
            if bucket is not None and row["state"] in bucket:
                bucket[row["state"]] += row["total"]

        return summaries
