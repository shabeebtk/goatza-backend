from django.urls import path
from organization.views.organization_views import (
    CreateOrganizationAPIView, ListUserOrganizationsAPIView, OrganizationsDetailsAPIView,
    UpdateOrganizationMediaAPIView, UpdateOrganizationAPIView
)
from organization.views.organization_location_views import (
    OrganizationLocationAPIView, DeleteOrganizationLocationAPIView
)

# base endpoint '/organizations/'

urlpatterns = [
    path('create', CreateOrganizationAPIView.as_view()),
    path('list', ListUserOrganizationsAPIView.as_view()),
    path('details', OrganizationsDetailsAPIView.as_view()),
    path('update/logo/cover', UpdateOrganizationMediaAPIView.as_view()),
    path('update', UpdateOrganizationAPIView.as_view()),
    
    # Location APIs
    path('locations/upsert', OrganizationLocationAPIView.as_view()),
    path('locations/delete', DeleteOrganizationLocationAPIView.as_view()),
]
