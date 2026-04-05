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
from accounts.models import User

logger = logging.getLogger(__name__)

class UserSportListAPIView(APIView):
    permission_classes = [IsAuthenticated]

    LIST_TYPE_All = "all"

    def get(self, request, username):
        try:
            if username == "me":
                target_user = request.user
            else:
                target_user = User.objects.filter(username=username).first()
                if not target_user:
                    return response_data(False, "User not found", status_code=404)

            list_type = request.query_params.get("list_type")

            queryset = UserSport.objects.filter(user=target_user).select_related("sport")

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
        



class UserSportUpsertAPIView(APIView):
    '''
    {
        "sport_id": "uuid",
        "is_primary": true,
        "experience_level": "advanced",
        "positions": [
            {
            "position_id": "uuid",
            "is_primary": true
            },
            {
            "position_id": "uuid-888888888888",
            "is_primary": false
            }
        ],
        "attributes": [
            {
            "attribute_id": "uuid-tyfyfy",
            "option_id": "uuid-dddddddddddd"
            },
            {
            "attribute_id": "uuid-dddddddddddd",
            "value_text": "180"
            }
        ]
    }
    '''

    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        user = request.user
        data = request.data

        logger.info(f"[USER SPORT UPSERT] user={user.id}")

        try:
            sport_id = data.get("sport_id")
            if not sport_id:
                return response_data(False, "sport_id is required", status_code=400)

            sport = Sport.objects.get(id=sport_id)

            # 🔹 UPSERT UserSport
            user_sport, created = UserSport.objects.update_or_create(
                user=user,
                sport=sport,
                defaults={
                    "is_primary": data.get("is_primary", False),
                    "experience_level": data.get("experience_level"),
                }
            )

            # Ensure only one primary sport
            if data.get("is_primary"):
                UserSport.objects.filter(user=user).exclude(id=user_sport.id).update(is_primary=False)

            # =========================
            # POSITIONS (SYNC)
            # =========================
            positions_data = data.get("positions", [])

            # delete old positions for this sport
            UserSportPosition.objects.filter(user=user, sport=sport).delete()

            new_positions = []
            for pos in positions_data:
                position_obj = SportPosition.objects.get(id=pos["position_id"])

                # validate belongs to sport
                if position_obj.sport_id != sport.id:
                    raise ValidationError("Position does not belong to sport")

                new_positions.append(
                    UserSportPosition(
                        user=user,
                        sport=sport,
                        position=position_obj,
                        is_primary=pos.get("is_primary", False),
                    )
                )

            UserSportPosition.objects.bulk_create(new_positions)

            # ensure single primary position per sport
            primary_positions = [p for p in new_positions if p.is_primary]
            if len(primary_positions) > 1:
                raise ValidationError("Only one primary position allowed")

            # =========================
            # 🔹 ATTRIBUTES (SYNC)
            # =========================
            attributes_data = data.get("attributes", [])

            # delete old
            UserAttributeValue.objects.filter(user=user, sport=sport).delete()

            new_attrs = []

            for attr in attributes_data:
                attribute_obj = SportAttribute.objects.get(id=attr["attribute_id"])

                if attribute_obj.sport_id != sport.id:
                    raise ValidationError("Attribute does not belong to sport")

                option = None
                if attr.get("option_id"):
                    option = SportAttributeOption.objects.get(id=attr["option_id"])

                new_attrs.append(
                    UserAttributeValue(
                        user=user,
                        sport=sport,
                        attribute=attribute_obj,
                        option=option,
                        value_text=attr.get("value_text"),
                    )
                )

            UserAttributeValue.objects.bulk_create(new_attrs)

            logger.info(f"[USER SPORT UPSERT] success user={user.id} sport={sport.id}")

            return response_data(
                True,
                "User sport updated successfully",
                data={
                    "sport_id": str(sport.id),
                    "updated": True
                }
            )

        except Sport.DoesNotExist:
            return response_data(False, "Invalid sport_id", status_code=400)

        except ValidationError as e:
            return response_data(False, message=str(e), status_code=400)

        except Exception as e:
            logger.exception("UserSportUpsertAPIView error")
            return response_data(False, message="Something went wrong", error=str(e), status_code=500)


class UserSportDeleteAPIView(APIView):
    '''
    Delete a user's sport profile.
    Expected request: DELETE /user/sport/delete?sport_id=uuid
    or DELETE with JSON body {"sport_id": "uuid"}
    '''
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def delete(self, request):
        user = request.user
        
        sport_id = request.query_params.get("sports_id")
        logger.info(f"[USER SPORT DELETE] user={user.id} sport_id={sport_id}")

        if not sport_id:
            return response_data(False, "sport_id is required", status_code=400)

        try:
            # Check if user has this sport
            user_sport = UserSport.objects.filter(user=user, sport_id=sport_id).first()
            
            if not user_sport:
                return response_data(False, "User sport not found", status_code=404)
            
            # Delete related objects explicitly
            UserSportPosition.objects.filter(user=user, sport_id=sport_id).delete()
            UserAttributeValue.objects.filter(user=user, sport_id=sport_id).delete()
            
            # Delete the UserSport instance
            user_sport.delete()

            logger.info(f"[USER SPORT DELETE] success user={user.id} sport={sport_id}")

            return response_data(True, "User sport deleted successfully")
            
        except ValidationError:
            return response_data(False, "Invalid ID format", status_code=400)
        except Exception as e:
            logger.exception("UserSportDeleteAPIView error")
            return response_data(False, "Something went wrong", error=str(e), status_code=500)