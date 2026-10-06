from datetime import datetime, time

from django.db import models
from django.db.models import Q, F
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from django.utils import timezone
from shared.models import BaseUUIDModel, Location
from apps.organization.models import Organization, OrganizationMember
from apps.accounts.models import User
from apps.sports.models import Sport, SportPosition
from apps.recruitments.trial_window import is_trial_over
from utils.timezones import (
    TIMEZONE_MAX_LENGTH,
    default_timezone,
    validate_timezone,
    zone,
)
# Create your models here.



class Recruitment(BaseUUIDModel):

    class Type(models.TextChoices):
        # Two types. `scholarship` and `direct_recruitment` were retired —
        # a scholarship is a BENEFIT on an open trial, and a direct signing is
        # a "looking for players" post — and `migrate_recruitment_v3` folds
        # every historical row into one of these two.
        #
        # Their string values still exist in the DATABASE until that command
        # has run everywhere, so anything that READS old rows names them as
        # plain literals rather than members here. See that command's
        # LEGACY_TYPES.
        OPEN_TRIAL = "open_trial", "Open Trial"
        PLAYER_LOOKING = "player_looking", "Player Looking"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        CLOSED = "closed", "Closed"
        CANCELLED = "cancelled", "Cancelled"

    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public"
        FOLLOWERS_ONLY = "followers_only", "Followers Only"
        PRIVATE = "private", "Private"

    class Gender(models.TextChoices):
        MALE = "male", "Male"
        FEMALE = "female", "Female"
        ALL = "all", "All"

    class ApplyMethod(models.TextChoices):
        GOATZA = "goatza", "goatza"
        EXTERNAL = "external", "External"
        CONTACT = "contact", "contact"

    class SessionMode(models.TextChoices):
        """
        How a multi-date trial is attended - and therefore when
        applications close. ALL: every date is one trial, so a player who
        missed day one cannot join on day two. CHOOSE_ONE: each date is an
        independent round (a city tour), so the last one is still joinable.
        See Recruitment.applications_close_at.
        """
        ALL = "all", "Attend every date"
        CHOOSE_ONE = "choose_one", "Pick one date"
        

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="recruitments"
    )
    created_by_member = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_recruitments"
    )

    sport = models.ForeignKey(
        Sport,
        on_delete=models.CASCADE,
        related_name="recruitments"
    )

    title = models.CharField(max_length=255)
    short_description = models.CharField(
        max_length=300,
        blank=True
    )
    description = models.TextField(blank=True)

    recruitment_type = models.CharField(
        max_length=30,
        choices=Type.choices   
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )

    visibility = models.CharField(
        max_length=30,
        choices=Visibility.choices,
        default=Visibility.PUBLIC
    )

    gender = models.CharField(
        max_length=10,
        choices=Gender.choices,
        blank=True
    )

    # RETIRED FROM THE UI, NOT FROM THE DATA. The wizard offered five levels
    # as a dropdown; they became free-text criteria presets, so nothing writes
    # this any more and nothing renders or filters on it client-side.
    #
    # THE COLUMN AND THE `experience_level` FILTER PARAM BOTH STAY. Rows
    # written before that change carry real values, and a query param nobody
    # sends costs nothing. Dropping either would be a data decision; this was
    # only a UI one.
    experience_level = models.CharField(
        max_length=50,
        blank=True
    )

    # THE CALENDAR THIS TRIAL RUNS ON — an IANA name, copied from the
    # organization when the recruitment is created and editable afterwards,
    # because a London club can post a trial in Dubai.
    #
    # ONE RECRUITMENT = ONE TIMEZONE, deliberately not one per date. A
    # multi-city tour inside a country shares a zone, and a club running
    # trials in two countries under one posting can post twice. A per-date
    # override would be purely additive later (TrialSession would grow a
    # nullable `timezone` falling back to this one) and nothing here would
    # have to change.
    #
    # EVERYTHING DOWNSTREAM READS THE STORED INSTANTS, NOT THIS. The
    # timezone maths happens once, at write time: _sync_trial_window builds
    # event_date and trial_end_date IN this zone and stores the UTC instant,
    # so "is the trial over" is a plain UTC comparison with no timezone in
    # it. This column is for building those instants and for FORMATTING a
    # date the way the venue reads it.
    timezone = models.CharField(
        max_length=TIMEZONE_MAX_LENGTH,
        default=default_timezone,
        validators=[validate_timezone],
    )

    # Recruitment logistics
    application_deadline = models.DateTimeField(
        null=True,
        blank=True
    )

    # DERIVED, not authored: the FIRST non-cancelled TrialSession, written
    # by RecruitmentService._sync_trial_window. Kept as a column (and kept
    # on every payload) because the card, the ordering and the
    # valid_application_deadline constraint all read it.
    #
    # Built in THIS recruitment's `timezone` and stored, like everything
    # else, as the UTC instant.
    event_date = models.DateTimeField(
        null=True,
        blank=True
    )

    # DERIVED too: 23:59:59 of the LAST non-cancelled session's day, in this
    # recruitment's `timezone`. This, not event_date, is what "the trial is
    # over" means - a three-weekend trial is not over after weekend one
    # (trial_window.is_trial_over).
    #
    # Because the day boundary is resolved HERE, at write time, the read is
    # just `trial_end_date < now()`: a London trial ends at midnight London
    # and an Indian one at midnight IST, with no timezone in the query.
    trial_end_date = models.DateTimeField(
        null=True,
        blank=True
    )

    session_mode = models.CharField(
        max_length=20,
        choices=SessionMode.choices,
        default=SessionMode.ALL
    )

    # Open trial only. Everyone who applies is confirmed on the spot and
    # gets their trial pass — for a club that is not screening, only
    # counting. It does NOT widen the application window: the
    # applications_close_at gate runs first, which is the whole reason
    # that window was split from the trial's own.
    auto_confirm = models.BooleanField(default=False)

    apply_method = models.CharField(
        max_length=20,
        choices=ApplyMethod.choices,
        default=ApplyMethod.GOATZA
    )
    external_apply_url = models.URLField(
        blank=True
    )


    is_remote = models.BooleanField(default=False)
    max_applications = models.PositiveIntegerField(
        null=True,
        blank=True
    )

    # Optional fee info
    is_paid = models.BooleanField(default=False)
    fee_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True
    )
    fee_currency = models.CharField(
        max_length=10,
        default="INR"
    )
    payment_note = models.CharField(
        max_length=255,
        blank=True
    )

    # venue  
    venue_name = models.CharField(max_length=255, blank=True)
    venue_link = models.URLField(blank=True, max_length=500)

    # Location - denormalized 
    location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="recruitments"
    )
    location_name = models.CharField(max_length=255, blank=True)
    city = models.CharField(
        max_length=100,
        blank=True
    )
    country_code = models.CharField(
        max_length=5,
        blank=True
    )
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    # Denormalized analytics. confirmed_count / selected_count count the
    # applications currently in trial_confirmed / selected, and are kept in
    # step by ApplicationService.change_status and .withdraw.
    views_count = models.PositiveIntegerField(default=0)
    applications_count = models.PositiveIntegerField(default=0)
    confirmed_count = models.PositiveIntegerField(default=0)
    selected_count = models.PositiveIntegerField(default=0)

    # When the org was last EMAILED about new applicants. The applicant-alert
    # throttle measures its gap from this, never from the last application —
    # see settings.APPLICANT_ALERT_TIERS. Null means never alerted.
    last_applicant_alert_at = models.DateTimeField(null=True, blank=True)

    # Flags
    is_featured = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    published_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(
        auto_now_add=True
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "recruitments"

        indexes = [
            models.Index(fields=["organization"]),
            models.Index(fields=["sport"]),
            models.Index(fields=["published_at"]),
            models.Index(fields=["event_date"]),
            models.Index(fields=["latitude", "longitude"]),
        ]

        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(is_paid=False, fee_amount__isnull=True) |
                    Q(is_paid=True, fee_amount__isnull=False)
                ),
                name="recruitment_valid_fee"
            ),
            models.CheckConstraint(
                condition=(
                    Q(event_date__isnull=True) |
                    Q(application_deadline__isnull=True) |
                    Q(application_deadline__lte=F("event_date"))
                ),
                name="valid_application_deadline"
            ),
            models.CheckConstraint(
                condition=(
                    Q(apply_method="external", external_apply_url__isnull=False) |
                    ~Q(apply_method="external")
                ),
                name="external_apply_url_required"
            )
        ]

    def clean(self):
        if (
            self.apply_method == self.ApplyMethod.EXTERNAL
            and not self.external_apply_url
        ):
            raise ValidationError(
                "External apply URL required."
            )
        validate_timezone(self.timezone)

    @property
    def is_trial_over(self):
        """
        Whether the WHOLE trial window has closed — the stored end instant
        has passed. ``trial_end_date`` is already 23:59:59 of the last
        session's day IN THIS RECRUITMENT'S OWN ZONE, so this needs no
        timezone of its own. The rule lives in ``trial_window`` next to its
        queryset twin, ``trial_not_over_q``. No trial_end_date → never over.

        This is NOT the apply gate: applications usually close earlier
        (``applications_close_at``), and on an "attend every date" trial
        they close on the FIRST session.
        """
        return is_trial_over(self.trial_end_date)

    @property
    def applications_close_at(self):
        """
        When applications stop. NOT the same as when the trial is over.

        Only an open trial has a second boundary at all; every other type
        closes on its deadline and nothing else.

        For an open trial the boundary depends on how the dates are
        attended:

          * choose_one — each date is its own round, so a player can still
            join the LAST one: ``trial_end_date``.
          * all — every date is one trial, so the boundary is the FIRST
            session. Without that, someone applies at 9am Sunday on a
            Sat-Sun trial, auto-confirm issues them a pass for a trial that
            is half over, and they sit in the org's Confirmed tab as a
            phantom.

        Whichever bound applies, an explicit deadline can only bring it
        FORWARD — never push it out.
        """
        if self.recruitment_type != self.Type.OPEN_TRIAL:
            return self.application_deadline

        if self.session_mode == self.SessionMode.CHOOSE_ONE:
            boundary = self.trial_end_date
        else:
            boundary = self.event_date

        if self.application_deadline and boundary:
            return min(self.application_deadline, boundary)

        return self.application_deadline or boundary

    @property
    def is_accepting_applications(self):
        """
        True when the recruitment can still receive applications: active,
        the application window still open, and under the max cap (if set).
        Used by the apply flow and surfaced on the public detail serializer.

        The window is ``applications_close_at``, which already folds the
        deadline together with the trial's own boundary — deliberately NOT
        ``is_trial_over``: on an "attend every date" trial applications
        close on day one while the trial itself runs on.
        """
        if self.status != self.Status.ACTIVE:
            return False

        closes_at = self.applications_close_at
        if closes_at and closes_at < timezone.now():
            return False

        if (
            self.max_applications is not None
            and self.applications_count >= self.max_applications
        ):
            return False

        return True

    @property
    def zoneinfo(self):
        """This recruitment's ``ZoneInfo``. The venue's calendar, not ours."""
        return zone(self.timezone)

    def save(self, *args, **kwargs):
        """
        Validate the timezone on the way in — see ``Organization.save`` for
        why the field validator alone is not enough.
        """
        fields = kwargs.get("update_fields")
        if (
            (fields is None or "timezone" in fields)
            and "timezone" not in self.get_deferred_fields()
        ):
            validate_timezone(self.timezone)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.organization.name})"



