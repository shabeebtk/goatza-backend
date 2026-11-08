import logging
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from core.constant import TYPE_ORGANIZATION
from apps.sports.models import Sport
from apps.organization.services.organization_location_service import resolve_place
from apps.usernames.exceptions import UsernameTaken
from apps.usernames.services.username_service import (
    GENERATE_CLAIM_ATTEMPTS,
    UsernameService,
)
from utils.timezones import timezone_for_country
from apps.organization.models import (
    Organization,
    OrganizationProfile,
    OrganizationMember,
    OrganizationLocation,
    OrganizationSport
)

logger = logging.getLogger(__name__)

ORG_LIMIT_MESSAGE = "You can create up to 3 pages."
USERNAME_TAKEN_MESSAGE = "Username already taken"


class OrganizationCreateError(Exception):
    """
    A create the client can do something about: a code it switches on, a
    sentence for the person, and the field it belongs to (if any).

    Raised INSIDE create_organization's transaction so everything written
    before it rolls back, and turned into ``(False, as_data())`` on the way out.
    """

    def __init__(self, code, message, field=None, status_code=400):
        self.code = code
        self.message = message
        self.field = field
        self.status_code = status_code
        super().__init__(message)

    def as_data(self):
        data = {"code": self.code, "message": self.message,
                "status_code": self.status_code}
        if self.field:
            data["field"] = self.field
        return data


