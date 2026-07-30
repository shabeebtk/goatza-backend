from django.urls import path

from highlights.views.highlight_views import (
    CreateHighlightAPIView,
    HighlightDetailAPIView,
    HighlightStatsAPIView,
    RecordHighlightViewAPIView,
    ReorderHighlightsAPIView,
    UserHighlightListAPIView,
)

# base endpoint - highlights/
#
# NO TRAILING SLASHES — same as posts/accounts/recruitments, and not cosmetic.
# In production the client talks to /api/<path> and Vercel rewrites it to the
# backend. A trailing slash makes Vercel 308 to the slash-less form FIRST, Django
# then APPEND_SLASHes back with a path-only Location ("/highlights/stats/"),
# which the browser resolves against the FRONTEND origin — the /api prefix is
# gone and the call lands on the Next.js 404 page. Slash-less paths never enter
# that loop.
urlpatterns = [
    path('create', CreateHighlightAPIView.as_view()),
    path('reorder', ReorderHighlightsAPIView.as_view()),
    path('stats', HighlightStatsAPIView.as_view()),
    path('user/<str:username>', UserHighlightListAPIView.as_view()),
    path('<uuid:highlight_id>/view', RecordHighlightViewAPIView.as_view()),
    path('<uuid:highlight_id>', HighlightDetailAPIView.as_view()),
]
