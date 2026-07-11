import logging
from django.db import transaction
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from utils.response import response_data
from rest_framework.permissions import IsAuthenticated
from connections.models import Follow
from accounts.models import User
from organization.models import Organization
from core.constant import TYPE_USER, TYPE_ORGANIZATION
from core.views.base_views import BaseAPIView
from connections.services.follow_services import FollowService
from organization.services.user_organization_services import (
    UserOrganizationService,
)

logger = logging.getLogger(__name__)


class FollowAPIView(BaseAPIView):

    def post(self, request):
        try:
            actor = request.actor
            target_type = request.data.get("target_type")
            target_id = request.data.get("target_id")

            if target_type not in [TYPE_USER, TYPE_ORGANIZATION] or not target_id:
                return response_data(False, "Invalid input", status_code=400)

            target_user = None
            target_org = None

            if target_type == TYPE_USER:
                target_user = get_object_or_404(User, id=target_id)
            else:
                target_org = get_object_or_404(Organization, id=target_id)

            success, result = FollowService.follow(
                actor=actor,
                target_user=target_user,
                target_org=target_org
            )

            return response_data(
                success,
                result if isinstance(result, str) else "Followed successfully",
                data=result if isinstance(result, dict) else {"is_following": False}
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

            target_user = None
            target_org = None

            if target_type == TYPE_USER:
                target_user = get_object_or_404(User, id=target_id)
            else:
                target_org = get_object_or_404(Organization, id=target_id)

            success, result = FollowService.unfollow(
                actor=actor,
                target_user=target_user,
                target_org=target_org
            )

            return response_data(
                success,
                result if isinstance(result, str) else "Unfollowed successfully",
                data=result if isinstance(result, dict) else {"is_following": False}
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
    LIST_TYPE_CONNECTIONS = 'connections'

    VALID_TYPES = [
        LIST_TYPE_FOLLOWING,
        LIST_TYPE_FOLLOWERS,
        LIST_TYPE_CONNECTIONS,
    ]

    def get(self, request):
        try:
            actor = request.actor

            list_type = request.query_params.get("type")
            username = request.query_params.get("username", "").strip()
            search = request.query_params.get("search", "").strip()

            # Lenient param parsing — junk falls back to the defaults.
            try:
                limit = int(request.query_params.get("limit", 20))
            except (TypeError, ValueError):
                limit = 20
            try:
                offset = int(request.query_params.get("offset", 0))
            except (TypeError, ValueError):
                offset = 0

            limit = min(max(limit, 0), 50)
            offset = max(offset, 0)

            if list_type not in self.VALID_TYPES:
                return response_data(False, "Invalid type", status_code=400)

            # RESOLVE TARGET — the profile whose lists we return. Defaults to
            # the acting actor when no `username` is supplied (backward compat).
            if username:
                try:
                    profile = (
                        UserOrganizationService
                        .get_user_or_org_by_username(username)
                    )
                except ValueError:
                    return response_data(
                        False, "Profile not found", status_code=404
                    )

                if profile["type"] == TYPE_USER:
                    target_user = (
                        User.objects.select_related("profile")
                        .filter(id=profile["id"]).first()
                    )
                    target_org = None
                else:
                    target_user = None
                    target_org = (
                        Organization.objects.select_related("profile")
                        .filter(id=profile["id"]).first()
                    )

                if not target_user and not target_org:
                    return response_data(
                        False, "Profile not found", status_code=404
                    )
            else:
                target_user = actor.user if actor.is_user else None
                target_org = actor.organization if actor.is_org else None

            # CONNECTIONS is a user↔user notion only.
            if (
                list_type == self.LIST_TYPE_CONNECTIONS
                and not target_user
            ):
                return response_data(
                    False,
                    "Connections are only available for user profiles",
                    status_code=400,
                )

            # BASE QUERY — `user_field`/`org_field` name the Follow columns that
            # hold the row's ENTITY (the thing we serialise) for this list type.
            if list_type == self.LIST_TYPE_FOLLOWING:
                if target_user:
                    queryset = Follow.objects.filter(follower_user=target_user)
                else:
                    queryset = Follow.objects.filter(follower_org=target_org)

                queryset = queryset.select_related(
                    "following_user__profile",
                    "following_org__profile",
                )
                user_field, org_field = "following_user", "following_org"

            elif list_type == self.LIST_TYPE_FOLLOWERS:
                if target_user:
                    queryset = Follow.objects.filter(following_user=target_user)
                else:
                    queryset = Follow.objects.filter(following_org=target_org)

                queryset = queryset.select_related(
                    "follower_user__profile",
                    "follower_org__profile",
                )
                user_field, org_field = "follower_user", "follower_org"

            else:  # CONNECTIONS — mutual user↔user follows.
                # Users who follow the target …
                followers_of_target = Follow.objects.filter(
                    following_user=target_user,
                    follower_user__isnull=False,
                ).values_list("follower_user_id", flat=True)

                # … intersected with the users the target follows. The row is
                # the outgoing (target → U) follow, so ordering + entity match
                # the `following` branch. Single subquery — no per-row queries.
                queryset = Follow.objects.filter(
                    follower_user=target_user,
                    following_user__in=followers_of_target,
                ).select_related("following_user__profile")
                user_field, org_field = "following_user", None

            # SEARCH — username / profile name (users) and org name (orgs).
            if search:
                search_q = (
                    Q(**{f"{user_field}__username__icontains": search})
                    | Q(**{f"{user_field}__profile__name__icontains": search})
                )
                if org_field:
                    search_q |= Q(**{f"{org_field}__name__icontains": search})
                queryset = queryset.filter(search_q)

            total_count = queryset.count()

            queryset = queryset.order_by("-created_at")[offset: offset + limit]

            # Collect the page's entities + their ids for the is_following batch.
            rows = []
            user_ids = []
            org_ids = []

            for obj in queryset:
                entity_user = getattr(obj, user_field)
                entity_org = getattr(obj, org_field) if org_field else None

                rows.append((entity_user, entity_org))

                if entity_user:
                    user_ids.append(entity_user.id)
                if entity_org:
                    org_ids.append(entity_org.id)

            # BATCH is_following — does the CURRENT actor follow each entity?
            # One query per entity kind, for every list type.
            following_users_set = set()
            following_orgs_set = set()

            if actor.is_user:
                if user_ids:
                    following_users_set = set(
                        Follow.objects.filter(
                            follower_user=actor.user,
                            following_user__in=user_ids,
                        ).values_list("following_user_id", flat=True)
                    )
                if org_ids:
                    following_orgs_set = set(
                        Follow.objects.filter(
                            follower_user=actor.user,
                            following_org__in=org_ids,
                        ).values_list("following_org_id", flat=True)
                    )
            else:
                if user_ids:
                    following_users_set = set(
                        Follow.objects.filter(
                            follower_org=actor.organization,
                            following_user__in=user_ids,
                        ).values_list("following_user_id", flat=True)
                    )
                if org_ids:
                    following_orgs_set = set(
                        Follow.objects.filter(
                            follower_org=actor.organization,
                            following_org__in=org_ids,
                        ).values_list("following_org_id", flat=True)
                    )

            actor_user_id = actor.user.id if actor.is_user else None
            actor_org_id = actor.organization.id if actor.is_org else None

            # SERIALIZE — one unified row shape for every entity kind.
            results = []

            for entity_user, entity_org in rows:
                if entity_user:
                    profile = getattr(entity_user, "profile", None)
                    results.append({
                        "type": "user",
                        "id": entity_user.id,
                        "username": entity_user.username,
                        "name": getattr(profile, "name", "") or "",
                        "avatar": getattr(profile, "profile_photo", "") or "",
                        "headline": getattr(profile, "headline", "") or "",
                        "is_verified": False,
                        "is_following": entity_user.id in following_users_set,
                        "is_me": entity_user.id == actor_user_id,
                    })
                elif entity_org:
                    profile = getattr(entity_org, "profile", None)
                    results.append({
                        "type": "organization",
                        "id": entity_org.id,
                        "username": entity_org.username,
                        "name": entity_org.name or "",
                        "avatar": getattr(profile, "logo", "") or "",
                        "headline": getattr(profile, "headline", "") or "",
                        "is_verified": entity_org.is_verified,
                        "is_following": entity_org.id in following_orgs_set,
                        "is_me": entity_org.id == actor_org_id,
                    })

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