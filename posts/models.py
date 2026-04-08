import uuid
from django.db import models
from django.conf import settings
from django.db.models import Q
from shared.models import BaseUUIDModel, Location
from accounts.models import User
from organization.models import Organization
from sports.models import Sport


class Post(BaseUUIDModel):
    class PostType(models.TextChoices):
        NORMAL = "normal", "Normal"

    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public"
        FOLLOWERS = "followers", "Followers"

    author_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="posts"
    )
    author_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="posts"
    )
    content = models.TextField(blank=True)
    post_type = models.CharField(
        max_length=20,
        choices=PostType.choices,
        default=PostType.NORMAL
    )
    sport = models.ForeignKey(
        Sport,
        on_delete=models.CASCADE,
        related_name="posts",
        blank=True, null=True
    )
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.PUBLIC
    )
    # Denormalized counts
    likes_count = models.PositiveIntegerField(default=0)
    likes_breakdown = models.JSONField(default=dict)

    comments_count = models.PositiveIntegerField(default=0)
    is_deleted = models.BooleanField(default=False)

    # Location (city-based)
    location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posts"
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
        db_table = "posts"
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["author_user"]),
            models.Index(fields=["author_org"]),
            models.Index(fields=["sport"]),
            models.Index(fields=["city"]),
            models.Index(fields=["latitude", "longitude"])
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(author_user__isnull=False, author_org__isnull=True) |
                    Q(author_user__isnull=True, author_org__isnull=False)
                ),
                name="post_author_user_or_org"
            )
        ]

    def __str__(self):
        return f"Post {self.id}"
    


class PostMedia(BaseUUIDModel):
    class MediaType(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"

    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="media"
    )
    file_url = models.URLField()
    public_id = models.CharField(max_length=255)
    media_type = models.CharField(max_length=10, choices=MediaType.choices)
    thumbnail_url = models.URLField(blank=True)
    duration = models.PositiveIntegerField(null=True, blank=True)
    # For ordering (carousel)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "post_media"
        ordering = ["order"]
        indexes = [
            models.Index(fields=["post"]),
        ]

    def __str__(self):
        return f"{self.media_type} - {self.post_id}"
    

class Like(BaseUUIDModel):
    class Type(models.TextChoices):
        LIKE = "like"
        FIRE = "fire"
        RESPECT = "respect"
        FUNNY = "funny"

    # WHO liked
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="likes"
    )
    organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="likes"
    )
    # TARGET
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="likes"
    )
    type = models.CharField(max_length=10, choices=Type.choices, default=Type.LIKE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "likes"

        constraints = [
            # Only one actor
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=False, organization__isnull=True) |
                    Q(user__isnull=True, organization__isnull=False)
                ),
                name="like_user_or_org"
            ),

            # Unique user like
            models.UniqueConstraint(
                fields=["user", "post"],
                condition=Q(user__isnull=False),
                name="unique_user_post_like"
            ),

            # Unique org like
            models.UniqueConstraint(
                fields=["organization", "post"],
                condition=Q(organization__isnull=False),
                name="unique_org_post_like"
            ),
        ]

        indexes = [
            models.Index(fields=["post"]),
            models.Index(fields=["user"]),
            models.Index(fields=["organization"]),
        ]



class Comment(BaseUUIDModel):
    # TARGET
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="comments"
    )
    # WHO commented
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="comments"
    )
    organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="comments"
    )
    comment = models.TextField()
    reply_count = models.PositiveIntegerField(default=0)

    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="replies"
    )
    # WHO THIS REPLY IS TARGETING (NEW)
    reply_to = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+"
    )

    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "comments"

        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=False, organization__isnull=True) |
                    Q(user__isnull=True, organization__isnull=False)
                ),
                name="comment_user_or_org"
            )
        ]

        indexes = [
            models.Index(fields=["post"]),
            models.Index(fields=["parent"]),
        ]



class PostMention(BaseUUIDModel):
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="mentions"
    )
    mentioned_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )
    mentioned_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )

    class Meta:
        db_table = "post_mentions"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(mentioned_user__isnull=False, mentioned_org__isnull=True) |
                    Q(mentioned_user__isnull=True, mentioned_org__isnull=False)
                ),
                name="mention_user_or_org"
            )
        ]



# class Location(models.Model): 
#     id = models.BigAutoField(primary_key=True)

#     name = models.CharField(max_length=255)
#     address = models.TextField(blank=True)

#     city = models.CharField(max_length=100, blank=True)
#     state = models.CharField(max_length=100, blank=True)
#     country = models.CharField(max_length=100, blank=True)

#     latitude = models.FloatField()
#     longitude = models.FloatField()

#     google_place_id = models.CharField(max_length=255, unique=True)

#     created_at = models.DateTimeField(auto_now_add=True)

#     class Meta:
#         db_table = "locations"
#         indexes = [
#             models.Index(fields=["google_place_id"]),
#             models.Index(fields=["latitude", "longitude"]),
#         ]

#     def __str__(self):
#         return self.name


class SavedPost(BaseUUIDModel):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="saved_posts"
    )
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="saved_by"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "saved_posts"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "post"],
                name="unique_saved_post"
            )
        ]



class Hashtag(BaseUUIDModel):
    name = models.CharField(max_length=100, unique=True)
    class Meta:
        db_table = "hashtags"

    def __str__(self):
        return self.name
    

class PostHashtag(BaseUUIDModel):
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="post_hashtags"
    )

    hashtag = models.ForeignKey(
        Hashtag,
        on_delete=models.CASCADE,
        related_name="posts"
    )

    class Meta:
        db_table = "post_hashtags"
        constraints = [
            models.UniqueConstraint(
                fields=["post", "hashtag"],
                name="unique_post_hashtag"
            )
        ]