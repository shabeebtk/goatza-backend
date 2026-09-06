# recruitments/services/saved_recruitment_service.py
"""
The one write on the shortlist: flipping a save on or off.

Reads live in recruitments.selectors.saved_recruitment_selectors — see that
module for why a save belongs to the ACTOR rather than to the person.
"""

from recruitments.models import SavedRecruitment
from recruitments.selectors.saved_recruitment_selectors import (
    SavedRecruitmentSelector,
)


class SavedRecruitmentService:

    @staticmethod
    def toggle(actor, recruitment) -> bool:
        """
        Flip the save state of ``recruitment`` for ``actor``. Returns the NEW
        state.

        Idempotent per actor thanks to the partial unique constraints: a
        double-tap can only ever delete the one row or create the one row.
        """
        saved_by_actor = SavedRecruitmentSelector.actor_filter(actor)
        if saved_by_actor is None:
            raise ValueError("Invalid actor")

        existing = SavedRecruitment.objects.filter(
            recruitment=recruitment, **saved_by_actor
        ).first()

        if existing:
            existing.delete()
            return False

        SavedRecruitment.objects.create(
            recruitment=recruitment, **saved_by_actor
        )
        return True
