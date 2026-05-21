import logging

from firebase_admin import messaging

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
    if not receiver.fcm_token:
        return "No FCM token for recipient"

    if get_firebase_app() is None:
        return "Firebase is not configured"

    fcm_message = messaging.Message(
        data={str(key): str(value) for key, value in data.items()},
        android=messaging.AndroidConfig(priority="high"),
        apns=messaging.APNSConfig(
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
        messaging.send(fcm_message)
        return "Notification sent"
    except Exception as exc:
        if _is_invalid_token_error(exc):
            _clear_invalid_token(receiver)
            logger.warning("Cleared invalid FCM token for user_id=%s", receiver.id)
            return "Invalid FCM token cleared"
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
