from django.urls import path
from feed.views.feed_views import FeedAPIView

# base endpoint '/feed/

urlpatterns = [
    path('list', FeedAPIView.as_view())
]
