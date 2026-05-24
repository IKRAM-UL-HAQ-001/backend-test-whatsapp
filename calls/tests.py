from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from .models import CallSession
from .livekit import generate_join_token, livekit_identity
from .notifications import incoming_call_payload, missed_call_payload, send_incoming_call_push
from .tasks import mark_call_missed_if_unanswered


class DummyChannelLayer:
    def __init__(self):
        self.calls = []

    async def group_send(self, group, message):
        self.calls.append((group, message))


class CallSessionModelTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.caller = User.all_objects.create(
            country_code="+1",
            phone_number="+11111111111",
            name="Caller",
            is_verified=True,
        )
        self.receiver = User.all_objects.create(
            country_code="+1",
            phone_number="+12222222222",
            name="Receiver",
            is_verified=True,
        )

    def test_creates_audio_call_session(self):
        session = CallSession.objects.create(
            caller=self.caller,
            receiver=self.receiver,
            call_type=CallSession.CallType.AUDIO,
            room_name="audio-room",
        )

        self.assertEqual(session.call_type, CallSession.CallType.AUDIO)
        self.assertEqual(session.status, CallSession.Status.INITIATED)
        self.assertTrue(session.is_active)
        self.assertFalse(session.is_terminal)

    def test_creates_video_call_session(self):
        session = CallSession.objects.create(
            caller=self.caller,
            receiver=self.receiver,
            call_type=CallSession.CallType.VIDEO,
            room_name="video-room",
        )

        self.assertEqual(session.call_type, CallSession.CallType.VIDEO)

    def test_room_name_must_be_unique(self):
        CallSession.objects.create(
            caller=self.caller,
            receiver=self.receiver,
            call_type=CallSession.CallType.AUDIO,
            room_name="unique-room",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CallSession.objects.create(
                    caller=self.receiver,
                    receiver=self.caller,
                    call_type=CallSession.CallType.VIDEO,
                    room_name="unique-room",
                )

    def test_caller_and_receiver_must_be_different(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CallSession.objects.create(
                    caller=self.caller,
                    receiver=self.caller,
                    call_type=CallSession.CallType.AUDIO,
                    room_name="same-user-room",
                )

    def test_status_choices_are_enforced_by_validation(self):
        session = CallSession(
            caller=self.caller,
            receiver=self.receiver,
            call_type=CallSession.CallType.AUDIO,
            status="invalid",
            room_name="invalid-status-room",
        )

        with self.assertRaises(ValidationError):
            session.full_clean()


@override_settings(
    MIDDLEWARE=[
        middleware
        for middleware in settings.MIDDLEWARE
        if middleware != "whitenoise.middleware.WhiteNoiseMiddleware"
    ]
)
class CallSessionApiTests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.caller = User.all_objects.create(
            country_code="+1",
            phone_number="+21111111111",
            name="Caller",
            is_verified=True,
        )
        self.receiver = User.all_objects.create(
            country_code="+1",
            phone_number="+22222222222",
            name="Receiver",
            is_verified=True,
        )
        self.third = User.all_objects.create(
            country_code="+1",
            phone_number="+23333333333",
            name="Third",
            is_verified=True,
        )
        self.client = APIClient()

    def authenticate(self, user):
        self.client.force_authenticate(user=user)

    def start_call(self, call_type=CallSession.CallType.AUDIO, queue_tasks=False):
        self.authenticate(self.caller)
        if queue_tasks:
            return self.client.post(
                "/api/calls/start/",
                {"receiver_id": self.receiver.id, "call_type": call_type},
                format="json",
            )
        with patch("calls.views.queue_incoming_call_notification"), patch(
            "calls.views.queue_missed_call_timeout"
        ):
            return self.client.post(
                "/api/calls/start/",
                {"receiver_id": self.receiver.id, "call_type": call_type},
                format="json",
            )

    def create_ringing_call(self):
        return CallSession.objects.create(
            caller=self.caller,
            receiver=self.receiver,
            call_type=CallSession.CallType.AUDIO,
            status=CallSession.Status.RINGING,
            room_name="api-ringing-room",
            started_at=timezone.now(),
        )

    def create_accepted_call(self):
        call = self.create_ringing_call()
        call.status = CallSession.Status.ACCEPTED
        call.accepted_at = timezone.now()
        call.save(update_fields=["status", "accepted_at", "updated_at"])
        return call

    def test_start_audio_call(self):
        response = self.start_call()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["call_type"], CallSession.CallType.AUDIO)
        self.assertEqual(response.data["status"], CallSession.Status.RINGING)
        self.assertEqual(response.data["room_name"], f"call_{response.data['id']}")

    def test_start_video_call(self):
        response = self.start_call(CallSession.CallType.VIDEO)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["call_type"], CallSession.CallType.VIDEO)

    def test_cannot_call_self(self):
        self.authenticate(self.caller)
        response = self.client.post(
            "/api/calls/start/",
            {"receiver_id": self.caller.id, "call_type": CallSession.CallType.AUDIO},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_receiver_busy_returns_user_busy(self):
        CallSession.objects.create(
            caller=self.third,
            receiver=self.receiver,
            call_type=CallSession.CallType.AUDIO,
            status=CallSession.Status.RINGING,
            room_name="receiver-busy-room",
        )

        response = self.start_call()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "user_busy")

    def test_caller_busy_cannot_start_new_call(self):
        CallSession.objects.create(
            caller=self.caller,
            receiver=self.third,
            call_type=CallSession.CallType.AUDIO,
            status=CallSession.Status.RINGING,
            room_name="caller-busy-room",
        )

        response = self.start_call()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "caller_busy")

    def test_receiver_with_ringing_call_cannot_start_new_call(self):
        self.create_ringing_call()
        self.authenticate(self.receiver)

        response = self.client.post(
            "/api/calls/start/",
            {"receiver_id": self.third.id, "call_type": CallSession.CallType.AUDIO},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "caller_busy")

    def test_second_caller_gets_user_busy_for_ringing_receiver(self):
        self.create_ringing_call()
        self.authenticate(self.third)

        response = self.client.post(
            "/api/calls/start/",
            {"receiver_id": self.receiver.id, "call_type": CallSession.CallType.AUDIO},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "user_busy")

    @patch("calls.views.mark_call_missed_if_unanswered.apply_async")
    @patch("calls.views.send_incoming_call_notification.apply_async")
    @patch("calls.realtime.get_channel_layer")
    def test_opposite_direction_simultaneous_call_returns_busy(
        self,
        channel_layer_mock,
        delay_mock,
        apply_async_mock,
    ):
        channel_layer_mock.return_value = DummyChannelLayer()
        first = self.start_call()
        self.authenticate(self.receiver)

        second = self.client.post(
            "/api/calls/start/",
            {"receiver_id": self.caller.id, "call_type": CallSession.CallType.AUDIO},
            format="json",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.data["code"], "caller_busy")
        self.assertEqual(
            CallSession.objects.filter(status__in=[
                CallSession.Status.INITIATED,
                CallSession.Status.RINGING,
                CallSession.Status.ACCEPTED,
                CallSession.Status.ACTIVE,
            ]).count(),
            1,
        )

    def test_receiver_accepts_call(self):
        call = self.create_ringing_call()
        self.authenticate(self.receiver)

        response = self.client.post(f"/api/calls/{call.id}/accept/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], CallSession.Status.ACCEPTED)
        self.assertIsNotNone(response.data["accepted_at"])

    def test_accept_fails_if_receiver_already_active_elsewhere(self):
        call = self.create_ringing_call()
        CallSession.objects.create(
            caller=self.receiver,
            receiver=self.third,
            call_type=CallSession.CallType.AUDIO,
            status=CallSession.Status.ACCEPTED,
            room_name="receiver-already-active-room",
            accepted_at=timezone.now(),
        )
        self.authenticate(self.receiver)

        response = self.client.post(f"/api/calls/{call.id}/accept/", {}, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "already_in_call")
        call.refresh_from_db()
        self.assertEqual(call.status, CallSession.Status.RINGING)

    def test_non_receiver_cannot_accept(self):
        call = self.create_ringing_call()
        self.authenticate(self.third)

        response = self.client.post(f"/api/calls/{call.id}/accept/", {}, format="json")

        self.assertEqual(response.status_code, 403)

    def test_receiver_rejects_call(self):
        call = self.create_ringing_call()
        self.authenticate(self.receiver)

        response = self.client.post(f"/api/calls/{call.id}/reject/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], CallSession.Status.REJECTED)
        self.assertEqual(response.data["ended_by"]["id"], str(self.receiver.id))
        self.assertIsNotNone(response.data["ended_at"])

    @patch("calls.views.cleanup_livekit_room")
    def test_caller_cancels_ringing_call(self, cleanup_mock):
        call = self.create_ringing_call()
        self.authenticate(self.caller)

        response = self.client.post(f"/api/calls/{call.id}/cancel/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], CallSession.Status.CANCELLED)
        self.assertEqual(response.data["ended_by"]["id"], str(self.caller.id))
        cleanup_mock.assert_called_once()

    def test_participant_ends_accepted_call(self):
        call = self.create_ringing_call()
        call.status = CallSession.Status.ACCEPTED
        call.accepted_at = timezone.now() - timedelta(seconds=65)
        call.save(update_fields=["status", "accepted_at", "updated_at"])
        self.authenticate(self.receiver)

        response = self.client.post(f"/api/calls/{call.id}/end/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], CallSession.Status.ENDED)
        self.assertGreaterEqual(response.data["duration_seconds"], 60)

    @patch("calls.views.delete_room")
    def test_terminal_call_actions_cleanup_livekit_room(self, delete_room_mock):
        call = self.create_ringing_call()
        self.authenticate(self.caller)

        cancel_response = self.client.post(f"/api/calls/{call.id}/cancel/", {}, format="json")

        self.assertEqual(cancel_response.status_code, 200)
        delete_room_mock.assert_called_once_with(call.room_name)

    @patch("calls.views.delete_room")
    def test_end_call_cleanup_livekit_room(self, delete_room_mock):
        call = self.create_accepted_call()
        self.authenticate(self.receiver)

        response = self.client.post(f"/api/calls/{call.id}/end/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        delete_room_mock.assert_called_once_with(call.room_name)

    @patch("calls.views.delete_room")
    def test_livekit_room_not_found_cleanup_is_treated_as_success(self, delete_room_mock):
        class DummyTwirpNotFound(Exception):
            code = "not_found"
            status = 404

        delete_room_mock.side_effect = DummyTwirpNotFound("requested room does not exist")
        call = self.create_accepted_call()
        self.authenticate(self.receiver)

        response = self.client.post(f"/api/calls/{call.id}/end/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], CallSession.Status.ENDED)

    def test_non_participant_cannot_view_call(self):
        call = self.create_ringing_call()
        self.authenticate(self.third)

        response = self.client.get(f"/api/calls/{call.id}/")

        self.assertEqual(response.status_code, 403)

    def test_call_history_returns_only_user_calls(self):
        own_call = self.create_ringing_call()
        CallSession.objects.create(
            caller=self.receiver,
            receiver=self.third,
            call_type=CallSession.CallType.VIDEO,
            status=CallSession.Status.RINGING,
            room_name="other-user-room",
        )
        self.authenticate(self.caller)

        response = self.client.get("/api/calls/history/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.data["results"]], [own_call.id])

    def test_current_call_returns_none_when_idle(self):
        self.authenticate(self.caller)

        response = self.client.get("/api/calls/current/")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["call"])

    def test_current_call_prefers_accepted_over_ringing(self):
        ringing = self.create_ringing_call()
        accepted = CallSession.objects.create(
            caller=self.caller,
            receiver=self.third,
            call_type=CallSession.CallType.AUDIO,
            status=CallSession.Status.ACCEPTED,
            room_name="current-accepted-room",
            accepted_at=timezone.now(),
        )
        self.authenticate(self.caller)

        response = self.client.get("/api/calls/current/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["call"]["id"], accepted.id)
        self.assertNotEqual(response.data["call"]["id"], ringing.id)

    @override_settings(
        LIVEKIT_URL="wss://livekit.qubrixe.com",
        LIVEKIT_API_KEY="test-key",
        LIVEKIT_API_SECRET="test-secret",
    )
    @patch("calls.views.generate_join_token", return_value="test-livekit-token")
    def test_caller_can_get_join_token_after_accepted(self, token_mock):
        call = self.create_accepted_call()
        self.authenticate(self.caller)

        response = self.client.post(f"/api/calls/{call.id}/join/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["call_id"], call.id)
        self.assertEqual(response.data["server_url"], "wss://livekit.qubrixe.com")
        self.assertEqual(response.data["room_name"], call.room_name)
        self.assertEqual(response.data["token"], "test-livekit-token")
        self.assertNotIn("LIVEKIT_API_SECRET", response.data)
        self.assertNotIn("api_secret", response.data)
        token_mock.assert_called_once_with(self.caller, call)

    @override_settings(
        LIVEKIT_URL="wss://livekit.qubrixe.com",
        LIVEKIT_API_KEY="test-key",
        LIVEKIT_API_SECRET="test-secret",
    )
    @patch("calls.views.generate_join_token", return_value="receiver-token")
    def test_receiver_can_get_join_token_after_accepted(self, token_mock):
        call = self.create_accepted_call()
        self.authenticate(self.receiver)

        response = self.client.post(f"/api/calls/{call.id}/join/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["token"], "receiver-token")
        token_mock.assert_called_once_with(self.receiver, call)

    @patch("calls.views.generate_join_token", return_value="test-livekit-token")
    def test_non_participant_cannot_get_join_token(self, token_mock):
        call = self.create_accepted_call()
        self.authenticate(self.third)

        response = self.client.post(f"/api/calls/{call.id}/join/", {}, format="json")

        self.assertEqual(response.status_code, 403)
        token_mock.assert_not_called()

    @patch("calls.views.generate_join_token", return_value="test-livekit-token")
    def test_ringing_call_cannot_get_join_token(self, token_mock):
        call = self.create_ringing_call()
        self.authenticate(self.caller)

        response = self.client.post(f"/api/calls/{call.id}/join/", {}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "call_not_joinable")
        token_mock.assert_not_called()

    @patch("calls.views.generate_join_token", return_value="test-livekit-token")
    def test_ended_call_cannot_get_join_token(self, token_mock):
        call = self.create_accepted_call()
        call.status = CallSession.Status.ENDED
        call.ended_at = timezone.now()
        call.save(update_fields=["status", "ended_at", "updated_at"])
        self.authenticate(self.caller)

        response = self.client.post(f"/api/calls/{call.id}/join/", {}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "call_not_joinable")
        token_mock.assert_not_called()

    @override_settings(LIVEKIT_URL="", LIVEKIT_API_KEY="", LIVEKIT_API_SECRET="")
    def test_missing_livekit_env_config_fails_safely(self):
        call = self.create_accepted_call()
        self.authenticate(self.caller)

        response = self.client.post(f"/api/calls/{call.id}/join/", {}, format="json")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "livekit_not_configured")
        self.assertNotIn("LIVEKIT_API_SECRET", response.data)
        self.assertNotIn("test-secret", str(response.data))

    @override_settings(
        LIVEKIT_URL="wss://livekit.qubrixe.com",
        LIVEKIT_API_KEY="test-key",
        LIVEKIT_API_SECRET="test-secret",
        LIVEKIT_TOKEN_TTL_MINUTES=15,
    )
    def test_generate_join_token_uses_expected_livekit_claims(self):
        class DummyVideoGrants:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class DummyAccessToken:
            instances = []

            def __init__(self, api_key, api_secret):
                self.api_key = api_key
                self.api_secret = api_secret
                self.identity = None
                self.name = None
                self.grants = None
                self.ttl = None
                DummyAccessToken.instances.append(self)

            def with_identity(self, identity):
                self.identity = identity
                return self

            def with_name(self, name):
                self.name = name
                return self

            def with_grants(self, grants):
                self.grants = grants
                return self

            def with_ttl(self, ttl):
                self.ttl = ttl
                return self

            def to_jwt(self):
                return "dummy-token"

        class DummyLiveKitApi:
            AccessToken = DummyAccessToken
            VideoGrants = DummyVideoGrants

        call = self.create_accepted_call()

        with patch("calls.livekit.livekit_api", DummyLiveKitApi):
            token = generate_join_token(self.caller, call)

        access_token = DummyAccessToken.instances[0]
        self.assertEqual(token, "dummy-token")
        self.assertEqual(access_token.api_key, "test-key")
        self.assertEqual(access_token.api_secret, "test-secret")
        self.assertEqual(access_token.identity, f"user_{self.caller.id}")
        self.assertEqual(access_token.name, self.caller.name)
        self.assertEqual(access_token.grants.kwargs["room_join"], True)
        self.assertEqual(access_token.grants.kwargs["room"], call.room_name)
        self.assertEqual(access_token.ttl.total_seconds(), 900)

    def test_livekit_identity_is_user_prefixed(self):
        self.assertEqual(livekit_identity(self.caller), f"user_{self.caller.id}")

    def assert_call_event(self, layer, index, group, event_name, call):
        sent_group, message = layer.calls[index]
        self.assertEqual(sent_group, group)
        self.assertEqual(message["type"], f"{event_name}_event")
        self.assertEqual(message["payload"]["type"], event_name)
        self.assertEqual(message["payload"]["call"]["id"], call.id)
        self.assertEqual(message["payload"]["call"]["call_type"], call.call_type)
        self.assertEqual(message["payload"]["call"]["status"], call.status)
        self.assertEqual(message["payload"]["call"]["caller"]["id"], self.caller.id)
        self.assertEqual(message["payload"]["call"]["receiver"]["id"], self.receiver.id)

    @patch("calls.realtime.get_channel_layer")
    def test_start_call_sends_call_invite_to_receiver_group(self, channel_layer_mock):
        layer = DummyChannelLayer()
        channel_layer_mock.return_value = layer

        response = self.start_call()
        call = CallSession.objects.get(id=response.data["id"])

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(layer.calls), 2)
        self.assert_call_event(layer, 0, f"user_{self.receiver.id}", "call_invite", call)
        self.assert_call_event(layer, 1, f"user_{self.caller.id}", "call_ringing", call)

    @patch("calls.views.mark_call_missed_if_unanswered.apply_async")
    @patch("calls.views.send_incoming_call_notification.apply_async")
    @patch("calls.views.send_call_event")
    def test_start_call_triggers_incoming_call_push_task(
        self,
        event_mock,
        delay_mock,
        missed_timeout_mock,
    ):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.start_call(queue_tasks=True)

        self.assertEqual(response.status_code, 201)
        delay_mock.assert_called_once_with(
            (response.data["id"],),
            queue="call_notifications",
            priority=9,
        )
        missed_timeout_mock.assert_called_once()

    @override_settings(CALL_RING_TIMEOUT_SECONDS=60)
    @patch("calls.views.send_incoming_call_notification.apply_async")
    @patch("calls.views.mark_call_missed_if_unanswered.apply_async")
    @patch("calls.views.send_call_event")
    def test_start_call_schedules_missed_call_timeout(
        self,
        event_mock,
        apply_async_mock,
        incoming_push_mock,
    ):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.start_call(queue_tasks=True)

        self.assertEqual(response.status_code, 201)
        apply_async_mock.assert_called_once_with((response.data["id"],), countdown=60)
        incoming_push_mock.assert_called_once()

    @patch("calls.tasks.send_missed_call_notification.apply_async")
    @patch("calls.tasks.send_call_event")
    def test_ringing_call_becomes_missed_after_timeout_task(self, event_mock, delay_mock):
        call = self.create_ringing_call()

        result = mark_call_missed_if_unanswered(call.id)
        call.refresh_from_db()

        self.assertEqual(result, "Call marked missed")
        self.assertEqual(call.status, CallSession.Status.MISSED)
        self.assertIsNotNone(call.ended_at)
        self.assertEqual(call.duration_seconds, 0)
        self.assertIsNone(call.ended_by)
        event_mock.assert_any_call(self.caller.id, "call_missed", call)
        event_mock.assert_any_call(self.receiver.id, "call_missed", call)
        delay_mock.assert_called_once_with(
            (call.id,),
            queue="push_notifications",
            priority=5,
        )

    @patch("calls.tasks.send_missed_call_notification.apply_async")
    @patch("calls.tasks.send_call_event")
    def test_accepted_call_is_not_marked_missed(self, event_mock, delay_mock):
        call = self.create_accepted_call()

        result = mark_call_missed_if_unanswered(call.id)
        call.refresh_from_db()

        self.assertEqual(result, f"Call already {CallSession.Status.ACCEPTED}")
        self.assertEqual(call.status, CallSession.Status.ACCEPTED)
        event_mock.assert_not_called()
        delay_mock.assert_not_called()

    @patch("calls.tasks.send_missed_call_notification.apply_async")
    @patch("calls.tasks.send_call_event")
    def test_rejected_call_is_not_marked_missed(self, event_mock, delay_mock):
        call = self.create_ringing_call()
        call.status = CallSession.Status.REJECTED
        call.ended_at = timezone.now()
        call.ended_by = self.receiver
        call.save(update_fields=["status", "ended_at", "ended_by", "updated_at"])

        result = mark_call_missed_if_unanswered(call.id)
        call.refresh_from_db()

        self.assertEqual(result, f"Call already {CallSession.Status.REJECTED}")
        self.assertEqual(call.status, CallSession.Status.REJECTED)
        self.assertEqual(call.ended_by, self.receiver)
        event_mock.assert_not_called()
        delay_mock.assert_not_called()

    @patch("calls.tasks.send_missed_call_notification.apply_async")
    @patch("calls.tasks.send_call_event")
    def test_cancelled_call_is_not_marked_missed(self, event_mock, delay_mock):
        call = self.create_ringing_call()
        call.status = CallSession.Status.CANCELLED
        call.ended_at = timezone.now()
        call.ended_by = self.caller
        call.save(update_fields=["status", "ended_at", "ended_by", "updated_at"])

        result = mark_call_missed_if_unanswered(call.id)
        call.refresh_from_db()

        self.assertEqual(result, f"Call already {CallSession.Status.CANCELLED}")
        self.assertEqual(call.status, CallSession.Status.CANCELLED)
        self.assertEqual(call.ended_by, self.caller)
        event_mock.assert_not_called()
        delay_mock.assert_not_called()

    @patch("calls.tasks.send_missed_call_notification.apply_async")
    @patch("calls.realtime.get_channel_layer")
    def test_missed_event_is_emitted(self, channel_layer_mock, delay_mock):
        layer = DummyChannelLayer()
        channel_layer_mock.return_value = layer
        call = self.create_ringing_call()

        mark_call_missed_if_unanswered(call.id)
        call.refresh_from_db()

        self.assertEqual(len(layer.calls), 2)
        self.assert_call_event(layer, 0, f"user_{self.caller.id}", "call_missed", call)
        self.assert_call_event(layer, 1, f"user_{self.receiver.id}", "call_missed", call)
        delay_mock.assert_called_once_with(
            (call.id,),
            queue="push_notifications",
            priority=5,
        )

    @patch("calls.realtime.get_channel_layer")
    def test_accept_call_sends_call_accepted_to_caller_group(self, channel_layer_mock):
        layer = DummyChannelLayer()
        channel_layer_mock.return_value = layer
        call = self.create_ringing_call()
        self.authenticate(self.receiver)

        response = self.client.post(f"/api/calls/{call.id}/accept/", {}, format="json")
        call.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(layer.calls), 2)
        self.assert_call_event(layer, 0, f"user_{self.caller.id}", "call_accepted", call)
        self.assert_call_event(layer, 1, f"user_{self.receiver.id}", "call_accepted", call)

    @patch("calls.realtime.get_channel_layer")
    def test_reject_call_sends_call_rejected_to_caller_group(self, channel_layer_mock):
        layer = DummyChannelLayer()
        channel_layer_mock.return_value = layer
        call = self.create_ringing_call()
        self.authenticate(self.receiver)

        response = self.client.post(f"/api/calls/{call.id}/reject/", {}, format="json")
        call.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(layer.calls), 1)
        self.assert_call_event(layer, 0, f"user_{self.caller.id}", "call_rejected", call)

    @patch("calls.realtime.get_channel_layer")
    def test_cancel_call_sends_call_cancelled_to_receiver_group(self, channel_layer_mock):
        layer = DummyChannelLayer()
        channel_layer_mock.return_value = layer
        call = self.create_ringing_call()
        self.authenticate(self.caller)

        response = self.client.post(f"/api/calls/{call.id}/cancel/", {}, format="json")
        call.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(layer.calls), 1)
        self.assert_call_event(layer, 0, f"user_{self.receiver.id}", "call_cancelled", call)

    @patch("calls.realtime.get_channel_layer")
    def test_end_call_sends_call_ended_to_both_participants(self, channel_layer_mock):
        layer = DummyChannelLayer()
        channel_layer_mock.return_value = layer
        call = self.create_ringing_call()
        call.status = CallSession.Status.ACCEPTED
        call.accepted_at = timezone.now() - timedelta(seconds=10)
        call.save(update_fields=["status", "accepted_at", "updated_at"])
        self.authenticate(self.receiver)

        response = self.client.post(f"/api/calls/{call.id}/end/", {}, format="json")
        call.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(layer.calls), 2)
        self.assert_call_event(layer, 0, f"user_{self.caller.id}", "call_ended", call)
        self.assert_call_event(layer, 1, f"user_{self.receiver.id}", "call_ended", call)

    def test_incoming_call_push_payload_includes_required_keys(self):
        call = self.create_ringing_call()

        payload = incoming_call_payload(call)

        self.assertEqual(payload["type"], "incoming_call")
        self.assertEqual(payload["call_id"], str(call.id))
        self.assertEqual(payload["caller_id"], str(self.caller.id))
        self.assertEqual(payload["caller_name"], self.caller.name)
        self.assertIn("caller_profile_picture", payload)
        self.assertEqual(payload["call_type"], call.call_type)
        self.assertEqual(payload["room_name"], call.room_name)

    def test_missed_call_push_payload_includes_required_keys(self):
        call = self.create_ringing_call()

        payload = missed_call_payload(call)

        self.assertEqual(payload["type"], "missed_call")
        self.assertEqual(payload["call_id"], str(call.id))
        self.assertEqual(payload["caller_id"], str(self.caller.id))
        self.assertEqual(payload["caller_name"], self.caller.name)
        self.assertEqual(payload["call_type"], call.call_type)

    @patch("calls.notifications.messaging.send")
    def test_no_push_is_sent_when_receiver_has_no_fcm_token(self, send_mock):
        call = self.create_ringing_call()
        call.receiver.fcm_token = None

        result = send_incoming_call_push(call)

        self.assertEqual(result, "No FCM token for recipient")
        send_mock.assert_not_called()

    @patch("calls.notifications.get_firebase_app", return_value=object())
    @patch("calls.notifications.messaging.send")
    def test_invalid_token_handling_does_not_crash(self, send_mock, firebase_mock):
        call = self.create_ringing_call()
        call.receiver.fcm_token = "stale-token"
        call.receiver.save(update_fields=["fcm_token"])
        send_mock.side_effect = Exception("Requested entity was not found")

        result = send_incoming_call_push(call)

        self.assertEqual(result, "Invalid FCM token cleared")
        self.receiver.refresh_from_db()
        self.assertIsNone(self.receiver.fcm_token)

    @patch("calls.livekit.check_room_state")
    def test_stale_call_cleanup_missing_room(self, mock_check):
        # 1. Active call with missing LiveKit room is cleaned (treated as stale)
        mock_check.return_value = (False, 0, True)

        call = self.create_accepted_call()
        call.accepted_at = timezone.now() - timedelta(seconds=200)
        call.save(update_fields=["accepted_at", "updated_at"])

        from .tasks import cleanup_stale_active_calls
        cleaned_count = cleanup_stale_active_calls()

        self.assertEqual(cleaned_count, 1)
        call.refresh_from_db()
        self.assertEqual(call.status, CallSession.Status.ENDED)
        self.assertIsNotNone(call.ended_at)
        self.assertGreaterEqual(call.duration_seconds, 200)

    @patch("calls.livekit.check_room_state")
    def test_active_call_with_participants_not_cleaned(self, mock_check):
        # 2. Active call with participant count > 0 is not cleaned
        mock_check.return_value = (True, 2, True)

        call = self.create_accepted_call()
        call.accepted_at = timezone.now() - timedelta(seconds=200)
        call.save(update_fields=["accepted_at", "updated_at"])

        from .tasks import cleanup_stale_active_calls
        cleaned_count = cleanup_stale_active_calls()

        self.assertEqual(cleaned_count, 0)
        call.refresh_from_db()
        self.assertEqual(call.status, CallSession.Status.ACCEPTED)

    @patch("calls.livekit.check_room_state")
    def test_livekit_api_failure_fallback_to_heartbeat_not_stale(self, mock_check):
        # 3. LiveKit API failure does not crash and uses heartbeat (updated_at)
        mock_check.return_value = (False, 0, False)

        call = self.create_accepted_call()
        call.accepted_at = timezone.now() - timedelta(seconds=200)
        call.save(update_fields=["accepted_at"])

        from .tasks import cleanup_stale_active_calls
        cleaned_count = cleanup_stale_active_calls()

        self.assertEqual(cleaned_count, 0)
        call.refresh_from_db()
        self.assertEqual(call.status, CallSession.Status.ACCEPTED)

    @patch("calls.livekit.check_room_state")
    def test_livekit_api_failure_fallback_to_heartbeat_stale(self, mock_check):
        # 4. LiveKit API failure (is_available=False) and updated_at older than timeout -> cleaned!
        mock_check.return_value = (False, 0, False)

        call = self.create_accepted_call()
        call.accepted_at = timezone.now() - timedelta(seconds=200)
        call.save(update_fields=["accepted_at"])
        CallSession.objects.filter(id=call.id).update(updated_at=timezone.now() - timedelta(seconds=200))

        from .tasks import cleanup_stale_active_calls
        cleaned_count = cleanup_stale_active_calls()

        self.assertEqual(cleaned_count, 1)
        call.refresh_from_db()
        self.assertEqual(call.status, CallSession.Status.ENDED)

    @patch("calls.livekit.check_room_state")
    @patch("calls.realtime.get_channel_layer")
    def test_events_emitted_on_cleanup(self, channel_layer_mock, mock_check):
        # 5. Events emitted on cleanup
        mock_check.return_value = (False, 0, True)
        layer = DummyChannelLayer()
        channel_layer_mock.return_value = layer

        call = self.create_accepted_call()
        call.accepted_at = timezone.now() - timedelta(seconds=200)
        call.save(update_fields=["accepted_at", "updated_at"])

        from .tasks import cleanup_stale_active_calls
        cleanup_stale_active_calls()

        call.refresh_from_db()
        self.assertEqual(len(layer.calls), 2)
        self.assert_call_event(layer, 0, f"user_{self.caller.id}", "call_ended", call)
        self.assert_call_event(layer, 1, f"user_{self.receiver.id}", "call_ended", call)

    @patch("calls.livekit.check_room_state")
    def test_start_call_clears_stale_busy_call(self, mock_check):
        # 6. start call clears stale busy call before returning busy
        mock_check.return_value = (False, 0, True)

        stale_call = CallSession.objects.create(
            caller=self.third,
            receiver=self.receiver,
            call_type=CallSession.CallType.AUDIO,
            status=CallSession.Status.ACCEPTED,
            room_name="receiver-stale-room",
            accepted_at=timezone.now() - timedelta(seconds=200),
        )

        self.assertEqual(CallSession.objects.filter(status=CallSession.Status.ACCEPTED).count(), 1)

        response = self.start_call()

        self.assertEqual(response.status_code, 201)
        stale_call.refresh_from_db()
        self.assertEqual(stale_call.status, CallSession.Status.ENDED)

    @override_settings(
        LIVEKIT_URL="wss://livekit.qubrixe.com",
        LIVEKIT_API_KEY="test-key",
        LIVEKIT_API_SECRET="test-secret",
    )
    def test_check_room_state_success(self):
        # 7. Unit test check_room_state success and not_found behavior
        class DummyRoom:
            def __init__(self, name, num_participants):
                self.name = name
                self.num_participants = num_participants

        class DummyListRoomsResponse:
            def __init__(self, rooms):
                self.rooms = rooms

        class DummyRoomService:
            async def list_rooms(self, req):
                return DummyListRoomsResponse([DummyRoom("my-room", 3)])

            async def delete_room(self, req):
                pass

        class DummyLiveKitAPI:
            def __init__(self, url, key, secret):
                self.room = DummyRoomService()

            async def aclose(self):
                pass

        from .livekit import check_room_state
        with patch("calls.livekit.livekit_api.LiveKitAPI", DummyLiveKitAPI):
            exists, count, is_available = check_room_state("my-room")
            self.assertTrue(exists)
            self.assertEqual(count, 3)
            self.assertTrue(is_available)

            exists, count, is_available = check_room_state("other-room")
            self.assertFalse(exists)
            self.assertEqual(count, 0)
            self.assertTrue(is_available)
