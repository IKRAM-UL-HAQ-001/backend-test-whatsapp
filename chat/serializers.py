import json

from django.conf import settings
from rest_framework import serializers

from users.models import UserContact

from .models import Chat, DeletedMessage, Message, MessageReaction, MessageReceipt


class MessageReactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MessageReaction
        fields = ["user", "emoji"]


class ReplyForwardSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            "id",
            "sender",
            "encrypted_text",
            "message_type",
            "is_deleted_for_everyone",
            "file_url",
            "file_name",
            "file_type",
            "thumbnail_url",
            "created_at",
        ]

    def get_file_url(self, obj):
        if not obj.file:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url

    def get_thumbnail_url(self, obj):
        if not obj.thumbnail:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.thumbnail.url) if request else obj.thumbnail.url


class MessageStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = MessageReceipt
        fields = ["user", "sent_at", "delivered_at", "read_at"]


class MessageSerializer(serializers.ModelSerializer):
    is_reply = serializers.SerializerMethodField()
    is_own_reply = serializers.SerializerMethodField()
    is_forwarded = serializers.SerializerMethodField()
    is_forwarded_many = serializers.SerializerMethodField()
    is_deleted_for_everyone = serializers.BooleanField()
    is_own_message = serializers.SerializerMethodField()
    my_reaction = serializers.SerializerMethodField()
    reactions = MessageReactionSerializer(many=True, read_only=True)
    reply_to = ReplyForwardSerializer(read_only=True)
    forwarded_from = ReplyForwardSerializer(read_only=True)
    status = serializers.SerializerMethodField()
    is_deleted_for_me = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            "id",
            "chat",
            "sender",
            "client_uuid",
            "encrypted_text",
            "message_type",
            "reply_to",
            "status_reply",
            "forwarded_from",
            "is_reply",
            "is_own_reply",
            "is_forwarded",
            "is_forwarded_many",
            "is_deleted_for_everyone",
            "is_deleted_for_me",
            "is_own_message",
            "my_reaction",
            "reactions",
            "file",
            "file_url",
            "file_name",
            "file_size",
            "file_type",
            "duration",
            "thumbnail",
            "thumbnail_url",
            "width",
            "height",
            "status",
            "delivered_at",
            "read_at",
            "edited_at",
            "created_at",
        ]

    def get_is_reply(self, obj):
        return obj.reply_to_id is not None

    def get_is_own_reply(self, obj):
        user = self.context["request"].user
        return obj.reply_to is not None and obj.reply_to.sender_id == user.id

    def get_is_forwarded(self, obj):
        return obj.is_forwarded or obj.forwarded_from_id is not None

    def get_is_forwarded_many(self, obj):
        depth = 0
        current = obj.forwarded_from
        while current:
            depth += 1
            current = current.forwarded_from
        return depth > 1

    def get_my_reaction(self, obj):
        user = self.context["request"].user
        for reaction in obj.reactions.all():
            if reaction.user_id == user.id:
                return reaction.emoji
        return None

    def get_is_own_message(self, obj):
        user = self.context["request"].user
        return obj.sender_id == user.id

    def get_is_deleted_for_me(self, obj):
        user = self.context["request"].user
        return obj.deleted_for_users.filter(id=user.id).exists()

    def get_file_url(self, obj):
        if not obj.file:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url

    def get_thumbnail_url(self, obj):
        if not obj.thumbnail:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.thumbnail.url) if request else obj.thumbnail.url

    def get_status(self, obj):
        return {
            "state": obj.status,
            "delivered_at": obj.delivered_at,
            "read_at": obj.read_at,
        }


class SharedMediaSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    is_voice_message = serializers.SerializerMethodField()
    sent_at = serializers.DateTimeField(source="created_at")

    class Meta:
        model = Message
        fields = [
            "id",
            "file_url",
            "thumbnail_url",
            "file_name",
            "file_size",
            "file_type",
            "message_type",
            "duration",
            "is_voice_message",
            "sent_at",
        ]

    def get_file_url(self, obj):
        if not obj.file:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url

    def get_thumbnail_url(self, obj):
        if not obj.thumbnail:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.thumbnail.url) if request else obj.thumbnail.url

    def get_is_voice_message(self, obj):
        return obj.message_type == "audio" and obj.encrypted_text == "[File]"


