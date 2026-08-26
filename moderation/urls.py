from django.urls import path

from moderation.views.block_views import (
    BlockAPIView,
    BlockedListAPIView,
)

# base endpoint - moderation/
urlpatterns = [
    # POST blocks, DELETE unblocks — one resource, two verbs.
    path("block", BlockAPIView.as_view()),
    path("blocked", BlockedListAPIView.as_view()),
]
