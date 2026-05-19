from rest_framework import serializers

from users.models import UserContact

from .models import StatusPrivacyException, StatusView, UserStatus


class StatusOwnerSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    profile_picture_url = serializers.SerializerMethodField()

    def get_profile_picture_url(self, obj):
        request = self.context.get("request")
        if obj.profile_picture and request:
            return request.build_absolute_uri(obj.profile_picture.url)
        return None


class StatusViewerSerializer(serializers.ModelSerializer):
    viewer_id = serializers.IntegerField(source="viewer.id")
    viewer_name = serializers.CharField(source="viewer.name")
    viewer_picture_url = serializers.SerializerMethodField()

    class Meta:
        model = StatusView
        fields = ["viewer_id", "viewer_name", "viewer_picture_url", "viewed_at"]

    def get_viewer_picture_url(self, obj):
        request = self.context.get("request")
        if obj.viewer.profile_picture and request:
            return request.build_absolute_uri(obj.viewer.profile_picture.url)
        return None


class CreateStatusSerializer(serializers.Serializer):
    status_type = serializers.ChoiceField(choices=["text", "image", "video"])
    text_content = serializers.CharField(required=False, allow_blank=True, default="")
    media_file = serializers.FileField(required=False, allow_null=True)
    background_color = serializers.CharField(required=False, default="#128C7E")
    font_size = serializers.IntegerField(required=False, default=28)
    privacy = serializers.ChoiceField(
        choices=["all_contacts", "except", "only"], required=False, default="all_contacts"
    )
    user_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list
    )

    def validate(self, data):
        if data["status_type"] == "text" and not data.get("text_content"):
            raise serializers.ValidationError("text_content is required for text statuses.")
        if data["status_type"] in ("image", "video") and not data.get("media_file"):
            raise serializers.ValidationError("media_file is required for image/video statuses.")
        if data.get("privacy") in ("except", "only") and not data.get("user_ids"):
            raise serializers.ValidationError("user_ids is required for this privacy option.")
        return data


class PrivacyUpdateSerializer(serializers.Serializer):
    privacy = serializers.ChoiceField(choices=["all_contacts", "except", "only"])
    user_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list
    )
