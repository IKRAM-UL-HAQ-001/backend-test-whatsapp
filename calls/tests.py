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
from .notifications import incoming_call_payload, missed_call_payload, send_incoming_call_push


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

    def start_call(self, call_type=CallSession.CallType.AUDIO):
        self.authenticate(self.caller)
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

    def test_receiver_accepts_call(self):
        call = self.create_ringing_call()
        self.authenticate(self.receiver)

        response = self.client.post(f"/api/calls/{call.id}/accept/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], CallSession.Status.ACCEPTED)
        self.assertIsNotNone(response.data["accepted_at"])

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

    def test_caller_cancels_ringing_call(self):
        call = self.create_ringing_call()
        self.authenticate(self.caller)

        response = self.client.post(f"/api/calls/{call.id}/cancel/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], CallSession.Status.CANCELLED)
        self.assertEqual(response.data["ended_by"]["id"], str(self.caller.id))

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

    @patch("calls.views.send_incoming_call_notification.delay")
    def test_start_call_triggers_incoming_call_push_task(self, delay_mock):
        response = self.start_call()

        self.assertEqual(response.status_code, 201)
        delay_mock.assert_called_once_with(response.data["id"])

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
