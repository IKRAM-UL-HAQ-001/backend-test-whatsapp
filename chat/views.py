import logging
import os
from datetime import timedelta

from django.core.files.base import ContentFile
from django.http import FileResponse
from django.core.cache import cache
from django.db import transaction
from django.db.models import Case, Count, F, OuterRef, Q, Subquery, When
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import CursorPagination, LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from users.models import User

from .models import Chat, DeletedMessage, Message, MessageReaction, MessageReceipt, MessageStatus
from .serializers import (
    ChatSerializer,
    DeleteMessageSerializer,
    EditMessageSerializer,
    MessageIdsSerializer,
    MessageSerializer,
    ReactSerializer,
    ReadMessagesSerializer,
    SendMessageSerializer,
    SharedMediaSerializer,
    TypingSerializer,
)
from .tasks import send_message_notification
from .utils import create_image_thumbnail


logger = logging.getLogger(__name__)


class ChatMessagesCursorPagination(CursorPagination):
    page_size = 30
    ordering = "-created_at"


class ChatListPagination(LimitOffsetPagination):
    default_limit = 20
    max_limit = 50


def broadcast_socket_event(user_id, event_name, payload):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(
        f"user_{user_id}",
        {
            "type": f"{event_name}_event",
            "payload": payload,
        },
    )


def queue_push_notification(message_id, receiver_id):
    queued_at = timezone.now()
    logger.info(
        "push_task_queued kind=message message_id=%s receiver_id=%s queued_at=%s queue=default",
        message_id,
        receiver_id,
        queued_at.isoformat(),
    )

    def enqueue():
        try:
            send_message_notification.apply_async(
                (message_id, receiver_id),
                queue="default",
                priority=5,
            )
        except Exception as exc:
            logger.warning("Failed to queue push notification for user_id=%s: %s", receiver_id, exc)

    try:
        transaction.on_commit(enqueue)
    except Exception as exc:
        logger.warning("Failed to queue push notification for user_id=%s: %s", receiver_id, exc)


def validate_message_reference(message_id, chat):
    if message_id is None:
        return None
    referenced = Message.objects.filter(id=message_id, chat=chat).first()
    return referenced


ALLOWED_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp", "heic",
    "mp4", "mov", "avi", "mkv", "3gp",
    "mp3", "ogg", "wav", "m4a", "aac", "opus",
    "pdf", "txt", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv",
    "zip", "rar", "7z", "tar", "gz",
}
MAX_FILE_SIZE = 100 * 1024 * 1024


def validate_upload(upload):
    if upload is None:
        return
    extension = os.path.splitext(upload.name or "")[1].lower().lstrip(".")
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported file type")
    if upload.size > MAX_FILE_SIZE:
        raise ValueError("File exceeds 100MB limit")


def other_participant(chat, user):
    return chat.user2 if chat.user1_id == user.id else chat.user1


def restore_chat_for_participants(chat, *users):
    changed_fields = []
    for user in users:
        if user is None:
            continue
        if chat.user1_id == user.id and chat.deleted_for_user1:
            chat.deleted_for_user1 = False
            chat.deleted_at_user1 = None
            changed_fields.extend(["deleted_for_user1", "deleted_at_user1"])
        elif chat.user2_id == user.id and chat.deleted_for_user2:
            chat.deleted_for_user2 = False
            chat.deleted_at_user2 = None
            changed_fields.extend(["deleted_for_user2", "deleted_at_user2"])
    if changed_fields:
        chat.save(update_fields=list(dict.fromkeys(changed_fields)))


def update_message_statuses(messages, next_status, timestamp=None):
    timestamp = timestamp or timezone.now()
    message_ids = []
    by_sender_chat = {}
    for message in messages:
        changed = False
        if next_status == MessageStatus.DELIVERED and message.status == MessageStatus.SENT:
            message.status = MessageStatus.DELIVERED
            message.delivered_at = timestamp
            changed = True
        elif next_status == MessageStatus.READ and message.status in {MessageStatus.SENT, MessageStatus.DELIVERED}:
            if message.delivered_at is None:
                message.delivered_at = timestamp
            message.status = MessageStatus.READ
            message.read_at = timestamp
            changed = True
        if changed:
            message.save(update_fields=["status", "delivered_at", "read_at"])
            MessageReceipt.objects.filter(message=message).update(
                delivered_at=message.delivered_at,
                read_at=message.read_at,
            )
            message_ids.append(str(message.id))
            by_sender_chat.setdefault((message.sender_id, message.chat_id), []).append(str(message.id))
    for (sender_id, chat_id), ids in by_sender_chat.items():
        broadcast_socket_event(
            sender_id,
            "status_update",
            {
                "message_ids": ids,
                "chat_id": chat_id,
                "status": next_status,
                "read_at": timestamp.isoformat() if next_status == MessageStatus.READ else None,
            },
        )
    return message_ids


