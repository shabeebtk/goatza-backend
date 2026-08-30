"""
Pre-launch player registration — the list people join before Goatza opens.

A signup is NOT a user. Nobody here has an account, a password or an actor;
they left a phone number on an Instagram link and are waiting. The whole app is
built around that one fact:

  * ``phone`` is the identity, and it is unique. There is no login to
    deduplicate against, and a phone number is the one thing a player will type
    the same way twice. Re-submitting it is not an error — it is somebody
    checking whether they are still on the list (see PlayerSignupService).

  * ``sport`` and ``position`` are plain CharFields, NOT foreign keys into the
    ``sports`` catalog. The catalog is seeded by a management command and may
    not have been run in a given environment; a landing page that 500s because
    a lookup table is empty is a landing page that loses the signup. The values
    line up with the seeded football positions on purpose, so converting a
    signup into a real profile later is a mapping, not a guess.

  * ``signup_number`` and ``ref_code`` are public. "You're #413" is the reason
    somebody shares the card, so the number is sequential and visible rather
    than the UUID primary key. The service assigns it — never the caller.
    What is STORED here is the honest sequence starting at 1; what the API
    shows is that number plus ``WAITLIST_DISPLAY_OFFSET``
    (waitlist.selectors.signup_selectors.display_number).

  * THE LOCATION BLOCK MIRRORS ``accounts.models.UserProfile``. Same names,
    same types, same order, so converting a signup into a real profile at
    launch is a field copy rather than a mapping. Goatza is not Kerala-only:
    the FK resolves through the shared LocationService against whatever the
    place picker returned, and every part of it is optional — a player who declines the
    location prompt, or whose geocoding fails, still gets on the list.
"""

from django.db import models

from shared.models import BaseUUIDModel, Location


class PlayerSignup(BaseUUIDModel):
    """One player on the pre-launch waitlist."""

    class Position(models.TextChoices):
        """
        Football positions, matching the six seeded by
        ``sports.management.commands.data_add`` — a signup that converts should
        map straight onto a SportPosition without a translation table.
        """

        GOALKEEPER = "goalkeeper", "Goalkeeper"
        DEFENDER = "defender", "Defender"
        MIDFIELDER = "midfielder", "Midfielder"
        LEFT_WING = "left_wing", "Left Wing"
        RIGHT_WING = "right_wing", "Right Wing"
        STRIKER = "striker", "Striker"

    class Level(models.TextChoices):
        """How far the player has actually played — self-reported, unverified."""

        SCHOOL = "school", "School"
        CLUB = "club", "Club"
        DISTRICT = "district", "District"
        STATE = "state", "State"
        UNIVERSITY = "university", "University"
        NONE = "none", "None / just play"

    # WHO
    name = models.CharField(max_length=150)

    # The identity of a signup. E.164 ("+919847012345"), normalised by the
    # service — the form accepts whatever shape somebody types.
    phone = models.CharField(max_length=20, unique=True)

    email = models.EmailField(blank=True)

    # Bare handle: no leading @, no instagram.com/ prefix, lowercased. The
    # service strips all of that, so this column is always something that can
    # be pasted after "instagram.com/".
    instagram = models.CharField(max_length=100, blank=True)

    date_of_birth = models.DateField(null=True, blank=True)

    # WHERE
    #
    # Deliberately identical to UserProfile's location block — see the module
    # docstring. ``location`` is the shared, deduplicated place row; the five
    # text/coordinate columns below it are the denormalised copy, written at
    # the same time by PlayerSignupService.
    #
    # SET_NULL rather than CASCADE: a place row being cleaned up must never
    # take a signup with it. The copy below survives, so a signup that loses
    # its FK still knows where the player was.
    location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="waitlist_signups"
    )
    # Denormalized for better query
    location_name = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country_code = models.CharField(max_length=5, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    # WHAT THEY PLAY
    #
    # Deliberately not an FK to sports.Sport — see the module docstring.
    sport = models.CharField(max_length=30, default="football")
    position = models.CharField(
        max_length=40,
        choices=Position.choices,
        blank=True
    )
    level = models.CharField(
        max_length=30,
        choices=Level.choices,
        blank=True
    )
    club_or_academy = models.CharField(max_length=150, blank=True)

    # PUBLIC IDENTITY ON THE LIST
    #
    # Assigned by PlayerSignupService inside a transaction, never by the client.
    signup_number = models.PositiveIntegerField(unique=True)
    ref_code = models.CharField(max_length=12, unique=True)

    # ATTRIBUTION — the ?src= on the link they arrived through, so "which
    # Instagram post actually converted" is answerable from the admin.
    source = models.CharField(max_length=50, blank=True)

    # Internal. Never accepted from the API, never returned by it.
    notes = models.TextField(blank=True)

    # db_index on top of the Meta entry below is redundant on PostgreSQL (both
    # are a plain btree on one column) — kept because both were specified, and
    # the table is small enough that the second index costs nothing worth
    # arguing about.
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "waitlist_player_signups"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["city"]),
            models.Index(fields=["country_code"]),
            models.Index(fields=["position"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"#{self.signup_number} {self.name} ({self.phone})"