class TrialSession(BaseUUIDModel):
    """
    ONE date an open trial is held on. A trial has at least one; a city tour
    or a multi-weekend selection has several.

    Two things about a trial are derived from this table and cached on the
    recruitment by ``RecruitmentService._sync_trial_window``: ``event_date``
    (the first non-cancelled session) and ``trial_end_date`` (23:59:59 of the
    last one). Which of the two closes applications depends on
    ``Recruitment.session_mode`` — see ``applications_close_at``.

    THE DATE IS A CALENDAR DATE AT THE VENUE, and the TIME a wall clock
    there. Both are read in ``Recruitment.timezone`` — deliberately NOT a
    per-session zone: a tour inside one country shares a clock, and a club
    running trials in two countries posts twice. See that field's comment.

    The venue fields are OVERRIDES, blank by default: a session with no venue
    of its own inherits the recruitment's. That is the common case (three
    weekends at the same ground) and it keeps the org from retyping a venue
    per date.

    There is deliberately NO ``capacity``. Per-session caps are out of v1;
    ``Recruitment.max_applications`` is the only cap there is.

    Cancelled sessions are KEPT, never deleted: applications point here, and a
    player whose date was called off must still see it on their application
    rather than find an empty slot.
    """

    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="sessions"
    )

    title = models.CharField(max_length=120, blank=True)
    date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)

    # Blank / null means "inherit the recruitment's venue".
    venue_name = models.CharField(max_length=255, blank=True)
    venue_link = models.URLField(max_length=500, blank=True)
    location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="trial_sessions"
    )
    city = models.CharField(max_length=100, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    is_cancelled = models.BooleanField(default=False)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recruitment_trial_sessions"
        ordering = ["date", "start_time", "display_order"]
        indexes = [
            models.Index(fields=["recruitment"]),
            # What a nearest-centre distance scan reads.
            models.Index(fields=["latitude", "longitude"]),
        ]

    def _local(self, at):
        """
        ``date`` + a wall-clock time, read in the PARENT RECRUITMENT'S zone.

        No query in the usual case: a session loaded through
        ``recruitment.sessions`` already carries that recruitment as its
        known related object, so ``self.recruitment`` is the in-memory row
        the caller is holding. A session fetched on its own (through
        ``application.session``, say) should have its parent primed — see
        ``send_trial_reminders._session_for`` — or this costs one SELECT.
        """
        return datetime.combine(self.date, at, tzinfo=self.recruitment.zoneinfo)

    @property
    def starts_at(self):
        """
        The session's start as an aware datetime in the recruitment's zone,
        which Django stores as the corresponding UTC instant.

        A session with no time starts at 23:59 — the same date-only sentinel
        the wizard writes for a trial with no time (frontend
        ``wizardDate.ts``), which is what stops a date-only trial from reading
        as already past on its own day.
        """
        return self._local(self.start_time or time(23, 59))

    @property
    def ends_at(self):
        """
        23:59:59 of the session's day, AT THE VENUE — the instant it stops
        being today there. This is what makes the trial-over read a plain
        UTC comparison: a London date ends five and a half hours after an
        Indian one with the same calendar date.
        """
        return self._local(time(23, 59, 59))

    def __str__(self):
        return f"{self.title or self.date} ({self.recruitment_id})"


class RecruitmentAgeCategory(BaseUUIDModel):
    """
    One age group a recruitment is open to, expressed in birth YEARS (the way
    trials are actually posted), never dates.

    Either bound may be null, which makes the group open-ended:
      min=2011, max=2012 → born 2011-2012 (inclusive range)
      min=2010, max=None → born 2010 or later ("U17")
      min=None, max=1991 → born 1991 or earlier ("Veterans 35+")
    Both null is meaningless — "open to all ages" is expressed by the
    recruitment having NO age categories at all — so the DB rejects it.
    """

    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="age_categories"
    )
    title = models.CharField(max_length=50)
    min_birth_year = models.PositiveIntegerField(null=True, blank=True)
    max_birth_year = models.PositiveIntegerField(null=True, blank=True)
    reporting_time = models.TimeField(null=True, blank=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:

        db_table = "recruitment_age_categories"

        ordering = ["display_order"]

        indexes = [
            models.Index(fields=["recruitment"]),
        ]

        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(min_birth_year__isnull=False) |
                    Q(max_birth_year__isnull=False)
                ),
                name="age_category_birth_year_required"
            )
        ]

    def __str__(self):
        return self.title


