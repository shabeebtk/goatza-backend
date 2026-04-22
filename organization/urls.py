from django.urls import path
from organization.views.organization_views import (
    CreateOrganizationAPIView, ListUserOrganizationsAPIView, OrganizationsDetailsAPIView
)

# base endpoint '/organizations/

urlpatterns = [
    path('create', CreateOrganizationAPIView.as_view()),
    path('list', ListUserOrganizationsAPIView.as_view()),
    path('details', OrganizationsDetailsAPIView.as_view())
]
