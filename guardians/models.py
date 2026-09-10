"""
Parental consent: the guardian, and the trail of what they were asked and what
they answered.

A GUARDIAN IS NOT A USER. There is no account here, no role, no login and no
password — a parent asked to approve their child's signup should not have to
become a member of a sports network in order to do it, and giving them an
account would put an adult identity on the platform whose only reason to exist
is being somebody's parent. So a guardian is a CONTACT: a name and a way to
reach them, owned by the consent flow and by nothing else. One parent can stand
behind several children (siblings), which is why this is its own table rather
than a handful of columns on ``UserProfile``.

The pair of tables mirrors ``legal``: an append-only evidence table plus a
denormalized status on ``accounts.User``. That shape is not a coincidence —
both answer the same kind of question months after the fact ("who agreed to
what, when, and how do you know"), and both are read on a path far too hot to
be a query. ``User.guardian_consent_status`` is the cache; the events below are
the record it is derived from.

Nothing outside this app writes either table. ``services/consent_service.py``
is the only writer, ``selectors/consent_selectors.py`` the read side, and the
views that expose them land next.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from guardians.constants import (
    GuardianConsentEventType,
    GuardianConsentLevel,
    GuardianConsentMethod,
)
from shared.models import BaseUUIDModel


# The choices live in guardians/constants.py, not here. Four other modules
# name them -- the service, the selectors, the serializers and the admin --
# and none of them should have to import a model to spell a string. See the
# module docstring there.


class Guardian(BaseUUIDModel):
    """
    One parent or legal guardian, as a contact record. May cover many children.

    Deliberately thin. The only reason this row exists is to be reachable and
    to be recognisable to the child who named it, so it holds a name and the
    two channels a consent request can travel down — nothing else about the
    person. Anything more would be personal data collected about somebody who
    never signed up for anything.
    """

    name = models.CharField(max_length=150)

    # AT LEAST ONE of the two, enforced below. Which one is not fixed: a parent
    # in India is far more reachable on a phone than on an email address, and
    # demanding both would block a child's signup on a channel their parent
    # does not have rather than on their parent's actual answer.
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=15, blank=True)

    # Set only when this guardian's contact turns out to belong to an account
    # that already exists — a parent who is themselves on Goatza, often a coach.
    # It is a CONVENIENCE LINK, not an ownership claim and not a login: the
    # guardian still answers through the same sent link, and the account it
    # points at gains no rights over the child.
    #
    # SET_NULL because the guardian outlives the account. If that user deletes
    # themselves, the consent they gave is still valid and the contact is still
    # reachable — CASCADE here would delete a parent, and with them the evidence
    # for every child they ever consented for.
    #
    # NOTHING READS THIS YET. It is filled opportunistically when the contact
    # happens to match, so that the first feature to want it ("approve as
    # yourself, you're already signed in") does not start with a backfill.
    linked_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="guardian_records",
    )

    class Meta:
        db_table = "guardians"
        indexes = [
            # Both are lookup keys rather than display columns: before creating
            # a second row for the same parent the service asks "is there
            # already a guardian at this address", and an inbound reply is
            # resolved back to a guardian by whichever channel it arrived on.
            models.Index(fields=["email"]),
            models.Index(fields=["phone"]),
        ]
        constraints = [
            # A guardian with neither channel is unreachable, and an
            # unreachable guardian can never consent to anything — the row
            # would be a child left pending forever with nothing that could
            # unblock them. The database says so rather than the service,
            # because a row like that is not a validation slip, it is a broken
            # record.
            models.CheckConstraint(
                condition=~Q(email="") | ~Q(phone=""),
                name="guardian_email_or_phone_required",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.email or self.phone})"


class GuardianConsentEvent(BaseUUIDModel):
    """
    One row per thing that happened between us and a guardian about one child.

    THIS TABLE IS APPEND-ONLY. Nothing updates a row and nothing deletes one —
    the same rule as ``legal.LegalAcceptance``, and for the same reason. The
    table's entire job is to be the answer when a regulator, a parent or a court
    asks whether consent was obtained for a particular child, when, from whom
    and on what basis. A row that can be edited answers nothing. A withdrawal
    therefore does NOT modify the approval it revokes: it is a new row, and the
    approval stays exactly as it was written, because "approved on the 3rd,
    withdrawn on the 9th" is the true story and "never approved" is not.

    ``accounts.User.guardian_consent_status`` carries a denormalized copy of
    what the newest row here means for the child. That copy is what the request
    path reads; this table is what an audit reads. The copy is derived and could
    be rebuilt from here — never the reverse.
    """

    guardian = models.ForeignKey(
        Guardian,
        on_delete=models.CASCADE,
        related_name="events",
    )

    # The minor this event is about. Called ``child`` and not ``user`` because
    # there are two people in every row and "user" would not say which.
    child = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="guardian_consent_events",
    )

    event_type = models.CharField(
        max_length=20,
        choices=GuardianConsentEventType.choices,
    )

    # Meaningful ONLY on an approved row — see GuardianConsentMethod. Blank
    # everywhere else, and blank rather than nullable so that "we did not check"
    # and "there was nothing to check" cannot become two different empties in
    # the same column.
    method = models.CharField(
        max_length=20,
        choices=GuardianConsentMethod.choices,
        blank=True,
    )

    level = models.CharField(
        max_length=20,
        choices=GuardianConsentLevel.choices,
        default=GuardianConsentLevel.ACKNOWLEDGED,
    )

    # WHICH TEXT the guardian was shown, stored per row for the same reason
    # ``legal.LegalAcceptance`` stores a version: the notice will be rewritten,
    # and an approval that cannot name the words it was given is not evidence
    # of anything.
    notice_version = models.CharField(max_length=20)

    # What the guardian said about THEMSELVES when they answered. Self-declared
    # and unverified, which is the whole reason it is kept apart from
    # ``Guardian.name`` — that one is what the CHILD said their parent was
    # called, and the two disagreeing is a signal worth being able to see.
    #
    # The birthdate is here because a guardian confirming they are an adult is
    # part of what makes the consent theirs to give. Both stay empty on every
    # row that is not an approval.
    parent_name_given = models.CharField(max_length=150, blank=True)
    parent_birthdate_given = models.DateField(null=True, blank=True)

    # The consent link, as a HASH and never as the token itself. These rows are
    # long-lived evidence, and a live token sitting in an audit table is a
    # standing "approve any child" credential for anyone who can read the
    # database — the same reason a password column holds a hash.
    #
    # Blank on the rows that carry no link (an approval, a withdrawal); indexed
    # because "which request does this incoming click belong to" is the one
    # lookup the reply path performs.
    token_hash = models.CharField(max_length=64, blank=True, db_index=True)

    # When that link stops working. NULL means this row has no link at all.
    # An expiry that passes unanswered becomes an EXPIRED row of its own — the
    # sweeper writes history rather than letting a request quietly rot.
    token_expires_at = models.DateTimeField(null=True)

    # Best-effort request context, not identity — the same pair, for the same
    # reason, as ``legal.LegalAcceptance``. Null/blank whenever the caller had
    # none: an expiry sweep or a management command is still a real event.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "guardian_consent_events"
        ordering = ["-created_at"]
        indexes = [
            # "What is the most recent thing that happened to this child" — the
            # only read shape there is, and the one a rebuild of
            # ``User.guardian_consent_status`` would run per user.
            models.Index(
                fields=["child", "-created_at"],
                name="guardian_child_recent_idx",
            ),
        ]

    def __str__(self):
        return f"{self.event_type} for {self.child_id} by {self.guardian_id}"
