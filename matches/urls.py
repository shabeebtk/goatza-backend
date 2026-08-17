from django.urls import path

from matches.views.match_views import (
    CreateMatchAPIView,
    DeleteMatchAPIView,
    MatchDiarySettingsAPIView,
    MatchListAPIView,
    MatchStatFieldsAPIView,
    MatchSummaryAPIView,
    ShowcaseMatchSummaryAPIView,
    UpcomingMatchesAPIView,
    UpdateMatchAPIView,
)

# base endpoint - matches/
#
# NO TRAILING SLASHES — same as posts/accounts/recruitments/highlights, and not
# cosmetic. In production the client talks to /api/<path> and Vercel rewrites it
# to the backend. A trailing slash makes Vercel 308 to the slash-less form
# FIRST, Django then APPEND_SLASHes back with a path-only Location
# ("/matches/summary/"), which the browser resolves against the FRONTEND origin
# — the /api prefix is gone and the call lands on the Next.js 404 page.
# Slash-less paths never enter that loop.
#
# Literal paths before the UUID ones. Nothing here could actually shadow
# anything (a <uuid:> converter only matches a UUID, so "summary" can never be
# read as a match id), but keeping the fixed routes on top means the file reads
# in the order the router resolves.
urlpatterns = [
    path('create', CreateMatchAPIView.as_view()),
    path('list', MatchListAPIView.as_view()),
    path('upcoming', UpcomingMatchesAPIView.as_view()),
    path('summary', MatchSummaryAPIView.as_view()),
    # Above nothing in particular — 'summary/<username>' cannot collide with
    # 'summary', and no username can be read as a match id (that route takes a
    # <uuid:> converter).
    path('summary/<str:username>', ShowcaseMatchSummaryAPIView.as_view()),
    path('settings', MatchDiarySettingsAPIView.as_view()),
    path('stat-fields', MatchStatFieldsAPIView.as_view()),

    path('<uuid:match_id>/update', UpdateMatchAPIView.as_view()),
    path('<uuid:match_id>', DeleteMatchAPIView.as_view()),
]
