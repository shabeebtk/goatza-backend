import logging
from django.db import transaction
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from utils.response import response_data
from utils.emails import send_email
from django.core.exceptions import ValidationError
from sports.models import (
    Sport, UserSport, SportPosition, UserAttributeValue, UserSportPosition, 
    SportAttribute, SportAttributeOption
)
from sports.serializers.user_sports_serializers import (
    UserSportFullSerializer, UserSportMiniSerializer
)

logger = logging.getLogger(__name__)

class UserSportListAPIView(APIView):
    permission_classes = [IsAuthenticated]

    LIST_TYPE_All = "all"

    def get(self, request):
        try:
            user = request.user
            list_type = request.query_params.get("list_type")

            queryset = UserSport.objects.filter(user=user).select_related("sport")

            if list_type == self.LIST_TYPE_All:
                queryset = queryset.prefetch_related(
                    "sport",
                    "user__positions__position",
                    "user__attributes__attribute",
                    "user__attributes__option",
                )
                serializer = UserSportFullSerializer(queryset, many=True)
            else:
                serializer = UserSportMiniSerializer(queryset, many=True)

            return response_data(
                True,
                "User sports fetched",
                serializer.data
            )

        except Exception as e:
            logger.exception("UserSportListAPIView error")
            return response_data(False, "Failed to fetch user sports", error=str(e), status_code=500)
        


class UserSportCreateAPIView(APIView):
    '''
    expected request format
    {
        "sport_id": "uuid",
        "is_primary": true,
        "experience_level": "advanced",
        "positions": [
            {"position_id": "uuid", "is_primary": true}
        ],
        "attributes": [
            {
            "attribute_id": "uuid",
            "option_id": "uuid"
            },
            {
            "attribute_id": "uuid",
            "value_text": "180"
            }
        ]
        }
    '''
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        try:
            user = request.user
            data = request.data

            sport_id = data.get("sport_id")

            if not sport_id:
                return response_data(False, "sport_id is required", status_code=400)

            sport = Sport.objects.get(id=sport_id)

            # UserSport
            user_sport, _ = UserSport.objects.get_or_create(
                user=user,
                sport=sport,
                defaults={
                    "is_primary": data.get("is_primary", False),
                    "experience_level": data.get("experience_level"),
                }
            )

            # update if exists
            user_sport.is_primary = data.get("is_primary", user_sport.is_primary)
            user_sport.experience_level = data.get("experience_level", user_sport.experience_level)
            user_sport.save()

            # Positions
            positions = data.get("positions", [])

            for pos in positions:
                position_obj = SportPosition.objects.get(id=pos["position_id"])

                UserSportPosition.objects.get_or_create(
                    user=user,
                    sport=sport,
                    position=position_obj,
                    defaults={
                        "is_primary": pos.get("is_primary", False)
                    }
                )

            # Attributes
            attributes = data.get("attributes", [])

            for attr in attributes:
                attribute_obj = SportAttribute.objects.get(id=attr["attribute_id"])

                option = None
                if attr.get("option_id"):
                    option = SportAttributeOption.objects.get(id=attr["option_id"])

                UserAttributeValue.objects.update_or_create(
                    user=user,
                    sport=sport,
                    attribute=attribute_obj,
                    defaults={
                        "option": option,
                        "value_text": attr.get("value_text")
                    }
                )

            return response_data(True, "User sport profile saved successfully")

        except Sport.DoesNotExist:
            return response_data(False, "Invalid sport_id", status_code=400)

        except ValidationError:
            return response_data(False, "Invalid ID format", status_code=400)

        except Exception as e:
            logger.exception("UserSportCreateAPIView error")
            return response_data(False, "Something went wrong", error=str(e), status_code=500)