class StartChat(APIView):
    """
    POST /api/start/
    Auth: bearer
    Request: {"receiver_id": 2}
    Response: {"chat_id": 1, "created": true}
    Errors: 404 user missing
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        receiver_id = request.data.get("receiver_id")
        receiver = get_object_or_404(User, id=receiver_id)
        u1, u2 = sorted([request.user, receiver], key=lambda user: user.id)
        chat, created = Chat.objects.get_or_create(user1=u1, user2=u2)
        if not created:
            restore_chat_for_participants(chat, request.user)
        return Response({"chat_id": chat.id, "created": created})


class ChatMessages(APIView):
    """
    GET /api/chats/{chat_id}/messages/
    Auth: bearer
    Response: paginated message history
    Errors: 403 not a participant
    """

    permission_classes = [IsAuthenticated]
    pagination_class = ChatMessagesCursorPagination

    def get(self, request, chat_id):
        chat = get_object_or_404(Chat.objects.select_related("user1", "user2"), id=chat_id)
        if not chat.has_participant(request.user):
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)

        deleted_ids = DeletedMessage.objects.filter(user=request.user, message__chat=chat).values_list("message_id", flat=True)
        messages = (
            Message.objects.filter(chat=chat)
            .exclude(id__in=deleted_ids)
            .exclude(deleted_for_users=request.user)
            .select_related("sender", "reply_to", "reply_to__sender", "forwarded_from", "forwarded_from__sender")
            .prefetch_related("reactions__user", "statuses", "deleted_for_users")
            .order_by("-created_at")
        )

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(messages, request)
        serialized = MessageSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serialized.data)


class SendMessage(APIView):
    """
    POST /api/send/
    Auth: bearer
    Request: multipart with receiver_id, encrypted_text, client_uuid, file?, reply_to?, forwarded_from?
    Response: message payload
    Errors: 400 invalid references/file
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = SendMessageSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        receiver = get_object_or_404(User, id=data["receiver_id"])
        upload = request.FILES.get("file")
        try:
            validate_upload(upload)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        u1, u2 = sorted([request.user, receiver], key=lambda user: user.id)
        chat, _ = Chat.objects.get_or_create(user1=u1, user2=u2)
        restore_chat_for_participants(chat, request.user, receiver)

        existing = Message.objects.filter(sender=request.user, client_uuid=data.get("client_uuid")).first()
        if existing:
            return Response(MessageSerializer(existing, context={"request": request}).data)

        reply_to = validate_message_reference(data.get("reply_to"), chat)
        if data.get("reply_to") and reply_to is None:
            return Response({"error": "reply_to must belong to the same chat"}, status=status.HTTP_400_BAD_REQUEST)

        forwarded_from = validate_message_reference(data.get("forwarded_from"), chat)
        if data.get("forwarded_from") and forwarded_from is None:
            return Response({"error": "forwarded_from must belong to the same chat"}, status=status.HTTP_400_BAD_REQUEST)

        thumbnail = create_image_thumbnail(upload) if upload and data["message_type"] == "image" else None
        msg = Message.objects.create(
            chat=chat,
            sender=request.user,
            encrypted_text=data["encrypted_text"],
            client_uuid=data.get("client_uuid") or None,
            message_type=data["message_type"],
            reply_to=reply_to,
            forwarded_from=forwarded_from,
            file=upload,
            file_name=upload.name if upload else "",
            file_size=upload.size if upload else None,
            file_type=getattr(upload, "content_type", "") if upload else "",
            duration=data.get("duration"),
            thumbnail=thumbnail,
            status=MessageStatus.SENT,
        )
        logger.info(
            "message_created message_id=%s chat_id=%s sender_id=%s receiver_id=%s created_at=%s",
            msg.id,
            chat.id,
            request.user.id,
            receiver.id,
            msg.created_at.isoformat(),
        )
        chat.last_activity = msg.created_at
        chat.save(update_fields=["last_activity"])
        MessageReceipt.objects.get_or_create(message=msg, user=receiver)

        serialized_data = MessageSerializer(
            Message.objects.select_related("sender", "chat").prefetch_related("reactions", "statuses", "deleted_for_users").get(id=msg.id),
            context={"request": request},
        ).data
        broadcast_socket_event(receiver.id, "chat_message", serialized_data)
        broadcast_socket_event(request.user.id, "chat_message", serialized_data)

        queue_push_notification(msg.id, receiver.id)
        return Response(serialized_data)


