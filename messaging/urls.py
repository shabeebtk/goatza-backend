from django.urls import path
from messaging.views.conversation_views import (
    ConversationListAPIView, ConversationDetailAPIView, MarkConversationReadAPIView,
    GetOrCreateConversationAPIView, AcceptConversationAPIView
)
from messaging.views.messaging_views import MessageListAPIView

# base endpoint '/conversations/

urlpatterns = [
    path('get-or-create', GetOrCreateConversationAPIView.as_view()),
    path('list', ConversationListAPIView.as_view()),
    path('messages/list', MessageListAPIView.as_view()),
    path('<uuid:conversation_id>/details', ConversationDetailAPIView.as_view()),
    path('mark/read/all', MarkConversationReadAPIView.as_view()),

    # accept request
    path('accept', AcceptConversationAPIView.as_view()),

]
