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

from .livekit import delete_room, generate_join_token, is_room_not_found_error, livekit_identity
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


def cleanup_livekit_room(call):
    try:
        delete_room(call.room_name)
    except ImproperlyConfigured as exc:
        logger.warning("LiveKit room cleanup skipped for call_id=%s: %s", call.id, exc)
    except Exception as exc:
        if is_room_not_found_error(exc):
            logger.info("LiveKit room already absent for call_id=%s room=%s", call.id, call.room_name)
            return
        logger.warning("LiveKit room cleanup failed for call_id=%s: %s", call.id, exc)


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
                status=CallSession.Status.RINGING,
                room_name=f"pending-call-{uuid.uuid4()}",
                started_at=timezone.now(),
            )
            call.room_name = f"call_{call.id}"
            call.save(update_fields=["room_name", "updated_at"])
            logger.info(
                "call_created call_id=%s caller_id=%s receiver_id=%s created_at=%s",
                call.id,
                request.user.id,
                receiver.id,
                call.created_at.isoformat(),
            )

        send_call_event(receiver.id, "call_invite", call)
        send_call_event(request.user.id, "call_ringing", call)
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

        try:
            token = generate_join_token(request.user, call)
        except ImproperlyConfigured as exc:
            logger.warning("LiveKit token generation is unavailable: %s", exc)
            return Response(
                {"detail": "LiveKit is not configured", "code": "livekit_not_configured"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        logger.info(
            "LiveKit join token generated call_id=%s user_id=%s identity=%s room_name=%s server_url=%s token_length=%s",
            call.id,
            request.user.id,
            livekit_identity(request.user),
            call.room_name,
            settings.LIVEKIT_URL,
            len(token),
        )

        return Response(
            {
                "call_id": call.id,
                "server_url": settings.LIVEKIT_URL,
                "room_name": call.room_name,
                "token": token,
            }
        )


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
            cleanup_livekit_room(call)
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


class AcceptCall(CallAction):
    def apply_action(self, request, call):
        permission_error = self.require_receiver(request, call)
        if permission_error:
            return permission_error
        if call.status != CallSession.Status.RINGING:
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
        return None

    def emit_events(self, request, call):
        send_call_event(call.caller_id, "call_accepted", call)
        send_call_event(call.receiver_id, "call_accepted", call)


class RejectCall(CallAction):
    def apply_action(self, request, call):
        permission_error = self.require_receiver(request, call)
        if permission_error:
            return permission_error
        if call.status != CallSession.Status.RINGING:
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
