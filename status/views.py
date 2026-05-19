import json
import logging
import os

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status as http_status

from users.models import UserContact

from .models import StatusPrivacyException, StatusPrivacySetting, StatusView, UserStatus
from .serializers import (
    CreateStatusSerializer,
    PrivacyUpdateSerializer,
    StatusOwnerSerializer,
    StatusViewerSerializer,
)

logger = logging.getLogger(__name__)

MAX_STATUS_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def _broadcast(user_id, event_name, payload):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f"user_{user_id}",
            {"type": f"{event_name}_event", "payload": payload},
        )
    except Exception as exc:
        logger.warning("WebSocket broadcast failed for user_id=%s: %s", user_id, exc)


def _media_url(request, field):
    if field and request:
        return request.build_absolute_uri(field.url)
    return None


def _serialize_status(status, request, viewer):
    return {
        "id": str(status.id),
        "status_type": status.status_type,
        "text_content": status.text_content,
        "media_url": _media_url(request, status.media_file),
        "thumbnail_url": _media_url(request, status.thumbnail),
        "background_color": status.background_color,
        "font_size": status.font_size,
        "duration": status.duration,
        "created_at": status.created_at.isoformat(),
        "expires_at": status.expires_at.isoformat(),
        "is_viewed": getattr(status, "is_viewed", False),
        "view_count": getattr(status, "view_count", 0),
    }


def _active_statuses_qs():
    return UserStatus.objects.filter(is_active=True, expires_at__gt=timezone.now())


def _visibility_filter(viewer):
    """Return Q that keeps only statuses visible to `viewer`."""
    excluded_by = StatusPrivacyException.objects.filter(
        excepted_user=viewer, exception_type="except"
    ).values_list("status_owner_id", flat=True)

    included_by = StatusPrivacyException.objects.filter(
        excepted_user=viewer, exception_type="only"
    ).values_list("status_owner_id", flat=True)

    return (
        Q(privacy="all_contacts")
        | (Q(privacy="except") & ~Q(user_id__in=excluded_by))
        | (Q(privacy="only") & Q(user_id__in=included_by))
    )


def _visible_contact_owner_ids(viewer):
    return UserContact.objects.filter(contact=viewer).values_list("user_id", flat=True)


def _current_privacy_settings(user):
    setting, _ = StatusPrivacySetting.objects.get_or_create(user=user)
    except_user_ids = list(
        StatusPrivacyException.objects.filter(
            status_owner=user,
            exception_type="except",
        ).values_list("excepted_user_id", flat=True)
    )
    only_user_ids = list(
        StatusPrivacyException.objects.filter(
            status_owner=user,
            exception_type="only",
        ).values_list("excepted_user_id", flat=True)
    )
    privacy = setting.privacy
    user_ids = []
    if privacy == "except":
        user_ids = except_user_ids
    elif privacy == "only":
        user_ids = only_user_ids
    return privacy, user_ids, except_user_ids, only_user_ids


class StatusFeedView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        owner_ids = _visible_contact_owner_ids(request.user)

        viewer_viewed = StatusView.objects.filter(
            status=OuterRef("pk"), viewer=request.user
        )

        statuses = (
            _active_statuses_qs()
            .filter(user_id__in=owner_ids)
            .filter(_visibility_filter(request.user))
            .select_related("user")
            .annotate(
                is_viewed=Exists(viewer_viewed),
                view_count=Count("views"),
            )
            .order_by("user_id", "-created_at")
        )

        grouped: dict = {}
        for s in statuses:
            uid = s.user_id
            if uid not in grouped:
                grouped[uid] = {"user": s.user, "statuses": []}
            grouped[uid]["statuses"].append(s)

        result = []
        for data in grouped.values():
            user = data["user"]
            statuses_data = [
                _serialize_status(s, request, request.user) for s in data["statuses"]
            ]
            unviewed = sum(1 for s in data["statuses"] if not s.is_viewed)
            latest = data["statuses"][0].created_at if data["statuses"] else None
            result.append(
                {
                    "user": {
                        "id": user.id,
                        "name": user.name,
                        "profile_picture_url": _media_url(request, user.profile_picture),
                    },
                    "statuses": statuses_data,
                    "unviewed_count": unviewed,
                    "latest_status_time": latest.isoformat() if latest else None,
                }
            )

        result.sort(key=lambda x: x["latest_status_time"] or "", reverse=True)
        return Response(result)


class MyStatusesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        viewer_viewed = StatusView.objects.filter(
            status=OuterRef("pk"), viewer=request.user
        )
        statuses = (
            _active_statuses_qs()
            .filter(user=request.user)
            .annotate(is_viewed=Exists(viewer_viewed), view_count=Count("views"))
        )
        return Response([_serialize_status(s, request, request.user) for s in statuses])


class CreateStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        incoming = request.data.copy()
        if isinstance(incoming.get("user_ids"), str):
            try:
                incoming["user_ids"] = json.loads(incoming["user_ids"])
            except (TypeError, ValueError):
                incoming["user_ids"] = []
        ser = CreateStatusSerializer(data=incoming)
        if not ser.is_valid():
            return Response(ser.errors, status=http_status.HTTP_400_BAD_REQUEST)

        data = ser.validated_data
        media_file = request.FILES.get("media_file")

        if media_file:
            if media_file.size > MAX_STATUS_FILE_SIZE:
                return Response(
                    {"detail": "File exceeds 50 MB limit."},
                    status=http_status.HTTP_400_BAD_REQUEST,
                )

        thumbnail = None
        if data["status_type"] == "image" and media_file:
            try:
                from chat.utils import create_image_thumbnail
                thumbnail = create_image_thumbnail(media_file, size=(320, 320))
            except Exception:
                pass

        with transaction.atomic():
            privacy = data.get("privacy", "all_contacts")
            user_ids = data.get("user_ids", [])
            if privacy == "all_contacts" and not user_ids:
                privacy, user_ids, _, _ = _current_privacy_settings(request.user)

            status_obj = UserStatus.objects.create(
                user=request.user,
                status_type=data["status_type"],
                text_content=data.get("text_content") or "",
                media_file=media_file,
                thumbnail=thumbnail,
                background_color=data.get("background_color", "#128C7E"),
                font_size=data.get("font_size", 28),
                privacy=privacy,
            )
            if status_obj.privacy in ("except", "only"):
                StatusPrivacyException.objects.bulk_create(
                    [
                        StatusPrivacyException(
                            status_owner=request.user,
                            excepted_user_id=user_id,
                            exception_type=status_obj.privacy,
                        )
                        for user_id in user_ids
                    ],
                    ignore_conflicts=True,
                )

        payload = {
            "status_id": str(status_obj.id),
            "user_id": request.user.id,
            "user_name": request.user.name,
            "status_type": status_obj.status_type,
            "created_at": status_obj.created_at.isoformat(),
            "expires_at": status_obj.expires_at.isoformat(),
        }

        # Notify all contacts who should see this status
        contact_ids = UserContact.objects.filter(contact=request.user).values_list("user_id", flat=True)

        excluded_by = StatusPrivacyException.objects.filter(
            status_owner=request.user, exception_type="except"
        ).values_list("excepted_user_id", flat=True)

        only_for = StatusPrivacyException.objects.filter(
            status_owner=request.user, exception_type="only"
        ).values_list("excepted_user_id", flat=True)

        for cid in contact_ids:
            if status_obj.privacy == "all_contacts" and cid not in excluded_by:
                _broadcast(cid, "new_user_status", payload)
            elif status_obj.privacy == "except" and cid not in excluded_by:
                _broadcast(cid, "new_user_status", payload)
            elif status_obj.privacy == "only" and cid in only_for:
                _broadcast(cid, "new_user_status", payload)

        return Response(
            _serialize_status(status_obj, request, request.user),
            status=http_status.HTTP_201_CREATED,
        )


class DeleteStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, status_id):
        status_obj = UserStatus.objects.filter(
            id=status_id, user=request.user, is_active=True
        ).first()
        if not status_obj:
            return Response(status=http_status.HTTP_404_NOT_FOUND)
        status_obj.is_active = False
        status_obj.save(update_fields=["is_active"])
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class ViewStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, status_id):
        status_obj = (
            _active_statuses_qs()
            .filter(id=status_id)
            .filter(Q(user=request.user) | Q(user_id__in=_visible_contact_owner_ids(request.user)))
            .filter(_visibility_filter(request.user))
            .first()
        )
        if not status_obj:
            return Response(status=http_status.HTTP_404_NOT_FOUND)

        _, created = StatusView.objects.get_or_create(
            status=status_obj, viewer=request.user
        )

        if created and status_obj.user_id != request.user.id:
            _broadcast(
                status_obj.user_id,
                "status_viewed",
                {
                    "status_id": str(status_obj.id),
                    "viewer_id": request.user.id,
                    "viewer_name": request.user.name,
                },
            )

        return Response({"view_count": status_obj.views.count()})


class StatusViewersView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, status_id):
        status_obj = UserStatus.objects.filter(
            id=status_id, user=request.user
        ).first()
        if not status_obj:
            return Response(status=http_status.HTTP_404_NOT_FOUND)

        viewers = status_obj.views.select_related("viewer").order_by("-viewed_at")
        ser = StatusViewerSerializer(viewers, many=True, context={"request": request})
        return Response(ser.data)


class StatusPrivacyView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        privacy, _, except_user_ids, only_user_ids = _current_privacy_settings(request.user)

        return Response(
            {
                "privacy": privacy,
                "except_user_ids": except_user_ids,
                "only_user_ids": only_user_ids,
            }
        )

    def post(self, request):
        return self._update(request)

    def put(self, request):
        return self._update(request)

    def _update(self, request):
        incoming = request.data.copy()
        privacy = incoming.get("privacy")
        incoming["user_ids"] = []

        ser = PrivacyUpdateSerializer(data=incoming)
        if not ser.is_valid():
            return Response(ser.errors, status=http_status.HTTP_400_BAD_REQUEST)

        privacy = ser.validated_data["privacy"]
        except_user_ids = [int(uid) for uid in request.data.get("except_user_ids", [])]
        only_user_ids = [int(uid) for uid in request.data.get("only_user_ids", [])]

        setting, _ = StatusPrivacySetting.objects.get_or_create(user=request.user)
        setting.privacy = privacy
        setting.save(update_fields=["privacy", "updated_at"])
        UserStatus.objects.filter(user=request.user, is_active=True).update(privacy=privacy)

        StatusPrivacyException.objects.filter(
            status_owner=request.user,
            exception_type="except",
        ).delete()
        StatusPrivacyException.objects.filter(
            status_owner=request.user,
            exception_type="only",
        ).delete()

        if except_user_ids:
            StatusPrivacyException.objects.bulk_create(
                [
                    StatusPrivacyException(
                        status_owner=request.user,
                        excepted_user_id=uid,
                        exception_type="except",
                    )
                    for uid in except_user_ids
                ],
                ignore_conflicts=True,
            )
        if only_user_ids:
            StatusPrivacyException.objects.bulk_create(
                [
                StatusPrivacyException(
                    status_owner=request.user,
                    excepted_user_id=uid,
                    exception_type="only",
                )
                    for uid in only_user_ids
                ],
                ignore_conflicts=True,
            )

        return Response(
            {
                "privacy": privacy,
                "except_user_ids": except_user_ids,
                "only_user_ids": only_user_ids,
            }
        )
