from django.urls import path

from legal.views.acceptance_views import (
    AcceptLegalDocumentsAPIView,
    LegalVersionsAPIView,
)

# base url - /legal/

urlpatterns = [
    path('versions', LegalVersionsAPIView.as_view()),
    path('accept', AcceptLegalDocumentsAPIView.as_view()),
]
