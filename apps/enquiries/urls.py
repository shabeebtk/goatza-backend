from django.urls import path

from apps.enquiries.views.enquiry_views import (
    EnquiryAlertsAPIView,
    EnquiryDetailAPIView,
    EnquiryInboxAPIView,
    EnquirySubmitAPIView,
    EnquiryUnreadCountAPIView,
)

# base endpoint - enquiries/
#
# NO TRAILING SLASHES (see careers/urls.py). Fixed words first; the one id
# route takes a <uuid:…>, so 'inbox' and 'alerts' can never be read as an id.
urlpatterns = [
    # The public form. Anyone, signed in or not; 30/hour per IP.
    path('submit/<str:org_username>', EnquirySubmitAPIView.as_view(), name='enquiry-submit'),

    # The org's inbox (X-Actor: organization, enquiries.manage).
    path('inbox', EnquiryInboxAPIView.as_view(), name='enquiry-inbox'),
    path('inbox/unread-count', EnquiryUnreadCountAPIView.as_view(), name='enquiry-unread-count'),

    # The person's own email switch (X-Actor: user).
    path('alerts', EnquiryAlertsAPIView.as_view(), name='enquiry-alerts'),

    path('<uuid:enquiry_id>', EnquiryDetailAPIView.as_view(), name='enquiry-detail'),
]
