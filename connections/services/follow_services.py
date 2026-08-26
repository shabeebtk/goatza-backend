from connections.models import Follow
from accounts.models import User, UserProfile
from django.db import transaction
from django.db.models import F, Q
from notifications.services.notification_service import NotificationService
from core.constant import TYPE_ORGANIZATION, TYPE_USER
from organization.models import Organization, OrganizationProfile
from moderation.services.block_guard import require_not_blocked
from moderation.services.block_services import BlockService


class FollowService:

    @staticmethod
    def get_following_ids(actor):
        """
        Returns:
        {
            "user_ids": [...],
            "org_ids": [...]
        }

        ``actor`` is None for an anonymous caller (core.actor.resolve_actor
        returns None when there is no token) — the public profile endpoints
        reach here through the shared post-visibility filter. An anonymous
        viewer follows nobody, so the empty answer is the correct one and every
        caller's `id in following_ids[...]` test degrades to False on its own.
        """

        if actor is None:
            return {"user_ids": [], "org_ids": []}

        if actor.is_user:
            user_ids = Follow.objects.filter(
                follower_user=actor.user,
                following_user__isnull=False
            ).values_list("following_user_id", flat=True)

            org_ids = Follow.objects.filter(
                follower_user=actor.user,
                following_org__isnull=False
            ).values_list("following_org_id", flat=True)

        else:
            user_ids = Follow.objects.filter(
                follower_org=actor.organization,
                following_user__isnull=False
            ).values_list("following_user_id", flat=True)

            org_ids = Follow.objects.filter(
                follower_org=actor.organization,
                following_org__isnull=False
            ).values_list("following_org_id", flat=True)

        return {
            "user_ids": list(user_ids),
            "org_ids": list(org_ids)
        }
    
    @staticmethod
    @transaction.atomic
    def follow(actor, target_user=None, target_org=None):
        """
        actor: request.actor
        target_user OR target_org required
        """

        # Self follow prevention
        if actor.is_user and target_user and actor.user.id == target_user.id:
            return False, "Cannot follow yourself"

        if actor.is_org and target_org and actor.organization.id == target_org.id:
            return False, "Cannot follow your own organization"

        # BLOCK GUARD — either direction. Raises BlockedError (403).
        require_not_blocked(actor, target_user or target_org)

        follow_data = {}

        # set follower
        if actor.is_user:
            follow_data["follower_user"] = actor.user
        else:
            follow_data["follower_org"] = actor.organization

        # set target
        if target_user:
            follow_data["following_user"] = target_user
        else:
            follow_data["following_org"] = target_org

        follow, created = Follow.objects.get_or_create(**follow_data)

        if not created:
            return False, "Already following"

        # =========================
        # COUNT LOGIC (USER + ORG)
        # =========================
        if actor.is_user:
            UserProfile.objects.filter(user=actor.user).update(
                following_count=F("following_count") + 1
            )
        elif actor.is_org:
            OrganizationProfile.objects.filter(organization=actor.organization).update(
                following_count=F("following_count") + 1
            )

        if target_user:
            # target followers count
            UserProfile.objects.filter(user=target_user).update(
                followers_count=F("followers_count") + 1
            )

            # MUTUAL CONNECTION
            is_mutual = False

            if actor.is_user:
                is_mutual = Follow.objects.filter(
                    follower_user=target_user,
                    following_user=actor.user
                ).exists()

                if is_mutual:
                    UserProfile.objects.filter(user=actor.user).update(
                        connections_count=F("connections_count") + 1
                    )
                    UserProfile.objects.filter(user=target_user).update(
                        connections_count=F("connections_count") + 1
                    )

            # NOTIFICATION NORMAL FOLLOW
            if is_mutual:
                # ONLY FOLLOW BACK
                NotificationService.follow_back(
                    actor_user=actor.user,
                    target_user=target_user
                )
            else:
                # NORMAL FOLLOW
                NotificationService.follow(
                    actor_user=actor.user if actor.is_user else None,
                    actor_org=actor.organization if actor.is_org else None,
                    target_user=target_user,
                    target_org=None
                )

        elif target_org:
            OrganizationProfile.objects.filter(organization=target_org).update(
                followers_count=F("followers_count") + 1
            )
            # org follow notification
            NotificationService.follow(
                actor_user=actor.user if actor.is_user else None,
                actor_org=actor.organization if actor.is_org else None,
                target_user=None,
                target_org=target_org
            )

        return True, {
            "is_following": True,
            "is_connected": bool(target_user and actor.is_user and is_mutual)
        }

    @staticmethod
    @transaction.atomic
    def unfollow(actor, target_user=None, target_org=None):
        filters = {}

        # actor
        if actor.is_user:
            filters["follower_user"] = actor.user
        else:
            filters["follower_org"] = actor.organization

        # target
        if target_user:
            filters["following_user"] = target_user
        else:
            filters["following_org"] = target_org

        deleted, _ = Follow.objects.filter(**filters).delete()

        if not deleted:
            return False, "Not following"

        # =========================
        # COUNT LOGIC (USER + ORG)
        # =========================

        if actor.is_user:
            UserProfile.objects.filter(user=actor.user).update(
                following_count=F("following_count") - 1
            )
        elif actor.is_org:
            OrganizationProfile.objects.filter(organization=actor.organization).update(
                following_count=F("following_count") - 1
            )

        if target_user:
            UserProfile.objects.filter(user=target_user).update(
                followers_count=F("followers_count") - 1
            )

            # remove connection if existed
            if actor.is_user:
                is_mutual = Follow.objects.filter(
                    follower_user=target_user,
                    following_user=actor.user
                ).exists()

                if is_mutual:
                    UserProfile.objects.filter(user=actor.user).update(
                        connections_count=F("connections_count") - 1
                    )
                    UserProfile.objects.filter(user=target_user).update(
                        connections_count=F("connections_count") - 1
                    )
                    
        elif target_org:
            OrganizationProfile.objects.filter(organization=target_org).update(
                followers_count=F("followers_count") - 1
            )

        return True, {
            "is_following": False,
            "is_connected": False
        }
    

    @staticmethod
    def get_relationship(actor, target_id, target_type):
        """
        Returns relationship between actor and target (user/org)

        ``actor`` is None for an anonymous caller. Nobody is following anybody
        in that case, so return the all-false shape rather than querying: the
        client renders the same "Follow" affordance it would for a stranger,
        and tapping it hits the login wall.
        """
        if actor is None:
            return FollowService._anonymous_response()

        # ----------------------------------
        # SELF CHECK
        # ----------------------------------
        if actor.is_user and target_type == TYPE_USER:
            if actor.user.id == target_id:
                return FollowService._self_response()

        if actor.is_org and target_type == TYPE_ORGANIZATION:
            if actor.organization.id == target_id:
                return FollowService._self_response()

        # ----------------------------------
        # BUILD QUERY
        # ----------------------------------
        filters = Q()

        # actor → target
        if actor.is_user and target_type == TYPE_USER:
            filters |= Q(follower_user=actor.user, following_user_id=target_id)

        elif actor.is_user and target_type == TYPE_ORGANIZATION:
            filters |= Q(follower_user=actor.user, following_org_id=target_id)

        elif actor.is_org and target_type == TYPE_USER:
            filters |= Q(follower_org=actor.organization, following_user_id=target_id)

        elif actor.is_org and target_type == TYPE_ORGANIZATION:
            filters |= Q(follower_org=actor.organization, following_org_id=target_id)

        # target → actor
        if actor.is_user and target_type == TYPE_USER:
            filters |= Q(follower_user_id=target_id, following_user=actor.user)

        elif actor.is_user and target_type == TYPE_ORGANIZATION:
            filters |= Q(follower_org_id=target_id, following_user=actor.user)

        elif actor.is_org and target_type == TYPE_USER:
            filters |= Q(follower_user_id=target_id, following_org=actor.organization)

        elif actor.is_org and target_type == TYPE_ORGANIZATION:
            filters |= Q(follower_org_id=target_id, following_org=actor.organization)

        relations = Follow.objects.filter(filters).values(
            "follower_user_id",
            "follower_org_id"
        )

        is_following = False
        is_followed_by = False

        for rel in relations:
            if actor.is_user and rel["follower_user_id"] == actor.user.id:
                is_following = True
            elif actor.is_org and rel["follower_org_id"] == actor.organization.id:
                is_following = True
            else:
                is_followed_by = True

        return {
            "is_me": False,
            "is_following": is_following,
            "is_followed_by": is_followed_by,
            "is_connected": is_following and is_followed_by,
            **FollowService._block_state(actor, target_id, target_type),
        }

    @staticmethod
    def _block_state(actor, target_id, target_type):
        """
        The block half of the relationship, so the profile buttons can switch
        to the blocked state without a second round trip.

        ``is_blocked`` is read off the CACHED symmetric set — free on the hot
        path. ``is_blocked_by_me`` costs one directed query, and is only ever
        asked when the symmetric answer is already true, which is rare. The
        client needs both: symmetric decides "hide Follow/Message", directed
        decides "offer Unblock" versus "offer nothing".
        """
        blocked = BlockService.blocked_ids(actor)

        ids = (
            blocked["user_ids"] if target_type == TYPE_USER
            else blocked["org_ids"]
        )

        # blocked_ids holds UUIDs; callers pass UUIDs or strings.
        is_blocked = any(str(i) == str(target_id) for i in ids)

        if not is_blocked:
            return {"is_blocked": False, "is_blocked_by_me": False}

        if target_type == TYPE_USER:
            directed = BlockService.is_blocked_between_directed(
                actor, target_user=User(id=target_id)
            )
        else:
            directed = BlockService.is_blocked_between_directed(
                actor, target_org=Organization(id=target_id)
            )

        return {"is_blocked": True, "is_blocked_by_me": directed}

    @staticmethod
    def _self_response():
        return {
            "is_me": True,
            "is_following": False,
            "is_followed_by": False,
            "is_connected": False,
            "is_blocked": False,
            "is_blocked_by_me": False,
        }

    @staticmethod
    def _anonymous_response():
        """Same shape as a stranger's — `is_me` False, every edge absent."""
        return {
            "is_me": False,
            "is_following": False,
            "is_followed_by": False,
            "is_connected": False,
            "is_blocked": False,
            "is_blocked_by_me": False,
        }


    # CHECK IS MUTUAL FOLLOW
    @staticmethod
    def is_mutual_follow(actor_user, actor_org, target_user, target_org):

        # USER ↔ USER
        if actor_user and target_user:
            return (
                Follow.objects.filter(
                    follower_user=actor_user,
                    following_user=target_user
                ).exists()
                and
                Follow.objects.filter(
                    follower_user=target_user,
                    following_user=actor_user
                ).exists()
            )

        # USER ↔ ORG
        if actor_user and target_org:
            return Follow.objects.filter(
                follower_user=actor_user,
                following_org=target_org
            ).exists()

        if actor_org and target_user:
            return Follow.objects.filter(
                follower_org=actor_org,
                following_user=target_user
            ).exists()


        # ORG ↔ ORG
        if actor_org and target_org:
            return (
                Follow.objects.filter(
                    follower_org=actor_org,
                    following_org=target_org
                ).exists()
                and
                Follow.objects.filter(
                    follower_org=target_org,
                    following_org=actor_org
                ).exists()
            )

        return False