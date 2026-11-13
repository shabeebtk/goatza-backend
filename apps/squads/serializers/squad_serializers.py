"""
Response shapes for the club's squads screens. Reads only; every rule is in the
services. The keys are a contract with the frontend.

Context every serializer here may read:
  * ``can_review`` — the caller holds squads.review. Gates the invite link on
    SquadDetail and ``notes`` on MemberItem (club-private: never to the player
    or the public).
  * ``primary_positions`` — ``{user_id: SportPosition}`` from
    ``primary_positions_for``, so a list costs one query for all of them.

``age`` is for the club side only, and only here. Never on a public payload.
"""

from rest_framework import serializers

from apps.accounts.constants import age_on
from apps.squads.models import Squad, SquadMember
from apps.squads.selectors.config_selectors import squad_config_for


def person_mini(user, *, with_age=False):
    """PersonMini: ``{id, username, name, photo}`` (+ ``age`` for the club's lists)."""
    profile = getattr(user, "profile", None)
    data = {
        "id": str(user.id),
        "username": user.username or "",
        "name": getattr(profile, "name", "") or user.username or "",
        "photo": getattr(profile, "profile_photo", "") or "",
    }
    if with_age:
        data["age"] = age_on(getattr(profile, "birthdate", None))
    return data


def position_mini(position, *, with_group=False):
    if position is None:
        return None
    data = {"id": str(position.id), "name": position.name}
    if with_group:
        data["group"] = position.group
    return data


def _primary_position(serializer, user_id):
    return position_mini((serializer.context.get("primary_positions") or {}).get(user_id))


# ---------------------------------------------------------------------
# SQUADS
# ---------------------------------------------------------------------

class SquadListItemSerializer(serializers.ModelSerializer):
    sport = serializers.SerializerMethodField()
    level_label = serializers.SerializerMethodField()
    open_invites_count = serializers.SerializerMethodField()

    class Meta:
        model = Squad
        fields = (
            "id", "name", "sport", "level", "level_label", "age_group", "season",
            "photo_url", "is_public", "invite_enabled", "members_count",
            "pending_count", "open_invites_count", "created_at",
        )

    def get_sport(self, obj):
        return {
            "id": str(obj.sport_id),
            "name": obj.sport.name,
            "icon_name": obj.sport.icon_name,
        }

    def get_level_label(self, obj):
        return obj.get_level_display() if obj.level else ""

    def get_open_invites_count(self, obj):
        # Annotated by the selectors (org_squads, squad_detail).
        return getattr(obj, "open_invites_count", 0) or 0


class SquadDetailSerializer(SquadListItemSerializer):
    config = serializers.SerializerMethodField()
    staff = serializers.SerializerMethodField()
    captain_member_id = serializers.SerializerMethodField()
    invite = serializers.SerializerMethodField()

    class Meta(SquadListItemSerializer.Meta):
        fields = SquadListItemSerializer.Meta.fields + (
            "description", "config", "staff", "captain_member_id", "invite",
        )

    def get_config(self, obj):
        return squad_config_for(obj.sport)

    def get_staff(self, obj):
        # ``active_staff`` is prefetched by squad_detail, already filtered to
        # members who still belong to the org.
        return [
            {
                "id": str(staff.id),
                "member_id": str(staff.member_id),
                "role": staff.role,
                "role_label": staff.get_role_display(),
                "title": staff.title,
                "user": person_mini(staff.member.user),
            }
            for staff in getattr(obj, "active_staff", [])
        ]

    def get_captain_member_id(self, obj):
        captain_id = getattr(obj, "captain_member_id", None)
        return str(captain_id) if captain_id else None

    def get_invite(self, obj):
        if not self.context.get("can_review"):
            return None
        # Imported here: the service layer imports nothing from serializers,
        # and keeping it that way is what keeps this module a leaf.
        from apps.squads.services.squad_service import SquadService

        return {"url": SquadService.invite_url(obj), "enabled": obj.invite_enabled}


# ---------------------------------------------------------------------
# MEMBERS
# ---------------------------------------------------------------------

class MemberItemSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    primary_position = serializers.SerializerMethodField()
    position = serializers.SerializerMethodField()
    career_entry_id = serializers.SerializerMethodField()

    class Meta:
        model = SquadMember
        fields = (
            "id", "status", "source", "user", "primary_position", "position",
            "jersey_number", "is_captain", "joined_at", "career_entry_id", "notes",
        )

    def get_user(self, obj):
        return person_mini(obj.user, with_age=True)

    def get_primary_position(self, obj):
        return _primary_position(self, obj.user_id)

    def get_position(self, obj):
        return position_mini(obj.position, with_group=True)

    def get_career_entry_id(self, obj):
        return str(obj.career_entry_id) if obj.career_entry_id else None

    def to_representation(self, obj):
        data = super().to_representation(obj)
        # Club-private. Only the people who run the players see it.
        if not self.context.get("can_review"):
            data.pop("notes", None)
        return data


class RequestItemSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    primary_position = serializers.SerializerMethodField()

    class Meta:
        model = SquadMember
        fields = ("id", "source", "requested_at", "user", "primary_position")

    def get_user(self, obj):
        return person_mini(obj.user, with_age=True)

    def get_primary_position(self, obj):
        return _primary_position(self, obj.user_id)


class InviteItemSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    position = serializers.SerializerMethodField()
    invited_by_name = serializers.SerializerMethodField()
    recruitment = serializers.SerializerMethodField()

    class Meta:
        model = SquadMember
        fields = (
            "id", "source", "requested_at", "expires_at", "user", "position",
            "jersey_number", "invited_by_name", "recruitment",
        )

    def get_user(self, obj):
        return person_mini(obj.user)

    def get_position(self, obj):
        return position_mini(obj.position, with_group=True)

    def get_invited_by_name(self, obj):
        inviter = obj.invited_by_member
        return person_mini(inviter.user)["name"] if inviter else ""

    def get_recruitment(self, obj):
        application = obj.recruitment_application
        if application is None:
            return None
        return {
            "id": str(application.recruitment_id),
            "title": application.recruitment.title,
        }


# ---------------------------------------------------------------------
# COMPOSITE PAYLOADS
# ---------------------------------------------------------------------

def roster_payload(roster, *, can_review):
    """Roster: ``{count, groups: [{name, count, members: [MemberItem]}]}``."""
    context = {
        "can_review": can_review,
        "primary_positions": roster["primary_positions"],
    }
    return {
        "count": roster["count"],
        "groups": [
            {
                "name": group["name"],
                "count": len(group["members"]),
                "members": MemberItemSerializer(
                    group["members"], many=True, context=context
                ).data,
            }
            for group in roster["groups"]
        ],
    }


def requests_payload(requests, invites, primary_positions):
    """Requests: ``{requests: [...], invites: [...]}``."""
    return {
        "requests": RequestItemSerializer(
            requests, many=True, context={"primary_positions": primary_positions}
        ).data,
        "invites": InviteItemSerializer(invites, many=True).data,
    }


def candidate_payload(candidate):
    """Candidate: ``{user: PersonMini, primary_position, state}``."""
    return {
        "user": person_mini(candidate["user"]),
        "primary_position": position_mini(candidate["primary_position"]),
        "state": candidate["state"],
    }


def approve_payload(result, *, can_review, primary_positions):
    return {
        "approved": MemberItemSerializer(
            result["approved"],
            many=True,
            context={"can_review": can_review, "primary_positions": primary_positions},
        ).data,
        "failed": result["failed"],
    }
