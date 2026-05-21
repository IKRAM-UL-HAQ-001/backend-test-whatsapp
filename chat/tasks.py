from celery import shared_task

from .utils import send_push_notification
from .utils import get_firebase_app
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache
from firebase_admin import messaging
from django.utils import timezone


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


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=2, retry_kwargs={"max_retries": 3})
def send_push_notification_task(self, receiver_id, title, message, data=None):
    from users.models import User

    receiver = User.objects.filter(id=receiver_id).first()
    if not receiver:
        return False
    return send_push_notification(receiver, title, message, data=data)


@shared_task(bind=True, max_retries=3)
def send_message_notification(self, message_id, recipient_id):
    try:
        from chat.models import Message, MessageReceipt, MessageStatus
        from users.models import User

        recipient = User.objects.get(id=recipient_id)
        if not recipient.fcm_token:
            return "No FCM token for recipient"

        if get_firebase_app() is None:
            return "Firebase is not configured"

        message = (
            Message.objects.select_related("sender", "chat")
            .get(id=message_id)
        )
        sent_key = f"push-sent:message:{message.id}:recipient:{recipient.id}"
        lock_key = f"push-lock:message:{message.id}:recipient:{recipient.id}"
        if cache.get(sent_key):
            return "Push already sent for message"
        if not cache.add(lock_key, "1", timeout=300):
            return "Push send already in progress"

        if message.message_type == "text":
            body = message.encrypted_text or ""
            if len(body) > 100:
                body = f"{body[:97]}..."
        elif message.message_type == "image":
            body = "Photo"
        elif message.message_type == "audio":
            body = "Voice message"
        elif message.message_type == "video":
            body = "Video"
        elif message.message_type == "document":
            body = f"{message.file_name or 'Document'}"
        else:
            body = "New message"

        sender_name = message.sender.name or "Someone"

        fcm_message = messaging.Message(
            data={
                "title": sender_name,
                "body": body,
                "chat_id": str(message.chat.id),
                "sender_id": str(message.sender.id),
                "message_id": str(message.id),
                "message_kind": message.message_type,
                "sender_name": sender_name,
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
            },
            android=messaging.AndroidConfig(
                priority="high",
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        alert=messaging.ApsAlert(
                            title=sender_name,
                            body=body,
                        ),
                        sound="default",
                        badge=1,
                    ),
                ),
            ),
            token=recipient.fcm_token,
        )

        messaging.send(fcm_message)
        cache.set(sent_key, "1", timeout=60 * 60 * 24 * 7)
        cache.delete(lock_key)

        now = timezone.now()
        updated = Message.objects.filter(
            id=message.id,
            status=MessageStatus.SENT,
        ).update(status=MessageStatus.DELIVERED, delivered_at=now)
        if updated:
            MessageReceipt.objects.filter(message=message, user=recipient).update(
                delivered_at=now,
            )
            broadcast_socket_event(
                message.sender_id,
                "status_update",
                {
                    "message_ids": [str(message.id)],
                    "chat_id": message.chat_id,
                    "status": MessageStatus.DELIVERED,
                    "delivered_at": now.isoformat(),
                },
            )
        return f"Notification sent to {recipient.phone_number}"

    except Exception as exc:
        try:
            cache.delete(f"push-lock:message:{message_id}:recipient:{recipient_id}")
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
