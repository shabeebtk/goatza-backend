import logging
from django.db import transaction
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from utils.response import response_data
from rest_framework.permissions import IsAuthenticated
from connections.models import Follow
from accounts.models import User
from accounts.serializers.user_serializers import UserMiniSerializer
from organization.serializers.organization_serializers import OrganizationMiniSerializer
from organization.models import Organization
from core.constant import TYPE_USER, TYPE_ORGANIZATION
from core.views.base_views import BaseAPIView

logger = logging.getLogger(__name__)

class FollowAPIView(BaseAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            print(request.user)
            actor = request.actor  # from middleware
            target_type = request.data.get("target_type")
            target_id = request.data.get("target_id")

            if target_type not in [TYPE_USER, TYPE_ORGANIZATION] or not target_id:
                return response_data(False, "Invalid input", status_code=400)

            follow_data = {}

            # Set follower
            if actor.is_user:
                follow_data["follower_user"] = actor.user
            else:
                follow_data["follower_org"] = actor.organization

            # Set target
            if target_type == "user":
                target_user = get_object_or_404(User, id=target_id)

                # Prevent self-follow
                if actor.is_user and actor.user.id == target_user.id:
                    return response_data(False, "Cannot follow yourself", status_code=400)

                follow_data["following_user"] = target_user

            else:
                target_org = get_object_or_404(Organization, id=target_id)

                if actor.is_org and actor.organization.id == target_org.id:
                    return response_data(False, "Cannot follow your own organization", status_code=400)

                follow_data["following_org"] = target_org

            with transaction.atomic():
                follow, created = Follow.objects.get_or_create(**follow_data)

            return response_data(
                True,
                "Followed successfully" if created else "Already following",
                data={"is_following": True}
            )

        except Exception as e:
            logger.error(f"Follow error: {str(e)}")
            return response_data(False, "Something went wrong", status_code=500, error=str(e))
        

class UnfollowAPIView(BaseAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            actor = request.actor

            target_type = request.data.get("target_type")
            target_id = request.data.get("target_id")

            if target_type not in [TYPE_USER, TYPE_ORGANIZATION] or not target_id:
                return response_data(False, "Invalid input", status_code=400)

            filters = {}

            # follower
            if actor.is_user:
                filters["follower_user"] = actor.user
            else:
                filters["follower_org"] = actor.organization

            # target
            if target_type == "user":
                filters["following_user_id"] = target_id
            else:
                filters["following_org_id"] = target_id

            deleted, _ = Follow.objects.filter(**filters).delete()

            return response_data(
                True,
                "Unfollowed successfully" if deleted else "Not following",
                data={"is_following": False}
            )

        except Exception as e:
            logger.error(f"Unfollow error: {str(e)}")
            return response_data(False, "Something went wrong", status_code=500, error=str(e))
        

class CheckFollowStatusAPIView(BaseAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            actor = request.actor

            target_type = request.query_params.get("target_type")
            target_id = request.query_params.get("target_id")

            print(target_id, target_type)
            

            if target_type not in [TYPE_USER, TYPE_ORGANIZATION] or not target_id:
                return response_data(
                    success=False,
                    message="target_type and target_id are required",
                    status_code=400
                )

            filters = {}

            # 🔥 Actor (who is checking)
            if actor.is_user:
                filters["follower_user"] = actor.user
            else:
                filters["follower_org"] = actor.organization

            # 🔥 Target (whom we check)
            if target_type == "user":
                filters["following_user_id"] = target_id
            else:
                filters["following_org_id"] = target_id

            is_following = Follow.objects.filter(**filters).exists()

            return response_data(
                success=True,
                data={"is_following": is_following}
            )

        except Exception as e:
            logger.error(f"check follow status error (actor={request.user.id}): {str(e)}")

            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )

class FollowListAPIView(BaseAPIView):
    permission_classes = [IsAuthenticated]

    LIST_TYPE_FOLLOWING = 'following'
    LIST_TYPE_FOLLOWERS = 'followers'

    def get(self, request):
        try:
            actor = request.actor

            list_type = request.query_params.get("type")
            search = request.query_params.get("search", "").strip()
            limit = int(request.query_params.get("limit", 20))
            offset = int(request.query_params.get("offset", 0))

            limit = min(limit, 50)
            offset = max(offset, 0)

            if list_type not in [self.LIST_TYPE_FOLLOWING, self.LIST_TYPE_FOLLOWERS]:
                return response_data(False, "Invalid type", status_code=400)

            # BASE QUERY
            if list_type == self.LIST_TYPE_FOLLOWING:
                if actor.is_user:
                    queryset = Follow.objects.filter(follower_user=actor.user)
                else:
                    queryset = Follow.objects.filter(follower_org=actor.organization)

                queryset = queryset.select_related(
                    "following_user__profile",
                    "following_org"
                )

            else:
                if actor.is_user:
                    queryset = Follow.objects.filter(following_user=actor.user)
                else:
                    queryset = Follow.objects.filter(following_org=actor.organization)

                queryset = queryset.select_related(
                    "follower_user__profile",
                    "follower_org"
                )

            # SEARCH
            if search:
                if list_type == self.LIST_TYPE_FOLLOWING:
                    queryset = queryset.filter(
                        Q(following_user__username__icontains=search) |
                        Q(following_user__profile__name__icontains=search) |
                        Q(following_org__name__icontains=search)
                    )
                else:
                    queryset = queryset.filter(
                        Q(follower_user__username__icontains=search) |
                        Q(follower_user__profile__name__icontains=search) |
                        Q(follower_org__name__icontains=search)
                    )

            total_count = queryset.count()

            queryset = queryset.order_by("-created_at")[offset: offset + limit]

            # Collect IDs for is_following
            target_user_ids = []
            target_org_ids = []

            targets = []

            for obj in queryset:
                if list_type == self.LIST_TYPE_FOLLOWING:
                    target_user = obj.following_user
                    target_org = obj.following_org
                else:
                    target_user = obj.follower_user
                    target_org = obj.follower_org

                targets.append((obj, target_user, target_org))

                if target_user:
                    target_user_ids.append(target_user.id)
                if target_org:
                    target_org_ids.append(target_org.id)

            # Batch check (only needed for followers)
            following_users_set = set()
            following_orgs_set = set()

            if list_type == self.LIST_TYPE_FOLLOWERS:
                if actor.is_user:
                    following_users_set = set(
                        Follow.objects.filter(
                            follower_user=actor.user,
                            following_user__in=target_user_ids
                        ).values_list("following_user_id", flat=True)
                    )

                    following_orgs_set = set(
                        Follow.objects.filter(
                            follower_user=actor.user,
                            following_org__in=target_org_ids
                        ).values_list("following_org_id", flat=True)
                    )

                else:
                    following_users_set = set(
                        Follow.objects.filter(
                            follower_org=actor.organization,
                            following_user__in=target_user_ids
                        ).values_list("following_user_id", flat=True)
                    )

                    following_orgs_set = set(
                        Follow.objects.filter(
                            follower_org=actor.organization,
                            following_org__in=target_org_ids
                        ).values_list("following_org_id", flat=True)
                    )

            # SERIALIZE
            results = []

            for obj, target_user, target_org in targets:

                if target_user:
                    data = UserMiniSerializer(target_user).data
                    data["type"] = "user"

                    if list_type == self.LIST_TYPE_FOLLOWING:
                        data["is_following"] = True
                    else:
                        data["is_following"] = target_user.id in following_users_set

                else:
                    data = OrganizationMiniSerializer(target_org).data
                    data["type"] = "organization"

                    if list_type == self.LIST_TYPE_FOLLOWING:
                        data["is_following"] = True
                    else:
                        data["is_following"] = target_org.id in following_orgs_set

                data["followed_at"] = obj.created_at

                results.append(data)

            return response_data(
                success=True,
                data={
                    "count": total_count,
                    "limit": limit,
                    "offset": offset,
                    "results": results
                }
            )

        except Exception as e:
            logger.error(f"Follow list error (actor={request.user.id}): {str(e)}")

            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )