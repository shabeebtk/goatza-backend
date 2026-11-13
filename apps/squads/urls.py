from django.urls import path

from apps.squads.views.member_views import (
    SquadCandidatesAPIView,
    SquadInviteCancelAPIView,
    SquadInviteCreateAPIView,
    SquadMemberMoveAPIView,
    SquadMemberRemoveAPIView,
    SquadMemberUpdateAPIView,
    SquadRequestsAPIView,
    SquadRequestsApproveAPIView,
    SquadRequestsDeclineAPIView,
    SquadRosterAPIView,
)
from apps.squads.views.player_views import (
    MySquadAcceptAPIView,
    MySquadDeclineAPIView,
    MySquadLeaveAPIView,
    MySquadsAPIView,
    MySquadWithdrawAPIView,
    SquadInvitePageAPIView,
    SquadJoinAPIView,
)
from apps.squads.views.showcase_views import (
    SquadShowcaseDetailAPIView,
    SquadShowcaseListAPIView,
)
from apps.squads.views.squad_views import (
    SquadCreateAPIView,
    SquadDeleteAPIView,
    SquadDetailAPIView,
    SquadInviteLinkAPIView,
    SquadInviteLinkResetAPIView,
    SquadListAPIView,
    SquadPhotoAPIView,
    SquadStaffAddAPIView,
    SquadStaffRemoveAPIView,
    SquadUpdateAPIView,
)

# base endpoint - squads/
#
# NO TRAILING SLASHES, for the same reason as careers/urls.py: a trailing slash
# makes Vercel's /api rewrite 308 and Django's APPEND_SLASH send the browser to
# the frontend origin. That is also why the list is 'list' and not the bare
# collection root: "/squads/" is exactly that broken form, so no app mounts
# anything at ''.
#
# Every route below takes a <uuid:…> or a fixed word, never a bare <str:…>, so
# the player-side routes still to come ('invite/<str:token>', 'mine',
# 'showcase/…') can sit beside them without being shadowed.
urlpatterns = [
    # ---- the club's squads (X-Actor: organization) ----
    path('list', SquadListAPIView.as_view(), name='squad-list'),
    path('create', SquadCreateAPIView.as_view(), name='squad-create'),
    path('<uuid:squad_id>', SquadDetailAPIView.as_view(), name='squad-detail'),
    path('<uuid:squad_id>/update', SquadUpdateAPIView.as_view(), name='squad-update'),
    path('<uuid:squad_id>/delete', SquadDeleteAPIView.as_view(), name='squad-delete'),
    path('<uuid:squad_id>/photo', SquadPhotoAPIView.as_view(), name='squad-photo'),
    path('<uuid:squad_id>/invite-link', SquadInviteLinkAPIView.as_view(), name='squad-invite-link'),
    path('<uuid:squad_id>/invite-link/reset', SquadInviteLinkResetAPIView.as_view(), name='squad-invite-link-reset'),
    path('<uuid:squad_id>/staff', SquadStaffAddAPIView.as_view(), name='squad-staff-add'),
    path('<uuid:squad_id>/staff/<uuid:staff_id>/remove', SquadStaffRemoveAPIView.as_view(), name='squad-staff-remove'),

    # ---- players in a squad ----
    path('<uuid:squad_id>/members', SquadRosterAPIView.as_view(), name='squad-roster'),
    path('<uuid:squad_id>/members/<uuid:member_id>', SquadMemberUpdateAPIView.as_view(), name='squad-member-update'),
    path('<uuid:squad_id>/members/<uuid:member_id>/remove', SquadMemberRemoveAPIView.as_view(), name='squad-member-remove'),
    path('<uuid:squad_id>/members/<uuid:member_id>/move', SquadMemberMoveAPIView.as_view(), name='squad-member-move'),
    path('<uuid:squad_id>/requests', SquadRequestsAPIView.as_view(), name='squad-requests'),
    path('<uuid:squad_id>/requests/approve', SquadRequestsApproveAPIView.as_view(), name='squad-requests-approve'),
    path('<uuid:squad_id>/requests/decline', SquadRequestsDeclineAPIView.as_view(), name='squad-requests-decline'),
    path('<uuid:squad_id>/candidates', SquadCandidatesAPIView.as_view(), name='squad-candidates'),
    path('<uuid:squad_id>/invites', SquadInviteCreateAPIView.as_view(), name='squad-invite-create'),
    path('<uuid:squad_id>/invites/<uuid:member_id>/cancel', SquadInviteCancelAPIView.as_view(), name='squad-invite-cancel'),

    # ---- the player (X-Actor: user) ----
    # The page is PUBLIC (anyone with the link); its token is kept out of logs
    # by core/log_redaction.py.
    path('invite/<str:token>', SquadInvitePageAPIView.as_view(), name='squad-invite-page'),
    path('invite/<str:token>/join', SquadJoinAPIView.as_view(), name='squad-join'),
    path('mine', MySquadsAPIView.as_view(), name='squad-mine'),
    path('mine/<uuid:member_id>/accept', MySquadAcceptAPIView.as_view(), name='squad-mine-accept'),
    path('mine/<uuid:member_id>/decline', MySquadDeclineAPIView.as_view(), name='squad-mine-decline'),
    path('mine/<uuid:member_id>/withdraw', MySquadWithdrawAPIView.as_view(), name='squad-mine-withdraw'),
    path('mine/<uuid:member_id>/leave', MySquadLeaveAPIView.as_view(), name='squad-mine-leave'),

    # ---- the showcase, signed in (any actor) ----
    # The anonymous twin is /public/organization/<username>/squads… in
    # core/public_urls.py. Same rules: selectors/showcase_selectors.py.
    path('showcase/<str:org_username>', SquadShowcaseListAPIView.as_view(), name='squad-showcase'),
    path('showcase/<str:org_username>/<uuid:squad_id>', SquadShowcaseDetailAPIView.as_view(), name='squad-showcase-detail'),
]
