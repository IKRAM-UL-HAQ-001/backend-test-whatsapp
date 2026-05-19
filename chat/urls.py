from django.urls import path

from .views import (
    ChatMessages,
    DeleteChat,
    DeleteMessage,
    DownloadMessageFile,
    EditMessage,
    ForwardMessage,
    MessagesDelivered,
    MessagesRead,
    ReactMessage,
    SendMessage,
    SharedMediaView,
    StartChat,
    TypingIndicator,
    UserChats,
)

urlpatterns = [
    path("chats/", UserChats.as_view()),
    path("chats/<int:chat_id>/messages/", ChatMessages.as_view()),
    path("start/", StartChat.as_view()),
    path("send/", SendMessage.as_view()),
    path("typing/", TypingIndicator.as_view()),
    path("delete-message/", DeleteMessage.as_view()),
    path("edit-message/", EditMessage.as_view()),
    path("messages/delivered/", MessagesDelivered.as_view()),
    path("messages/read/", MessagesRead.as_view()),
    path("messages/<int:message_id>/download/", DownloadMessageFile.as_view()),
    path("shared-media/", SharedMediaView.as_view()),
    path("forward/", ForwardMessage.as_view()),
    path("delete-chat/", DeleteChat.as_view()),
    path("react/", ReactMessage.as_view()),
]