class DeleteMessage(APIView):
    """
    POST /api/delete-message/
    Auth: bearer
    Request: {"message_id": 1, "for_everyone": false}
    Response: {"status": "deleted"}
    Errors: 403 not a participant
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeleteMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        msg = get_object_or_404(Message.objects.select_related("chat", "sender"), id=serializer.validated_data["message_id"])
        if not msg.chat.has_participant(request.user):
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
        delete_type = serializer.validated_data.get("delete_type")
        if delete_type is None:
            delete_type = "for_everyone" if serializer.validated_data.get("for_everyone") else "for_me"
        if delete_type == "for_everyone":
            if msg.sender_id != request.user.id:
                return Response({"error": "Only sender can delete for everyone"}, status=status.HTTP_403_FORBIDDEN)
            if timezone.now() > msg.created_at + timedelta(seconds=60):
                return Response({"error": "Delete for everyone window expired"}, status=status.HTTP_400_BAD_REQUEST)
            msg.is_deleted_for_everyone = True
            msg.encrypted_text = ""
            msg.file = None
            msg.file_name = ""
            msg.file_size = None
            msg.file_type = ""
            msg.save(update_fields=["is_deleted_for_everyone", "encrypted_text", "file", "file_name", "file_size", "file_type"])
            payload = {"message_id": str(msg.id), "chat_id": msg.chat_id, "delete_type": "for_everyone"}
            broadcast_socket_event(msg.chat.user1_id, "message_deleted", payload)
            broadcast_socket_event(msg.chat.user2_id, "message_deleted", payload)
        else:
            DeletedMessage.objects.get_or_create(user=request.user, message=msg)
            msg.deleted_for_users.add(request.user)
        return Response({"status": "deleted", "delete_type": delete_type})


class EditMessage(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = EditMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        msg = get_object_or_404(Message.objects.select_related("chat", "sender"), id=serializer.validated_data["message_id"])
        if not msg.chat.has_participant(request.user):
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
        if msg.sender_id != request.user.id:
            return Response({"error": "Only sender can edit"}, status=status.HTTP_403_FORBIDDEN)
        if msg.message_type != "text" or msg.file:
            return Response({"error": "Only text messages can be edited"}, status=status.HTTP_400_BAD_REQUEST)
        if msg.is_deleted_for_everyone:
            return Response({"error": "Deleted messages cannot be edited"}, status=status.HTTP_400_BAD_REQUEST)
        msg.encrypted_text = serializer.validated_data["encrypted_text"]
        msg.edited_at = timezone.now()
        msg.save(update_fields=["encrypted_text", "edited_at"])
        payload = {
            "message_id": str(msg.id),
            "chat_id": msg.chat_id,
            "new_content": msg.encrypted_text,
            "edited_at": msg.edited_at.isoformat(),
        }
        broadcast_socket_event(other_participant(msg.chat, request.user).id, "message_edited", payload)
        broadcast_socket_event(request.user.id, "message_edited", payload)
        return Response(payload)


class MessagesDelivered(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = MessageIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        messages = (
            Message.objects.filter(id__in=serializer.validated_data["message_ids"])
            .filter(Q(chat__user1=request.user) | Q(chat__user2=request.user))
            .exclude(sender=request.user)
        )
        updated_ids = update_message_statuses(messages, MessageStatus.DELIVERED)
        return Response({"message_ids": updated_ids, "status": MessageStatus.DELIVERED})


class MessagesRead(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ReadMessagesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        chat = get_object_or_404(Chat, id=serializer.validated_data["chat_id"])
        if not chat.has_participant(request.user):
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
        messages = Message.objects.filter(chat=chat).exclude(sender=request.user).exclude(status=MessageStatus.READ)
        updated_ids = update_message_statuses(messages, MessageStatus.READ)
        return Response({"message_ids": updated_ids, "status": MessageStatus.READ})


class DownloadMessageFile(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, message_id):
        msg = get_object_or_404(Message.objects.select_related("chat"), id=message_id)
        if not msg.chat.has_participant(request.user):
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
        if not msg.file:
            return Response({"error": "No file attached"}, status=status.HTTP_404_NOT_FOUND)
        response = FileResponse(msg.file.open("rb"), content_type=msg.file_type or "application/octet-stream")
        file_name = msg.file_name or os.path.basename(msg.file.name)
        response["Content-Disposition"] = f'attachment; filename="{file_name}"'
        return response


class SharedMediaView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        chat_id = request.query_params.get("chat_id")
        user_id = request.query_params.get("user_id")
        media_type = request.query_params.get("type", "media")

        if chat_id:
            chat = get_object_or_404(Chat.objects.select_related("user1", "user2"), id=chat_id)
        elif user_id:
            other = get_object_or_404(User, id=user_id)
            chat = get_object_or_404(
                Chat.objects.select_related("user1", "user2"),
                Q(user1=request.user, user2=other) | Q(user1=other, user2=request.user),
            )
        else:
            return Response({"detail": "chat_id or user_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        if not chat.has_participant(request.user):
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)

        messages = (
            Message.objects.filter(chat=chat, file__isnull=False, is_deleted_for_everyone=False)
            .exclude(deleted_for_users=request.user)
            .select_related("sender")
            .order_by("-created_at")
        )

        if media_type == "media":
            messages = messages.filter(message_type__in=["image", "video"])
        elif media_type == "docs":
            messages = messages.filter(message_type="document")
        elif media_type == "audio":
            messages = messages.filter(message_type="audio")
        else:
            return Response({"detail": "Unsupported media type."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = SharedMediaSerializer(messages, many=True, context={"request": request})
        return Response(serializer.data)


class ForwardMessage(APIView):
    """
    POST /api/forward/
    Auth: bearer
    Request: {"receiver_id": 2, "forwarded_from": 10, "encrypted_text": "ignored"}
    Response: message payload
    Errors: 403 unauthorized, 400 invalid source
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        original_id = request.data.get("message_id") or request.data.get("forwarded_from")
        target_chat_id = request.data.get("chat_id")
        orig = get_object_or_404(
            Message.objects.select_related("chat", "sender", "chat__user1", "chat__user2"),
            id=original_id,
        )
        if not orig.chat.has_participant(request.user):
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)

        if target_chat_id:
            chat = get_object_or_404(Chat.objects.select_related("user1", "user2"), id=target_chat_id)
            if not chat.has_participant(request.user):
                return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
            receiver = other_participant(chat, request.user)
        else:
            receiver = get_object_or_404(User, id=request.data.get("receiver_id"))
            u1, u2 = sorted([request.user, receiver], key=lambda user: user.id)
            chat, _ = Chat.objects.get_or_create(user1=u1, user2=u2)

        restore_chat_for_participants(chat, request.user, receiver)
        msg = Message(
            chat=chat,
            sender=request.user,
            encrypted_text=orig.encrypted_text,
            message_type=orig.message_type,
            forwarded_from=orig,
            is_forwarded=True,
            file_name=orig.file_name,
            file_size=orig.file_size,
            file_type=orig.file_type,
            duration=orig.duration,
            width=orig.width,
            height=orig.height,
        )
        if orig.file:
            orig.file.open("rb")
            msg.file.save(os.path.basename(orig.file.name), ContentFile(orig.file.read()), save=False)
            orig.file.close()
        if orig.thumbnail:
            orig.thumbnail.open("rb")
            msg.thumbnail.save(os.path.basename(orig.thumbnail.name), ContentFile(orig.thumbnail.read()), save=False)
            orig.thumbnail.close()
        msg.save()
        chat.last_activity = msg.created_at
        chat.save(update_fields=["last_activity"])
        MessageReceipt.objects.get_or_create(message=msg, user=receiver)
        payload = MessageSerializer(
            Message.objects.select_related("sender", "forwarded_from", "forwarded_from__sender")
            .prefetch_related("reactions", "statuses", "deleted_for_users")
            .get(id=msg.id),
            context={"request": request},
        ).data
        broadcast_socket_event(receiver.id, "chat_message", payload)
        broadcast_socket_event(request.user.id, "chat_message", payload)
        return Response(payload, status=status.HTTP_201_CREATED)


