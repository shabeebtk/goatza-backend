"""
Keeps achievements consistent when an issuing organization is hard-deleted.

``awarded_by`` is SET_NULL, which Django applies as a bulk UPDATE that never
reaches ``Achievement.save()``. On its own that would strand a decided row with
no issuer — exactly what ``achievement_verification_requires_issuer`` refuses —
so deleting an org that had issued anything failed with an IntegrityError and
took the whole delete down with it.

This is the "the service resets the status in the same breath" half of that
constraint's contract, and it belongs to the app that owns the constraint rather
than to organization/.

``awarded_by_name`` is deliberately left alone: it is synced from the org on
every save precisely so the award keeps the name of the body that issued it
after that body is gone. The org's outstanding verification-request
notifications need no cleanup either — ``Notification.recipient_org`` is CASCADE,
so they go with the org.
"""

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from achievements.models import Achievement
from organization.models import Organization


@receiver(
    pre_delete,
    sender=Organization,
    dispatch_uid="achievements_release_issued_on_org_delete",
)
def release_issued_achievements(sender, instance, **kwargs):
    """
    Drop every award this org issued back to ``self_reported`` before the
    SET_NULL lands.

    ``pre_delete`` runs ahead of Django's field updates, so this closes the
    window the constraint would otherwise fail in. The verification audit fields
    go with the status — nobody is left standing behind the confirmation, and a
    ``self_reported`` row carrying a ``verified_by`` would be a lie.
    """
    (
        Achievement.objects
        .filter(awarded_by=instance)
        .exclude(verification_status=Achievement.VerificationStatus.SELF_REPORTED)
        .update(
            verification_status=Achievement.VerificationStatus.SELF_REPORTED,
            verified_by=None,
            verified_at=None,
        )
    )
