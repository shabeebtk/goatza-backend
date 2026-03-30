from django.urls import path
from connections.views.connections_views import (
    FollowListAPIView, FollowAPIView, UnfollowAPIView, CheckFollowStatusAPIView
)

# base endpoint - connections/
urlpatterns = [
    path('user/follow/list', FollowListAPIView.as_view()),
    path('user/follow', FollowAPIView.as_view()),
    path('user/unfollow', UnfollowAPIView.as_view()),
    path('user/follow/status', CheckFollowStatusAPIView.as_view()),
]
