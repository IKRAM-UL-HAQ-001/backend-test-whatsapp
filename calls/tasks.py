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

        logger.info(
            "push_task_started kind=incoming_call call_id=%s started_at=%s task_id=%s",
            call_id,
            timezone.now().isoformat(),
            getattr(self.request, "id", None),
        )
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

        logger.info(
            "push_task_started kind=missed_call call_id=%s started_at=%s task_id=%s",
            call_id,
            timezone.now().isoformat(),
            getattr(self.request, "id", None),
        )
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
        logger.info(
            "push_task_queued kind=missed_call call_id=%s queued_at=%s queue=default",
            call.id,
            timezone.now().isoformat(),
        )
        send_missed_call_notification.apply_async(
            (call.id,),
            queue="default",
            priority=5,
        )
    except Exception as exc:
        logger.warning("Failed to queue missed call notification for call_id=%s: %s", call.id, exc)
        return "Call marked missed; missed push queue failed"
    return "Call marked missed"


def cleanup_stale_active_calls(user_ids=None):
    """
    Find accepted or active calls older than ACTIVE_CALL_STALE_TIMEOUT_SECONDS and clean them up.
    Checks if updated_at is older than the timeout, and deletes the Chime meeting.
    """
    from django.conf import settings
    from django.utils import timezone
    from django.db.models import Q
    from django.core.exceptions import ImproperlyConfigured
    from .models import CallSession
    from .realtime import send_call_event

    now = timezone.now()
    timeout_seconds = getattr(settings, "ACTIVE_CALL_STALE_TIMEOUT_SECONDS", 180)
    cutoff_time = now - timezone.timedelta(seconds=timeout_seconds)

    query = Q(status__in=[CallSession.Status.ACCEPTED, CallSession.Status.ACTIVE])
    
    if user_ids:
        query &= (Q(caller_id__in=user_ids) | Q(receiver_id__in=user_ids))

    query &= (Q(accepted_at__lt=cutoff_time) | Q(accepted_at__isnull=True, created_at__lt=cutoff_time))

    candidates = CallSession.objects.filter(query)
    cleaned_count = 0

    for call in candidates:
        if call.updated_at >= cutoff_time:
            continue

        with transaction.atomic():
            call_locked = CallSession.objects.select_for_update().filter(id=call.id).first()
            if not call_locked or call_locked.status not in [CallSession.Status.ACCEPTED, CallSession.Status.ACTIVE]:
                continue

            call_locked.status = CallSession.Status.ENDED
            call_locked.ended_at = now
            if call_locked.accepted_at:
                call_locked.duration_seconds = max(0, int((now - call_locked.accepted_at).total_seconds()))
            else:
                call_locked.duration_seconds = call_locked.calculate_duration()
            call_locked.ended_by = None
            call_locked.save(update_fields=["status", "ended_at", "duration_seconds", "ended_by", "updated_at"])
            call = call_locked

        try:
            send_call_event(call.caller_id, "call_ended", call)
            send_call_event(call.receiver_id, "call_ended", call)
        except Exception as exc:
            logger.warning("Failed to emit end call events for stale call_id=%s: %s", call.id, exc)

        try:
            from .chime import delete_meeting
            delete_meeting(call)
        except Exception as exc:
            logger.warning("Chime meeting cleanup failed for stale call_id=%s: %s", call.id, exc)

        cleaned_count += 1

    return cleaned_count


@shared_task
def cleanup_stale_active_calls_task():
    try:
        count = cleanup_stale_active_calls()
        return f"Cleaned up {count} stale active calls"
    except Exception as exc:
        logger.exception("Error in cleanup_stale_active_calls_task: %s", exc)
        raise
