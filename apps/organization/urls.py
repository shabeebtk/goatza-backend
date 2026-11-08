from django.urls import path
from apps.organization.views.organization_views import (
    CreateOrganizationAPIView, ListUserOrganizationsAPIView, OrganizationsDetailsAPIView,
    OrganizationCreateEligibilityAPIView,
    UpdateOrganizationMediaAPIView, UpdateOrganizationAPIView
)
from apps.organization.views.organization_location_views import (
    OrganizationLocationAPIView, DeleteOrganizationLocationAPIView
)
from apps.organization.views.dashboard_views import OrganizationDashboardAPIView
from apps.organization.views.organization_privacy_views import (
    OrganizationPublicProfilePrivacyAPIView
)
from apps.organization.views.organization_member_views import (
    InviteAcceptAPIView,
    InviteDeclineAPIView,
    InviteTokenAcceptAPIView,
    InviteTokenDeclineAPIView,
    InviteTokenPageAPIView,
    MyInvitesAPIView,
    OrganizationInviteCancelAPIView,
    OrganizationInviteCreateAPIView,
    OrganizationLeaveAPIView,
    OrganizationMemberRemoveAPIView,
    OrganizationMemberRoleAPIView,
    OrganizationMembersAPIView,
    OrganizationPendingInvitesAPIView,
)

# base endpoint '/organizations/'

urlpatterns = [
    path('create', CreateOrganizationAPIView.as_view()),
    path('create/eligibility', OrganizationCreateEligibilityAPIView.as_view()),
    path('list', ListUserOrganizationsAPIView.as_view()),
    path('details', OrganizationsDetailsAPIView.as_view()),
    path('update/logo/cover', UpdateOrganizationMediaAPIView.as_view()),
    path('update', UpdateOrganizationAPIView.as_view()),

    # Privacy
    path('privacy/public-profile', OrganizationPublicProfilePrivacyAPIView.as_view()),

    # Dashboard
    path('dashboard', OrganizationDashboardAPIView.as_view()),

    # Location APIs
    path('locations/upsert', OrganizationLocationAPIView.as_view()),
    path('locations/delete', DeleteOrganizationLocationAPIView.as_view()),

    # Members (org actor)
    path('members', OrganizationMembersAPIView.as_view()),
    path('members/invites', OrganizationPendingInvitesAPIView.as_view()),
    path('members/invite', OrganizationInviteCreateAPIView.as_view()),
    path('members/invites/<uuid:invite_id>/cancel', OrganizationInviteCancelAPIView.as_view()),
    path('members/leave', OrganizationLeaveAPIView.as_view()),
    path('members/<uuid:member_id>/role', OrganizationMemberRoleAPIView.as_view()),
    path('members/<uuid:member_id>/remove', OrganizationMemberRemoveAPIView.as_view()),

    # Invites (the invited person)
    path('invites/mine', MyInvitesAPIView.as_view()),
    path('invites/token/<str:token>', InviteTokenPageAPIView.as_view()),
    path('invites/token/<str:token>/accept', InviteTokenAcceptAPIView.as_view()),
    path('invites/token/<str:token>/decline', InviteTokenDeclineAPIView.as_view()),
    path('invites/<uuid:invite_id>/accept', InviteAcceptAPIView.as_view()),
    path('invites/<uuid:invite_id>/decline', InviteDeclineAPIView.as_view()),
]
