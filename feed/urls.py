from django.urls import path
from feed.views.feed_views import FeedAPIView
from feed.views.explore_views import (
    ExplorePlayersAPIView, ExploreOrganizationsAPIView,
    ExploreTrendingPostsAPIView,
)

# base endpoint '/feed/

urlpatterns = [
    path('list', FeedAPIView.as_view()),

    # discovery
    path('explore/players', ExplorePlayersAPIView.as_view()),
    path('explore/organizations', ExploreOrganizationsAPIView.as_view()),
    path('explore/posts', ExploreTrendingPostsAPIView.as_view()),
]
