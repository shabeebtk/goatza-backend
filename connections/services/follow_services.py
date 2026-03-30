from connections.models import Follow


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