from django.contrib import admin

from .models import CallSession


@admin.register(CallSession)
class CallSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "caller",
        "receiver",
        "call_type",
        "status",
        "room_name",
        "created_at",
    )
    list_filter = ("call_type", "status", "created_at")
    search_fields = (
        "room_name",
        "caller__phone_number",
        "receiver__phone_number",
        "caller__name",
        "receiver__name",
    )
    readonly_fields = ("created_at", "updated_at")
