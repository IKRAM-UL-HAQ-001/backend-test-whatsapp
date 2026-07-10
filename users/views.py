import json
import logging
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import update_last_login
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Device, DeviceLinkToken, InviteLog, OTP, User, UserContact, WebSocketTicket
from .rate_limits import hit_rate_limit
from .serializers import (
    ActivateLinkTokenSerializer,
    CompleteProfileSerializer,
    InviteContactSerializer,
    RequestOTPSerializer,
    ReverifyDeleteAccountSerializer,
    SyncContactsSerializer,
    VerifyOTPSerializer,
)
from .validation import normalize_contact_phone, normalize_phone


logger = logging.getLogger(__name__)


def _profile_data(user, request):
    return {
        "id": user.id,
        "phone_number": user.phone_number,
        "name": user.name,
        "about": user.about,
        "profile_picture": request.build_absolute_uri(user.profile_picture.url)
        if user.profile_picture and hasattr(user.profile_picture, "url")
        else None,
    }


def _send_otp_sms(phone_number, otp_code):
    return "000000"


def _issue_tokens(user, request):
    refresh = RefreshToken.for_user(user)
    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
        "user": _profile_data(user, request),
    }


def _normalize_contact_entry(raw_contact):
    if isinstance(raw_contact, str):
        raw_phone = raw_contact
        contact_name = ""
    else:
        raw_phone = (
            raw_contact.get("phone_number")
            or raw_contact.get("phone")
            or raw_contact.get("number")
            or ""
        )
        contact_name = raw_contact.get("name") or raw_contact.get("contact_name") or ""
    phone_number = normalize_contact_phone(raw_phone, settings.CONTACT_DEFAULT_COUNTRY_CODE)
    return phone_number, str(contact_name or "")


def _extract_contact_entries(request):
    entries = []
    data = request.data if isinstance(request.data, dict) else {}
    if isinstance(data.get("contacts"), list):
        entries.extend(data["contacts"])
    if isinstance(data.get("phones"), list):
        entries.extend(data["phones"])
    if data.get("phone"):
        entries.append(data["phone"])

    for key in ("contacts", "phones", "phone"):
        values = request.query_params.getlist(key)
        for value in values:
            if key in {"contacts", "phones"}:
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        entries.extend(parsed)
                        continue
                except (TypeError, ValueError):
                    pass
                entries.extend([part.strip() for part in value.split(",") if part.strip()])
            else:
                entries.append(value)
    return entries


def _serialize_contact_user(user, contact_name, request):
    profile_photo = None
    if user.profile_picture and hasattr(user.profile_picture, "url"):
        profile_photo = request.build_absolute_uri(user.profile_picture.url)
    return {
        "id": str(user.id),
        "phone": user.phone_number,
        "name": user.name,
        "contact_name": contact_name,
        "about": user.about,
        "has_account": True,
        "profile_photo": profile_photo,
        "is_online": cache.get(f"presence:{user.id}") == "online",
    }