class OrganizationService:

    MAX_ORG_PER_USER = 3

    @staticmethod
    def organizations_created_count(user):
        """
        Every org this person created, active or not: the limit is on pages
        made, so deactivating one does not free a slot.
        """
        return Organization.objects.filter(created_by=user).count()

    @staticmethod
    def can_create_organization(user):
        return (
            OrganizationService.organizations_created_count(user)
            < OrganizationService.MAX_ORG_PER_USER
        )

    @staticmethod
    def create_eligibility(user):
        """``{allowed, limit, used}`` — the same count the create itself uses."""
        used = OrganizationService.organizations_created_count(user)
        limit = OrganizationService.MAX_ORG_PER_USER
        return {"allowed": used < limit, "limit": limit, "used": used}

    @staticmethod
    def _create_with_chosen_handle(user, data, username):
        """
        The Organization row plus EXACTLY the handle the person chose, in one
        savepoint. No retry: a taken handle is an answer, not a coin-flip.

        Two places can say "taken": the Organization table's own unique column
        (IntegrityError) and the shared registry (UsernameTaken) — a person's
        own handle lives only in the latter, which is what makes it taken for
        their page too.
        """
        try:
            with transaction.atomic():
                org = Organization.objects.create(
                    name=data["name"],
                    username=username,
                    type=data["type"],
                    created_by=user
                )
                UsernameService.claim(username, organization=org)
            return org
        except (IntegrityError, UsernameTaken):
            logger.info(f"[ORG CREATE] chosen handle {username} taken")
            raise OrganizationCreateError(
                "username_taken", USERNAME_TAKEN_MESSAGE, field="username"
            )
        except ValueError as exc:
            # The serializer already ran the validator; this is the claim's own
            # copy of the same rule, kept so the service is safe on its own.
            raise OrganizationCreateError(
                "username_invalid", str(exc), field="username"
            )


    @staticmethod
    def _create_with_handle(user, data):
        """
        The Organization row plus its UsernameRegistry row, or None if the
        namespace refused every candidate.

        The handle is generated FIRST because ``Organization.username`` is a
        non-null unique column — an org cannot exist without one, so it cannot
        be created and then named. That leaves a gap between generating and
        inserting, which two simultaneous creates from the same org name can
        both land in; the savepoint below is what lets the loser re-roll
        instead of poisoning ``create_organization``'s transaction.

        The registry is still the arbiter across actor types: generate() looks
        at it (not at Organization alone), and claim()'s unique constraint has
        the final say.
        """
        for _ in range(GENERATE_CLAIM_ATTEMPTS):
            username = UsernameService.generate(
                data["name"], owner_type=TYPE_ORGANIZATION
            )

            try:
                with transaction.atomic():
                    org = Organization.objects.create(
                        name=data["name"],
                        username=username,
                        type=data["type"],
                        created_by=user
                    )
                    UsernameService.claim(username, organization=org)
                return org
            except (IntegrityError, UsernameTaken):
                logger.info(
                    f"[ORG CREATE] handle {username} taken, re-rolling"
                )
                continue

        return None

    @staticmethod
    def create_organization(user, data):
        """
        data: the validated OrganizationCreateSerializer payload — name, type,
        and optionally username (already normalized), profile fields,
        location and sport_ids.

        Returns ``(True, payload)`` or ``(False, {code, message, field?,
        status_code})``. Codes: ``org_limit``, ``username_taken``,
        ``username_invalid``, ``create_failed``.
        """
        try:
            with transaction.atomic():
                payload = OrganizationService._create(user, data)
            return True, payload

        except OrganizationCreateError as exc:
            return False, exc.as_data()

        except Exception as e:
            logger.exception(f"Create organization error (user={user.id}): {str(e)}")
            return False, OrganizationCreateError(
                "create_failed", "Failed to create organization", status_code=500
            ).as_data()

    @staticmethod
    def _create(user, data):
        """The body of create_organization; runs inside its transaction."""
        # LIMIT CHECK, under a lock on the person's own row. Without it two
        # creates sent together both count 2, both pass, and the person ends
        # up with 4 pages. The second one now waits here until the first
        # commits, and then counts 3.
        get_user_model().objects.select_for_update().filter(pk=user.pk).first()

        if not OrganizationService.can_create_organization(user):
            raise OrganizationCreateError("org_limit", ORG_LIMIT_MESSAGE)

        # CREATE ORGANIZATION (+ its handle)
        chosen = (data.get("username") or "").strip()
        if chosen:
            org = OrganizationService._create_with_chosen_handle(user, data, chosen)
        else:
            org = OrganizationService._create_with_handle(user, data)

        if org is None:
            raise OrganizationCreateError(
                "create_failed", "Could not allocate a username, please try again"
            )

        # PROFILE
        profile = OrganizationProfile.objects.create(
            organization=org,
            headline=data.get("headline", ""),
            website=data.get("website", ""),
            logo=data.get("logo", ""),
            description=data.get("description", ""),
            level=data.get("level", ""),
        )

        # OWNER MEMBER
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            role=OrganizationMember.Role.OWNER
        )

        # LOCATION (optional)
        # The payload is one branch: `name`/`address` are the org's own,
        # everything else describes the place and goes through the shared
        # LocationService so the FK — and therefore coordinate refresh —
        # points at the same row every other actor uses.
        location = data.get('location', {})
        if location and isinstance(location, dict) and location.get('city'):
            place, columns = resolve_place(location)

            OrganizationLocation.objects.create(
                organization=org,
                name=location.get("name", ""),
                address=location.get("address", ""),
                location=place,
                city=columns["city"],
                state=columns["state"],
                country_code=columns["country_code"],
                latitude=columns["latitude"],
                longitude=columns["longitude"],
                is_primary=True
            )

            # TIMEZONE, SUGGESTED FROM THE COUNTRY. The country lives on
            # the LOCATION, not on Organization, so this is the only
            # moment on create when one is known — and only for an org
            # that gave a location at all.
            #
            # A SUGGESTION, NEVER A GUESS: a country that keeps more than
            # one clock (US, AU, BR…) is not in the map and leaves the
            # field on its default, because a wrong zone looks
            # authoritative and nobody re-checks it. Editable in org
            # settings either way.
            suggested = timezone_for_country(columns["country_code"])
            if suggested != org.timezone:
                org.timezone = suggested
                org.save(update_fields=["timezone", "updated_at"])

        # SPORTS (optional), in the order they were sent: the FIRST one is
        # the primary, and the create page labels it so. A plain
        # filter(id__in=…) comes back in the database's order, which made
        # whichever sport sorted first the primary instead.
        sport_ids = data.get("sport_ids", [])

        if sport_ids:
            by_id = {
                sport.id: sport
                for sport in Sport.objects.filter(id__in=sport_ids)
            }

            sport_objects = []
            seen = set()

            for sport_id in sport_ids:
                sport = by_id.get(sport_id)
                if sport is None or sport.id in seen:
                    continue
                seen.add(sport.id)
                sport_objects.append(
                    OrganizationSport(
                        organization=org,
                        sport=sport,
                        is_primary=not sport_objects
                    )
                )

            OrganizationSport.objects.bulk_create(sport_objects)

        logger.info(
            f"Organization created org={org.id} user={user.id}"
        )

        return {
            "id": str(org.id),
            "name": org.name,
            "username": org.username,
            "type": org.type,
            "headline": profile.headline,
            "logo": profile.logo,
        }


    @staticmethod
    def get_user_organizations(user):
        """
        List organizations where user is an active member.

        ``members__status`` must sit in the SAME filter() as ``members__user``:
        this join does not go through the active manager, and a second filter()
        would be allowed to match a different member's row.
        """
        queryset = (
            Organization.objects.filter(
                members__user=user,
                members__status=OrganizationMember.Status.ACTIVE,
                is_active=True
            )
            .select_related("profile")
            .distinct()
            .order_by("name")
        )

        logger.info(f"Fetched organizations for user={user.id}")

        return queryset

    @staticmethod
    def get_user_memberships(user):
        """
        ``{organization_id: member}`` for this user's active memberships. One
        query, so a list of their orgs can show my_role / my_permissions per row
        without a lookup per row.
        """
        return {
            member.organization_id: member
            for member in OrganizationMember.objects.filter(
                user=user,
                status=OrganizationMember.Status.ACTIVE,
            )
        }


    @staticmethod
    def get_organization(id=None, username=None):
        """
        Fetch organization by id OR username

        Args:
            id: UUID (organization_id)
            username: str

        Returns:
            Organization instance or None
        """

        queryset = (
            Organization.objects
            # A suspended org is not found, not "found but hidden": the profile
            # view turns a None from here into the standard 404 envelope, which
            # is the same answer an unknown handle gets.
            .filter(is_active=True, is_suspended=False)
            .select_related("profile")
            .prefetch_related(
                "locations",
                "sports__sport"
            )
        )

        if id:
            return queryset.filter(id=id).first()

        if username:
            return queryset.filter(username=username).first()

        return None
    