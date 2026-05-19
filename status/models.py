import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone

from users.models import User


class UserStatus(models.Model):
    STATUS_TYPES = [
        ("text", "Text"),
        ("image", "Image"),
        ("video", "Video"),
    ]
    PRIVACY_CHOICES = [
        ("all_contacts", "All Contacts"),
        ("except", "All Except"),
        ("only", "Only Share With"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="statuses")

    status_type = models.CharField(max_length=10, choices=STATUS_TYPES)
    text_content = models.TextField(blank=True, null=True)
    media_file = models.FileField(upload_to="statuses/", null=True, blank=True)
    thumbnail = models.ImageField(upload_to="status_thumbs/", null=True, blank=True)

    background_color = models.CharField(max_length=20, default="#128C7E")
    font_size = models.IntegerField(default=28)

    duration = models.FloatField(null=True, blank=True)

    privacy = models.CharField(max_length=20, choices=PRIVACY_CHOICES, default="all_contacts")

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField()
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "is_active", "expires_at"]),
        ]

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(hours=24)
        super().save(*args, **kwargs)


class StatusPrivacyException(models.Model):
    EXCEPTION_TYPES = [
        ("except", "Hide from this user"),
        ("only", "Show only to this user"),
    ]

    status_owner = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="privacy_exceptions"
    )
    excepted_user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="excepted_from"
    )
    exception_type = models.CharField(max_length=10, choices=EXCEPTION_TYPES)

    class Meta:
        unique_together = ["status_owner", "excepted_user", "exception_type"]


class StatusPrivacySetting(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="status_privacy_setting"
    )
    privacy = models.CharField(
        max_length=20,
        choices=UserStatus.PRIVACY_CHOICES,
        default="all_contacts",
    )
    updated_at = models.DateTimeField(auto_now=True)


class StatusView(models.Model):
    status = models.ForeignKey(
        UserStatus, on_delete=models.CASCADE, related_name="views"
    )
    viewer = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="viewed_statuses"
    )
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["status", "viewer"]
        ordering = ["-viewed_at"]
