from celery import shared_task

from .utils import send_push_notification
from .utils import get_firebase_app
from firebase_admin import messaging


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
        from chat.models import Message
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
            notification=messaging.Notification(
                title=sender_name,
                body=body,
            ),
            data={
                "chat_id": str(message.chat.id),
                "sender_id": str(message.sender.id),
                "message_id": str(message.id),
                "message_type": message.message_type,
                "sender_name": sender_name,
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
            },
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="m2m_messages",
                    priority="high",
                    default_sound=True,
                    default_vibrate_timings=True,
                    color="#6B00D7",
                ),
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
        return f"Notification sent to {recipient.phone_number}"

    except Exception as exc:
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
