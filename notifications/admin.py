from django.contrib import admin
from notifications.models import Notification, UserFCMToken

# Register your models here.


admin.site.register(Notification)
admin.site.register(UserFCMToken)
