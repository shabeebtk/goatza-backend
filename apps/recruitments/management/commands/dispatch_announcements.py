"""
Drain the announcement outbox: send what the request only wrote down.

WHY THIS COMMAND EXISTS. Creating an announcement writes one
``AnnouncementDelivery`` row per recipient per channel and sends nothing. It
has to, because of three facts about this codebase together:

  * ``utils.background_jobs.enqueue`` has no callers and CELERY_ENABLED is off
    by default, so a job dispatched from a request runs INLINE, in that
    request.
  * ``utils.emails.send_email_async`` is one daemon OS THREAD per email.
  * the existing bulk fan-out is capped at 100 and that is the tested ceiling.

An announcement to 340 confirmed players, sent from the request that created
it, would therefore be 340 OS threads and 340 inline FCM calls on a web dyno.
So: the request WRITES rows, this command SENDS them.

What one run does:

  1. CLAIM   PENDING rows with ``select_for_update(skip_locked=True)``, oldest
             first, in transactions of CHUNK_SIZE up to --limit. skip_locked
             is what lets two runs overlap harmlessly — the second takes the
             rows the first did not lock rather than blocking on them or
             double-sending. The lock is held ACROSS the send, which is what
             makes that true; the chunking is why that does not mean one
             transaction open for minutes while Resend is called 500 times.
             The cap is per RUN, not per announcement, so one 3,000-person
             send cannot monopolise a pass and starve the one behind it.
  2. ATTEMPT attempts is incremented BEFORE the send, not after. A process
             killed mid-send must count as having tried: the alternative
             retries a message that may already have gone out, forever.
  3. SEND    dm -> RecruitmentMessageService, a real Goatza message in the
             org's direct thread with the player.
             notification -> NotificationService.recruitment_announcement,
             the same path every other notification takes (it creates the row
             AND fans out the push; there is no separate push step).
             email -> the BLOCKING utils.emails.send_email, through
             ``send_announcement_email``. Never send_email_async: a thread per
             email is the exact thing this outbox exists to avoid, and a
             fire-and-forget send has no outcome to record.

             ONE PUSH, NOT TWO. A delivered dm already produces messaging's
             own push. So rows are ordered dm-first (see CHANNEL_ORDER) and
             a recipient whose dm landed gets the in-app announcement row
             written WITHOUT a second push. The row still has to exist —
             the player wants the update in their notifications list — it
             just must not buzz the phone twice for one thing.
  4. RECORD  success -> SENT + sent_at. Failure -> last_error, and at
             MAX_ATTEMPTS the row goes FAILED and is never tried again. A SENT
             row is never re-sent.

Characteristics:
  * Idempotent  — only PENDING rows are claimed and SENT is terminal, so a
                  second run immediately after a first is a no-op. The
                  outbox's unique constraint on (announcement, application,
                  channel) is what makes that true at the storage layer.
  * Safe to overlap — see skip_locked above. Two crons, or a cron and a
                  manual run, cannot send the same row twice.
  * Flat memory — one batch at a time, capped by --limit.
  * --dry-run   — claims and reports, writes nothing and sends nothing.

A DELETED announcement's pending rows are SKIPPED rather than sent. Deleting
recalls nothing that already left, but there is no reason to keep sending
something the org has retracted.

HOW IT RUNS

  Render Cron (today):
      */5 * * * *  python manage.py dispatch_announcements

  Celery beat (later, no code change): the task in
  ``apps/recruitments/tasks.py`` calls this same command, so whichever
  scheduler is in front of it runs identical code. CELERY_BEAT_SCHEDULE stays
  empty until a worker actually exists — see CLAUDE.md, "Background jobs".

    python manage.py dispatch_announcements --dry-run
    python manage.py dispatch_announcements --limit 200
    python manage.py dispatch_announcements --announcement <uuid>
"""

from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from apps.messaging.models import Message
from apps.recruitments.models import AnnouncementDelivery
from apps.recruitments.services.recruitment_message_service import (
    RecruitmentMessageService,
)
from utils.transactional_emails import send_announcement_email

Channel = AnnouncementDelivery.Channel
State = AnnouncementDelivery.State

# Tries before a row is given up on. Three is what utils.emails already uses
# per send, so a FAILED row has had nine transport attempts behind it.
MAX_ATTEMPTS = 3

