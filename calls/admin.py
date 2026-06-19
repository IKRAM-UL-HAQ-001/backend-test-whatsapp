from django.contrib import admin

from .models import CallAttendee, CallSession


@admin.register(CallSession)
class CallSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "caller",
        "receiver",
        "call_type",
        "status",
        "provider",
        "room_name",
        "created_at",
    )
    list_filter = ("call_type", "status", "provider", "created_at")
    search_fields = (
        "room_name",
        "caller__phone_number",
        "receiver__phone_number",
        "caller__name",
        "receiver__name",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(CallAttendee)
class CallAttendeeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "call",
        "user",
        "chime_attendee_id",
        "joined_at",
        "left_at",
    )
    list_filter = ("joined_at",)
    search_fields = ("chime_attendee_id", "chime_external_user_id")
    readonly_fields = ("chime_join_token",)
