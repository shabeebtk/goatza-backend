

from django.contrib import admin
from sports.models import (
    Sport,
    SportAttribute,
    SportAttributeOption,
    UserSport,
    SportPosition,
    UserSportPosition,
    UserAttributeValue,
)


# ================================
# 🔹 INLINE CONFIGS
# ================================

class SportAttributeOptionInline(admin.TabularInline):
    model = SportAttributeOption
    extra = 1


class SportAttributeInline(admin.TabularInline):
    model = SportAttribute
    extra = 1


class UserSportPositionInline(admin.TabularInline):
    model = UserSportPosition
    extra = 1


class UserAttributeValueInline(admin.TabularInline):
    model = UserAttributeValue
    extra = 1


# ================================
# 🏅 SPORT ADMIN
# ================================

@admin.register(Sport)
class SportAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "icon_name", "icon_url", "created_at")
    search_fields = ("name",)
    ordering = ("name",)

    # show attributes inline inside sport
    inlines = [SportAttributeInline]


# ================================
# ⚙️ SPORT ATTRIBUTE ADMIN
# ================================

@admin.register(SportAttribute)
class SportAttributeAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "sport", "data_type", "is_required", "display_order")
    list_filter = ("sport", "data_type", "is_required")
    search_fields = ("name", "sport__name")
    ordering = ("sport", "display_order")

    inlines = [SportAttributeOptionInline]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("sport")


# ================================
# ⚙️ ATTRIBUTE OPTION ADMIN
# ================================

@admin.register(SportAttributeOption)
class SportAttributeOptionAdmin(admin.ModelAdmin):
    list_display = ("id", "value", "attribute", "get_sport")
    search_fields = ("value", "attribute__name")
    list_filter = ("attribute__sport",)

    def get_sport(self, obj):
        return obj.attribute.sport.name
    get_sport.short_description = "Sport"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("attribute__sport")


# ================================
# 👤 USER SPORT ADMIN
# ================================

@admin.register(UserSport)
class UserSportAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "sport", "is_primary", "experience_level", "created_at")
    list_filter = ("sport", "is_primary", "experience_level")
    search_fields = ("user__email", "sport__name")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user", "sport")


# ================================
# 🎯 SPORT POSITION ADMIN
# ================================

@admin.register(SportPosition)
class SportPositionAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "sport")
    search_fields = ("name", "sport__name")
    list_filter = ("sport",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("sport")


# ================================
# 👤 USER POSITION ADMIN
# ================================

@admin.register(UserSportPosition)
class UserSportPositionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "sport", "position", "is_primary")
    list_filter = ("sport", "is_primary")
    search_fields = ("user__email", "position__name")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user", "sport", "position")


# ================================
# 👤 USER ATTRIBUTE VALUE ADMIN
# ================================

@admin.register(UserAttributeValue)
class UserAttributeValueAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "sport", "attribute", "option", "value_text")
    list_filter = ("sport", "attribute")
    search_fields = ("user__email", "attribute__name")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "user", "sport", "attribute", "option"
        )