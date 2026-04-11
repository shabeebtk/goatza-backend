from connections.models import Follow
from accounts.models import User, UserProfile
from django.db import transaction
from django.db.models import F, Q
from notifications.services.notification_service import NotificationService

class FollowService:

    @staticmethod
    def get_following_ids(actor):
        """
        Returns:
        {
            "user_ids": [...],
            "org_ids": [...]
        }
        """

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
        # COUNT LOGIC (USER ONLY)
        # =========================
        if actor.is_user:
            # actor following count
            UserProfile.objects.filter(user=actor.user).update(
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
        # COUNT LOGIC (USER ONLY)
        # =========================

        if actor.is_user:
            UserProfile.objects.filter(user=actor.user).update(
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

        return True, {
            "is_following": False,
            "is_connected": False
        }
    

    @staticmethod
    def get_relationship(viewer, target_user_id):
        """
        Returns relationship between viewer and target user
        """
        is_me = viewer.id == target_user_id
        if is_me:
            return {
                "is_me": True,
                "is_following": False,
                "is_followed_by": False,
                "is_connected": False,
            }

        relations = Follow.objects.filter(
            Q(follower_user=viewer, following_user_id=target_user_id) |
            Q(follower_user_id=target_user_id, following_user=viewer)
        ).values("follower_user_id")

        is_following = False
        is_followed_by = False

        for rel in relations:
            if rel["follower_user_id"] == viewer.id:
                is_following = True
            else:
                is_followed_by = True

        return {
            "is_me": False,
            "is_following": is_following,
            "is_followed_by": is_followed_by,
            "is_connected": is_following and is_followed_by,
        }