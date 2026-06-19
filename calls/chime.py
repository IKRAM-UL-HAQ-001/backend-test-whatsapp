"""
Amazon Chime SDK Meetings integration for the calls app.

Uses the EC2 IAM role for AWS credentials — no access keys in code or .env.
"""
import logging
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

logger = logging.getLogger(__name__)


class ChimeError(Exception):
    """Wraps any Chime SDK API error with a clean message."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _get_client():
    return boto3.client(
        "chime-sdk-meetings",
        region_name=getattr(settings, "AWS_REGION", "ap-south-1"),
    )


def _external_meeting_id(call):
    return f"call_{call.id}"


def _external_user_id(call, user):
    return f"user_{user.id}_call_{call.id}"


# ── Meeting lifecycle ──────────────────────────────────────────────────


def create_meeting(call):
    """Create a Chime meeting and return the full Meeting dict."""
    try:
        client = _get_client()
        response = client.create_meeting(
            ClientRequestToken=str(uuid.uuid4()),
            MediaRegion=getattr(settings, "CHIME_MEDIA_REGION", "ap-south-1"),
            ExternalMeetingId=_external_meeting_id(call),
        )
        meeting = response["Meeting"]
        logger.info(
            "chime_meeting_created call_id=%s meeting_id=%s region=%s",
            call.id,
            meeting["MeetingId"],
            meeting.get("MediaRegion"),
        )
        return meeting
    except (BotoCoreError, ClientError) as exc:
        logger.error("chime_create_meeting_failed call_id=%s error=%s", call.id, exc)
        raise ChimeError(f"Failed to create Chime meeting: {exc}", code="chime_api_error") from exc


def create_attendee(call, user):
    """Create a Chime attendee for *user* in the meeting stored on *call*.

    Returns the full Attendee dict (contains JoinToken).
    """
    if not call.chime_meeting_id:
        raise ChimeError("Call has no Chime meeting", code="no_meeting")
    try:
        client = _get_client()
        response = client.create_attendee(
            MeetingId=call.chime_meeting_id,
            ExternalUserId=_external_user_id(call, user),
        )
        attendee = response["Attendee"]
        # Never log JoinToken
        logger.info(
            "chime_attendee_created call_id=%s user_id=%s attendee_id=%s",
            call.id,
            user.id,
            attendee["AttendeeId"],
        )
        return attendee
    except (BotoCoreError, ClientError) as exc:
        logger.error(
            "chime_create_attendee_failed call_id=%s user_id=%s error=%s",
            call.id,
            user.id,
            exc,
        )
        raise ChimeError(f"Failed to create Chime attendee: {exc}", code="chime_api_error") from exc


def get_or_create_attendee(call, user):
    """Return an existing CallAttendee for this (call, user) or create one via the Chime API.

    Returns a ``CallAttendee`` model instance.
    """
    from .models import CallAttendee

    existing = CallAttendee.objects.filter(call=call, user=user).first()
    if existing:
        return existing

    attendee_data = create_attendee(call, user)
    return CallAttendee.objects.create(
        call=call,
        user=user,
        chime_attendee_id=attendee_data["AttendeeId"],
        chime_external_user_id=_external_user_id(call, user),
        chime_join_token=attendee_data["JoinToken"],
    )


# ── High-level orchestration ──────────────────────────────────────────


def provision_call(call):
    """Idempotently create a Chime meeting + attendees for both participants.

    Called when a call is accepted.  Safe to call multiple times.
    """
    from .models import CallAttendee

    if not call.chime_meeting_id:
        meeting = create_meeting(call)
        call.chime_meeting_id = meeting["MeetingId"]
        call.chime_media_region = meeting.get("MediaRegion", "")
        call.chime_external_meeting_id = _external_meeting_id(call)
        call.chime_meeting_data = meeting
        call.save(update_fields=[
            "chime_meeting_id",
            "chime_media_region",
            "chime_external_meeting_id",
            "chime_meeting_data",
            "updated_at",
        ])

    get_or_create_attendee(call, call.caller)
    get_or_create_attendee(call, call.receiver)


def build_join_response(call, user):
    """Build the /join/ response payload for a Chime-backed call.

    Returns only the requesting user's own attendee data — never another
    user's JoinToken.
    """
    from .models import CallAttendee

    attendee_obj = CallAttendee.objects.filter(call=call, user=user).first()
    if not attendee_obj:
        raise ChimeError("Attendee not found; call may not be provisioned yet", code="attendee_missing")

    meeting_data = call.chime_meeting_data or {}

    return {
        "call_id": call.id,
        "provider": "chime",
        "meeting": {
            "MeetingId": call.chime_meeting_id,
            "MediaRegion": call.chime_media_region or "",
            "MediaPlacement": meeting_data.get("MediaPlacement", {}),
        },
        "attendee": {
            "AttendeeId": attendee_obj.chime_attendee_id,
            "ExternalUserId": attendee_obj.chime_external_user_id,
            "JoinToken": attendee_obj.chime_join_token,
        },
    }


def delete_meeting(call):
    """Delete the Chime meeting if one exists.  Idempotent — ignores 'not found'."""
    if not call.chime_meeting_id:
        return
    try:
        client = _get_client()
        client.delete_meeting(MeetingId=call.chime_meeting_id)
        logger.info("chime_meeting_deleted call_id=%s meeting_id=%s", call.id, call.chime_meeting_id)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "NotFoundException":
            logger.info("chime_meeting_already_deleted call_id=%s meeting_id=%s", call.id, call.chime_meeting_id)
            return
        logger.error("chime_delete_meeting_failed call_id=%s error=%s", call.id, exc)
        raise ChimeError(f"Failed to delete Chime meeting: {exc}", code="chime_api_error") from exc
    except BotoCoreError as exc:
        logger.error("chime_delete_meeting_failed call_id=%s error=%s", call.id, exc)
        raise ChimeError(f"Failed to delete Chime meeting: {exc}", code="chime_api_error") from exc
