from unittest.mock import patch

from rest_framework.test import APIClient, APITestCase

from chat.models import Chat, Message, MessageReceipt, MessageStatus
from chat.tasks import send_message_notification
from users.models import User


class DummyChannelLayer:
    def __init__(self):
        self.calls = []

    async def group_send(self, group, message):
        self.calls.append((group, message))


class ChatApiTests(APITestCase):
    def setUp(self):
        self.sender = User.all_objects.create(
            country_code="+1",
            phone_number="+11111111111",
            name="Sender",
            is_verified=True,
            fcm_token="receiver-token",
        )
        self.receiver = User.all_objects.create(
            country_code="+1",
            phone_number="+12222222222",
            name="Receiver",
            is_verified=True,
            fcm_token="receiver-token",
        )
        self.third = User.all_objects.create(
            country_code="+1",
            phone_number="+13333333333",
            name="Third",
            is_verified=True,
        )

    @patch("chat.views.get_channel_layer")
    @patch("chat.views.send_message_notification.apply_async")
    def test_send_message_creates_status_and_broadcasts(self, push_mock, channel_layer_mock):
        dummy_channel_layer = DummyChannelLayer()
        channel_layer_mock.return_value = dummy_channel_layer
        client = APIClient()
        client.force_authenticate(user=self.sender)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                "/api/send/",
                {
                    "receiver_id": self.receiver.id,
                    "encrypted_text": "hello world",
                    "message_type": "text",
                    "client_uuid": "11111111-1111-1111-1111-111111111111",
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Chat.objects.count(), 1)
        self.assertEqual(len(dummy_channel_layer.calls), 2)
        push_mock.assert_called_once()

    @patch("chat.views.get_channel_layer")
    @patch("chat.views.send_message_notification.apply_async", side_effect=Exception("broker down"))
    def test_send_message_succeeds_when_push_queue_is_unavailable(self, push_mock, channel_layer_mock):
        channel_layer_mock.return_value = DummyChannelLayer()
        client = APIClient()
        client.force_authenticate(user=self.sender)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                "/api/send/",
                {
                    "receiver_id": self.receiver.id,
                    "encrypted_text": "hello while broker is down",
                    "message_type": "text",
                    "client_uuid": "22222222-2222-4222-8222-222222222222",
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Message.objects.count(), 1)
        push_mock.assert_called_once()

    def test_forward_message_requires_membership(self):
        chat = Chat.objects.create(user1=self.sender, user2=self.receiver)
        original = Message.objects.create(
            chat=chat,
            sender=self.sender,
            encrypted_text="unread",
            message_type="text",
        )
        client = APIClient()
        client.force_authenticate(user=self.third)
        response = client.post(
            "/api/forward/",
            {"receiver_id": self.receiver.id, "forwarded_from": original.id, "encrypted_text": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_react_message_requires_membership(self):
        chat = Chat.objects.create(user1=self.sender, user2=self.receiver)
        message = Message.objects.create(
            chat=chat,
            sender=self.sender,
            encrypted_text="hello",
            message_type="text",
        )
        client = APIClient()
        client.force_authenticate(user=self.third)
        response = client.post("/api/react/", {"message_id": message.id, "emoji": "👍"}, format="json")
        self.assertEqual(response.status_code, 403)

    @patch("chat.tasks.messaging.send", return_value="firebase-message-id")
    @patch("chat.tasks.get_firebase_app", return_value=object())
    @patch("chat.tasks.get_channel_layer")
    def test_push_provider_acceptance_marks_message_delivered(
        self,
        channel_layer_mock,
        firebase_app_mock,
        firebase_send_mock,
    ):
        channel_layer_mock.return_value = DummyChannelLayer()
        chat = Chat.objects.create(user1=self.sender, user2=self.receiver)
        message = Message.objects.create(
            chat=chat,
            sender=self.sender,
            encrypted_text="wait for device receipt",
            message_type="text",
            status=MessageStatus.SENT,
        )
        MessageReceipt.objects.create(message=message, user=self.receiver)

        send_message_notification.run(message.id, self.receiver.id)

        message.refresh_from_db()
        receipt = MessageReceipt.objects.get(message=message, user=self.receiver)
        self.assertEqual(message.status, MessageStatus.DELIVERED)
        self.assertIsNotNone(message.delivered_at)
        self.assertIsNotNone(receipt.delivered_at)
        firebase_send_mock.assert_called_once()

    @patch("chat.views.get_channel_layer")
    def test_device_delivery_then_chat_read_progresses_receipts(self, channel_layer_mock):
        dummy_channel_layer = DummyChannelLayer()
        channel_layer_mock.return_value = dummy_channel_layer
        chat = Chat.objects.create(user1=self.sender, user2=self.receiver)
        message = Message.objects.create(
            chat=chat,
            sender=self.sender,
            encrypted_text="receipt lifecycle",
            message_type="text",
            status=MessageStatus.SENT,
        )
        MessageReceipt.objects.create(message=message, user=self.receiver)
        client = APIClient()
        client.force_authenticate(user=self.receiver)

        delivered = client.post(
            "/api/messages/delivered/",
            {"message_ids": [message.id]},
            format="json",
        )
        self.assertEqual(delivered.status_code, 200)
        message.refresh_from_db()
        self.assertEqual(message.status, MessageStatus.DELIVERED)

        read = client.post(
            "/api/messages/read/",
            {"chat_id": chat.id},
            format="json",
        )
        self.assertEqual(read.status_code, 200)
        message.refresh_from_db()
        receipt = MessageReceipt.objects.get(message=message, user=self.receiver)
        self.assertEqual(message.status, MessageStatus.READ)
        self.assertIsNotNone(receipt.delivered_at)
        self.assertIsNotNone(receipt.read_at)

    def test_messages_endpoint_is_paginated(self):
        chat = Chat.objects.create(user1=self.sender, user2=self.receiver)
        for index in range(40):
            Message.objects.create(
                chat=chat,
                sender=self.sender,
                encrypted_text=f"message-{index}",
                message_type="text",
            )
        client = APIClient()
        client.force_authenticate(user=self.receiver)
        response = client.get(f"/api/chats/{chat.id}/messages/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.data)
