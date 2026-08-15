from django.urls import path

from cv.views.cv_settings_views import CVSettingsAPIView

# base endpoint - user/cv/
#
# NO TRAILING SLASHES — see the comment in highlights/urls.py for the
# Vercel 308 → Django APPEND_SLASH 301 redirect loop this avoids in production.
#
# Mounted ABOVE 'user/' in core/urls.py: accounts.urls carries a
# '<str:username>/details' pattern, and a later include would let a player
# called "cv" shadow this one.
urlpatterns = [
    path('settings', CVSettingsAPIView.as_view()),
]
