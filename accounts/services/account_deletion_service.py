"""
User-initiated account deletion — the DEACTIVATE half.

Two steps, because the credential a user can produce depends on how they signed
up:

  * ``initiate_account_deletion`` says WHICH credential this account can be
    confirmed with. An account with a usable password confirms with it; a
    Google-only account has no password it has ever chosen, so a one-time code
    goes to its address instead.
  * ``confirm_account_deletion`` checks that credential and takes the account
    off the platform.

Nothing here deletes a row. Confirming flips ``is_active`` off and stamps
``deletion_requested_at``, which is what the 30-day purge
(``accounts/management/commands/purge_deleted_accounts.py``) selects on later.
The gap is the whole point: an account taken by someone else, or a decision
regretted at 2am, is still recoverable by a human until the purge runs.

``deletion_requested_at`` is also what keeps the three meanings of
``is_active=False`` apart — an unverified signup and a staff suspension both
leave it NULL and are never swept up by the purge.
"""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken, OutstandingToken,
)

from notifications.models import UserFCMToken
from organization.models import Organization, OrganizationMember
from utils.emails import send_email_async
from utils.otp_validation import generate_otp, verify_otp

logger = logging.getLogger(__name__)

# Scopes the deletion code to its own cache key. Without it the code mailed by
# forgot-password — which anyone who reaches the inbox can request — would also
# confirm the deletion of a Google-only account.
DELETION_OTP_PURPOSE = "account_delete"

METHOD_PASSWORD = "password"
METHOD_OTP = "otp"

# Said to a caller whose password or code did not match. One message for both
# so a wrong code and an expired one are indistinguishable.
INVALID_CREDENTIAL_MESSAGE = "The password or code you entered is incorrect."


def mask_email(email):
    """
    ``shabeeb@gmail.com`` -> ``s*****b@gmail.com``.

    Enough for the owner to recognise which inbox to open, not enough to be
    worth reading off a screen someone else is looking at. Short local parts
    degrade rather than leak: one or two characters are returned fully starred.
    """
    if not email or "@" not in email:
        return ""

    local, _, domain = email.partition("@")

    if len(local) <= 2:
        return f"{'*' * len(local)}@{domain}"

    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


# ─────────────────────────────────────────────
# SOLE-OWNER GUARD
# ─────────────────────────────────────────────

def sole_owned_organizations(user):
    """
    The organizations that would be left with NO owner if this user went.

    An org whose only OWNER leaves is unadministrable: nobody can add members,
    edit the profile, publish a recruitment or hand ownership on. Nothing in
    the app can recover from that state, so it is refused at the door rather
    than cleaned up afterwards.

    Co-owned orgs are fine (the other owner carries on) and so is an org this
    user is only an ADMIN or COACH of — that is somebody else's org.

    Three queries regardless of how many orgs are involved: the user's owner
    memberships, one pass over every OWNER row for those orgs, then the names.
    """
    owned_org_ids = list(
        OrganizationMember.objects
        .filter(user=user, role=OrganizationMember.Role.OWNER)
        .values_list("organization_id", flat=True)
    )

    if not owned_org_ids:
        return []

    # Every OTHER owner across those orgs, in one query.
    co_owned_ids = set(
        OrganizationMember.objects
        .filter(
            organization_id__in=owned_org_ids,
            role=OrganizationMember.Role.OWNER,
        )
        .exclude(user=user)
        .values_list("organization_id", flat=True)
    )

    orphan_ids = [
        org_id for org_id in owned_org_ids if org_id not in co_owned_ids
    ]

    if not orphan_ids:
        return []

    return list(
        Organization.objects
        .filter(id__in=orphan_ids)
        .only("id", "name", "username")
        .order_by("name")
    )


def _assert_no_orphaned_organizations(user):
    orphans = sole_owned_organizations(user)
    if not orphans:
        return

    names = ", ".join(
        f"{org.name} (@{org.username})" if org.username else org.name
        for org in orphans
    )
    raise ValidationError(
        "Transfer ownership of these organizations before deleting your "
        f"account: {names}"
    )