class DeleteChat(APIView):
    """
    POST /api/delete-chat/
    Auth: bearer
    Request: {"chat_id": 1}
    Response: {"status": "chat_deleted"}
    Errors: 403 unauthorized
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        chat = get_object_or_404(Chat, id=request.data.get("chat_id"))
        if not chat.has_participant(request.user):
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
        if chat.user1_id == request.user.id:
            chat.deleted_for_user1 = True
            chat.deleted_at_user1 = timezone.now()
            update_fields = ["deleted_for_user1", "deleted_at_user1"]
        else:
            chat.deleted_for_user2 = True
            chat.deleted_at_user2 = timezone.now()
            update_fields = ["deleted_for_user2", "deleted_at_user2"]
        chat.save(update_fields=update_fields)
        if chat.deleted_for_user1 and chat.deleted_for_user2:
            chat.delete()
        return Response({"status": "chat_deleted", "success": True})


class ReactMessage(APIView):
    """
    POST /api/react/
    Auth: bearer
    Request: {"message_id": 1, "emoji": "👍"}
    Response: {"status": "reacted", "emoji": "👍"}
    Errors: 403 unauthorized
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ReactSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        msg = get_object_or_404(Message.objects.select_related("chat", "chat__user1", "chat__user2"), id=serializer.validated_data["message_id"])
        if not msg.chat.has_participant(request.user):
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
        emoji = serializer.validated_data["emoji"]
        existing = MessageReaction.objects.filter(user=request.user, message=msg).first()
        action = "add"
        if existing and existing.emoji == emoji:
            existing.delete()
            action = "remove"
        else:
            react, _ = MessageReaction.objects.get_or_create(user=request.user, message=msg)
            react.emoji = emoji
            react.save(update_fields=["emoji"])
        payload = {
            "message_id": str(msg.id),
            "chat_id": msg.chat_id,
            "emoji": emoji,
            "user_id": str(request.user.id),
            "action": action,
        }
        broadcast_socket_event(msg.chat.user1_id, "reaction_update", payload)
        broadcast_socket_event(msg.chat.user2_id, "reaction_update", payload)
        return Response({"status": "reacted", "emoji": emoji, "action": action})


