from django.urls import path

from highlights.views.highlight_views import (
    CreateHighlightAPIView,
    HighlightDetailAPIView,
    RecordHighlightViewAPIView,
    ReorderHighlightsAPIView,
    UserHighlightListAPIView,
)

# base endpoint - api/highlights/
urlpatterns = [
    path('', CreateHighlightAPIView.as_view()),
    path('reorder/', ReorderHighlightsAPIView.as_view()),
    path('user/<str:username>/', UserHighlightListAPIView.as_view()),
    path('<uuid:highlight_id>/view/', RecordHighlightViewAPIView.as_view()),
    path('<uuid:highlight_id>/', HighlightDetailAPIView.as_view()),
]