class RecruitmentContact(BaseUUIDModel):

    class ContactType(models.TextChoices):
        PHONE = "phone", "Phone"
        EMAIL = "email", "Email"

    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="contacts"
    )
    name = models.CharField(max_length=255, blank=True)
    contact_type = models.CharField(max_length=20, choices=ContactType.choices)
    value = models.CharField(max_length=255)

    class Meta:
        db_table = "recruitment_contacts"
        indexes = [
            models.Index(fields=["recruitment"]),
            models.Index(fields=["contact_type"]),
        ]

    def clean(self):
        if (
            self.contact_type
            == self.ContactType.EMAIL
        ):
            from django.core.validators import (
                validate_email
            )

            validate_email(self.value)

    def __str__(self):

        return (
            f"{self.contact_type} - {self.value}"
        )


class RecruitmentPosition(BaseUUIDModel):

    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="positions"
    )

    position = models.ForeignKey(
        SportPosition,
        on_delete=models.CASCADE,
        related_name="recruitments"
    )
    is_primary = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recruitment_positions"

        constraints = [
            models.UniqueConstraint(
                fields=["recruitment", "position"],
                name="unique_recruitment_position"
            )
        ]

        indexes = [
            models.Index(fields=["recruitment"]),
            models.Index(fields=["position"]),
        ]

    def clean(self):
        if self.position.sport_id != self.recruitment.sport_id:
            raise ValidationError(
                "Position does not belong to recruitment sport."
            )

    def __str__(self):
        return f"{self.recruitment_id} - {self.position.name}"


