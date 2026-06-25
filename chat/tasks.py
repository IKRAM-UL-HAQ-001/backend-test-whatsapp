import logging
from datetime import timedelta

from celery import shared_task

from .utils import send_push_notification
from .utils import get_firebase_app
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache
from firebase_admin import messaging
from django.utils import timezone


logger = logging.getLogger(__name__)


def _is_invalid_token_error(exc):
    unregistered_error = getattr(messaging, "UnregisteredError", None)
    if unregistered_error is not None and isinstance(exc, unregistered_error):
        return True
    error_code = str(getattr(exc, "code", "") or getattr(exc, "error_code", "")).lower()
    message = str(exc).lower()
    return (
        "unregistered" in error_code
        or "registration-token-not-registered" in message
        or "requested entity was not found" in message
    )


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

        logger.info(
            "push_task_started kind=message message_id=%s recipient_id=%s started_at=%s task_id=%s",
            message_id,
            recipient_id,
            timezone.now().isoformat(),
            getattr(self.request, "id", None),
        )
        recipient = User.objects.get(id=recipient_id)
        if not recipient.fcm_token:
            logger.info(
                "fcm_token_missing kind=message message_id=%s recipient_id=%s at=%s",
                message_id,
                recipient_id,
                timezone.now().isoformat(),
            )
            return "No FCM token for recipient"

        if get_firebase_app() is None:
            logger.warning(
                "firebase_send_skipped kind=message message_id=%s recipient_id=%s reason=not_configured at=%s",
                message_id,
                recipient_id,
                timezone.now().isoformat(),
            )
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

        # Android: send DATA-ONLY (no `notification` block) so the app itself
        # renders the message notification via the FCM background/foreground
        # handler. That lets the app track each notification per-chat and clear
        # it from the tray the moment the chat is opened (WhatsApp behaviour).
        # iOS keeps the APNS alert below so it still displays when terminated.
        fcm_message = messaging.Message(
            data={
                "type": "message",
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
                ttl=timedelta(minutes=5),
            ),
            apns=messaging.APNSConfig(
                headers={
                    "apns-priority": "10",
                    "apns-push-type": "alert",
                },
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

        logger.info(
            "firebase_send_called kind=message message_id=%s recipient_id=%s at=%s",
            message.id,
            recipient.id,
            timezone.now().isoformat(),
        )
        try:
            messaging.send(fcm_message)
        except Exception as exc:
            cache.delete(lock_key)
            if _is_invalid_token_error(exc):
                recipient.fcm_token = None
                recipient.save(update_fields=["fcm_token"])
                logger.warning(
                    "fcm_token_invalid kind=message message_id=%s recipient_id=%s cleared_at=%s",
                    message.id,
                    recipient.id,
                    timezone.now().isoformat(),
                )
                return "Invalid FCM token cleared"
            logger.warning(
                "firebase_send_failed kind=message message_id=%s recipient_id=%s at=%s error=%s",
                message.id,
                recipient.id,
                timezone.now().isoformat(),
                exc,
            )
            raise
        logger.info(
            "firebase_send_success kind=message message_id=%s recipient_id=%s at=%s",
            message.id,
            recipient.id,
            timezone.now().isoformat(),
        )
        cache.set(sent_key, "1", timeout=60 * 60 * 24 * 7)
        cache.delete(lock_key)

        # Notification messages displayed by Android/iOS while the Flutter app
        # is backgrounded or terminated do not reliably execute Dart code. A
        # successful provider handoff is therefore the delivery acknowledgement
        # for push recipients. The guarded SENT update keeps the lifecycle
        # monotonic and can never downgrade an already-read message.
        now = timezone.now()
        updated = Message.objects.filter(
            id=message.id,
            status=MessageStatus.SENT,
        ).update(status=MessageStatus.DELIVERED, delivered_at=now)
        if updated:
            MessageReceipt.objects.filter(
                message=message,
                user=recipient,
            ).update(delivered_at=now)
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
