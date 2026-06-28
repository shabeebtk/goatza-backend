from django.urls import path
from recruitments.views.recruitment_views import (
    CreateRecruitmentAPIView, ListRecruitmentsAPIView, RecruitmentDetailAPIView,
    UpdateRecruitmentAPIView, ChangeRecruitmentStatusAPIView
)

# base endpoint - "/recruitments"

urlpatterns = [
    path('create', CreateRecruitmentAPIView.as_view()),
    path('list', ListRecruitmentsAPIView.as_view()),
    path('<uuid:recruitment_id>/details', RecruitmentDetailAPIView.as_view()),
    path('<uuid:recruitment_id>/update', UpdateRecruitmentAPIView.as_view()),
    path('<uuid:recruitment_id>/status', ChangeRecruitmentStatusAPIView.as_view()),
]
