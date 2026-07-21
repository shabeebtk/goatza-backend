from django.urls import path
from messaging.views.conversation_views import (
    ConversationListAPIView, ConversationDetailAPIView, MarkConversationReadAPIView,
    GetOrCreateConversationAPIView, AcceptConversationAPIView,
    ConversationUnreadSummaryAPIView, MessageTargetSearchAPIView
)
from messaging.views.messaging_views import MessageListAPIView
from messaging.views.share_views import ShareToConversationsAPIView
from messaging.views.media_views import SendMediaMessageAPIView
from messaging.views.message_delete_views import DeleteMessageAPIView

# base endpoint '/conversations/

urlpatterns = [
    path('get-or-create', GetOrCreateConversationAPIView.as_view()),
    path('share', ShareToConversationsAPIView.as_view()),
    path('search', MessageTargetSearchAPIView.as_view()),
    path('list', ConversationListAPIView.as_view()),
    path('unread/summary', ConversationUnreadSummaryAPIView.as_view()),
    path('messages/list', MessageListAPIView.as_view()),
    path('<uuid:conversation_id>/details', ConversationDetailAPIView.as_view()),
    path('<uuid:conversation_id>/messages/media', SendMediaMessageAPIView.as_view()),
    path(
        '<uuid:conversation_id>/messages/<uuid:message_id>',
        DeleteMessageAPIView.as_view()
    ),
    path('mark/read/all', MarkConversationReadAPIView.as_view()),

    # accept request
    path('accept', AcceptConversationAPIView.as_view()),

]
