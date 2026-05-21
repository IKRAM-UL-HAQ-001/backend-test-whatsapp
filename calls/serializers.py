from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import CallSession


class CallParticipantSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    phone = serializers.SerializerMethodField()
    profile_picture = serializers.SerializerMethodField()

    def get_phone(self, obj):
        return obj.phone_number

    def get_profile_picture(self, obj):
        if not getattr(obj, "profile_picture", None):
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.profile_picture.url) if request else obj.profile_picture.url


class CallSessionSerializer(serializers.ModelSerializer):
    caller = CallParticipantSerializer(read_only=True)
    receiver = CallParticipantSerializer(read_only=True)
    ended_by = CallParticipantSerializer(read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    is_terminal = serializers.BooleanField(read_only=True)

    class Meta:
        model = CallSession
        fields = [
            "id",
            "caller",
            "receiver",
            "call_type",
            "status",
            "room_name",
            "started_at",
            "accepted_at",
            "ended_at",
            "duration_seconds",
            "ended_by",
            "created_at",
            "updated_at",
            "is_active",
            "is_terminal",
        ]


class StartCallSerializer(serializers.Serializer):
    receiver_id = serializers.IntegerField()
    call_type = serializers.ChoiceField(choices=CallSession.CallType.choices)

    def validate_receiver_id(self, value):
        User = get_user_model()
        receiver = User.objects.filter(id=value).first()
        if receiver is None:
            raise serializers.ValidationError("Receiver not found.")
        self.context["receiver"] = receiver
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        receiver = self.context.get("receiver")
        if request and receiver and request.user.id == receiver.id:
            raise serializers.ValidationError({"receiver_id": "Caller and receiver must be different."})
        return attrs
