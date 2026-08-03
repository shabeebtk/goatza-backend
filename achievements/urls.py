from django.urls import path

from achievements.views.achievement_verification_views import (
    AchievementVerificationRequestListAPIView,
    RejectAchievementAPIView,
    VerifyAchievementAPIView,
)
from achievements.views.achievement_views import (
    AchievementDetailAPIView,
    CreateAchievementAPIView,
    UserAchievementListAPIView,
)

# base endpoint - achievements/
#
# NO TRAILING SLASHES — same as posts/highlights/recruitments/careers, and not
# cosmetic. In production the client talks to /api/<path> and Vercel rewrites it
# to the backend. A trailing slash makes Vercel 308 to the slash-less form FIRST,
# Django then APPEND_SLASHes back with a path-only Location ("/achievements/"),
# which the browser resolves against the FRONTEND origin — the /api prefix is
# gone and the call lands on the Next.js 404 page. That also rules out mounting
# create at the bare collection root, since "/achievements/" is exactly that
# broken form; it lives at "achievements/create" like every other app's create.
urlpatterns = [
    path('create', CreateAchievementAPIView.as_view(), name='create-achievement'),
    path('verification-requests', AchievementVerificationRequestListAPIView.as_view(), name='achievement-verification-requests'),
    path('users/<uuid:user_id>', UserAchievementListAPIView.as_view(), name='list-user-achievements'),
    path('<uuid:achievement_id>/verify', VerifyAchievementAPIView.as_view(), name='verify-achievement'),
    path('<uuid:achievement_id>/reject', RejectAchievementAPIView.as_view(), name='reject-achievement'),
    path('<uuid:achievement_id>', AchievementDetailAPIView.as_view(), name='achievement-detail'),
]