class SendMessageSerializer(serializers.Serializer):
    receiver_id = serializers.IntegerField()
    encrypted_text = serializers.CharField()
    client_uuid = serializers.UUIDField(required=False)
    message_type = serializers.ChoiceField(choices=Message.MESSAGE_TYPES, default="text")
    reply_to = serializers.IntegerField(required=False)
    forwarded_from = serializers.IntegerField(required=False)
    file = serializers.FileField(required=False)
    duration = serializers.FloatField(required=False, allow_null=True)
    # Sent as a JSON string in multipart form data; parsed to a dict here.
    status_reply = serializers.CharField(required=False, allow_blank=True)

    def validate_status_reply(self, value):
        if not value:
            return None
        if isinstance(value, dict):
            return value
        try:
            data = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError("status_reply must be valid JSON") from exc
        if not isinstance(data, dict):
            raise serializers.ValidationError("status_reply must be a JSON object")
        return data


class DeleteMessageSerializer(serializers.Serializer):
    message_id = serializers.IntegerField()
    delete_type = serializers.ChoiceField(choices=["for_me", "for_everyone"], required=False)
    for_everyone = serializers.BooleanField(default=False, required=False)


class EditMessageSerializer(serializers.Serializer):
    message_id = serializers.IntegerField()
    encrypted_text = serializers.CharField()


class MessageIdsSerializer(serializers.Serializer):
    message_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)


class ReadMessagesSerializer(serializers.Serializer):
    chat_id = serializers.IntegerField()


class ReactSerializer(serializers.Serializer):
    message_id = serializers.IntegerField()
    emoji = serializers.CharField(max_length=10)


class TypingSerializer(serializers.Serializer):
    chat_id = serializers.IntegerField()
    is_typing = serializers.BooleanField()


class ChatSerializer(serializers.ModelSerializer):
    other_user = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    last_message_status = serializers.SerializerMethodField()
    unread_count = serializers.IntegerField(read_only=True)
    presence = serializers.SerializerMethodField()
    last_activity = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Chat
        fields = [
            "id",
            "other_user",
            "last_message",
            "last_message_status",
            "unread_count",
            "presence",
            "last_activity",
        ]

    def get_other_user(self, obj):
        user = self.context["request"].user
        other = obj.user2 if obj.user1 == user else obj.user1
        request = self.context.get("request")
        profile_url = None
        if other.profile_picture and hasattr(other.profile_picture, "url"):
            if request:
                profile_url = request.build_absolute_uri(other.profile_picture.url)
            else:
                profile_url = other.profile_picture.url
        saved_contact = UserContact.objects.filter(user=user, phone_number=other.phone_number).first()
        display_name = saved_contact.contact_name if saved_contact and saved_contact.contact_name else other.phone_number
        return {
            "id": other.id,
            "phone_number": other.phone_number,
            "phone": other.phone_number,
            "name": display_name,
            "contact_name": saved_contact.contact_name if saved_contact else "",
            "about": other.about,
            "profile_picture": profile_url,
            "profile_photo": profile_url,
        }

    def get_last_message(self, obj):
        if getattr(obj, "last_message_id", None):
            request = self.context.get("request")
            file_url = None
            last_message_file = getattr(obj, "last_message_file", None)
            if last_message_file:
                relative_url = f"{settings.MEDIA_URL}{last_message_file}"
                file_url = request.build_absolute_uri(relative_url) if request else relative_url
            return {
                "id": obj.last_message_id,
                "content": obj.last_message_content,
                "sender_id": obj.last_message_sender_id,
                "created_at": obj.last_message_created_at,
                "message_type": obj.last_message_type,
                "file_url": file_url,
                "status": getattr(obj, "last_message_status", "sent"),
            }
        return None

    def get_last_message_status(self, obj):
        return getattr(obj, "last_message_status", None) or "sent"

    def get_presence(self, obj):
        user = self.context["request"].user
        other = obj.user2 if obj.user1 == user else obj.user1
        return {
            "user_id": other.id,
            "is_online": getattr(obj, "other_user_online", False),
        }
