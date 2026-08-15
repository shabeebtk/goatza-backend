"""
THE public surface. Every endpoint reachable without a token is routed from
this one file, so "what can an anonymous caller see?" is answerable by reading
it — no grepping for permission_classes across a dozen urls.py.

Mounted at /public/ from core.urls. Views live in the apps that own the data;
only the routing is centralised.

Adding anything here is a deliberate act: the view must extend
core.views.base_views.PublicAPIView, and its payload must be an explicit
allow-list (see accounts/serializers/public_profile_serializers.py for why).
"""

from django.urls import path

from core.views.public_profile_views import (
    PublicOrganizationPostsAPIView,
    PublicOrganizationProfileAPIView,
    PublicUserPostsAPIView,
    PublicUserProfileAPIView,
)
from cv.views.public_cv_views import PublicCVAPIView

urlpatterns = [
    # Individual users — all roles (player, coach, scout, org_user).
    path('profile/<str:username>', PublicUserProfileAPIView.as_view()),
    path('profile/<str:username>/posts', PublicUserPostsAPIView.as_view()),

    # Sports CV — players only, and only where the profile is public AND the
    # CV is enabled. Every other case is the same 404 as an unknown username.
    path('cv/<str:username>', PublicCVAPIView.as_view()),

    # Organizations.
    path(
        'organization/<str:username>',
        PublicOrganizationProfileAPIView.as_view(),
    ),
    path(
        'organization/<str:username>/posts',
        PublicOrganizationPostsAPIView.as_view(),
    ),
]
