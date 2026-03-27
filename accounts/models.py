from django.db import models
from django.db.models import Q
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from core.models import BaseUUIDModel

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

    email = models.EmailField(unique=True, null=True, blank=True)
    phone = models.CharField(max_length=15, unique=True, null=True, blank=True)

    username = models.CharField(max_length=50, unique=True, null=True, blank=True)

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.PLAYER)

    is_email_verified = models.BooleanField(default=False)
    is_phone_verified = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["phone"]),
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

    gender = models.CharField(
        max_length=10,
        choices=Gender.choices,
        blank=True
    )
    birthdate = models.DateField(null=True, blank=True)

    profile_photo = models.URLField(blank=True)
    cover_photo = models.URLField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.user_id})"