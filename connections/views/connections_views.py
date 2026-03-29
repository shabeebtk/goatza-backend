import logging
from django.db import transaction
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from utils.response import response_data
from rest_framework.permissions import IsAuthenticated
from connections.models import Follow
from accounts.models import User
from accounts.serializers.user_serializers import UserMiniSerializer

logger = logging

class FollowUserAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            follower = request.user
            user_id = request.data.get("user_id")

            if not user_id:
                return response_data(
                    success=False,
                    message="user_id is required",
                    status_code=400
                )

            target_user = get_object_or_404(User, id=user_id)

            if follower.id == target_user.id:
                return response_data(
                    success=False,
                    message="You cannot follow yourself",
                    status_code=400
                )

            with transaction.atomic():
                follow, created = Follow.objects.get_or_create(
                    follower=follower,
                    following_user=target_user
                )

            if created:
                logger.info(f"{follower.id} followed user {target_user.id}")
                return response_data(
                    success=True,
                    message="Followed successfully",
                    data={"is_following": True}
                )
            else:
                return response_data(
                    success=True,
                    message="Already following",
                    data={"is_following": True}
                )

        except ValidationError as e:
            logger.error(f"validation error: {str(e)}")
            return response_data(
                success=False,
                message="invalid uuid data",
                status_code=400,
                error=str(e)
            )
        except Exception as e:
            logger.error(f"Follow user error: {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=400,
                error=str(e)
            )

class UnfollowUserAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            follower = request.user
            user_id = request.data.get("user_id")

            if not user_id:
                return response_data(
                    success=False,
                    message="user_id is required",
                    status_code=400
                )

            target_user = get_object_or_404(User, id=user_id)

            deleted, _ = Follow.objects.filter(
                follower=follower,
                following_user=target_user
            ).delete()

            if deleted:
                logger.info(f"{follower.id} unfollowed user {target_user.id}")
                return response_data(
                    success=True,
                    message="Unfollowed successfully",
                    data={"is_following": False}
                )
            else:
                return response_data(
                    success=True,
                    message="You were not following this user",
                    data={"is_following": False}
                )
            
        except ValidationError as e:
            logger.error(f"validation error: {str(e)}")
            return response_data(
                success=False,
                message="invalid uuid data",
                status_code=400,
                error=str(e)
            )

        except Exception as e:
            logger.error(f"Unfollow user error: {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )
        


class CheckFollowStatusAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            user_id = request.query_params.get("user_id")
            if not user_id:
                return response_data(
                    success=False,
                    message="user_id is required",
                    status_code=400
                )

            is_following = Follow.objects.filter(
                follower=request.user,
                following_user_id=user_id
            ).exists()

            return response_data(
                success=True,
                data={"is_following": is_following}
            )
        except ValidationError as e:
            logger.error(f"validation error: {str(e)}")
            return response_data(
                success=False,
                message="invalid uuid data",
                status_code=400,
                error=str(e)
            )
        
        except Exception as e:
            logger.error(f"check follow status user error: {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )
    

class FollowListAPIView(APIView):
    permission_classes = [IsAuthenticated]

    LIST_TYPE_FOLLOWING = 'following'
    LIST_TYPE_FOLLOWERS = 'followers'

    def get(self, request):
        try:
            user = request.user

            list_type = request.query_params.get("type")
            search = request.query_params.get("search", "").strip()
            limit = int(request.query_params.get("limit", 20))
            offset = int(request.query_params.get("offset", 0))

            limit = min(limit, 50)
            offset = max(offset, 0)

            if list_type not in [self.LIST_TYPE_FOLLOWING, self.LIST_TYPE_FOLLOWERS]:
                return response_data(
                    success=False,
                    message="Invalid type. Use 'followers' or 'following'",
                    status_code=400
                )

            # Base queryset
            if list_type == self.LIST_TYPE_FOLLOWING:
                queryset = Follow.objects.filter(
                    follower=user,
                    following_user__isnull=False
                ).select_related("following_user__profile")

            else:
                queryset = Follow.objects.filter(
                    following_user=user
                ).select_related("follower__profile")

            # Search
            if search:
                if list_type == self.LIST_TYPE_FOLLOWING:
                    queryset = queryset.filter(
                        Q(following_user__username__icontains=search) |
                        Q(following_user__profile__name__icontains=search)
                    )
                else:
                    queryset = queryset.filter(
                        Q(follower__username__icontains=search) |
                        Q(follower__profile__name__icontains=search)
                    )

            total_count = queryset.count()

            queryset = queryset.order_by("-created_at")[offset: offset + limit]

            # Prepare users
            if list_type == self.LIST_TYPE_FOLLOWING:
                target_users = [obj.following_user for obj in queryset]
                # Always true (requested user follow them)
                following_map = None

            else:
                target_users = [obj.follower for obj in queryset]
                target_user_ids = [u.id for u in target_users]

                # Users requested user follow back
                following_map = set(
                    Follow.objects.filter(
                        follower=user,
                        following_user__in=target_user_ids
                    ).values_list("following_user_id", flat=True)
                )

            # Serialize
            results = []

            for obj, target_user in zip(queryset, target_users):
                user_data = UserMiniSerializer(target_user).data
                user_data["followed_at"] = obj.created_at

                if list_type == self.LIST_TYPE_FOLLOWING:
                    user_data["is_following"] = True
                else:
                    user_data["is_following"] = target_user.id in following_map

                results.append(user_data)

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
            logger.error(f"Follow list error (user={request.user.id}): {str(e)}")

            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )