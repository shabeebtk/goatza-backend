import logging
from django.db import IntegrityError, transaction
from rest_framework.views import APIView
from django.db.models import Prefetch
from rest_framework.permissions import AllowAny, IsAuthenticated
from utils.response import response_data
from utils.emails import send_email
from sports.models import Sport, SportAttribute, SportAttributeOption, SportPosition
from sports.serializers.sports_serializers import SportSerializer, SportFullDetailsSerializer

logger = logging.getLogger(__name__)


class SportListAPIView(APIView):
    permission_classes = [AllowAny]
    LIST_TYPE_ALL = 'all'

    def get(self, request):
        try:
            user = None
            if request.user.is_authenticated:
                user = request.user

            sport_id = request.query_params.get("sport_id")
            name = request.query_params.get("name")
            list_type = request.query_params.get('list_type')
            exclude_user_sports = request.query_params.get('exclude_user_sports', False) == 'true'


            if exclude_user_sports and user:
                queryset = Sport.objects.exclude(users__id=user.id)
            else:
                queryset = Sport.objects.all()


            # Filters
            if sport_id:
                queryset = queryset.filter(id=sport_id)

            if name:
                queryset = queryset.filter(name__icontains=name)

            # ⚡ Optimized prefetch
            if list_type == self.LIST_TYPE_ALL:
                queryset = queryset.prefetch_related(
                    Prefetch(
                        "attributes",
                        queryset=SportAttribute.objects.all().prefetch_related(
                            Prefetch(
                                "options",
                                queryset=SportAttributeOption.objects.all()
                            )
                        )
                    )
                )
                serializer = SportFullDetailsSerializer(queryset, many=True)

            else:
                serializer = SportSerializer(queryset, many=True)
                

            return response_data(
                success=True,
                message="Sports fetched successfully",
                data=serializer.data
            )

        except Exception as e:
            logger.exception("Error fetching sports")

            return response_data(
                success=False,
                message="Something went wrong",
                error=str(e),
                status_code=500
            )