class RecruitmentBenefit(BaseUUIDModel):
    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="benefits"
    )
    title = models.CharField(
        max_length=255
    )
    icon_name = models.CharField(
        max_length=50,
        blank=True
    )
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recruitment_benefits"
        ordering = ["display_order"]
        indexes = [
            models.Index(fields=["recruitment"]),
        ]

    def __str__(self):
        return self.title


class RecruitmentRequirement(BaseUUIDModel):
    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="requirements"
    )
    title = models.CharField(max_length=255)
    is_mandatory = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recruitment_requirements"
        ordering = ["display_order"]
        indexes = [
            models.Index(fields=["recruitment"]),
        ]

    def __str__(self):
        return self.title


class RecruitmentEligibilityCriteria(BaseUUIDModel):
    """
    A free-text line the recruiter wrote about who may attend ("Kerala
    residents only", "District-level experience required"). Displayed only —
    nothing is ever checked against an applicant's profile.
    """

    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="eligibility_criteria"
    )
    title = models.CharField(max_length=255)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recruitment_eligibility_criteria"
        ordering = ["display_order"]
        indexes = [
            models.Index(fields=["recruitment"]),
        ]

    def __str__(self):
        return self.title



class RecruitmentMedia(BaseUUIDModel):
    class MediaType(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"

    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="media"
    )
    media_type = models.CharField(
        max_length=10,
        choices=MediaType.choices
    )

    # Media URLs carry a deep nested folder path
    # (organizations/<uuid>/recruitments/<uuid>/<uuid>.jpg) that overflows the
    # 200-char URLField default, so give these headroom.
    file_url = models.URLField(max_length=500)
    public_id = models.CharField(max_length=255)

    thumbnail_url = models.URLField(blank=True, max_length=500)

    duration = models.PositiveIntegerField(
        null=True,
        blank=True
    )
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recruitment_media"
        ordering = ["order"]

        indexes = [
            models.Index(fields=["recruitment"]),
        ]




