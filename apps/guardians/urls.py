from django.urls import path

from apps.guardians.views.consent_views import (
    GuardianDetailsAPIView,
    GuardianResendAPIView,
    GuardianSharedApproveAPIView,
)
from apps.guardians.views.public_consent_views import (
    PublicConsentApproveAPIView,
    PublicConsentDeclineAPIView,
    PublicConsentPageAPIView,
    PublicConsentWithdrawAPIView,
)

# base url - /guardian/
#
# TWO SURFACES, and they share nothing but a prefix.
#
# The first three are the CHILD's: authenticated as the account being unlocked,
# outside the terms gate so a locked minor can still use them.
#
# Everything under consent/ is the PARENT's, and is ANONYMOUS — no session, no
# account, the token in the URL is the whole identity. It is listed in
# core/public_urls.py's docstring as the one anonymous prefix routed from
# somewhere else, so that file stays the index of what a stranger can reach.
#
# consent/<token> is matched before the sub-paths would ever be ambiguous
# because the token is one segment and the actions are two; Django's resolver
# takes the first exact match either way.

urlpatterns = [
    # ---- the child's side (authenticated) ----
    path('details', GuardianDetailsAPIView.as_view()),
    path('shared/approve', GuardianSharedApproveAPIView.as_view()),
    path('resend', GuardianResendAPIView.as_view()),

    # ---- the parent's side (anonymous, token-addressed) ----
    path('consent/<str:token>', PublicConsentPageAPIView.as_view()),
    path('consent/<str:token>/approve', PublicConsentApproveAPIView.as_view()),
    path('consent/<str:token>/decline', PublicConsentDeclineAPIView.as_view()),
    path('consent/<str:token>/withdraw', PublicConsentWithdrawAPIView.as_view()),
]
