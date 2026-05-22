from datetime import timedelta
import logging

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

try:
    from livekit import api as livekit_api
except ImportError:
    livekit_api = None

logger = logging.getLogger(__name__)


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


def livekit_identity(user):
    return f"user_{user.id}"


def generate_join_token(user, call):
    _require_livekit_config()

    return (
        livekit_api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(livekit_identity(user))
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


def is_room_not_found_error(exc):
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if callable(code):
        code = code()
    if callable(status):
        status = status()

    message = str(exc).lower()
    return (
        str(code).lower() == "not_found"
        or str(status) == "404"
        or ("code=not_found" in message and "status=404" in message)
    )


async def _check_room_state_async(room_name):
    _require_livekit_config()
    client = livekit_api.LiveKitAPI(
        settings.LIVEKIT_URL,
        settings.LIVEKIT_API_KEY,
        settings.LIVEKIT_API_SECRET,
    )
    try:
        req = livekit_api.ListRoomsRequest(names=[room_name])
        res = await client.room.list_rooms(req)
        for room in res.rooms:
            if room.name == room_name:
                return True, room.num_participants
        return False, 0
    finally:
        await client.aclose()


def check_room_state(room_name):
    if not room_name:
        return False, 0, True
    try:
        exists, participant_count = async_to_sync(_check_room_state_async)(room_name)
        return exists, participant_count, True
    except ImproperlyConfigured as exc:
        logger.warning("LiveKit configuration is missing or incomplete: %s", exc)
        return False, 0, False
    except Exception as exc:
        if is_room_not_found_error(exc):
            logger.info("LiveKit room not found for room=%s", room_name)
            return False, 0, True
        logger.warning("LiveKit API error checking room state for room=%s: %s", room_name, exc)
        return False, 0, False