class RecruitmentApplication(BaseUUIDModel):
    """
    One player's application to one recruitment.

    ``status`` is the ORG's word and is only ever written by the org (or by
    auto-confirm on their behalf). The self-reported block at the bottom is
    the PLAYER's word about the same trial, and the two never touch: a player
    saying they were selected does not select them, it only tells the org
    where to look.

    PRIVACY — the rating and the written feedback are visible to the owning
    ORG only, today. That is a SERIALIZER decision, not a schema one: there is
    deliberately no ``is_public`` flag, because making ratings public later
    for trust is a product decision and a serializer change, not a migration.
    Do not add a flag now.
    """

    class Status(models.TextChoices):
        # `invited` and `rejected` were retired by the v3 status split:
        # `invited` became `trial_confirmed`, and `rejected` split into the
        # honest pair below — `not_shortlisted` (never called in) and
        # `not_selected` (came, and did not make it).
        #
        # They are gone from the CHOICES, not from the world. Rows written
        # before the backfill still carry them, a stale PWA client still
        # SENDS them (apps/recruitments/legacy_status.py translates those),
        # and the copy maps still render them. Everything that reads an old
        # value names it as a plain literal.
        APPLIED = "applied", "Applied"
        REVIEWING = "reviewing", "Reviewing"
        SHORTLISTED = "shortlisted", "Shortlisted"
        SELECTED = "selected", "Selected"
        WITHDRAWN = "withdrawn", "Withdrawn"
        TRIAL_CONFIRMED = "trial_confirmed", "Trial Confirmed"
        NOT_SHORTLISTED = "not_shortlisted", "Not Shortlisted"
        NOT_SELECTED = "not_selected", "Not Selected"

    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="applications"
    )

    applicant = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="recruitment_applications"
    )

    # Contact the applicant chose to share for THIS application. Prefilled from
    # their profile on the client, but user-editable — so these are stored as
    # submitted in the request body, NOT re-read from the profile server-side.
    shared_name = models.CharField(max_length=255)
    shared_email = models.EmailField(blank=True)
    shared_phone = models.CharField(max_length=15)

    applied_position = models.ForeignKey(
        SportPosition,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applications"
    )

    # The age group the applicant chose to apply under. Never enforced against
    # their profile — it is what the org filters its pipeline by, and what the
    # player is shown a reporting time for. SET_NULL so deleting a group on an
    # edit degrades to "no group" instead of deleting the application.
    age_category = models.ForeignKey(
        RecruitmentAgeCategory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applications"
    )

    # Whether the profile birth year fell OUTSIDE the chosen age group's band
    # when the application was made (or remade). Computed by
    # ApplicationService.apply, never taken from the client. Recorded, never
    # enforced: an unknown birth year or no group is not a mismatch.
    age_mismatch_at_apply = models.BooleanField(default=False)

    # The date the applicant picked, on a choose_one trial only. Null
    # everywhere else — in `all` mode there is nothing to pick, and a
    # session sent by a client is ignored rather than stored. SET_NULL for
    # the same reason age_category is: deleting a date on an edit must
    # degrade to "no date", never delete the application.
    session = models.ForeignKey(
        TrialSession,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applications"
    )

    # THE TRIAL FEE, as the org recorded it at the gate. Information, not
    # a gate: nothing in the pipeline reads these, and a player who has
    # not paid can still be confirmed and selected. Deliberately no
    # payment_ref — that is out of v1.
    fee_paid = models.BooleanField(default=False)
    fee_paid_at = models.DateTimeField(null=True, blank=True)
    fee_marked_by = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fee_marked_applications"
    )

    message = models.TextField(blank=True)

    highlight_video_url = models.URLField(blank=True)

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.APPLIED,
        db_index=True
    )

    reviewed_by = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_applications"
    )

    reviewed_at = models.DateTimeField(
        null=True,
        blank=True
    )

    # When the evening-before reminder went out for THIS application's trial
    # date. The whole idempotency of ``send_trial_reminders`` is this column:
    # the command runs hourly and re-running it is a no-op because a stamped
    # row is not selected again.
    #
    # CLEARED when the date moves (RecruitmentService._clear_reminders),
    # because a reminder naming a date the trial is no longer on is worse
    # than none — the new date has to earn its own.
    trial_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    # ── THE PASS ──────────────────────────────────────────────────
    # The booking reference on the player's pass — what they show if an
    # organiser asks to confirm they registered. Nothing scans it and
    # nothing checks anybody in against it.
    #
    # Minted the moment an application becomes trial_confirmed and NEVER
    # re-issued: a player may already have screenshotted it, so a second
    # code would send them to a desk that cannot find them. Blank on
    # everyone who was never confirmed.
    pass_code = models.CharField(max_length=9, blank=True)

    # ── THE PLAYER'S OWN ACCOUNT OF THE TRIAL ─────────────────────────
    # A HINT for the org, never the truth. Orgs do not reliably come back to
    # post results — they ran the trial, they know who they picked, and the
    # website is the last thing on their mind — so the player is asked too.
    # It makes the org's job smaller (confirm the 18 who say they were
    # picked, rather than review 300); it never makes the decision.
    #
    # NOTHING HERE MAY EVER WRITE ``status``. That separation is the whole
    # design, and it is why these columns are written by their own service.

    # null = not answered yet.
    attended_self_reported = models.BooleanField(null=True, blank=True)

    class SelfOutcome(models.TextChoices):
        SELECTED = "selected", "Selected"
        NOT_SELECTED = "not_selected", "Not selected"
        # A real answer, not a missing one: most players genuinely do not
        # know yet, and without this they would guess or say nothing.
        WAITING = "waiting", "Still waiting to hear"

    # Blank when they did not attend, or have not answered.
    outcome_self_reported = models.CharField(
        max_length=15,
        blank=True,
        choices=SelfOutcome.choices,
    )

    trial_rating = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    trial_feedback = models.TextField(blank=True, max_length=1000)

    # Stamped on EVERY accepted answer, including "I did not attend" — which
    # clears the outcome and the rating and would otherwise be
    # indistinguishable from never having answered. Re-stamped on a resubmit.
    feedback_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)

    applied_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "recruitment_applications"

        constraints = [
            models.UniqueConstraint(
                fields=["recruitment", "applicant"],
                name="unique_recruitment_application"
            ),
            # PARTIAL: blank is the common value — every applicant who was
            # never confirmed has one — and an unconditional unique would make
            # the second unconfirmed application on a recruitment impossible.
            # A code is only ever read WITHIN one recruitment, so that is
            # the scope the guarantee needs.
            models.UniqueConstraint(
                fields=["recruitment", "pass_code"],
                condition=~Q(pass_code=""),
                name="unique_pass_code"
            ),
        ]

        indexes = [
            models.Index(fields=["recruitment"]),
            models.Index(fields=["applicant"]),
            models.Index(fields=["status"]),
            models.Index(fields=["-applied_at"]),
            models.Index(fields=["recruitment", "status"]),
        ]

    def __str__(self):
        return f"{self.applicant_id} -> {self.recruitment_id}"