# Rows per run. Deliberately well above one announcement's typical audience
# and well below "every pending row on the platform": the cap is there so one
# huge send cannot hold a pass open indefinitely.
DEFAULT_LIMIT = 500

# Rows per TRANSACTION. The row lock has to be held across the send — that is
# what stops a second run re-sending a row this one is half way through — but
# an email is a blocking HTTP call to Resend, so a 500-row batch in ONE
# transaction would hold it open for minutes. Postgres hates that (it blocks
# vacuum, and Render's idle-in-transaction timeouts end it for you), so a run
# is many short transactions up to --limit rather than one long one.
CHUNK_SIZE = 25

# Channels in SEND order, lowest first. `dm` has to go before `notification`
# so the drain knows, when it reaches the notification row, whether this
# recipient already got a push from the message. A plain column sort is all
# it takes, and it costs no extra query — which is the point.
CHANNEL_ORDER = {
    Channel.DM: 0,
    Channel.NOTIFICATION: 1,
    Channel.EMAIL: 2,
}

# last_error is a CharField(255). Truncate here rather than letting the write
# raise and lose the row's error entirely.
ERROR_MAX = 255


class Command(BaseCommand):
    help = (
        "Send pending announcement deliveries (in-app/push and email). "
        "Designed to be run every few minutes by cron."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Claim and report, but send nothing and write nothing.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_LIMIT,
            help=f"Maximum rows this run (default {DEFAULT_LIMIT}).",
        )
        parser.add_argument(
            "--announcement",
            type=str,
            default=None,
            help="Only drain one announcement, by id.",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.limit = max(1, options["limit"])
        self.announcement_id = options["announcement"]

        self.counts = Counter()
        # (announcement_id, application_id) pairs whose dm landed in this
        # run. Read when the matching `notification` row comes up, so the
        # push is suppressed without a second query. Survives chunk
        # boundaries because it is run state; a pair split across two RUNS
        # (dm claimed by one, notification by the next) simply gets the
        # extra push, which is rare and harmless.
        self.dm_sent = set()
        # Rows this RUN has already looked at. Two rows would otherwise come
        # back round in the next chunk: one left PENDING by a failed attempt
        # that has not reached MAX_ATTEMPTS (its retry belongs to the NEXT
        # run, not to this one, three chunks later), and every row in a
        # --dry-run, which writes nothing and so never leaves PENDING.
        self.seen = set()

        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — claiming and reporting only, nothing is sent."
            ))

        self._drain()
        self._report()

    # -- the pass -----------------------------------------------------

    def _pending(self, exclude_seen=False):
        queryset = AnnouncementDelivery.objects.filter(state=State.PENDING)
        if self.announcement_id:
            queryset = queryset.filter(announcement_id=self.announcement_id)
        if exclude_seen and self.seen:
            queryset = queryset.exclude(id__in=self.seen)
        return queryset

    def _drain(self):
        """Up to --limit rows, in transactions of CHUNK_SIZE."""
        claimed = 0
        while claimed < self.limit:
            batch = self._drain_chunk(min(CHUNK_SIZE, self.limit - claimed))
            if batch == 0:
                break
            claimed += batch

        if claimed == 0:
            self.stdout.write("  nothing pending")
        else:
            self.stdout.write(f"  claimed {claimed} row(s)")

    def _drain_chunk(self, size):
        """One transaction: claim, send, record. Returns how many it took."""
        with transaction.atomic():
            deliveries = list(
                self._pending(exclude_seen=True)
                # of=("self",): lock ONLY the delivery rows. The
                # select_related below reaches through two nullable FKs, and
                # Postgres refuses FOR UPDATE across the nullable side of an
                # outer join — the same reason change_status cannot ride
                # applied_position on its lock.
                .select_for_update(skip_locked=True, of=("self",))
                .select_related(
                    "announcement__recruitment__organization",
                    "announcement__session",
                    "application__applicant",
                    "recipient",
                )
                # Oldest first, then the trio for one applicant together,
                # then dm before notification. Annotated rather than a
                # Python sort so the ordering survives the LIMIT.
                .annotate(
                    channel_rank=Case(
                        *[
                            When(channel=channel, then=Value(rank))
                            for channel, rank in CHANNEL_ORDER.items()
                        ],
                        default=Value(9),
                        output_field=IntegerField(),
                    )
                )
                .order_by("created_at", "application_id", "channel_rank")[:size]
            )

            if not deliveries:
                return 0

            for delivery in deliveries:
                self.seen.add(delivery.id)
                self._send_one(delivery)

            return len(deliveries)

    def _send_one(self, delivery):
        announcement = delivery.announcement

        # Retracted after it was queued: stop sending, but do not pretend the
        # ones already delivered can be taken back. A DIRECT row has no
        # announcement to retract.
        if announcement is not None and announcement.is_deleted:
            self.counts["skipped_deleted"] += 1
            if not self.dry_run:
                delivery.state = State.SKIPPED
                delivery.last_error = "announcement deleted"
                delivery.save(update_fields=["state", "last_error"])
            return

        if self.dry_run:
            self.counts[f"would_send_{delivery.channel}"] += 1
            return

        # BEFORE the attempt, always. A process killed mid-send must count as
        # having tried, or a message that may already have gone out is retried
        # forever.
        delivery.attempts += 1

        error = ""
        try:
            sent = self._deliver(delivery, announcement)
        except Exception as exc:
            sent = False
            error = f"{type(exc).__name__}: {exc}"

        if sent:
            delivery.state = State.SENT
            delivery.sent_at = timezone.now()
            delivery.last_error = ""
            self.counts[f"sent_{delivery.channel}"] += 1
        else:
            delivery.last_error = (error or "send returned false")[:ERROR_MAX]
            if delivery.attempts >= MAX_ATTEMPTS:
                delivery.state = State.FAILED
                self.counts[f"failed_{delivery.channel}"] += 1
            else:
                self.counts[f"retry_{delivery.channel}"] += 1

        delivery.save(
            update_fields=["state", "attempts", "last_error", "sent_at"]
        )

    def _deliver(self, delivery, announcement):
        """Do the actual send. Returns whether it worked."""
        recruitment = delivery.application.recruitment
        pair = (delivery.announcement_id, delivery.application_id)

        if delivery.channel == Channel.DM:
            # A real Goatza message, through MessageService, into the org's
            # direct thread with this player. An announcement rides as a
            # recruitment CARD with the text as its caption, so the
            # existing SharedRecruitmentMessage component renders it with
            # no new message type; a direct message is plain TEXT, because
            # "come at 7 instead of 8" gains nothing from a card.
            if announcement is not None:
                body = f"{announcement.title}\n\n{announcement.body}"
                message_type = Message.Type.SHARED_RECRUITMENT
            else:
                body = delivery.direct_body
                message_type = Message.Type.TEXT

            RecruitmentMessageService.send_one(
                recruitment,
                delivery.application,
                body=body,
                message_type=message_type,
            )
            self.dm_sent.add(pair)
            return True

        if delivery.channel == Channel.NOTIFICATION:
            # Through NotificationService like everything else: it writes the
            # row and fans the push out from it, so in-app and push are one
            # operation and cannot drift apart.
            from apps.notifications.services.notification_service import (
                NotificationService,
            )

            # Already pushed as a message: write the row, skip the buzz.
            push = pair not in self.dm_sent
            if not push:
                self.counts["push_suppressed_after_dm"] += 1

            NotificationService.recruitment_announcement(
                actor_org=recruitment.organization,
                recipient_user=delivery.recipient,
                recruitment=recruitment,
                announcement=announcement,
                push=push,
            )
            return True

        if delivery.channel == Channel.EMAIL:
            # BLOCKING on purpose. See the module docstring.
            return send_announcement_email(
                announcement=announcement,
                application=delivery.application,
                blocking=True,
            )

        self.counts[f"unsupported_{delivery.channel}"] += 1
        raise ValueError(f"unsupported channel: {delivery.channel}")

    # -- report -------------------------------------------------------

    def _line(self, label, value, width=40):
        self.stdout.write(f"  {label:<{width}}: {value}")

    def _report(self):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("THIS RUN"))

        if not self.counts:
            self._line("nothing to do", 0)
        for key in sorted(self.counts):
            self._line(key.replace("_", " "), self.counts[key])

        still_pending = self._pending().count()
        self._line("still pending after this run", still_pending)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            "Done (dry-run — nothing sent)." if self.dry_run else "Done."
        ))
