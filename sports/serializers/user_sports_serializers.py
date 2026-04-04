from rest_framework import serializers
from sports.models import (
    UserSport, UserAttributeValue, SportAttributeOption, SportAttribute, SportPosition, UserSportPosition
)
from .sports_serializers import SportSerializer 


class UserSportMiniSerializer(serializers.ModelSerializer):
    sport = SportSerializer()

    class Meta:
        model = UserSport
        fields = [
            "id",
            "sport",
            "is_primary",
            "experience_level",
        ]



class UserSportSerializer(serializers.Serializer):
    sport_id = serializers.UUIDField(source="sport.id")
    sport_name = serializers.CharField(source="sport.name")
    icon_name = serializers.CharField(source="sport.icon_name")
    icon_url = serializers.CharField(source="sport.icon_url")

    is_primary = serializers.BooleanField()
    experience_level = serializers.CharField()

    positions = serializers.SerializerMethodField()
    primary_position = serializers.SerializerMethodField()

    def get_positions(self, obj):
        user = obj.user

        positions = user.positions.filter(sport=obj.sport)

        return [
            {
                "position": p.position.name,
                "is_primary": p.is_primary
            }
            for p in positions
        ]

    def get_primary_position(self, obj):
        user = obj.user

        primary = user.positions.filter(
            sport=obj.sport,
            is_primary=True
        ).first()

        return primary.position.name if primary else None
    


class UserAttributeValueSerializer(serializers.ModelSerializer):
    attribute = serializers.CharField(source="attribute.name")
    value = serializers.SerializerMethodField()

    class Meta:
        model = UserAttributeValue
        fields = ["attribute", "value"]

    def get_value(self, obj):
        return obj.option.value if obj.option else obj.value_text


class UserSportPositionSerializer(serializers.ModelSerializer):
    position = serializers.CharField(source="position.name")

    class Meta:
        model = UserSportPosition
        fields = ["position", "is_primary"]


class UserSportFullSerializer(serializers.ModelSerializer):
    sport = SportSerializer()
    positions = serializers.SerializerMethodField()
    attributes = serializers.SerializerMethodField()

    class Meta:
        model = UserSport
        fields = [
            "id",
            "sport",
            "is_primary",
            "experience_level",
            "positions",
            "attributes",
        ]

    def get_positions(self, obj):
        positions = obj.user.positions.filter(sport=obj.sport)
        return UserSportPositionSerializer(positions, many=True).data

    def get_attributes(self, obj):
        attrs = obj.user.attributes.filter(sport=obj.sport)
        return UserAttributeValueSerializer(attrs, many=True).data