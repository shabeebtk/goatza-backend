from django.urls import path
from recruitments.views.recruitment_views import (
    CreateRecruitmentAPIView, ListRecruitmentsAPIView, RecruitmentDetailAPIView
)

# base endpoint - "/recruitments" 

urlpatterns = [
    path('create', CreateRecruitmentAPIView.as_view()),
    path('list', ListRecruitmentsAPIView.as_view()),
    path('<uuid:recruitment_id>/details', RecruitmentDetailAPIView.as_view()),
]
