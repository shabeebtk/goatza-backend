from django.urls import path

from moderation.views.block_views import (
    BlockAPIView,
    BlockedListAPIView,
)
from moderation.views.report_views import ReportAPIView

# base endpoint - moderation/
urlpatterns = [
    # POST blocks, DELETE unblocks — one resource, two verbs.
    path("block", BlockAPIView.as_view()),
    path("blocked", BlockedListAPIView.as_view()),
    # The only user-facing report surface. Everything else is admin.py.
    path("report", ReportAPIView.as_view()),
]
