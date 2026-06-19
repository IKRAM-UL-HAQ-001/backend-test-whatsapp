from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class CallSession(models.Model):
    class CallType(models.TextChoices):
        AUDIO = "audio", "Audio"
        VIDEO = "video", "Video"

    class Status(models.TextChoices):
        INITIATED = "initiated", "Initiated"
        RINGING = "ringing", "Ringing"
        ACCEPTED = "accepted", "Accepted"
        ACTIVE = "active", "Active"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"
        MISSED = "missed", "Missed"
        ENDED = "ended", "Ended"
        FAILED = "failed", "Failed"
        BUSY = "busy", "Busy"

    TERMINAL_STATUSES = {
        Status.REJECTED,
        Status.CANCELLED,
        Status.MISSED,
        Status.ENDED,
        Status.FAILED,
        Status.BUSY,
    }

    caller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="outgoing_call_sessions",
    )
    receiver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="incoming_call_sessions",
    )
    call_type = models.CharField(max_length=10, choices=CallType.choices)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.INITIATED,
    )
    room_name = models.CharField(max_length=255, unique=True)
    started_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ended_call_sessions",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Provider tracking.
    provider = models.CharField(max_length=20, default="chime")

    # Amazon Chime fields — populated only when provider="chime".
    chime_meeting_id = models.CharField(max_length=128, null=True, blank=True)
    chime_media_region = models.CharField(max_length=32, null=True, blank=True)
    chime_external_meeting_id = models.CharField(max_length=128, null=True, blank=True)
    chime_meeting_data = models.JSONField(null=True, blank=True)
    provider_error_code = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["caller"]),
            models.Index(fields=["receiver"]),
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["room_name"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=~Q(caller=F("receiver")),
                name="calls_session_caller_receiver_different",
            ),
        ]

    def __str__(self):
        return f"{self.call_type} call {self.room_name} ({self.status})"

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL_STATUSES

    @property
    def is_active(self):
        return not self.is_terminal

    def calculate_duration(self):
        if not self.ended_at:
            return 0
        started_at = self.accepted_at or self.started_at
        if not started_at:
            return 0
        return max(0, int((self.ended_at - started_at).total_seconds()))


class CallAttendee(models.Model):
    """Tracks per-user Chime attendee credentials for a call session."""

    call = models.ForeignKey(
        CallSession,
        on_delete=models.CASCADE,
        related_name="attendees",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="call_attendees",
    )
    chime_attendee_id = models.CharField(max_length=128)
    chime_external_user_id = models.CharField(max_length=128)
    chime_join_token = models.TextField()
    joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["call", "user"],
                name="calls_attendee_unique_per_call_user",
            ),
        ]

    def __str__(self):
        return f"Attendee user_{self.user_id} for call_{self.call_id}"
