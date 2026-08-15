"""
Shaping for the CV — the public payload's CV-scoped parts, and the owner's
settings.

The header block is NOT shaped here: it is ``PublicUserProfileSerializer``,
reused unchanged. That serializer is an explicit allow-list with a pinning test
(see accounts/serializers/public_profile_serializers.py) and widening it to suit
the CV would widen the profile page with it. Anything the CV needs and the
profile does not carry gets its own serializer here, with its own allow-list and
its own pinning test — which is exactly what the contact block below is.
"""

from rest_framework import serializers

from cv.models import PlayerCVSettings


# =====================================================================
# PUBLIC
# =====================================================================

class PublicCVContactSerializer(serializers.Serializer):
    """
    The contact block, rendered from the ``User`` — phone and nothing else.

    Email is deliberately absent and there is no toggle that can produce it. A
    phone number on a bio-data sheet is what an Indian coach actually calls, it
    is what the player chose to publish, and it is revocable by switching the
    toggle off. An email address is the account identifier: publishing it hands
    a scraper the first half of every credential-stuffing attempt against this
    person's Goatza login.

    Only rendered when ``show_contact`` is on AND the phone is set — the key is
    absent otherwise, never present-and-empty.
    """

    phone = serializers.CharField()


# The exact key set a contact block may carry. Pinned by a test so "and their
# email, it would be handy" has to be a deliberate act.
CV_CONTACT_KEYS = {"phone"}


# The exact key set the public CV payload may contain.
#
#   profile / views_count — always present
#   contact / career / achievements / highlights — present only when the
#   matching toggle is on. Toggled off means ABSENT, not empty: an empty list
#   still sits in the server-rendered page source, and "hidden" that a reader
#   can View Source is not hidden.
#
# Imported by the pinning test in cv/tests. Keep in sync with
# ``cv.selectors.cv_selectors.cv_payload`` and nowhere else.
CV_PAYLOAD_ALWAYS_KEYS = {
    "profile",
    "views_count",
}

CV_PAYLOAD_OPTIONAL_KEYS = {
    "contact",
    "career",
    "achievements",
    "highlights",
}

CV_PAYLOAD_KEYS = CV_PAYLOAD_ALWAYS_KEYS | CV_PAYLOAD_OPTIONAL_KEYS


# =====================================================================
# OWNER
# =====================================================================

class CVSettingsSerializer(serializers.ModelSerializer):
    """
    What the owner's settings screen reads back, from GET and from PATCH alike.

    ``is_public_profile`` rides along because the CV depends on it (see
    ``get_cv_user``) and the screen has to be able to say "your link will not
    work until your profile is public" without a second fetch. ``username``
    likewise, so the screen can print the shareable link from one response.

    Owner-scoped, never public: this is the only place ``views_count`` is
    readable as the owner's own number.
    """

    username = serializers.SerializerMethodField()
    is_public_profile = serializers.SerializerMethodField()

    class Meta:
        model = PlayerCVSettings
        fields = [
            "is_enabled",
            "show_contact",
            "show_attributes",
            "show_career",
            "show_achievements",
            "show_highlights",
            "views_count",
            "username",
            "is_public_profile",
            "updated_at",
        ]
        read_only_fields = fields

    def get_username(self, obj):
        return obj.user.username

    def get_is_public_profile(self, obj):
        profile = getattr(obj.user, "profile", None)
        return bool(profile and profile.is_public_profile)


class CVSettingsUpdateSerializer(serializers.Serializer):
    """
    PATCH body — send only what changes.

    Every field is optional and none has a default: an absent key must leave
    its toggle alone, and a `required=True` default of False would let a
    malformed request silently strip a section off somebody's CV. An empty body
    is rejected rather than being a no-op that reports success.
    """

    is_enabled = serializers.BooleanField(required=False)
    show_contact = serializers.BooleanField(required=False)
    show_attributes = serializers.BooleanField(required=False)
    show_career = serializers.BooleanField(required=False)
    show_achievements = serializers.BooleanField(required=False)
    show_highlights = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Nothing to update. Send at least one setting."
            )
        return attrs
