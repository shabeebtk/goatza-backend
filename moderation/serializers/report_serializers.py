"""
Wire shape for POST /moderation/report.

One body serves all six target types — ``{target_type, target_id, category,
details}`` — the same target_type/target_id pair follow and block already take,
so the client's "⋯" menu sends one shape whatever it is pointing at.

Resolution happens HERE and every miss raises the SAME NotFound string the
service uses (``TARGET_NOT_FOUND``). That identity matters: a message id that
does not exist and a message the caller was never in a thread with have to be
indistinguishable, and they only stay indistinguishable if the lookup and the
participant check answer with the same words.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.exceptions import NotFound

from accounts.models import User
from messaging.models import Message
from moderation.models import ReportCategory
from moderation.services.report_services import (
    TARGET_NOT_FOUND,
    TARGET_TYPES,
    ReportService,
)
from organization.models import Organization
from posts.models import Comment, Post
from recruitments.models import Recruitment


# target_type -> the model to resolve target_id against. Keyed off
# TARGET_TYPES so a type added to the service cannot be silently unreachable
# from the API (or vice versa) — the assert below is the check.
_MODELS = {
    "user": User,
    "organization": Organization,
    "post": Post,
    "comment": Comment,
    "message": Message,
    "recruitment": Recruitment,
}

assert set(_MODELS) == set(TARGET_TYPES), "report target types are out of sync"


class ReportCreateSerializer(serializers.Serializer):
    """
    Validates the envelope and turns it into service kwargs.

    Deliberately NOT a ModelSerializer: the request names its target by
    type + id, while the model stores it in one of six columns, and the mapping
    between the two belongs in one readable dict rather than in six write-only
    fields.
    """

    target_type = serializers.ChoiceField(choices=sorted(TARGET_TYPES))
    target_id = serializers.UUIDField()
    category = serializers.ChoiceField(choices=ReportCategory.choices)
    details = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=ReportService.MAX_DETAILS_LENGTH,
    )

    def validate(self, attrs):
        target_type = attrs["target_type"]

        model = _MODELS[target_type]

        try:
            target = model.objects.filter(id=attrs["target_id"]).first()
        except DjangoValidationError:
            # A well-formed UUID that the column still refuses. Same answer as
            # a miss — nothing here is worth a distinct message.
            target = None

        if target is None:
            raise NotFound(TARGET_NOT_FOUND)

        attrs["target_kwarg"] = TARGET_TYPES[target_type]
        attrs["target"] = target

        return attrs

    def to_service_kwargs(self):
        """The validated body as ``ReportService.create`` keyword arguments."""
        data = self.validated_data

        return {
            "category": data["category"],
            "details": data.get("details", ""),
            data["target_kwarg"]: data["target"],
        }
