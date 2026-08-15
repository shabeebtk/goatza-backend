"""
The player's shareable Sports CV — settings only.

The CV itself stores nothing: it is a presentation of the profile, career,
achievements and highlights the player already has. All this app owns is *who
has switched it on and what they chose to show*, which is why there is one
model and it is called Settings.

No soft delete. A CV is switched off, never removed, so the section choices a
player made survive them turning it off and back on again.

Private by default, and ``show_contact`` most of all: a large share of the
players on Goatza are minors, and this is a page anybody holding the link can
read. Nothing about the CV is visible until the owner takes two separate
actions — making their profile public, and enabling the CV.
"""

from django.db import models

from accounts.models import User
from shared.models import BaseUUIDModel


class PlayerCVSettings(BaseUUIDModel):
    """
    One row per player, created lazily the first time they open the CV settings
    screen (GET /user/cv/settings get-or-creates it), so a first-time player
    reads defaults rather than a 404.

    The public CV is reachable only when BOTH ``is_enabled`` here and
    ``UserProfile.is_public_profile`` are true — see
    ``cv.selectors.cv_selectors.get_cv_user`` for why the two are coupled.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="cv_settings"
    )

    # Explicit opt-in. A player who never visits the settings screen has no
    # public CV, whatever the state of their profile.
    is_enabled = models.BooleanField(default=False)

    # Phone only, never email — see cv.selectors.cv_selectors.cv_payload.
    # OFF by default: safeguarding, not preference.
    show_contact = models.BooleanField(default=False)

    # Section toggles. ON by default — they only take effect once the CV is
    # enabled, and a CV missing its career and achievements is not a CV.
    show_attributes = models.BooleanField(default=True)
    show_career = models.BooleanField(default=True)
    show_achievements = models.BooleanField(default=True)
    show_highlights = models.BooleanField(default=True)

    # Denormalized counter, maintained by CVService.record_view — one count per
    # IP per half hour, so a refresh loop cannot inflate it.
    views_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "player_cv_settings"

    def __str__(self):
        state = "on" if self.is_enabled else "off"
        return f"CV settings ({state}) - {self.user_id}"
