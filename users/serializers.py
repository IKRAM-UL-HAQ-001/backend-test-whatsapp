from rest_framework import serializers

from .models import User


class RequestOTPSerializer(serializers.Serializer):
    country_code = serializers.CharField()
    phone_number = serializers.CharField()


class VerifyOTPSerializer(serializers.Serializer):
    country_code = serializers.CharField()
    phone_number = serializers.CharField()
    otp = serializers.CharField(max_length=6)


class CompleteProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["name", "about", "profile_picture", "fcm_token"]


class ActivateLinkTokenSerializer(serializers.Serializer):
    token = serializers.CharField()


class ReverifyDeleteAccountSerializer(serializers.Serializer):
    otp = serializers.CharField(max_length=6)


class WebSocketTicketSerializer(serializers.Serializer):
    ticket = serializers.CharField(read_only=True)
    expires_in = serializers.IntegerField(read_only=True)


class SyncContactsSerializer(serializers.Serializer):
    contacts = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=True,
    )


class InviteContactSerializer(serializers.Serializer):
    phone = serializers.CharField()
    contact_name = serializers.CharField(required=False, allow_blank=True, default="")
