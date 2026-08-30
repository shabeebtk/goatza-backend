from django.urls import path

from places.views.places_views import (
    PlaceDetailsAPIView,
    PlacesAutocompleteAPIView,
)

# base endpoint - places/
#
# NO TRAILING SLASHES, like posts/accounts/recruitments/matches — and this is
# the one place the routes deviate from docs/PLACES_MIGRATION.md section 4,
# which writes them as `/places/autocomplete/` and `/places/details/<id>/`.
#
# A slashed route dies in production only. The client calls /api/<path>, Vercel
# (trailingSlash: false) 308s /api/places/autocomplete/ to the slash-less form
# BEFORE rewriting, the rewrite strips /api, and if Django then had to
# APPEND_SLASH it would answer with a path-only Location that has lost the /api
# prefix — which the browser resolves against the frontend origin and lands on
# the Next.js 404 page. Local dev and the Django test client accept either
# form, so nothing catches it before deploy.
#
# Declaring the slash-less form is also what makes the doc's spelling work in
# production anyway: Vercel's 308 hands us exactly this path.
urlpatterns = [
    path('autocomplete', PlacesAutocompleteAPIView.as_view()),
    path('details/<str:place_id>', PlaceDetailsAPIView.as_view()),
]
