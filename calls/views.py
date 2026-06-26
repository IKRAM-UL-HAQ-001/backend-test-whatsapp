import uuid
import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Case, IntegerField, Q, When
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .models import CallSession
from .realtime import send_call_event
from .serializers import CallSessionSerializer, StartCallSerializer
from .tasks import mark_call_missed_if_unanswered, send_incoming_call_notification


logger = logging.getLogger(__name__)


ACTIVE_PROGRESS_STATUSES = [
    CallSession.Status.INITIATED,
    CallSession.Status.RINGING,
    CallSession.Status.ACCEPTED,
    CallSession.Status.ACTIVE,
]

TERMINAL_STATUSES = [
    CallSession.Status.REJECTED,
    CallSession.Status.CANCELLED,
    CallSession.Status.MISSED,
    CallSession.Status.ENDED,
    CallSession.Status.FAILED,
    CallSession.Status.BUSY,
]


def user_call_queryset(user):
    return CallSession.objects.filter(Q(caller=user) | Q(receiver=user))


def current_call_queryset(user):
    return (
        user_call_queryset(user)
        .filter(status__in=ACTIVE_PROGRESS_STATUSES)
        .select_related("caller", "receiver", "ended_by")
        .annotate(
            current_priority=Case(
                When(status__in=[CallSession.Status.ACCEPTED, CallSession.Status.ACTIVE], then=0),
                When(status=CallSession.Status.RINGING, then=1),
                When(status=CallSession.Status.INITIATED, then=2),
                default=3,
                output_field=IntegerField(),
            )
        )
        .order_by("current_priority", "-updated_at")
    )


def lock_users(*user_ids):
    User = get_user_model()
    return list(
        User.objects.select_for_update()
        .filter(id__in=sorted(set(user_ids)))
        .order_by("id")
    )


def non_terminal_calls_for_users(*users):
    query = Q()
    for user in users:
        query |= Q(caller=user) | Q(receiver=user)
    return CallSession.objects.select_for_update().filter(query, status__in=ACTIVE_PROGRESS_STATUSES)


def queue_incoming_call_notification(call_id):
    queued_at = timezone.now()
    logger.info(
        "push_task_queued kind=incoming_call call_id=%s queued_at=%s queue=default",
        call_id,
        queued_at.isoformat(),
    )

    def enqueue():
        try:
            send_incoming_call_notification.apply_async(
                (call_id,),
                queue="default",
                priority=9,
            )
        except Exception as exc:
            logger.warning("Failed to queue incoming call notification for call_id=%s: %s", call_id, exc)

    try:
        transaction.on_commit(enqueue)
    except Exception as exc:
        logger.warning("Failed to queue incoming call notification for call_id=%s: %s", call_id, exc)


