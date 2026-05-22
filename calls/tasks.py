import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .notifications import send_incoming_call_push, send_missed_call_push
from .realtime import send_call_event


logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def send_incoming_call_notification(self, call_id):
    try:
        from .models import CallSession

        call = (
            CallSession.objects.select_related("caller", "receiver")
            .filter(id=call_id)
            .first()
        )
        if call is None:
            return "Call not found"
        return send_incoming_call_push(call)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)


@shared_task(bind=True, max_retries=3)
def send_missed_call_notification(self, call_id):
    try:
        from .models import CallSession

        call = (
            CallSession.objects.select_related("caller", "receiver")
            .filter(id=call_id)
            .first()
        )
        if call is None:
            return "Call not found"
        return send_missed_call_push(call)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)


@shared_task
def mark_call_missed_if_unanswered(call_id):
    from .models import CallSession

    with transaction.atomic():
        call = (
            CallSession.objects.select_for_update()
            .select_related("caller", "receiver")
            .filter(id=call_id)
            .first()
        )
        if call is None:
            return "Call not found"
        if call.status not in {CallSession.Status.INITIATED, CallSession.Status.RINGING}:
            return f"Call already {call.status}"

        call.status = CallSession.Status.MISSED
        call.ended_at = timezone.now()
        call.duration_seconds = 0
        call.ended_by = None
        call.save(update_fields=["status", "ended_at", "duration_seconds", "ended_by", "updated_at"])

    try:
        send_call_event(call.caller_id, "call_missed", call)
        send_call_event(call.receiver_id, "call_missed", call)
    except Exception as exc:
        logger.warning("Failed to emit missed call event for call_id=%s: %s", call.id, exc)
    try:
        send_missed_call_notification.delay(call.id)
    except Exception as exc:
        logger.warning("Failed to queue missed call notification for call_id=%s: %s", call.id, exc)
        return "Call marked missed; missed push queue failed"
    return "Call marked missed"
