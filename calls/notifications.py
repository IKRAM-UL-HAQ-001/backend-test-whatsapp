import logging
from datetime import timedelta

from firebase_admin import messaging
from django.utils import timezone

from chat.utils import get_firebase_app


logger = logging.getLogger(__name__)


def _profile_picture_url(user):
    if user.profile_picture and hasattr(user.profile_picture, "url"):
        return user.profile_picture.url
    return ""


def incoming_call_payload(call):
    caller_name = call.caller.name or call.caller.phone_number
    return {
        "type": "incoming_call",
        "call_id": str(call.id),
        "caller_id": str(call.caller_id),
        "caller_name": caller_name,
        "caller_profile_picture": _profile_picture_url(call.caller),
        "call_type": call.call_type,
        "room_name": call.room_name,
    }


def missed_call_payload(call):
    caller_name = call.caller.name or call.caller.phone_number
    return {
        "type": "missed_call",
        "call_id": str(call.id),
        "caller_id": str(call.caller_id),
        "caller_name": caller_name,
        "call_type": call.call_type,
    }


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


def _clear_invalid_token(user):
    user.fcm_token = None
    user.save(update_fields=["fcm_token"])


def send_call_push_notification(receiver, title, body, data):
    call_id = data.get("call_id")
    kind = data.get("type", "call")
    if not receiver.fcm_token:
        logger.info(
            "fcm_token_missing kind=%s call_id=%s recipient_id=%s at=%s",
            kind,
            call_id,
            receiver.id,
            timezone.now().isoformat(),
        )
        return "No FCM token for recipient"

    if get_firebase_app() is None:
        logger.warning(
            "firebase_send_skipped kind=%s call_id=%s recipient_id=%s reason=not_configured at=%s",
            kind,
            call_id,
            receiver.id,
            timezone.now().isoformat(),
        )
        return "Firebase is not configured"

    fcm_message = messaging.Message(
        data={str(key): str(value) for key, value in data.items()},
        android=messaging.AndroidConfig(
            priority="high",
            ttl=timedelta(seconds=30) if kind == "incoming_call" else timedelta(minutes=5),
        ),
        apns=messaging.APNSConfig(
            headers={"apns-priority": "10"},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(title=title, body=body),
                    sound="default",
                    badge=1,
                ),
            ),
        ),
        token=receiver.fcm_token,
    )

    try:
        logger.info(
            "firebase_send_called kind=%s call_id=%s recipient_id=%s at=%s",
            kind,
            call_id,
            receiver.id,
            timezone.now().isoformat(),
        )
        messaging.send(fcm_message)
        logger.info(
            "firebase_send_success kind=%s call_id=%s recipient_id=%s at=%s",
            kind,
            call_id,
            receiver.id,
            timezone.now().isoformat(),
        )
        return "Notification sent"
    except Exception as exc:
        if _is_invalid_token_error(exc):
            _clear_invalid_token(receiver)
            logger.warning(
                "fcm_token_invalid kind=%s call_id=%s recipient_id=%s cleared_at=%s",
                kind,
                call_id,
                receiver.id,
                timezone.now().isoformat(),
            )
            return "Invalid FCM token cleared"
        logger.warning(
            "firebase_send_failed kind=%s call_id=%s recipient_id=%s at=%s error=%s",
            kind,
            call_id,
            receiver.id,
            timezone.now().isoformat(),
            exc,
        )
        raise


def send_incoming_call_push(call):
    call_type_label = "Video" if call.call_type == "video" else "Audio"
    caller_name = call.caller.name or call.caller.phone_number
    return send_call_push_notification(
        call.receiver,
        f"Incoming {call_type_label.lower()} call",
        f"{caller_name} is calling you",
        incoming_call_payload(call),
    )


def send_missed_call_push(call):
    call_type_label = "Video" if call.call_type == "video" else "Audio"
    caller_name = call.caller.name or call.caller.phone_number
    return send_call_push_notification(
        call.receiver,
        f"Missed {call_type_label.lower()} call",
        f"Missed call from {caller_name}",
        missed_call_payload(call),
    )
