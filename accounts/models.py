from django.db import models
from django.db.models import Q
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from shared.models import BaseUUIDModel, Location
from django.core.validators import MinValueValidator, MaxValueValidator
# Aliased because `User` grows an `is_minor` PROPERTY below and the module
# exports a function of the same name — importing the name bare would make the
# two impossible to tell apart at the call site.
from accounts import constants as account_constants

class UserManager(BaseUserManager):
    def create_user(self, email=None, phone=None, password=None, **extra_fields):
        if not email and not phone:
            raise ValueError("User must have either email or phone")

        email = self.normalize_email(email) if email else None

        user = self.model(
            email=email,
            phone=phone,
            **extra_fields
        )

        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')

        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self.create_user(email=email, password=password, **extra_fields)


class User(BaseUUIDModel, AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        PLAYER = "player", "Player"
        COACH = "coach", "Coach"
        SCOUT = "scout", "Scout"
        ORG_USER = "org_user", "Org User"

    email = models.EmailField(unique=True, null=True, blank=True)
    phone = models.CharField(max_length=15, unique=True, null=True, blank=True)

    username = models.CharField(max_length=50, unique=True, null=True, blank=True)

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.PLAYER)
    # True once the user has explicitly chosen their role. Email/OTP signups pick a
    # role at signup so they stay True; new Google OAuth users start False until they
    # complete the one-time role-selection step.
    is_role_confirmed = models.BooleanField(default=True)

    # True once the user has finished (or dismissed with role saved) the post-signup
    # onboarding flow. New users start False and see onboarding; a data migration
    # backfills all pre-existing users to True so they never get thrown into it.
    is_onboarding_completed = models.BooleanField(default=False)

    is_email_verified = models.BooleanField(default=False)
    is_phone_verified = models.BooleanField(default=False)

    # THE LEGAL JURISDICTION. Deliberately NOT UserProfile.country_code.
    #
    # The two are different questions that happen to share a shape. The profile's
    # copy is derived from the city the user picked in the location search — it
    # answers "where is this person right now", it moves when they move, and it
    # is denormalized from a Google Places row for search and display. This one
    # answers "whose child-protection law governs this account", it is declared
    # by the user at signup (cross-checked against their dialling code, see
    # accounts/services/age_service.resolve_country), and it does not change
    # because somebody went on tour.
    #
    # A player living in Dubai may still be Indian, and the age of digital
    # consent that applies to them is India's 18, not the UAE's. Collapsing the
    # two columns would silently switch a minor's protections off the moment
    # they set their city to somewhere else.
    #
    # Blank means "never asked" — every row created before the signup gate.
    country_code = models.CharField(max_length=2, blank=True)

    # How many times this user has corrected their date of birth through the
    # app. The self-serve route allows exactly one (see
    # accounts/serializers/user_update_serilizer.validate_birthdate); after
    # that, corrections go through support. Counted rather than flagged so the
    # limit can be loosened, or a pattern of attempts spotted, without a
    # migration.
    birthdate_corrections = models.PositiveSmallIntegerField(default=0)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    # When the OWNER asked for this account to be deleted. NULL for every
    # account that has not, which is what separates a user-initiated deletion
    # from the other two things is_active=False already means: an unverified
    # signup, and a staff suspension. The purge job
    # (accounts/management/commands/purge_deleted_accounts.py) selects on
    # is_active=False AND this timestamp, so neither of those is ever swept up.
    #
    # Indexed because that job's only query filters and orders on it.
    deletion_requested_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Denormalized copy of the latest legal acceptance per gating document. The
    # system of record is legal.LegalAcceptance, which is append-only; these are
    # a cache of its newest row per document so the consent gate is a field read
    # on the already-loaded user, not a query on every request. Written ONLY by
    # legal.services.acceptance_service.record_acceptance, and rebuildable from
    # that table. NULL means never accepted, which reads as pending.
    terms_version = models.CharField(max_length=20, null=True, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    privacy_version = models.CharField(max_length=20, null=True, blank=True)
    privacy_accepted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["phone"]),
            # Explore discovery filters players by role.
            models.Index(fields=["role"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(email__isnull=False) | Q(phone__isnull=False),
                name="user_email_or_phone_required"
            )
        ]

    def __str__(self):
        return self.email or self.phone or str(self.id)
    
    @property
    def profile_name(self):
        """Return the name from profile if exists, else fallback to username"""
        return getattr(self.profile, 'name', self.username)

    @property
    def is_minor(self):
        """
        Whether this user is a minor under THEIR OWN jurisdiction's rules.

        The two halves live in different tables — the birthdate on the profile,
        the legal country on the user — so this is the one place that knows how
        to put them together. Callers ask the user, never the table.

        **Never raises, and answers True when it cannot tell.** The profile is
        a separate row created in a second INSERT: signup writes both in one
        transaction, but a Google account created before that code existed, a
        staff-made user, or a fixture can all be a User with no profile at all.
        A RelatedObjectDoesNotExist escaping from a property this cheap-looking
        would blow up whatever template or serializer touched it, and the
        answer it would have been blocking is the safe one anyway — an account
        whose age is unknown is treated as a child (see constants.is_minor).
        """
        profile = getattr(self, "profile", None)
        birthdate = getattr(profile, "birthdate", None)

        return account_constants.is_minor(birthdate, self.country_code)



class UserProfile(BaseUUIDModel):
    class Gender(models.TextChoices):
        MALE = "male", "Male"
        FEMALE = "female", "Female"
        OTHER = "other", "Other"

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile"
    )
    name = models.CharField(max_length=150)
    headline = models.CharField(max_length=255, blank=True)
    about = models.TextField(blank=True)

    profile_photo = models.URLField(blank=True)
    profile_photo_public_id = models.CharField(max_length=255, blank=True)

    cover_photo = models.URLField(blank=True)
    cover_photo_public_id = models.CharField(max_length=255, blank=True)

    followers_count = models.PositiveIntegerField(default=0)
    following_count = models.PositiveIntegerField(default=0)
    connections_count = models.PositiveIntegerField(default=0)

    # Governs the LOGGED-OUT web view only (GET /public/profile/<username>).
    # It is not a "private account" switch: every signed-in Goatza actor still
    # sees this profile exactly as they do today, and a hidden profile stays
    # shareable in DMs — the share preview renders normally. Turning it off
    # only stops anonymous visitors and link-preview crawlers.
    is_public_profile = models.BooleanField(
        default=True,
        help_text="Visible to logged-out visitors. Does not affect in-app visibility.",
    )

    gender = models.CharField(
        max_length=10,
        choices=Gender.choices,
        blank=True
    )
    birthdate = models.DateField(null=True, blank=True)
    height_cm = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(50), MaxValueValidator(300)]
    )
    weight_kg = models.DecimalField(
        max_digits=5, decimal_places=2,
        null=True, blank=True,
        validators=[MinValueValidator(20), MaxValueValidator(300)]
    )

    # Location (city-based)
    location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="users"
    )
    # Denormalized for better query
    location_name = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    country_code = models.CharField(max_length=5, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)


    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["city"]),
            models.Index(fields=["latitude", "longitude"]),
            # Explore "popular" mode orders players by follower count.
            models.Index(fields=["followers_count"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.user_id})"