import uuid

from django.db import models
from django.utils import timezone

from users.models import User


class MessageStatus(models.TextChoices):
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    DELIVERED = "delivered", "Delivered"
    READ = "read", "Read"
    FAILED = "failed", "Failed"


class Chat(models.Model):
    user1 = models.ForeignKey(User, on_delete=models.CASCADE, related_name="chats_as_user1")
    user2 = models.ForeignKey(User, on_delete=models.CASCADE, related_name="chats_as_user2")
    created_at = models.DateTimeField(auto_now_add=True)
    last_activity = models.DateTimeField(default=timezone.now)
    deleted_for_user1 = models.BooleanField(default=False)
    deleted_for_user2 = models.BooleanField(default=False)
    deleted_at_user1 = models.DateTimeField(null=True, blank=True)
    deleted_at_user2 = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("user1", "user2")
        indexes = [
            models.Index(fields=["last_activity"]),
        ]

    def participants(self):
        return {self.user1_id, self.user2_id}

    def has_participant(self, user):
        return user.id in self.participants()

    def get_receiver(self, user):
        return self.user2 if self.user1 == user else self.user1


class Message(models.Model):
    MESSAGE_TYPES = [
        ("text", "Text"),
        ("image", "Image"),
        ("video", "Video"),
        ("audio", "Audio"),
        ("document", "Document"),
        ("location", "Location"),
    ]

    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(User, on_delete=models.CASCADE)
    encrypted_text = models.TextField()
    client_uuid = models.UUIDField(default=uuid.uuid4, db_index=True)
    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPES, default="text")
    reply_to = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replies")
    forwarded_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="forwards")
    is_forwarded = models.BooleanField(default=False)
    is_deleted_for_everyone = models.BooleanField(default=False)
    deleted_for_users = models.ManyToManyField(User, blank=True, related_name="deleted_messages")
    file = models.FileField(upload_to="chat_files/", null=True, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    file_size = models.BigIntegerField(null=True, blank=True)
    file_type = models.CharField(max_length=100, blank=True)
    duration = models.FloatField(null=True, blank=True)
    thumbnail = models.ImageField(upload_to="chat_thumbnails/", null=True, blank=True)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=MessageStatus.choices, default=MessageStatus.SENT)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["chat", "created_at"]),
            models.Index(fields=["sender", "created_at"]),
        ]


class MessageReceipt(models.Model):
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="statuses")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    sent_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("message", "user")
        indexes = [
            models.Index(fields=["user", "read_at"]),
        ]


class DeletedMessage(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    message = models.ForeignKey(Message, on_delete=models.CASCADE)
    deleted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "message")


class MessageReaction(models.Model):
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    emoji = models.CharField(max_length=10)
    reacted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("message", "user")