# ─────────────────────────────────────────────
# STEP 1 — INITIATE
# ─────────────────────────────────────────────

def initiate_account_deletion(user):
    """
    Tell the client which credential confirms this account, and send the code
    when that credential is a code.

    Returns ``{"method": "password"}`` or
    ``{"method": "otp", "sent_to": "<masked email>"}``.

    The sole-owner guard runs HERE as well as in confirm. Confirm is where it
    is enforced — that is the write — but checking it first means a user who
    cannot delete yet is told why on the button press, instead of after typing
    their password.
    """
    _assert_no_orphaned_organizations(user)

    if user.has_usable_password():
        return {"method": METHOD_PASSWORD}

    # Google-only account: the password column holds an unusable hash nobody
    # has ever been shown, so the mailbox is the only credential they have.
    if not user.email:
        raise ValidationError(
            "This account has no password and no email address, so it cannot "
            "be deleted from the app. Please contact support."
        )

    otp = generate_otp(user.email, purpose=DELETION_OTP_PURPOSE)

    send_email_async(
        subject="Confirm your Goatza account deletion",
        message=(
            f"Hello {user.profile_name},\n\n"
            f"Your code to confirm deleting your Goatza account is: {otp}\n"
            "It is valid for 10 minutes.\n\n"
            "If you did not ask to delete your account, ignore this email and "
            "change your password."
        ),
        to_email=user.email,
    )

    logger.info(f"Account deletion OTP sent | user={user.id}")

    return {"method": METHOD_OTP, "sent_to": mask_email(user.email)}


# ─────────────────────────────────────────────
# STEP 2 — CONFIRM
# ─────────────────────────────────────────────

def _verify_credential(user, password, otp):
    """
    Check the ONE credential this account is allowed to confirm with.

    The method is decided by the ACCOUNT, never by which key the client sent:
    accepting whichever of the two was supplied would let a Google-only account
    be deleted with the empty-string password its unusable hash represents.
    """
    if user.has_usable_password():
        if not password or not user.check_password(password):
            raise ValidationError(INVALID_CREDENTIAL_MESSAGE)
        return

    if not otp or not user.email:
        raise ValidationError(INVALID_CREDENTIAL_MESSAGE)

    # verify_otp consumes the code on success, so it cannot be replayed.
    if not verify_otp(user.email, str(otp), purpose=DELETION_OTP_PURPOSE):
        raise ValidationError(INVALID_CREDENTIAL_MESSAGE)


@transaction.atomic
def confirm_account_deletion(user, *, password=None, otp=None):
    """
    Deactivate the account and end every session it has.

    Order matters and is the order below:

      1. The sole-owner guard, BEFORE anything is verified or written — an org
         must never be orphaned, and refusing costs nothing at this point.
      2. The credential. Everything after this is destructive to the caller's
         session, so it happens only once we know who is asking.
      3. Blacklist every outstanding refresh token (the same block
         ChangePasswordAPIView uses). Other devices die on their next refresh
         rather than staying signed in to a deleted account for 30 days.
      4. is_active=False + deletion_requested_at=now. This IS the deletion as
         far as everything that reads the app is concerned.
      5. Drop the FCM device tokens, so push stops immediately. Deleted rather
         than deactivated: a token is a device registration, and the account is
         not coming back to that device on its own.

    Raises ValidationError (400) for the orphaned-org and bad-credential cases.
    """
    _assert_no_orphaned_organizations(user)

    _verify_credential(user, password, otp)

    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)

    user.is_active = False
    user.deletion_requested_at = timezone.now()
    user.save(update_fields=["is_active", "deletion_requested_at", "updated_at"])

    removed_tokens, _ = UserFCMToken.objects.filter(user=user).delete()

    logger.info(
        f"Account deletion confirmed | user={user.id} "
        f"| fcm_tokens_removed={removed_tokens}"
    )
