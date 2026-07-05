from django.urls import path
from recruitments.views.recruitment_views import (
    CreateRecruitmentAPIView, ListRecruitmentsAPIView, RecruitmentDetailAPIView,
    UpdateRecruitmentAPIView, ChangeRecruitmentStatusAPIView
)
from recruitments.views.application_views import (
    ApplyRecruitmentAPIView,
    ListRecruitmentApplicationsAPIView,
    RecruitmentApplicationDetailAPIView
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
    path('applications/<uuid:application_id>/details', RecruitmentApplicationDetailAPIView.as_view()),
]
