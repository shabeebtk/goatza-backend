from django.urls import path

from careers.views.career_verification_views import (
    CareerVerificationRequestListAPIView,
    RejectCareerEntryAPIView,
    VerifyCareerEntryAPIView,
)
from careers.views.career_views import (
    CareerEntryDetailAPIView,
    CreateCareerEntryAPIView,
    CreateCareerEntryFromApplicationAPIView,
    UserCareerEntryListAPIView,
)

# base endpoint - careers/
#
# NO TRAILING SLASHES — same as posts/highlights/recruitments, and not cosmetic.
# In production the client talks to /api/<path> and Vercel rewrites it to the
# backend. A trailing slash makes Vercel 308 to the slash-less form FIRST, Django
# then APPEND_SLASHes back with a path-only Location ("/careers/"), which the
# browser resolves against the FRONTEND origin — the /api prefix is gone and the
# call lands on the Next.js 404 page. That also rules out mounting create at the
# bare collection root, since "/careers/" is exactly that broken form; it lives
# at "careers/create" like every other app's create.
urlpatterns = [
    path('create', CreateCareerEntryAPIView.as_view(), name='create-career-entry'),
    path('verification-requests', CareerVerificationRequestListAPIView.as_view(), name='career-verification-requests'),
    path('from-application/<uuid:application_id>', CreateCareerEntryFromApplicationAPIView.as_view(), name='create-career-entry-from-application'),
    path('users/<uuid:user_id>', UserCareerEntryListAPIView.as_view(), name='list-user-career-entries'),
    path('<uuid:entry_id>/verify', VerifyCareerEntryAPIView.as_view(), name='verify-career-entry'),
    path('<uuid:entry_id>/reject', RejectCareerEntryAPIView.as_view(), name='reject-career-entry'),
    path('<uuid:entry_id>', CareerEntryDetailAPIView.as_view(), name='career-entry-detail'),
]