class RecruitmentAnnouncement(BaseUUIDModel):
    """
    One message an org sends to the people on a recruitment — "the venue has
    moved", "bring your own water", "results are up".

    Editing a recruitment notifies nobody, by design: a posting is a document,
    and a silent typo fix should stay silent. An announcement is the explicit
    "tell them" that the edit deliberately is not.

    NOTHING IS SENT WHEN ONE IS CREATED. The request writes an
    ``AnnouncementDelivery`` row per recipient per channel and returns; a cron
    job drains them. That split is the whole point — see the
    AnnouncementDelivery docstring.

    ``recipients_count`` is the number of distinct PEOPLE, stamped at create
    time from the audience that was actually resolved, so the number the org
    was shown before sending is the number that was written. It is not
    recomputed later: an announcement is a snapshot of who was on the list
    that day, and an applicant who withdraws afterwards was still told.

    Deleting one is a SOFT delete and takes nothing back. The notifications and
    emails that already left cannot be recalled; ``is_deleted`` only removes it
    from the lists.
    """

    class Audience(models.TextChoices):
        ALL_APPLICANTS = "all_applicants", "All applicants"
        CONFIRMED = "confirmed", "Everyone called to the trial"
        SELECTED = "selected", "Selected players"

    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="announcements"
    )
    created_by_member = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_announcements"
    )

    title = models.CharField(max_length=120)
    body = models.TextField(max_length=1000)
    audience = models.CharField(max_length=20, choices=Audience.choices)

    # Narrows the audience to the people attending ONE date — "the Kochi
    # round has moved". Only means anything in choose_one mode, where an
    # application names a date; ignored otherwise. SET_NULL so deleting a date
    # widens the announcement back to its audience instead of deleting it.
    session = models.ForeignKey(
        TrialSession,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="announcements"
    )

    recipients_count = models.PositiveIntegerField(default=0)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recruitment_announcements"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recruitment", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.recruitment_id})"


