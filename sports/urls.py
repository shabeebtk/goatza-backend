from django.urls import path
from sports.views.sports_views import SportListAPIView
from sports.views.user_sports_views import (
    UserSportListAPIView, UserSportCreateAPIView
)

# base url 'sports/'

urlpatterns = [
    path('list', SportListAPIView.as_view()),
    path('user/sport/list', UserSportListAPIView.as_view()),
    path('user/sport/add', UserSportCreateAPIView.as_view()),
]
