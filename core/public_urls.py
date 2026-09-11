"""
THE public surface. Every endpoint reachable without a token is routed from
this one file, so "what can an anonymous caller see?" is answerable by reading
it — no grepping for permission_classes across a dozen urls.py.

Mounted at /public/ from core.urls. Views live in the apps that own the data;
only the routing is centralised.

Adding anything here is a deliberate act: the view must extend
core.views.base_views.PublicAPIView, and its payload must be an explicit
allow-list (see accounts/serializers/public_profile_serializers.py for why).

ONE deliberate exception to "every anonymous route lives here": /healthz,
routed straight from core.urls. It is an infra probe, not part of the public
data surface — it returns two booleans about our own database and Redis, not a
row of anybody's data — and it has to be a plain Django view so Render's poller
is not subject to JWT auth, the terms gate or the anon throttle. Anything that
returns USER data still belongs in this file.

AND ONE MORE, WHICH DOES RETURN USER DATA: /guardian/consent/<token> and its
three POSTs (guardians/urls.py, guardians/views/public_consent_views.py). They
are anonymous — a parent has no account, and the token in the URL is the whole
identity — so they are named here, because the question this file answers is
"what can a stranger reach?" and leaving them out would make the answer wrong.

They are routed from /guardian/ rather than from here for one reason: the
prefix is addressed by a link already sitting in parents' inboxes, and it sits
beside the child's authenticated endpoints it is the other half of. What keeps
them honest is not their prefix but their rule — nothing about a child leaves
without a resolved token, and every kind of failure to resolve one returns the
same body. Read that module before adding anything under it.
"""

from django.urls import path

from core.views.public_profile_views import (
    PublicOrganizationPostsAPIView,
    PublicOrganizationProfileAPIView,
    PublicUserPostsAPIView,
    PublicUserProfileAPIView,
)
from core.views.sitemap_views import PublicSitemapURLsAPIView
from apps.cv.views.public_cv_views import PublicCVAPIView
from apps.recruitments.views.public_recruitment_views import (
    PublicRecruitmentDetailAPIView,
)
from apps.support.views.problem_report_views import PublicProblemReportAPIView
from apps.waitlist.views.signup_views import (
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

    # A single recruitment. The flagship share: a club posts a trial, the link
    # lands in a WhatsApp group, and most of that group has no account yet.
    #
    # ALWAYS the public serializer — never the owner one, even when the caller
    # turns out to be the posting org. views_count, saves_count, status and the
    # applicant numbers are owner-only and stay on the authenticated
    # /recruitments/<id>/details; an org admin who opens their own share link
    # is sent there by the client instead.
    #
    # Visibility is the selector's, not this route's: an anonymous caller sees
    # a posting only while it is ACTIVE and public, and a draft, a closed
    # posting, a followers-only one and a typo'd uuid all answer the same 404.
    path(
        'recruitments/<uuid:recruitment_id>',
        PublicRecruitmentDetailAPIView.as_view(),
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

    # The handles the frontend's sitemap.xml is built from. JSON, not XML: the
    # URL shape (/profile/<username>, /organization/profile/<username>) belongs
    # to Next.js, and this answers only the part that needs the database.
    #
    # Read by the frontend's sitemap route once an hour, never by a browser, so
    # the response is one cached blob on the standard public read throttle.
    # Handles and timestamps only — the same allow-list rule as everything else
    # on this surface, and the visibility predicate is shared with the profile
    # endpoints above (core.selectors.public_profile_selectors) so a hidden
    # profile can never be advertised here while 404ing there.
    path('sitemap/urls', PublicSitemapURLsAPIView.as_view()),
]
