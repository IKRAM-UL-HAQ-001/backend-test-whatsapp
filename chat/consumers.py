import json

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from django.core.cache import cache
from django.utils import timezone
from .models import Chat, Message, MessageReceipt, MessageStatus
from users.models import User


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        query_string = self.scope["query_string"].decode()
        params = dict(part.split("=") for part in query_string.split("&") if "=" in part)
        ticket = params.get("ticket")

        self.user = await self.get_user(ticket)
        if self.user:
            self.group_name = f"user_{self.user.id}"
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
            await self.mark_presence()
        else:
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    @database_sync_to_async
    def get_user(self, ticket):
        cache_key = f"ws-ticket:{ticket}"
        user_id = cache.get(cache_key)
        if user_id is None:
            return None
        cache.delete(cache_key)
        return User.objects.filter(id=user_id).first()

    @database_sync_to_async
    def mark_presence(self):
        cache.set(f"presence:{self.user.id}", "online", timeout=45)

    async def receive(self, text_data=None, bytes_data=None):
        await self.mark_presence()
        if not text_data:
            return
        payload = json.loads(text_data)
        event = payload.get("type") or payload.get("event")
        if event == "heartbeat":
            await self._send_event("heartbeat", {"ok": True})
        elif event in {"messages_delivered", "message_delivered"}:
            message_ids = payload.get("message_ids") or payload.get("message_id") or []
            if not isinstance(message_ids, list):
                message_ids = [message_ids]
            await self.mark_messages_delivered(message_ids)
        elif event == "chat_opened":
            await self.mark_chat_read(payload.get("chat_id"))

    async def _send_event(self, event_name, payload):
        await self.send(
            text_data=json.dumps(
                {
                    "event": event_name,
                    "type": event_name,
                    "payload": payload,
                }
            )
        )

    async def chat_message_event(self, event):
        payload = event.get("payload", {})
        sender_id = str(payload.get("sender", ""))
        if sender_id != str(getattr(self.user, "id", "")):
            await self.mark_messages_delivered([payload.get("id")])
        await self._send_event("chat_message", payload)

    async def message_status_event(self, event):
        await self._send_event("message_status", event.get("payload", {}))

    async def status_update_event(self, event):
        await self._send_event("status_update", event.get("payload", {}))

    async def message_edited_event(self, event):
        await self._send_event("message_edited", event.get("payload", {}))

    async def message_deleted_event(self, event):
        await self._send_event("message_deleted", event.get("payload", {}))

    async def reaction_update_event(self, event):
        await self._send_event("reaction_update", event.get("payload", {}))

    async def typing_event(self, event):
        await self._send_event("typing", event.get("payload", {}))

    async def new_user_status_event(self, event):
        await self._send_event("new_user_status", event.get("payload", {}))

    async def status_viewed_event(self, event):
        await self._send_event("status_viewed", event.get("payload", {}))

    @database_sync_to_async
    def mark_messages_delivered(self, message_ids):
        now = timezone.now()
        messages = (
            Message.objects.filter(id__in=[mid for mid in message_ids if mid])
            .filter(chat__in=Chat.objects.filter(user1=self.user) | Chat.objects.filter(user2=self.user))
            .exclude(sender=self.user)
            .exclude(status__in=[MessageStatus.DELIVERED, MessageStatus.READ])
        )
        by_sender_chat = {}
        for message in messages:
            message.status = MessageStatus.DELIVERED
            message.delivered_at = now
            message.save(update_fields=["status", "delivered_at"])
            MessageReceipt.objects.filter(message=message, user=self.user).update(delivered_at=now)
            by_sender_chat.setdefault((message.sender_id, message.chat_id), []).append(str(message.id))
        for (sender_id, chat_id), ids in by_sender_chat.items():
            async_to_sync(self.channel_layer.group_send)(
                f"user_{sender_id}",
                {
                    "type": "status_update_event",
                    "payload": {
                        "message_ids": ids,
                        "chat_id": chat_id,
                        "status": MessageStatus.DELIVERED,
                        "delivered_at": now.isoformat(),
                    },
                },
            )

    @database_sync_to_async
    def mark_chat_read(self, chat_id):
        if not chat_id:
            return
        chat = Chat.objects.filter(id=chat_id).first()
        if not chat or not chat.has_participant(self.user):
            return
        now = timezone.now()
        messages = Message.objects.filter(chat=chat).exclude(sender=self.user).exclude(status=MessageStatus.READ)
        by_sender_chat = {}
        for message in messages:
            if message.delivered_at is None:
                message.delivered_at = now
            message.status = MessageStatus.READ
            message.read_at = now
            message.save(update_fields=["status", "delivered_at", "read_at"])
            MessageReceipt.objects.filter(message=message, user=self.user).update(delivered_at=message.delivered_at, read_at=now)
            by_sender_chat.setdefault((message.sender_id, message.chat_id), []).append(str(message.id))
        for (sender_id, chat_id), ids in by_sender_chat.items():
            async_to_sync(self.channel_layer.group_send)(
                f"user_{sender_id}",
                {
                    "type": "status_update_event",
                    "payload": {"message_ids": ids, "chat_id": chat_id, "status": MessageStatus.READ, "read_at": now.isoformat()},
                },
            )
