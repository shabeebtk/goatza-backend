from django.db import models
from django.db.models import Q, F
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from shared.models import BaseUUIDModel, Location
from organization.models import Organization, OrganizationMember
from accounts.models import User
from sports.models import Sport, SportPosition
# Create your models here.



class Recruitment(BaseUUIDModel):

    class Type(models.TextChoices):
        OPEN_TRIAL = "open_trial", "Open Trial"
        PLAYER_LOOKING = "player_looking", "Player Looking"
        DIRECT_RECRUITMENT = "direct_recruitment", "Direct Recruitment"
        SCHOLARSHIP = "scholarship", "Scholarship"

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

    # Experience / level
    experience_level = models.CharField(
        max_length=50,
        blank=True
    )

    # Recruitment logistics
    application_deadline = models.DateTimeField(
        null=True,
        blank=True
    )

    event_date = models.DateTimeField(
        null=True,
        blank=True
    )

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

    # Denormalized analytics
    views_count = models.PositiveIntegerField(default=0)
    applications_count = models.PositiveIntegerField(default=0)
    shortlisted_count = models.PositiveIntegerField(default=0)
    selected_count = models.PositiveIntegerField(default=0)

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

    def __str__(self):
        return f"{self.title} ({self.organization.name})"



class RecruitmentAgeCategory(BaseUUIDModel):
    recruitment = models.ForeignKey(
        Recruitment,
        on_delete=models.CASCADE,
        related_name="age_categories"
    )
    title = models.CharField(max_length=50)
    min_birth_year = models.PositiveIntegerField()
    max_birth_year = models.PositiveIntegerField()
    reporting_time = models.TimeField(null=True, blank=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:

        db_table = "recruitment_age_categories"

        ordering = ["display_order"]

        indexes = [
            models.Index(fields=["recruitment"]),
        ]


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

    file_url = models.URLField()
    public_id = models.CharField(max_length=255)

    thumbnail_url = models.URLField(blank=True)

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

    class Status(models.TextChoices):
        APPLIED = "applied", "Applied"
        REVIEWING = "reviewing", "Reviewing"
        SHORTLISTED = "shortlisted", "Shortlisted"
        INVITED = "invited", "Invited"
        SELECTED = "selected", "Selected"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

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

    applied_position = models.ForeignKey(
        SportPosition,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applications"
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
            )
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