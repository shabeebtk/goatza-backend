# recruitments/serializers/announcement_serializers.py
"""
Request and response shapes for announcements.

The write serializer validates SHAPE plus one cross-field rule the org should
not have to discover from a 500: a `session` must belong to this recruitment.
Everything else — who may post, the daily cap, whether the date means anything
in this session_mode — lives in AnnouncementService, where the recruitment row
is in hand and there is exactly one home for each rule.
"""

from rest_framework import serializers

from apps.recruitments.models import (
    AnnouncementDelivery,
    RecruitmentAnnouncement,
)
from apps.recruitments.serializers.recruitment_list_serializers import (
    trial_session_payload,
)


class AnnouncementCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=120)
    body = serializers.CharField(max_length=1000)
    audience = serializers.ChoiceField(
        choices=RecruitmentAnnouncement.Audience.choices
    )
    # Narrow to one date. Optional, and only meaningful in choose_one mode —
    # the service clears it otherwise rather than storing a claim the audience
    # resolver ignored.
    session = serializers.UUIDField(required=False, allow_null=True)

    def validate_title(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Give the announcement a title.")
        return value

    def validate_body(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Write something to send.")
        return value

    def validate(self, attrs):
        recruitment = self.context["recruitment"]

        # Belongs-to-THIS-recruitment, the same lenient-but-strict rule the
        # apply flow gives age groups and dates: an id from another
        # recruitment is REJECTED, never quietly dropped, or the org would
        # think it had narrowed an announcement that in fact went to everyone.
        session_id = attrs.get("session")
        if session_id is not None:
            session = recruitment.sessions.filter(id=session_id).first()
            if session is None:
                raise serializers.ValidationError(
                    "Invalid date for this recruitment."
                )
            attrs["session"] = session
        else:
            attrs["session"] = None

        return attrs


class RecipientsCountQuerySerializer(serializers.Serializer):
    """`?audience=&session=` for the pre-send count."""

    audience = serializers.ChoiceField(
        choices=RecruitmentAnnouncement.Audience.choices
    )
    session = serializers.UUIDField(required=False, allow_null=True)


class AnnouncementSerializer(serializers.ModelSerializer):
    """
    One announcement as anybody reads it.

    `delivery_summary` is OWNER-ONLY and is injected by the view through
    context, not annotated onto the queryset: a player has no business knowing
    how many people got the message, and computing it for every reader would
    make everyone pay for a number only the org is shown.
    """

    session = serializers.SerializerMethodField()
    posted_by = serializers.SerializerMethodField()
    delivery_summary = serializers.SerializerMethodField()

    class Meta:
        model = RecruitmentAnnouncement
        fields = [
            "id",
            "title",
            "body",
            "audience",
            "session",
            "recipients_count",
            "posted_by",
            "delivery_summary",
            "created_at",
        ]

    def get_session(self, obj):
        """The date this is about, with its venue resolved. Null when it is
        about the whole trial."""
        if obj.session is None:
            return None
        return trial_session_payload(obj.session, obj.recruitment)

    def get_posted_by(self, obj):
        """The member who posted it, for the org's own audit. Null once they
        leave — SET_NULL, so the announcement outlives the membership."""
        member = obj.created_by_member
        if member is None:
            return None

        profile = getattr(member.user, "profile", None)
        return {
            "id": str(member.id),
            "name": getattr(profile, "name", "") or member.user.username or "",
        }

    def get_delivery_summary(self, obj):
        summaries = self.context.get("delivery_summaries")
        if summaries is None:
            return None
        return summaries.get(str(obj.id))


# What a delete honestly promises. Sent as the API's own message so the client
# puts it in front of the org rather than inventing softer wording.
DELETE_MESSAGE = (
    "Announcement removed. Notifications and emails that already went out "
    "cannot be recalled."
)

DELIVERY_STATES = [
    AnnouncementDelivery.State.SENT,
    AnnouncementDelivery.State.PENDING,
    AnnouncementDelivery.State.FAILED,
    AnnouncementDelivery.State.SKIPPED,
]
