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