class TypingIndicator(APIView):
    """
    POST /api/typing/
    Auth: bearer
    Request: {"chat_id": 1, "is_typing": true}
    Response: {"ok": true}
    Errors: 403 unauthorized
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = TypingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        chat = get_object_or_404(Chat, id=serializer.validated_data["chat_id"])
        if not chat.has_participant(request.user):
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
        receiver = chat.get_receiver(request.user)
        broadcast_socket_event(
            receiver.id,
            "typing",
            {
                "chat_id": chat.id,
                "user_id": request.user.id,
                "is_typing": serializer.validated_data["is_typing"],
            },
        )
        return Response({"ok": True})


class UserChats(APIView):
    """
    GET /api/chats/
    Auth: bearer
    Response: paginated chat list
    Errors: 401 unauthorized
    """

    permission_classes = [IsAuthenticated]
    pagination_class = ChatListPagination

    def get(self, request):
        last_message_queryset = Message.objects.filter(chat=OuterRef("pk")).order_by("-created_at")
        my_user_id = request.user.id
        chats = (
            Chat.objects.filter(Q(user1=request.user) | Q(user2=request.user))
            .exclude(Q(user1=request.user, deleted_for_user1=True) | Q(user2=request.user, deleted_for_user2=True))
            .select_related("user1", "user2")
            .prefetch_related("messages")
            .annotate(
                last_message_id=Subquery(last_message_queryset.values("id")[:1]),
                last_message_content=Subquery(last_message_queryset.values("encrypted_text")[:1]),
                last_message_sender_id=Subquery(last_message_queryset.values("sender_id")[:1]),
                last_message_created_at=Subquery(last_message_queryset.values("created_at")[:1]),
                last_message_type=Subquery(last_message_queryset.values("message_type")[:1]),
                last_message_file=Subquery(last_message_queryset.values("file")[:1]),
                last_message_status=Subquery(last_message_queryset.values("status")[:1]),
                unread_count=Count(
                    "messages__statuses",
                    filter=Q(messages__statuses__user=request.user, messages__statuses__read_at__isnull=True)
                    & ~Q(messages__sender=request.user),
                    distinct=True,
                ),
                other_user_id=Case(When(user1_id=my_user_id, then=F("user2_id")), default=F("user1_id")),
            )
            .order_by("-last_activity")
        )
        serialized_chats = []
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(chats, request)
        for chat in page:
            setattr(chat, "other_user_online", cache.get(f"presence:{chat.other_user_id}") == "online")
            serialized_chats.append(chat)
        serializer = ChatSerializer(serialized_chats, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)
