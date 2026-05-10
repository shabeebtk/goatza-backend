from django.db.models import Q
from recruitments.models import Recruitment
from organization.services.user_organization_services import (
    UserOrganizationService
)
from core.constant import TYPE_ORGANIZATION
from connections.services.follow_services import FollowService
from connections.models import Follow

class RecruitmentSelector:

    @staticmethod
    def list_recruitments(
        actor,
        username=None,
        sport_id=None,
        recruitment_type=None,
        status=None,
        city=None,
        limit=10,
        offset=0
    ):

        queryset = Recruitment.objects.filter(
            is_deleted=False
        )

        target_org = None

        # PROFILE FILTER
        if username:
            profile = UserOrganizationService.get_user_or_org_by_username(
                username
            )

            # recruitments only for org
            if profile["type"] != TYPE_ORGANIZATION:
                return Recruitment.objects.none(), 0

            target_org = profile["id"]

            queryset = queryset.filter(
                organization_id=target_org
            )

        # VISIBILITY RULES
        visibility_filter = Q()

        # OWNER ACCESS
        is_owner = (
            actor.is_org
            and target_org
            and str(actor.organization.id) == str(target_org)
        )
        if is_owner:
            visibility_filter |= Q()
        else:
            visibility_filter |= Q(
                status=Recruitment.Status.ACTIVE,
                visibility=Recruitment.Visibility.PUBLIC
            )

            # followers-only
            if target_org:
                following_ids = FollowService.get_following_ids(actor)
                if target_org in following_ids["org_ids"]:
                    visibility_filter |= Q(
                        status=Recruitment.Status.ACTIVE,
                        visibility=Recruitment.Visibility.FOLLOWERS_ONLY
                    )

        queryset = queryset.filter(visibility_filter)

        # FILTERS
        if sport_id:
            queryset = queryset.filter(sport_id=sport_id)

        if recruitment_type:
            queryset = queryset.filter(
                recruitment_type=recruitment_type
            )

        if status and is_owner:
            queryset = queryset.filter(status=status)

        if city:
            queryset = queryset.filter(city__iexact=city)

        # COUNT
        total_count = queryset.count()

        # OPTIMIZATION
        queryset = queryset.select_related(
            "organization",
            "sport"
        ).prefetch_related(
            "positions__position",
            "media"
        )

        queryset = queryset.order_by(
            "-published_at",
            "-created_at"
        )[offset: offset + limit]

        return queryset, total_count
    

    @staticmethod
    def get_recruitment_detail(
        recruitment_id,
        actor
    ):

        queryset = Recruitment.objects.filter(
            id=recruitment_id,
            is_deleted=False
        )

        queryset = queryset.select_related(
            "organization",
            "sport",
            "created_by_member"
        ).prefetch_related(
            "positions__position",
            "media",
            "questions__options",
            "applications"
        )

        recruitment = queryset.first()

        if not recruitment:
            return None

        # OWNER ACCESS
        is_owner = (
            actor
            and actor.is_org
            and str(actor.organization.id)
            == str(recruitment.organization_id)
        )

        if is_owner:
            return recruitment

        # PUBLIC ACTIVE ONLY
        if (
            recruitment.status == Recruitment.Status.ACTIVE
            and recruitment.visibility
            == Recruitment.Visibility.PUBLIC
        ):
            return recruitment

        # FOLLOWERS ONLY
        if (
            recruitment.status == Recruitment.Status.ACTIVE
            and recruitment.visibility
            == Recruitment.Visibility.FOLLOWERS_ONLY
        ):

            if not actor:
                return None

            follows = Follow.objects.filter(
                follower_user=getattr(actor, "user", None),
                following_org_id=recruitment.organization_id
            ).exists()

            if follows:
                return recruitment

        return None