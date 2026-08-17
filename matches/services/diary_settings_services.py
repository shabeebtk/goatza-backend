"""
Write path for the player's match diary settings.

One row per player, holding the one thing they can switch (``showcase_summary``)
plus the streak counters the diary maintains for them. Same shape as
``cv.services.cv_services.CVService``'s settings half: get-or-create rather than
404, because a player who has never opened the screen has no row and "your
settings do not exist" is not something a settings screen can render — the model
defaults ARE the answer.

``require_player`` is imported from ``MatchService`` rather than copied. The CV
service copies its version deliberately, but that copy is across APPS: highlights
and the CV are different features that happen to agree today. This is the same
feature — the diary — split across two files, and one gate that drifted from the
other would mean the settings screen and the diary itself disagreeing about who
owns a diary.

The streak counters are deliberately NOT writable here. They are derived from
the player's matches by ``MatchService.recompute_streak``; a settings endpoint
that could set them would make them a claim rather than a fact.
"""

from rest_framework.exceptions import ValidationError

from matches.models import MatchDiarySettings
from matches.services.match_services import MatchService


class DiarySettingsService:

    # The only field a PATCH may touch. Everything else on the row is derived.
    TOGGLE_FIELDS = (
        "showcase_summary",
    )

    @staticmethod
    def get_or_create_for(user) -> MatchDiarySettings:
        """
        The player's settings row, created with model defaults on first use.

        Takes a User rather than an actor: the caller has already been through
        ``require_player``, and the streak writer inside ``MatchService`` needs
        the same row without an actor in hand.
        """
        settings, _ = MatchDiarySettings.objects.get_or_create(user=user)

        # Prime the FK cache — the serializer reads user fields, and
        # get_or_create does not guarantee it leaves the object it was handed
        # attached.
        settings.user = user

        return settings

    @staticmethod
    def get_settings(actor) -> MatchDiarySettings:
        """The requester's own settings, created on first read."""
        user = MatchService.require_player(actor)
        return DiarySettingsService.get_or_create_for(user)

    @staticmethod
    def update_settings(actor, *, showcase_summary=None) -> MatchDiarySettings:
        """
        Apply a partial update to the player's own row.

        Passing nothing is rejected rather than being a silent no-op, matching
        ``HighlightService.update_highlight``: an empty PATCH is a client bug,
        and answering 200 to it hides the bug behind a screen that looks like
        it saved.
        """
        user = MatchService.require_player(actor)
        settings = DiarySettingsService.get_or_create_for(user)

        if showcase_summary is None:
            raise ValidationError(
                "Nothing to update. Send showcase_summary."
            )

        if settings.showcase_summary != bool(showcase_summary):
            settings.showcase_summary = bool(showcase_summary)
            # auto_now only fires for fields listed in update_fields.
            settings.save(update_fields=["showcase_summary", "updated_at"])

        return settings
