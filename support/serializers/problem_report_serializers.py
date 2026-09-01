"""
Wire shapes for the two "report a problem" endpoints.

Two serializers, one service. The difference between them is exactly the
difference between the routes:

  * SCREENSHOTS are authenticated-only. The presigned PUT that produced them
    is issued to a signed-in actor and signed into that actor's own folder;
    there is no such folder for an anonymous caller, so the public serializer
    does not have the field at all rather than accepting and discarding it.

  * CONTACT EMAIL is required on the public route and optional on the
    authenticated one, where the account already carries an address.

Plain Serializers, not ModelSerializers. One of these is on the public surface,
where core/public_urls.py says the payload is an explicit allow-list — and the
model has ``status``, ``internal_note`` and ``resolved_by`` on it. Listing the
accepted fields by hand is what makes those impossible to set by guessing.
"""

from rest_framework import serializers

from support.models import ProblemCategory
from support.services.problem_report_service import ProblemReportService


class ScreenshotSerializer(serializers.Serializer):
    """
    One uploaded screenshot: where it is, and the object key it was signed to.

    Both are required and both are checked. The key is not decorative — the
    service re-validates that it sits under the CALLER's own storage prefix
    before the URL is trusted, and a URL with no key cannot be checked at all.
    """

    url = serializers.URLField(max_length=500)
    key = serializers.CharField(max_length=500)


class ProblemReportCreateSerializer(serializers.Serializer):
    """POST /support/problem-report — the signed-in report sheet."""

    category = serializers.ChoiceField(choices=ProblemCategory.choices)

    # Bounds mirrored from the service, which enforces them again for callers
    # that are not this serializer. Here they exist to produce a field-level
    # error the form can attach to the textarea.
    description = serializers.CharField(
        trim_whitespace=True,
        min_length=ProblemReportService.MIN_DESCRIPTION_LENGTH,
        max_length=ProblemReportService.MAX_DESCRIPTION_LENGTH,
    )

    screenshots = serializers.ListField(
        child=ScreenshotSerializer(),
        required=False,
        max_length=ProblemReportService.MAX_SCREENSHOTS,
    )

    # Optional here: the account has an address, and this is for a reporter who
    # wants the reply somewhere else.
    contact_email = serializers.EmailField(required=False, allow_blank=True)

    # A DictField rather than a nested serializer, because the useful key is
    # whichever one the client learns to send next and a nested serializer
    # would 400 on it. The service allow-lists and truncates what it keeps
    # (ProblemReportService.sanitise_context) — nothing here is trusted.
    client_context = serializers.DictField(required=False)


class PublicProblemReportCreateSerializer(serializers.Serializer):
    """
    POST /public/support/problem-report — the logged-out report sheet.

    No screenshots. See the module docstring, and the note in
    core/public_urls.py: a presigned PUT handed to an anonymous caller is a
    write path into the bucket from the open internet.
    """

    category = serializers.ChoiceField(choices=ProblemCategory.choices)

    description = serializers.CharField(
        trim_whitespace=True,
        min_length=ProblemReportService.MIN_DESCRIPTION_LENGTH,
        max_length=ProblemReportService.MAX_DESCRIPTION_LENGTH,
    )

    # REQUIRED here, and required HERE rather than as a database constraint.
    #
    # An anonymous report genuinely needs a return address — there is no
    # account to reply through, and a bug we cannot ask a question about is
    # often a bug we cannot reproduce. But ``ProblemReport.reported_by`` is
    # SET_NULL, so a database-level "no reporter implies an email" rule would
    # fail the day somebody deletes their account: that UPDATE turns an old
    # authenticated report (blank email, correctly) into an anonymous one and
    # the constraint would reject it, leaving a report blocking its own
    # reporter's deletion. This layer is the one that can tell "filed with no
    # account" apart from "the account was deleted later".
    contact_email = serializers.EmailField()

    client_context = serializers.DictField(required=False)

    # HONEYPOT.
    #
    # Rendered hidden and left empty by every human; filled in by the sort of
    # bot that submits every input it finds. Write-only and never persisted —
    # the view checks it and returns a normal-looking success without touching
    # the database (ProblemReportService.decoy_payload).
    #
    # Named "website" rather than anything with "honeypot" or "trap" in it, for
    # the obvious reason, and the same name the waitlist form uses so the two
    # hidden fields look like one convention rather than two tells.
    website = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
        max_length=200,
    )
