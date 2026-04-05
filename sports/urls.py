from django.urls import path
from sports.views.sports_views import SportListAPIView
from sports.views.user_sports_views import (
    UserSportListAPIView, UserSportCreateAPIView, UserSportUpsertAPIView, UserSportDeleteAPIView
)

# base url 'sports/'

urlpatterns = [
    path('list', SportListAPIView.as_view()),
    path('user/<str:username>/sport/list', UserSportListAPIView.as_view()),
    path('user/sport/add', UserSportCreateAPIView.as_view()),
    path('user/sport/update', UserSportUpsertAPIView.as_view()),
    path('user/sport/delete', UserSportDeleteAPIView.as_view()),
]
