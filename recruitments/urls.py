from django.urls import path
from recruitments.views.recruitment_views import (
    CreateRecruitmentAPIView, ListRecruitmentsAPIView, RecruitmentDetailAPIView,
    UpdateRecruitmentAPIView, ChangeRecruitmentStatusAPIView
)
from recruitments.views.application_views import (
    ApplyRecruitmentAPIView,
    ListRecruitmentApplicationsAPIView,
    RecruitmentApplicationDetailAPIView,
    WithdrawApplicationAPIView,
    BulkApplicationStatusAPIView,
    ApplicationStatusAPIView
)

# base endpoint - "/recruitments"

urlpatterns = [
    path('create', CreateRecruitmentAPIView.as_view()),
    path('list', ListRecruitmentsAPIView.as_view()),
    path('<uuid:recruitment_id>/details', RecruitmentDetailAPIView.as_view()),
    path('<uuid:recruitment_id>/update', UpdateRecruitmentAPIView.as_view()),
    path('<uuid:recruitment_id>/status', ChangeRecruitmentStatusAPIView.as_view()),
    path('<uuid:recruitment_id>/apply', ApplyRecruitmentAPIView.as_view()),
    path('<uuid:recruitment_id>/applications', ListRecruitmentApplicationsAPIView.as_view()),
    path('<uuid:recruitment_id>/applications/bulk-status', BulkApplicationStatusAPIView.as_view()),
    path('applications/<uuid:application_id>/details', RecruitmentApplicationDetailAPIView.as_view()),
    path('applications/<uuid:application_id>/withdraw', WithdrawApplicationAPIView.as_view()),
    path('applications/<uuid:application_id>/status', ApplicationStatusAPIView.as_view()),
]
