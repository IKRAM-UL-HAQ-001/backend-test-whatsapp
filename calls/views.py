import uuid
import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .livekit import generate_join_token
from .models import CallSession
from .realtime import send_call_event
from .serializers import CallSessionSerializer, StartCallSerializer
from .tasks import send_incoming_call_notification


logger = logging.getLogger(__name__)


ACTIVE_PROGRESS_STATUSES = [
    CallSession.Status.INITIATED,
    CallSession.Status.RINGING,
    CallSession.Status.ACCEPTED,
    CallSession.Status.ACTIVE,
]


def user_call_queryset(user):
    return CallSession.objects.filter(Q(caller=user) | Q(receiver=user))


def queue_incoming_call_notification(call_id):
    try:
        send_incoming_call_notification.delay(call_id)
    except Exception as exc:
        logger.warning("Failed to queue incoming call notification for call_id=%s: %s", call_id, exc)


class CallHistoryPagination(LimitOffsetPagination):
    default_limit = 20
    max_limit = 50


class StartCall(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = StartCallSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        receiver = serializer.context["receiver"]

        with transaction.atomic():
            User = get_user_model()
            list(
                User.objects.select_for_update()
                .filter(id__in=sorted([request.user.id, receiver.id]))
                .order_by("id")
            )

            active_calls = CallSession.objects.select_for_update().filter(
                Q(caller=request.user) | Q(receiver=request.user) | Q(caller=receiver) | Q(receiver=receiver),
                status__in=ACTIVE_PROGRESS_STATUSES,
            )
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

        send_call_event(receiver.id, "call_invite", call)
        send_call_event(request.user.id, "call_ringing", call)
        queue_incoming_call_notification(call.id)
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