class AnnouncementDelivery(BaseUUIDModel):
    """
    THE OUTBOX. One row per (announcement, recipient, channel), written by the
    request and sent by ``manage.py dispatch_announcements``.

    WHY THIS TABLE EXISTS. Three facts about this codebase, together:

      * ``utils.background_jobs.enqueue`` has no callers and CELERY_ENABLED is
        off by default, so a job dispatched today runs INLINE, in the request.
      * ``utils.emails.send_email_async`` is one daemon OS THREAD per email.
      * the existing bulk fan-out is capped at 100 and that is the tested
        ceiling.

    So an announcement to 340 confirmed players, sent from the request that
    created it, would spawn 340 OS threads and make 340 inline FCM calls on a
    web dyno. THE RULE IS THEREFORE: the request WRITES rows, a cron job SENDS
    them. No exceptions, and no ``enqueue`` call on the create path.

    THE UNIQUE CONSTRAINT IS LOAD-BEARING, not hygiene. ``(announcement,
    application, channel)`` is what makes the drain idempotent and a re-queue a
    no-op: a second create for the same audience cannot duplicate a row, a
    crashed drain cannot double-send on its next pass, and the SENT state is
    the record that this exact message reached this exact person once. Remove
    it and the outbox becomes a machine for sending the same email twice. It
    is PARTIAL on ``announcement IS NOT NULL`` because a direct message has no
    announcement and an org may legitimately send two of them.

    TWO KINDS OF ROW. With an announcement, it delivers that announcement.
    Without one, it is a DIRECT message: ``direct_body`` carries the text and
    ``created_by_member`` who sent it. Both drain through the same command and
    share the same daily cap, so neither can be used to route around the other.

    STATES: PENDING is the only thing the drain picks up. SENT is terminal and
    is never re-sent. FAILED is terminal too, after 3 attempts — the row stays
    so the failure is visible on the delivery summary rather than silently
    absent. SKIPPED is decided at WRITE time, never by the drain: a recipient
    with no email on file, or one blocked either way with the posting org. A
    skip is written rather than omitted so the counts reconcile against
    ``recipients_count``.

    CHANNELS: in-app and push are ONE operation here — NotificationService
    creates the row and ``_dispatch`` fans the push out from it, and splitting
    them would mean bypassing that path — so they are one ``notification``
    channel. ``dm`` delivers the update as a real Goatza message, which is
    why it exists: routing trial updates to WhatsApp hands the org the
    player's phone number and gives neither of them a reason to come back.

    A dm that lands ALSO produces messaging's own push, so the drain sends
    `dm` before `notification` and suppresses the second push for that
    recipient — the in-app announcement row is still written, because the
    player wants it in their notifications list.
    """

    class Channel(models.TextChoices):
        NOTIFICATION = "notification", "In-app + push"
        EMAIL = "email", "Email"
        DM = "dm", "Goatza message"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    # NULL for a DIRECT message — "message these 12 players" is a delivery
    # with no announcement behind it. Everything else about the row is the
    # same, which is the point: one outbox, one drain, one set of states,
    # rather than a second near-identical table.
    announcement = models.ForeignKey(
        RecruitmentAnnouncement,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="deliveries"
    )

    # The body of a direct message. Blank on an announcement delivery, whose
    # text lives on the announcement row it points at.
    direct_body = models.TextField(blank=True)

    # Who sent a direct message. An announcement records this on the
    # announcement itself; a direct row has nowhere else to put it.
    created_by_member = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="direct_deliveries"
    )
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="announcement_deliveries"
    )
    recipient = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="announcement_deliveries"
    )

    channel = models.CharField(max_length=15, choices=Channel.choices)
    state = models.CharField(
        max_length=10,
        choices=State.choices,
        default=State.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recruitment_announcement_deliveries"

        constraints = [
            # PARTIAL, because `announcement` is now nullable. In Postgres
            # NULL never equals NULL, so direct rows would not collide under
            # an unconditional unique either — the condition is here to say so
            # out loud: the guarantee is about ANNOUNCEMENT deliveries, and
            # two direct messages to the same player on the same channel are a
            # normal thing an org does twice.
            models.UniqueConstraint(
                fields=["announcement", "application", "channel"],
                condition=Q(announcement__isnull=False),
                name="unique_announcement_delivery"
            ),
        ]

        indexes = [
            # The drain's own query: PENDING, oldest first.
            models.Index(fields=["state", "created_at"]),
        ]

    def __str__(self):
        return f"{self.announcement_id} -> {self.recipient_id} ({self.channel})"


class RecruitmentApplicationStatusHistory(BaseUUIDModel):

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="status_history"
    )

    from_status = models.CharField(
        max_length=30,
        blank=True
    )

    to_status = models.CharField(
        max_length=30
    )

    changed_by = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )

    note = models.TextField(blank=True)

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True
    )

    class Meta:
        db_table = "recruitment_application_status_history"

        indexes = [
            models.Index(fields=["application"]),
            models.Index(fields=["created_at"]),
        ]


