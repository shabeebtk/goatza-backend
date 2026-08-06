from django.urls import path
from organization.views.organization_views import (
    CreateOrganizationAPIView, ListUserOrganizationsAPIView, OrganizationsDetailsAPIView,
    UpdateOrganizationMediaAPIView, UpdateOrganizationAPIView
)
from organization.views.organization_location_views import (
    OrganizationLocationAPIView, DeleteOrganizationLocationAPIView
)
from organization.views.dashboard_views import OrganizationDashboardAPIView
from organization.views.organization_privacy_views import (
    OrganizationPublicProfilePrivacyAPIView
)

# base endpoint '/organizations/'

urlpatterns = [
    path('create', CreateOrganizationAPIView.as_view()),
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
]
