from datetime import timedelta

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

try:
    from livekit import api as livekit_api
except ImportError:
    livekit_api = None


def _require_livekit_config():
    missing = [
        name
        for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
        if not getattr(settings, name, "")
    ]
    if missing:
        raise ImproperlyConfigured(
            f"LiveKit is not configured. Missing: {', '.join(missing)}."
        )
    if livekit_api is None:
        raise ImproperlyConfigured(
            "The livekit-api package is required for LiveKit token generation."
        )


def user_display_name(user):
    return getattr(user, "name", "") or getattr(user, "phone_number", "") or str(user.id)


def generate_join_token(user, call):
    _require_livekit_config()

    return (
        livekit_api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(str(user.id))
        .with_name(user_display_name(user))
        .with_grants(
            livekit_api.VideoGrants(room_join=True, room=call.room_name)
        )
        .with_ttl(timedelta(minutes=settings.LIVEKIT_TOKEN_TTL_MINUTES))
        .to_jwt()
    )


async def _delete_room_async(room_name):
    _require_livekit_config()
    client = livekit_api.LiveKitAPI(
        settings.LIVEKIT_URL,
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
    )
    try:
        await client.room.delete_room(livekit_api.DeleteRoomRequest(room=room_name))
    finally:
        await client.aclose()


def delete_room(room_name):
    if not room_name:
        return
    async_to_sync(_delete_room_async)(room_name)
