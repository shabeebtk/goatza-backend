"""
THE public surface. Every endpoint reachable without a token is routed from
this one file, so "what can an anonymous caller see?" is answerable by reading
it — no grepping for permission_classes across a dozen urls.py.

Mounted at /public/ from core.urls. Views live in the apps that own the data;
only the routing is centralised.

Adding anything here is a deliberate act: the view must extend
core.views.base_views.PublicAPIView, and its payload must be an explicit
allow-list (see accounts/serializers/public_profile_serializers.py for why).
"""

from django.urls import path

from core.views.public_profile_views import (
    PublicOrganizationPostsAPIView,
    PublicOrganizationProfileAPIView,
    PublicUserPostsAPIView,
    PublicUserProfileAPIView,
)
from cv.views.public_cv_views import PublicCVAPIView
from support.views.problem_report_views import PublicProblemReportAPIView
from waitlist.views.signup_views import (
    PlayerSignupCardAPIView,
    PlayerSignupCreateAPIView,
    WaitlistStatsAPIView,
)

urlpatterns = [
    # Individual users — all roles (player, coach, scout, org_user).
    path('profile/<str:username>', PublicUserProfileAPIView.as_view()),
    path('profile/<str:username>/posts', PublicUserPostsAPIView.as_view()),

    # Sports CV — players only, and only where the profile is public AND the
    # CV is enabled. Every other case is the same 404 as an unknown username.
    path('cv/<str:username>', PublicCVAPIView.as_view()),

    # Organizations.
    path(
        'organization/<str:username>',
        PublicOrganizationProfileAPIView.as_view(),
    ),
    path(
        'organization/<str:username>/posts',
        PublicOrganizationPostsAPIView.as_view(),
    ),

    # Pre-launch waitlist. The ONLY write on this surface — the point of the
    # thing is that nobody has an account yet — so the create view carries its
    # own throttle instead of PublicAPIView's read budget
    # (waitlist.throttles.WaitlistSignupThrottle, 5/hour per IP).
    #
    # The card endpoint is an allow-list of five fields and nothing else: a ref
    # code is short, public and screenshotted, so anything reachable by
    # guessing one is effectively published. Phone, email and Instagram are
    # never in that payload.
    path('waitlist/players', PlayerSignupCreateAPIView.as_view()),
    path('waitlist/stats', WaitlistStatsAPIView.as_view()),
    path('waitlist/players/<str:ref_code>', PlayerSignupCardAPIView.as_view()),

    # "Report a problem", filed without a session — the SECOND anonymous WRITE
    # on this surface after the waitlist, and it carries its own throttle for
    # the same reason: PublicAPIView's default is a 60/min read budget, which
    # is not a limit on a write (support.throttles
    # .PublicProblemReportThrottle, 3/hour per IP).
    #
    # The screens most likely to be broken are the ones somebody hits before
    # they have a session — login, signup, OTP — so this route has to exist.
    # It is TEXT ONLY: no screenshots, deliberately. A presigned upload handed
    # to an anonymous caller is a write path into the bucket from the open
    # internet, and it would need its own quarantine prefix and an orphan
    # sweeper before it were worth having.
    path('support/problem-report', PublicProblemReportAPIView.as_view()),
]
