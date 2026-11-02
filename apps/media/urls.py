from django.urls import path

from apps.media.views.media_job_views import (
    MediaCapabilitiesAPIView,
    MediaJobCreateAPIView,
    MediaJobDetailAPIView,
)
from apps.media.views.publication_views import (
    PublicationCancelAPIView,
    PublicationDismissAPIView,
    PublicationListAPIView,
)

# base url - /media/

urlpatterns = [
    path("capabilities", MediaCapabilitiesAPIView.as_view()),
    path("jobs", MediaJobCreateAPIView.as_view()),
    path("jobs/<uuid:job_id>", MediaJobDetailAPIView.as_view()),

    # Posts / highlights held while their media converts.
    path("publications", PublicationListAPIView.as_view()),
    path(
        "publications/<uuid:publication_id>/cancel",
        PublicationCancelAPIView.as_view(),
    ),
    path(
        "publications/<uuid:publication_id>/dismiss",
        PublicationDismissAPIView.as_view(),
    ),
]
