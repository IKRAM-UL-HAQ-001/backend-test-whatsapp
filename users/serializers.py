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
    remove_profile_picture = serializers.BooleanField(
        write_only=True,
        required=False,
        default=False,
    )

    class Meta:
        model = User
        fields = [
            "name",
            "about",
            "profile_picture",
            "remove_profile_picture",
            "fcm_token",
        ]

    def validate(self, attrs):
        if attrs.get("remove_profile_picture") and attrs.get("profile_picture"):
            raise serializers.ValidationError(
                "Upload a profile picture or remove the current one, not both."
            )
        return attrs

    def update(self, instance, validated_data):
        remove_picture = validated_data.pop("remove_profile_picture", False)
        replacement = validated_data.get("profile_picture")
        old_picture = instance.profile_picture
        old_name = old_picture.name if old_picture else ""
        old_storage = old_picture.storage if old_picture else None

        if remove_picture:
            validated_data["profile_picture"] = None

        instance = super().update(instance, validated_data)

        if old_name and (remove_picture or replacement is not None):
            current_name = instance.profile_picture.name if instance.profile_picture else ""
            if current_name != old_name and old_storage is not None:
                old_storage.delete(old_name)

        return instance


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
