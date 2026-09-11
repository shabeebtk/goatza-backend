from django.contrib import admin
from apps.messaging.models import Conversation, ConversationParticipant, Message
# Register your models here.

admin.site.register(Conversation)
admin.site.register(ConversationParticipant)
admin.site.register(Message)