# CUSTOM QUESTIONS
class RecruitmentQuestion(BaseUUIDModel):

    class FieldType(models.TextChoices):
        SHORT_TEXT = "short_text", "Short Text"
        LONG_TEXT = "long_text", "Long Text"
        SELECT = "select", "select"
        RADIO = "radio", "Radio"
        CHECKBOX = "checkbox", "Checkbox"
        NUMBER = "number", "Number"

    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="questions"
    )

    question = models.CharField(max_length=255)

    field_type = models.CharField(
        max_length=30,
        choices=FieldType.choices
    )

    is_required = models.BooleanField(default=False)

    placeholder = models.CharField(
        max_length=255,
        blank=True
    )

    help_text = models.CharField(
        max_length=255,
        blank=True
    )

    display_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recruitment_questions"

        ordering = ["display_order"]

        indexes = [
            models.Index(fields=["recruitment"]),
        ]


# QUESTION OPTIONS
class RecruitmentQuestionOption(BaseUUIDModel):

    question = models.ForeignKey(
        RecruitmentQuestion,
        on_delete=models.CASCADE,
        related_name="options"
    )
    value = models.CharField(max_length=255)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recruitment_question_options"

        ordering = ["display_order"]

        indexes = [
            models.Index(fields=["question"]),
        ]


# APPLICATION ANSWERS
class RecruitmentApplicationAnswer(BaseUUIDModel):

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="answers"
    )

    question = models.ForeignKey(
        RecruitmentQuestion,
        on_delete=models.CASCADE,
        related_name="answers"
    )

    answer_text = models.TextField(blank=True)

    selected_option = models.ForeignKey(
        RecruitmentQuestionOption,
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recruitment_application_answers"

        indexes = [
            models.Index(fields=["application"]),
            models.Index(fields=["question"]),
        ]

class SavedRecruitment(BaseUUIDModel):
    """
    A recruitment shortlisted by ONE actor — the player, or the org they act
    as. Mirrors posts.SavedPost exactly: same dual-actor shape, same partial
    uniques, and the same privacy rule — a save is counted, notified and shown
    to nobody but the saver.

    Deliberately no soft delete and no status column: unsaving is the delete,
    and the saved list keeps closed/cancelled postings on purpose (a shortlist
    is exactly where a player notices that a deadline passed).
    """

    # Dual-actor, same shape as SavedPost: a save belongs to the actor who made
    # it, so a person and an org they run keep separate lists.
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="saved_recruitments"
    )
    org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="saved_recruitments"
    )
    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="saved_by"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "saved_recruitments"

        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=False, org__isnull=True) |
                    Q(user__isnull=True, org__isnull=False)
                ),
                name="saved_recruitment_user_or_org",
            ),
            # Partial uniques — NULL never equals NULL, so an unconditional
            # unique on a nullable column would let duplicates through.
            models.UniqueConstraint(
                fields=["user", "recruitment"],
                condition=Q(user__isnull=False),
                name="unique_saved_recruitment_user",
            ),
            models.UniqueConstraint(
                fields=["org", "recruitment"],
                condition=Q(org__isnull=False),
                name="unique_saved_recruitment_org",
            ),
        ]