def queue_missed_call_timeout(call_id):
    try:
        mark_call_missed_if_unanswered.apply_async(
            (call_id,),
            countdown=settings.CALL_RING_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("Failed to queue missed call timeout for call_id=%s: %s", call_id, exc)


def cleanup_provider_resources(call):
    """Clean up the Chime meeting resources."""
    try:
        from .chime import delete_meeting, ChimeError
        delete_meeting(call)
    except Exception as exc:
        logger.warning("Chime meeting cleanup failed for call_id=%s: %s", call.id, exc)



class CallHistoryPagination(LimitOffsetPagination):
    default_limit = 20
    max_limit = 50


class StartCall(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = StartCallSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        receiver = serializer.context["receiver"]

        try:
            from .tasks import cleanup_stale_active_calls
            cleanup_stale_active_calls(user_ids=[request.user.id, receiver.id])
        except Exception as exc:
            logger.warning("Lightweight stale call cleanup failed during start call: %s", exc)

        with transaction.atomic():
            lock_users(request.user.id, receiver.id)

            active_calls = non_terminal_calls_for_users(request.user, receiver)
            if active_calls.filter(Q(caller=request.user) | Q(receiver=request.user)).exists():
                return Response(
                    {"detail": "Caller is busy", "code": "caller_busy"},
                    status=status.HTTP_409_CONFLICT,
                )
            if active_calls.filter(Q(caller=receiver) | Q(receiver=receiver)).exists():
                return Response(
                    {"detail": "User is busy", "code": "user_busy"},
                    status=status.HTTP_409_CONFLICT,
                )

            call = CallSession.objects.create(
                caller=request.user,
                receiver=receiver,
                call_type=serializer.validated_data["call_type"],
                status=CallSession.Status.INITIATED,
                room_name=f"pending-call-{uuid.uuid4()}",
                started_at=timezone.now(),
                provider="chime",
            )
            call.room_name = f"call_{call.id}"
            call.save(update_fields=["room_name", "updated_at"])
            logger.info(
                "call_created call_id=%s caller_id=%s receiver_id=%s provider=%s created_at=%s",
                call.id,
                request.user.id,
                receiver.id,
                call.provider,
                call.created_at.isoformat(),
            )

        send_call_event(receiver.id, "call_invite", call)
        # Do NOT mark the call as ringing yet. The caller stays on "Calling..."
        # until the receiver's device confirms it is actually ringing (the
        # ChatConsumer "call_ringing" ack flips the status to RINGING and
        # notifies the caller). If the receiver is unreachable/offline, no ack
        # arrives and the caller never sees "Ringing" — matching WhatsApp.
        queue_incoming_call_notification(call.id)
        queue_missed_call_timeout(call.id)
        return Response(CallSessionSerializer(call, context={"request": request}).data, status=status.HTTP_201_CREATED)


class CallDetail(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, call_id):
        call = get_object_or_404(
            CallSession.objects.select_related("caller", "receiver", "ended_by"),
            id=call_id,
        )
        if request.user.id not in {call.caller_id, call.receiver_id}:
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
        return Response(CallSessionSerializer(call, context={"request": request}).data)


class CurrentCall(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        call = current_call_queryset(request.user).first()
        if call is None:
            return Response({"call": None})
        return Response({"call": CallSessionSerializer(call, context={"request": request}).data})


class CallHistory(APIView):
    permission_classes = [IsAuthenticated]
    pagination_class = CallHistoryPagination

    def get(self, request):
        calls = (
            user_call_queryset(request.user)
            .select_related("caller", "receiver", "ended_by")
            .order_by("-created_at")
        )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(calls, request)
        serializer = CallSessionSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)


class JoinCall(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, call_id):
        call = get_object_or_404(
            CallSession.objects.select_related("caller", "receiver", "ended_by"),
            id=call_id,
        )
        if request.user.id not in {call.caller_id, call.receiver_id}:
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
        if call.status not in {CallSession.Status.ACCEPTED, CallSession.Status.ACTIVE}:
            return Response(
                {"detail": "Call is not joinable", "code": "call_not_joinable"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .chime import build_join_response, ChimeError
        try:
            payload = build_join_response(call, request.user)
        except ChimeError as exc:
            logger.warning("Chime join failed call_id=%s user_id=%s: %s", call.id, request.user.id, exc)
            return Response(
                {"detail": str(exc), "code": exc.code or "chime_error"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(payload)


class CallAction(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, call_id):
        with transaction.atomic():
            call = get_object_or_404(
                CallSession.objects.select_for_update().select_related("caller", "receiver", "ended_by"),
                id=call_id,
            )
            response = self.apply_action(request, call)
            if response is not None:
                return response

        self.emit_events(request, call)
        if call.status in TERMINAL_STATUSES:
            cleanup_provider_resources(call)
        return Response(CallSessionSerializer(call, context={"request": request}).data)

    def apply_action(self, request, call):
        raise NotImplementedError

    def emit_events(self, request, call):
        return None

    def require_receiver(self, request, call):
        if request.user.id != call.receiver_id:
            return Response({"detail": "Only receiver can perform this action."}, status=status.HTTP_403_FORBIDDEN)
        return None

    def require_caller(self, request, call):
        if request.user.id != call.caller_id:
            return Response({"detail": "Only caller can perform this action."}, status=status.HTTP_403_FORBIDDEN)
        return None

    def require_participant(self, request, call):
        if request.user.id not in {call.caller_id, call.receiver_id}:
            return Response({"detail": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
        return None


class CallHeartbeat(APIView):
    """Keep-alive ping from an in-call client.

    Refreshes the call's updated_at so cleanup_stale_active_calls does not reap
    a healthy long-running call. Without this, an active call's row is never
    re-saved (media flows through Chime, not the DB), so updated_at stays frozen
    at accept time and the call is force-ended once it crosses
    ACTIVE_CALL_STALE_TIMEOUT_SECONDS (~3 min). A single bulk UPDATE — no lock,
    no serialization. (QuerySet.update bypasses auto_now, so set it explicitly.)
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, call_id):
        updated = (
            CallSession.objects.filter(id=call_id)
            .filter(Q(caller=request.user) | Q(receiver=request.user))
            .filter(status__in=[CallSession.Status.ACCEPTED, CallSession.Status.ACTIVE])
            .update(updated_at=timezone.now())
        )
        return Response({"ok": bool(updated)})


class RingingCall(CallAction):
    """Receiver's device confirms it is actually ringing.

    Promotes INITIATED -> RINGING and notifies the caller so "Calling..."
    becomes "Ringing...". Idempotent and best-effort: this is the HTTP twin of
    the WebSocket `call_ringing` ack, used when the receiver was reached via
    push (app closed/backgrounded) and has no live socket to ack over.
    """

    def apply_action(self, request, call):
        self._did_ring = False
        permission_error = self.require_receiver(request, call)
        if permission_error:
            return permission_error
        if call.status == CallSession.Status.INITIATED:
            call.status = CallSession.Status.RINGING
            call.save(update_fields=["status", "updated_at"])
            self._did_ring = True
        return None

    def emit_events(self, request, call):
        if getattr(self, "_did_ring", False):
            send_call_event(call.caller_id, "call_ringing", call)


class AcceptCall(CallAction):
    def apply_action(self, request, call):
        permission_error = self.require_receiver(request, call)
        if permission_error:
            return permission_error
        if call.status not in {CallSession.Status.RINGING, CallSession.Status.INITIATED}:
            return Response({"detail": "Only ringing calls can be accepted."}, status=status.HTTP_400_BAD_REQUEST)
        lock_users(call.caller_id, call.receiver_id)
        conflicts = non_terminal_calls_for_users(call.caller, call.receiver).exclude(id=call.id)
        if conflicts.filter(Q(caller=call.receiver) | Q(receiver=call.receiver)).exists():
            return Response(
                {"detail": "Already in another call", "code": "already_in_call"},
                status=status.HTTP_409_CONFLICT,
            )
        if conflicts.filter(Q(caller=call.caller) | Q(receiver=call.caller)).exists():
            return Response(
                {"detail": "User is busy", "code": "user_busy"},
                status=status.HTTP_409_CONFLICT,
            )
        call.status = CallSession.Status.ACCEPTED
        call.accepted_at = timezone.now()
        call.save(update_fields=["status", "accepted_at", "updated_at"])

        try:
            from .chime import provision_call, ChimeError
            provision_call(call)
        except ChimeError as exc:
            logger.error("Chime provisioning failed for call_id=%s: %s", call.id, exc)
            # The call was already promoted to ACCEPTED above. If we leave it
            # there, the caller is stranded on "Connecting..." until the stale
            # reaper ends it (~3 min) and any half-created Chime meeting leaks.
            # Fail the call cleanly instead: mark FAILED, tell BOTH parties
            # (client maps call_failed -> CallState.failed), and release any
            # provider resources. emit_events()/terminal-cleanup in the base
            # CallAction are skipped once we return a response, so do it here.
            call.status = CallSession.Status.FAILED
            call.ended_at = timezone.now()
            call.provider_error_code = exc.code or "chime_provision_error"
            call.save(
                update_fields=["status", "ended_at", "provider_error_code", "updated_at"]
            )
            send_call_event(call.caller_id, "call_failed", call)
            send_call_event(call.receiver_id, "call_failed", call)
            cleanup_provider_resources(call)
            return Response(
                {"detail": f"Call provider error: {exc}", "code": "provider_error"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return None

    def emit_events(self, request, call):
        send_call_event(call.caller_id, "call_accepted", call)
        send_call_event(call.receiver_id, "call_accepted", call)


class RejectCall(CallAction):
    def apply_action(self, request, call):
        permission_error = self.require_receiver(request, call)
        if permission_error:
            return permission_error
        if call.status not in {CallSession.Status.RINGING, CallSession.Status.INITIATED}:
            return Response({"detail": "Only ringing calls can be rejected."}, status=status.HTTP_400_BAD_REQUEST)
        call.status = CallSession.Status.REJECTED
        call.ended_at = timezone.now()
        call.ended_by = request.user
        call.duration_seconds = call.calculate_duration()
        call.save(update_fields=["status", "ended_at", "ended_by", "duration_seconds", "updated_at"])
        return None

    def emit_events(self, request, call):
        send_call_event(call.caller_id, "call_rejected", call)


class CancelCall(CallAction):
    def apply_action(self, request, call):
        permission_error = self.require_caller(request, call)
        if permission_error:
            return permission_error
        if call.status not in {CallSession.Status.INITIATED, CallSession.Status.RINGING}:
            return Response({"detail": "Only initiated or ringing calls can be cancelled."}, status=status.HTTP_400_BAD_REQUEST)
        call.status = CallSession.Status.CANCELLED
        call.ended_at = timezone.now()
        call.ended_by = request.user
        call.duration_seconds = call.calculate_duration()
        call.save(update_fields=["status", "ended_at", "ended_by", "duration_seconds", "updated_at"])
        return None

    def emit_events(self, request, call):
        send_call_event(call.receiver_id, "call_cancelled", call)


class EndCall(CallAction):
    def apply_action(self, request, call):
        permission_error = self.require_participant(request, call)
        if permission_error:
            return permission_error
        if call.status not in {CallSession.Status.ACCEPTED, CallSession.Status.ACTIVE}:
            return Response({"detail": "Only accepted or active calls can be ended."}, status=status.HTTP_400_BAD_REQUEST)
        call.status = CallSession.Status.ENDED
        call.ended_at = timezone.now()
        call.ended_by = request.user
        call.duration_seconds = call.calculate_duration()
        call.save(update_fields=["status", "ended_at", "ended_by", "duration_seconds", "updated_at"])
        return None

    def emit_events(self, request, call):
        send_call_event(call.caller_id, "call_ended", call)
        send_call_event(call.receiver_id, "call_ended", call)
