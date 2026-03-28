from rest_framework import serializers
from sports.models import Sport, SportAttribute, SportAttributeOption, SportPosition


class SportSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sport
        fields = [
            "id",
            "name",
            "icon_name",
            "icon_url",
        ]

class SportPositionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SportPosition
        fields = ["id", "name"]


class SportAttributeOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SportAttributeOption
        fields = ["id", "value"]


class SportAttributeSerializer(serializers.ModelSerializer):
    options = SportAttributeOptionSerializer(many=True, read_only=True)

    class Meta:
        model = SportAttribute
        fields = [
            "id",
            "name",
            "data_type",
            "is_required",
            "display_order",
            "options",
        ]


class SportFullDetailsSerializer(serializers.ModelSerializer):
    attributes = SportAttributeSerializer(many=True, read_only=True)
    positions = SportPositionSerializer(many=True)

    class Meta:
        model = Sport
        fields = [
            "id",
            "name",
            "icon_name",
            "icon_url",
            "positions",
            "attributes",
        ]

