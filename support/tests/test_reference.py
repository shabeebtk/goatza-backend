"""
The public reference code — its shape, its alphabet, and its collision path.

The alphabet test is not pedantry. These codes are read off a screen and typed
into an email, and ``0``/``O`` and ``1``/``I`` are the pairs that get
transcribed wrong. A regenerated alphabet that quietly readmitted them would
cost a support thread per collision of the eye, and nothing else in the system
would notice.

The collision test covers the one path that only runs under contention: two
reports drawing the same code. The service does NOT pre-check availability —
the gap between a SELECT and an INSERT is exactly the race — so the unique
constraint is the arbiter and the retry is what turns its IntegrityError back
into a saved report.
"""

import re
from unittest.mock import patch

from django.test import TestCase

from support.models import ProblemReport
from support.services import problem_report_service
from support.services.problem_report_service import (
    MAX_REFERENCE_ATTEMPTS,
    ProblemReportService,
)
from support.services.reference import (
    REFERENCE_ALPHABET,
    REFERENCE_LENGTH,
    REFERENCE_PREFIX,
    generate_reference,
)

REFERENCE_RE = re.compile(r"^GZ-[A-Z2-9]{6}$")

# The four that get misread. Their absence IS the design.
AMBIGUOUS = set("0O1I")

SAMPLE = 500


class ReferenceFormatTests(TestCase):

    def test_every_generated_reference_matches_the_published_format(self):
        for _ in range(SAMPLE):
            self.assertRegex(generate_reference(), REFERENCE_RE)

    def test_no_generated_reference_contains_an_ambiguous_character(self):
        for _ in range(SAMPLE):
            body = generate_reference()[len(REFERENCE_PREFIX):]
            self.assertFalse(
                AMBIGUOUS & set(body),
                f"{body} contains one of {sorted(AMBIGUOUS)}",
            )

    def test_the_alphabet_itself_excludes_the_ambiguous_characters(self):
        # Asserted on the constant as well as on the output: a sampling test
        # can only prove a character is rare, not that it is impossible.
        self.assertFalse(AMBIGUOUS & set(REFERENCE_ALPHABET))
        self.assertEqual(len(REFERENCE_ALPHABET), 32)
        self.assertEqual(len(set(REFERENCE_ALPHABET)), 32)

    def test_a_reference_fits_the_column(self):
        # reference is CharField(12). "GZ-" + 6 leaves room, and this is what
        # catches a future length bump that would start truncating.
        length = len(REFERENCE_PREFIX) + REFERENCE_LENGTH
        column = ProblemReport._meta.get_field("reference").max_length

        self.assertLessEqual(length, column)
        self.assertEqual(len(generate_reference()), length)

    def test_references_are_not_sequential(self):
        # A guessable code would make the "never expose this as a URL" rule the
        # only thing protecting a report, and rules are weaker than entropy.
        codes = {generate_reference() for _ in range(SAMPLE)}

        # 32^6 is ~1.07 billion; 500 draws colliding would mean the generator
        # is not drawing from the alphabet it claims to.
        self.assertEqual(len(codes), SAMPLE)


class ReferenceCollisionTests(TestCase):

    def test_a_collision_on_insert_retries_and_still_creates_the_row(self):
        """
        The generator hands out a code that is already taken; the INSERT
        trips the unique constraint; the next attempt succeeds.

        Patched at the SERVICE's import site rather than in reference.py, so
        this exercises the retry loop that actually runs in production.
        """
        taken = ProblemReport.objects.create(
            reference="GZ-AAAAAA",
            category="other",
            description="An existing report holding the code.",
        )

        with patch.object(
            problem_report_service,
            "generate_reference",
            side_effect=["GZ-AAAAAA", "GZ-BBBBBB"],
        ) as generator:
            success, result = ProblemReportService.create(
                category="not_working",
                description="The second report draws a taken code first.",
                contact_email="someone@example.com",
            )

        self.assertTrue(success, result)
        self.assertEqual(result["reference"], "GZ-BBBBBB")
        self.assertEqual(generator.call_count, 2)

        # Both rows exist — the retry created one, it did not overwrite one.
        self.assertEqual(ProblemReport.objects.count(), 2)
        self.assertTrue(
            ProblemReport.objects.filter(reference="GZ-BBBBBB").exists()
        )
        taken.refresh_from_db()
        self.assertEqual(taken.description, "An existing report holding the code.")

    def test_a_generator_stuck_on_one_code_gives_up_rather_than_spinning(self):
        # The loop is BOUNDED. A genuinely broken unique constraint has to
        # surface as an error instead of an infinite retry.
        ProblemReport.objects.create(
            reference="GZ-AAAAAA",
            category="other",
            description="An existing report holding the code.",
        )

        with patch.object(
            problem_report_service,
            "generate_reference",
            return_value="GZ-AAAAAA",
        ) as generator:
            with self.assertRaises(Exception):
                ProblemReportService.create(
                    category="not_working",
                    description="Every attempt draws the same taken code.",
                    contact_email="someone@example.com",
                )

        self.assertEqual(generator.call_count, MAX_REFERENCE_ATTEMPTS)
        self.assertEqual(ProblemReport.objects.count(), 1)
