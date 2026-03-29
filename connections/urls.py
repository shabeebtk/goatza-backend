from django.urls import path
from connections.views.connections_views import (
    FollowListAPIView, FollowUserAPIView, UnfollowUserAPIView, CheckFollowStatusAPIView
)

# base endpoint - connections/
urlpatterns = [
    path('user/follow/list', FollowListAPIView.as_view()),
    path('user/follow', FollowUserAPIView.as_view()),
    path('user/unfollow', UnfollowUserAPIView.as_view()),
    path('user/follow/status', CheckFollowStatusAPIView.as_view()),
]
