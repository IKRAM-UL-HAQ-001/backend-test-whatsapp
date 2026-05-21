from celery import shared_task

from .notifications import send_incoming_call_push, send_missed_call_push


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
