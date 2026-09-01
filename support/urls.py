from django.urls import path

from support.views.problem_report_views import ProblemReportAPIView

# base url - /support/
#
# The logged-out half of this endpoint is NOT here — an anonymous route lives
# in core/public_urls.py, which is the one file that answers "what can a caller
# with no token reach?".

urlpatterns = [
    path("problem-report", ProblemReportAPIView.as_view()),
]
