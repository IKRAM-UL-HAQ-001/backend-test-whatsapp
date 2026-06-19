from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


CALL_EVENTS = {
    "call_invite",
    "call_ringing",
    "call_accepted",
    "call_rejected",
    "call_cancelled",
    "call_ended",
    "call_missed",
    "call_busy",
    "call_failed",
    "call_video_enabled",
    "call_video_disabled",
}


def _participant_payload(user):
    profile_picture = None
    if user.profile_picture and hasattr(user.profile_picture, "url"):
        profile_picture = user.profile_picture.url
    return {
        "id": user.id,
        "name": user.name,
        "profile_picture": profile_picture,
    }


def serialize_call(call):
    return {
        "id": call.id,
        "call_type": call.call_type,
        "status": call.status,
        "room_name": call.room_name,
        "caller": _participant_payload(call.caller),
        "receiver": _participant_payload(call.receiver),
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "accepted_at": call.accepted_at.isoformat() if call.accepted_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "duration_seconds": call.duration_seconds,
    }


def call_event_payload(event_name, call):
    return {
        "type": event_name,
        "call": serialize_call(call),
    }


def send_call_event(user_id, event_name, call):
    if event_name not in CALL_EVENTS:
        raise ValueError(f"Unsupported call event: {event_name}")
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(
        f"user_{user_id}",
        {
            "type": f"{event_name}_event",
            "payload": call_event_payload(event_name, call),
        },
    )
