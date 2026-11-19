from django.urls import path

from apps.orgpages.views.coach_views import (
    PageCoachDeleteAPIView,
    PageCoachDetailAPIView,
    PageCoachesAPIView,
    PageCoachReorderAPIView,
)
from apps.orgpages.views.editor_views import (
    PageAdminAPIView,
    PageContactAPIView,
    PageFactsAPIView,
    PageSectionsAPIView,
)
from apps.orgpages.views.facility_views import (
    PageFacilitiesAPIView,
    PageFacilityPhotoDeleteAPIView,
    PageFacilityPhotosAPIView,
    PageFacilityPhotosReorderAPIView,
    PageLocationFacilitiesAPIView,
)
from apps.orgpages.views.player_views import (
    PagePlayerDetailAPIView,
    PagePlayerRemoveAPIView,
    PagePlayerReorderAPIView,
    PagePlayersAPIView,
)
from apps.orgpages.views.showcase_views import PageShowcaseAPIView
from apps.orgpages.views.program_views import (
    PageProgramDeleteAPIView,
    PageProgramDetailAPIView,
    PageProgramReorderAPIView,
    PageProgramsAPIView,
)

# base endpoint - page/
#
# NO TRAILING SLASHES, for the same reason as careers/urls.py and squads: a
# trailing slash makes Vercel's /api rewrite 308 and Django's APPEND_SLASH send
# the browser to the frontend origin. Nothing is mounted at the bare root.
#
# Every route takes a <uuid:…> or a fixed word, so 'programs/reorder' and
# 'facilities/photos/…' can never be read as an id.
urlpatterns = [
    # ---- the club's editor (X-Actor: organization) ----
    path('admin', PageAdminAPIView.as_view(), name='page-admin'),
    path('contact', PageContactAPIView.as_view(), name='page-contact'),
    path('facts', PageFactsAPIView.as_view(), name='page-facts'),
    path('sections', PageSectionsAPIView.as_view(), name='page-sections'),

    path('programs', PageProgramsAPIView.as_view(), name='page-programs'),
    path('programs/reorder', PageProgramReorderAPIView.as_view(), name='page-programs-reorder'),
    path('programs/<uuid:program_id>', PageProgramDetailAPIView.as_view(), name='page-program'),
    path('programs/<uuid:program_id>/delete', PageProgramDeleteAPIView.as_view(), name='page-program-delete'),

    path('facilities', PageFacilitiesAPIView.as_view(), name='page-facilities'),
    path('facilities/photos/<uuid:photo_id>/delete', PageFacilityPhotoDeleteAPIView.as_view(), name='page-facility-photo-delete'),
    path('facilities/<uuid:location_id>', PageLocationFacilitiesAPIView.as_view(), name='page-location-facilities'),
    path('facilities/<uuid:location_id>/photos', PageFacilityPhotosAPIView.as_view(), name='page-facility-photos'),
    path('facilities/<uuid:location_id>/photos/reorder', PageFacilityPhotosReorderAPIView.as_view(), name='page-facility-photos-reorder'),

    path('coaches', PageCoachesAPIView.as_view(), name='page-coaches'),
    path('coaches/reorder', PageCoachReorderAPIView.as_view(), name='page-coaches-reorder'),
    path('coaches/<uuid:coach_id>', PageCoachDetailAPIView.as_view(), name='page-coach'),
    path('coaches/<uuid:coach_id>/delete', PageCoachDeleteAPIView.as_view(), name='page-coach-delete'),

    path('players', PagePlayersAPIView.as_view(), name='page-players'),
    path('players/reorder', PagePlayerReorderAPIView.as_view(), name='page-players-reorder'),
    path('players/<uuid:featured_id>', PagePlayerDetailAPIView.as_view(), name='page-player'),
    path('players/<uuid:featured_id>/remove', PagePlayerRemoveAPIView.as_view(), name='page-player-remove'),

    # ---- the page as everyone else sees it, signed in (any actor) ----
    # The anonymous twin is /public/organization/<username>/page in
    # core/public_urls.py. Same rules: selectors/page_selectors.py.
    path('showcase/<str:org_username>', PageShowcaseAPIView.as_view(), name='page-showcase'),
]