class RequestOTP(APIView):
    """
    POST /auth/request-otp/
    Auth: none
    Request: {"country_code": "+1", "phone_number": "5551234567"}
    Response: {"message": "OTP sent"}
    Errors: 400 invalid phone, 429 rate limited
    """

    def post(self, request):
        serializer = RequestOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        country_code, phone_number, e164 = normalize_phone(
            serializer.validated_data["country_code"],
            serializer.validated_data["phone_number"],
        )
        logger.debug("OTP requested")

        if hit_rate_limit(
            "otp-phone",
            e164,
            settings.OTP_RATE_LIMIT_MAX,
            settings.OTP_RATE_LIMIT_WINDOW_SECONDS,
        ):
            return Response({"error": "OTP rate limit exceeded"}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        user, _ = User.all_objects.get_or_create(
            phone_number=e164,
            defaults={"country_code": country_code},
        )
        if user.is_deleted:
            user.is_deleted = False
            user.deleted_at = None
            user.is_active = True
            user.save(update_fields=["is_deleted", "deleted_at", "is_active"])

        otp_code = "000000"
        try:
            _send_otp_sms(e164, otp_code)
        except Exception:
            logger.exception("Failed to send OTP SMS")
            return Response(
                {"error": "Unable to send OTP"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        OTP.objects.filter(phone_number=e164, is_used=False).update(is_used=True)
        OTP.objects.create(phone_number=e164, otp_code=otp_code)

        payload = {"message": "OTP sent"}
        if settings.ENABLE_DEV_OTP:
            payload["otp"] = otp_code
        return Response(payload)


class VerifyOTP(APIView):
    """
    POST /auth/verify-otp/
    Auth: none
    Request: {"phone_number": "+15551234567", "otp": "123456"}
    Response: JWT pair + user
    Errors: 400 invalid/expired OTP
    """

    def post(self, request):
        serializer = VerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        country_code, phone_number, e164 = normalize_phone(
            serializer.validated_data["country_code"],
            serializer.validated_data["phone_number"],
        )
        otp = serializer.validated_data["otp"]
        logger.debug("OTP verification attempted")

        otp_record = (
            OTP.objects.filter(phone_number=e164, otp_code=otp, is_used=False)
            .order_by("-created_at")
            .first()
        )
        if otp_record is None or otp_record.is_expired():
            return Response({"error": "Invalid or expired OTP"}, status=status.HTTP_400_BAD_REQUEST)

        otp_record.is_used = True
        otp_record.save(update_fields=["is_used"])
        logger.debug("OTP verified successfully")

        user = User.all_objects.filter(phone_number=e164).first()
        if user is None:
            return Response({"error": "User not found"}, status=status.HTTP_400_BAD_REQUEST)

        user.is_verified = True
        user.is_deleted = False
        user.deleted_at = None
        user.is_active = True
        user.save(update_fields=["is_verified", "is_deleted", "deleted_at", "is_active"])
        update_last_login(None, user)
        return Response(_issue_tokens(user, request))


class CompleteProfile(APIView):
    """
    GET/POST /auth/complete-profile/
    Auth: bearer
    Request: multipart/json with name/profile_picture/fcm_token
    Response: current updated profile, including a fresh media URL
    Errors: 400 validation
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"user": _profile_data(request.user, request)})

    def post(self, request):
        serializer = CompleteProfileSerializer(instance=request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {
                "message": "Profile updated",
                "user": _profile_data(user, request),
            }
        )


class UpdateFcmToken(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.data.get("fcm_token")
        if not token:
            return Response({"error": "fcm_token required"}, status=status.HTTP_400_BAD_REQUEST)

        request.user.fcm_token = token
        request.user.save(update_fields=["fcm_token"])
        return Response({"success": True})


class RegisterDevice(APIView):
    """
    POST /auth/devices/register/
    Auth: bearer
    Request: { device_id, platform: android|ios, fcm_token?, apns_voip_token?, app_version? }
    Response: { success: true }

    Upserts the per-device push tokens. iOS sends its PushKit VoIP token here;
    Android sends its FCM data-push token. For an Android device the FCM token is
    also mirrored onto User.fcm_token so the existing single-token push path keeps
    working until the push layer is migrated to per-device fan-out.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        device_id = (request.data.get("device_id") or "").strip()
        platform = (request.data.get("platform") or "").strip().lower()
        if not device_id:
            return Response({"error": "device_id required"}, status=status.HTTP_400_BAD_REQUEST)
        if platform not in dict(Device.Platform.choices):
            return Response(
                {"error": "platform must be 'android' or 'ios'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        fcm_token = request.data.get("fcm_token") or None
        apns_voip_token = request.data.get("apns_voip_token") or None
        app_version = (request.data.get("app_version") or "").strip()

        device, _ = Device.objects.update_or_create(
            user=request.user,
            device_id=device_id,
            defaults={
                "platform": platform,
                "fcm_token": fcm_token,
                "apns_voip_token": apns_voip_token,
                "app_version": app_version,
            },
        )

        # Backward-compat: keep the legacy single token populated for Android so
        # the current push path still reaches this user.
        if platform == Device.Platform.ANDROID and fcm_token and request.user.fcm_token != fcm_token:
            request.user.fcm_token = fcm_token
            request.user.save(update_fields=["fcm_token"])

        return Response({"success": True})


class ListUsers(APIView):
    """
    GET /auth/list-users/
    Auth: bearer
    Response: saved-contact user summaries
    Errors: 401 unauthorized
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return self._list_matching_contacts(request)

    def post(self, request):
        return self._list_matching_contacts(request)

    def _list_matching_contacts(self, request):
        entries = _extract_contact_entries(request)
        contact_names = {}

        if entries:
            for entry in entries:
                phone_number, contact_name = _normalize_contact_entry(entry)
                if phone_number:
                    contact_names.setdefault(phone_number, contact_name)
        else:
            saved_contacts = UserContact.objects.filter(user=request.user).only("phone_number", "contact_name")
            contact_names = {}
            for contact in saved_contacts:
                phone_number = normalize_contact_phone(contact.phone_number, settings.CONTACT_DEFAULT_COUNTRY_CODE)
                if phone_number:
                    contact_names.setdefault(phone_number, contact.contact_name)

        if not contact_names:
            return Response([])

        logger.info(
            "Contact list lookup user_id=%s unique_numbers=%s",
            request.user.id,
            len(contact_names),
        )
        users = (
            User.objects.filter(phone_number__in=contact_names.keys())
            .exclude(id=request.user.id)
            .only("id", "phone_number", "name", "about", "profile_picture")
        )
        logger.info(
            "Contact list lookup completed user_id=%s matches=%s",
            request.user.id,
            users.count(),
        )
        return Response(
            [
                _serialize_contact_user(user, contact_names.get(user.phone_number, ""), request)
                for user in users
            ]
        )


class GenerateLinkToken(APIView):
    """
    GET /auth/generate-link-token/
    Auth: none (WhatsApp-Web style: the *unauthenticated* web client requests a
    token and shows it as a QR; the logged-in phone then scans and activates it
    via ActivateLinkToken, which binds the token to the phone's user.)
    Response: {"token": "..."}
    Errors: 429 rate limited
    """

    permission_classes = [AllowAny]

    def get(self, request):
        # Rate-limit by client IP since there is no authenticated user yet.
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        client_ip = (
            forwarded.split(",")[0].strip()
            if forwarded
            else request.META.get("REMOTE_ADDR", "anon")
        )
        if hit_rate_limit(
            "link-generate",
            client_ip,
            settings.LINK_TOKEN_RATE_LIMIT_MAX,
            settings.LINK_TOKEN_RATE_LIMIT_WINDOW_SECONDS,
        ):
            return Response({"error": "Too many link tokens requested"}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        token = str(uuid.uuid4())
        device_name = (request.GET.get("device_name") or "").strip()[:120]
        DeviceLinkToken.objects.create(token=token, device_name=device_name)
        return Response({"token": token, "expires_in": 300})


class LinkedDevices(APIView):
    """
    GET /auth/linked-devices/
    Auth: bearer (called from the phone)
    Response: {"devices": [{"id", "device_name", "linked_at"}]}
    Lists this user's successfully linked web/companion sessions, newest first.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        sessions = DeviceLinkToken.objects.filter(
            user=request.user,
            is_active=True,
            consumed_at__isnull=False,
        ).order_by("-consumed_at")[:20]
        return Response(
            {
                "devices": [
                    {
                        "id": s.id,
                        "device_name": s.device_name or "Unknown device",
                        "linked_at": s.consumed_at.isoformat() if s.consumed_at else s.created_at.isoformat(),
                    }
                    for s in sessions
                ]
            }
        )


class ActivateLinkToken(APIView):
    """
    POST /auth/activate-link-token/
    Auth: bearer
    Request: {"token": "..."}
    Response: {"message": "Device linked successfully"}
    Errors: 400 invalid/expired token
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ActivateLinkTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token_str = serializer.validated_data["token"]
        token_obj = DeviceLinkToken.objects.filter(token=token_str).select_related("user").first()
        if token_obj is None or token_obj.is_expired():
            return Response({"error": "Invalid or expired token"}, status=status.HTTP_400_BAD_REQUEST)

        token_obj.user = request.user
        token_obj.is_active = True
        refresh = RefreshToken.for_user(request.user)
        token_obj.access_token = str(refresh.access_token)
        token_obj.refresh_token = str(refresh)
        token_obj.save(update_fields=["user", "is_active", "access_token", "refresh_token"])
        return Response({"message": "Device linked successfully"})


class CheckLinkStatus(APIView):
    """
    GET /auth/check-link-status/{token}/
    Auth: none
    Response: link activation state and tokens if activated
    Errors: 400 invalid token, 429 polled too quickly
    """

    def get(self, request, token):
        poll_key = f"link-poll:{token}"
        if cache.get(poll_key):
            return Response({"error": "Polling too fast"}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        cache.set(poll_key, 1, timeout=settings.LINK_STATUS_POLL_SECONDS)

        token_obj = DeviceLinkToken.objects.filter(token=token, consumed_at__isnull=True).select_related("user").first()
        if token_obj is None or token_obj.is_expired():
            return Response({"error": "Invalid or expired token"}, status=status.HTTP_400_BAD_REQUEST)

        if token_obj.is_active and token_obj.user:
            payload = {
                "is_active": True,
                "access": token_obj.access_token,
                "refresh": token_obj.refresh_token,
                "user": {
                    "id": token_obj.user.id,
                    "phone_number": token_obj.user.phone_number,
                    "name": token_obj.user.name,
                    "about": token_obj.user.about,
                    "profile_picture": request.build_absolute_uri(token_obj.user.profile_picture.url)
                    if token_obj.user.profile_picture and hasattr(token_obj.user.profile_picture, "url")
                    else None,
                },
            }
            token_obj.mark_consumed()
            return Response(payload)
        return Response({"is_active": False})


class IssueWebSocketTicket(APIView):
    """
    POST /auth/ws-ticket/
    Auth: bearer
    Request: {}
    Response: {"ticket": "...", "expires_in": 30}
    Errors: 401 unauthorized
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        ticket = secrets.token_urlsafe(32)
        cache.set(f"ws-ticket:{ticket}", request.user.id, timeout=settings.WEBSOCKET_TICKET_TTL_SECONDS)
        return Response({"ticket": ticket, "expires_in": settings.WEBSOCKET_TICKET_TTL_SECONDS})


class PresenceHeartbeat(APIView):
    """
    POST /auth/presence/heartbeat/
    Auth: bearer
    Request: {}
    Response: {"ok": true}
    Errors: 401 unauthorized
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        cache.set(f"presence:{request.user.id}", "online", timeout=settings.PRESENCE_TTL_SECONDS)
        return Response({"ok": True})


class UserPresence(APIView):
    """
    GET /auth/presence/{user_id}/
    Auth: bearer
    Response: {"user_id": 1, "is_online": true}
    Errors: 401 unauthorized
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        is_online = cache.get(f"presence:{user_id}") == "online"
        return Response({"user_id": user_id, "is_online": is_online})


class SyncContacts(APIView):
    """
    POST /auth/sync-contacts/
    Auth: bearer
    Request: {"contacts": [{"phone_number": "+15551234567", "name": "Jane"}]}
    Response: {"synced": 3}
    Errors: 400 validation
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = SyncContactsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        lock_key = f"contact-sync:{request.user.id}"
        if not cache.add(lock_key, "1", timeout=30):
            return Response({"synced": 0, "status": "already_running"})

        normalized_contacts = {}
        for raw_contact in serializer.validated_data["contacts"]:
            phone_number, contact_name = _normalize_contact_entry(raw_contact)
            if not phone_number or phone_number == request.user.phone_number:
                continue
            normalized_contacts.setdefault(phone_number, contact_name)
        logger.info(
            "Contact sync started user_id=%s unique_numbers=%s",
            request.user.id,
            len(normalized_contacts),
        )

        if not normalized_contacts:
            cache.delete(lock_key)
            return Response({"synced": 0})

        try:
            users_by_phone = {
                user.phone_number: user
                for user in User.objects.filter(phone_number__in=normalized_contacts.keys())
            }
            existing_contacts = {
                normalize_contact_phone(contact.phone_number, settings.CONTACT_DEFAULT_COUNTRY_CODE): contact
                for contact in UserContact.objects.filter(user=request.user)
                if normalize_contact_phone(contact.phone_number, settings.CONTACT_DEFAULT_COUNTRY_CODE)
            }

            to_create = []
            to_update = []
            for phone_number, contact_name in normalized_contacts.items():
                contact_user = users_by_phone.get(phone_number)
                if contact_user and contact_user.id == request.user.id:
                    continue

                existing = existing_contacts.get(phone_number)
                if existing is None:
                    to_create.append(
                        UserContact(
                            user=request.user,
                            phone_number=phone_number,
                            contact_name=contact_name,
                            contact=contact_user,
                        )
                    )
                    continue

                changed = False
                if existing.phone_number != phone_number:
                    existing.phone_number = phone_number
                    changed = True
                if existing.contact_name != contact_name:
                    existing.contact_name = contact_name
                    changed = True
                if existing.contact_id != (contact_user.id if contact_user else None):
                    existing.contact = contact_user
                    changed = True
                if changed:
                    to_update.append(existing)

            with transaction.atomic():
                if to_create:
                    UserContact.objects.bulk_create(to_create, ignore_conflicts=True, batch_size=500)
                if to_update:
                    UserContact.objects.bulk_update(
                        to_update,
                        ["phone_number", "contact_name", "contact"],
                        batch_size=500,
                    )
            synced_count = len(to_create) + len(to_update)
            logger.info(
                "Contact sync completed user_id=%s created=%s updated=%s matches=%s",
                request.user.id,
                len(to_create),
                len(to_update),
                len(users_by_phone),
            )
            return Response({"synced": synced_count})
        finally:
            cache.delete(lock_key)


class InviteContact(APIView):
    """
    POST /auth/invite-contact/
    Auth: bearer
    Request: {"phone": "+923001234567", "contact_name": "Ali Bhai"}
    Response: {"success": true, "status": "pending"}
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = InviteContactSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        phone_number = normalize_contact_phone(
            serializer.validated_data["phone"],
            settings.CONTACT_DEFAULT_COUNTRY_CODE,
        )
        if not phone_number:
            return Response({"error": "Phone number is required"}, status=status.HTTP_400_BAD_REQUEST)
        contact_name = serializer.validated_data.get("contact_name", "")
        invite = InviteLog.objects.create(
            invited_by=request.user,
            phone_number=phone_number,
            contact_name=contact_name,
        )
        logger.info("Invite logged id=%s invited_by=%s phone=%s", invite.id, request.user.id, phone_number)
        return Response({"success": True, "status": invite.status})


class DeleteAccount(APIView):
    """
    POST /auth/delete-account/
    Auth: bearer
    Request: {"otp": "123456"}
    Response: {"message": "Account deleted successfully"}
    Errors: 400 invalid OTP
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ReverifyDeleteAccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        otp = serializer.validated_data["otp"]

        otp_record = (
            OTP.objects.filter(phone_number=request.user.phone_number, otp_code=otp, is_used=False)
            .order_by("-created_at")
            .first()
        )
        if otp_record is None or otp_record.is_expired():
            return Response({"error": "Invalid or expired OTP"}, status=status.HTTP_400_BAD_REQUEST)

        otp_record.is_used = True
        otp_record.save(update_fields=["is_used"])
        request.user.soft_delete()
        DeviceLinkToken.objects.filter(user=request.user).delete()
        WebSocketTicket.objects.filter(user=request.user).delete()
        return Response({"message": "Account deleted successfully"})
