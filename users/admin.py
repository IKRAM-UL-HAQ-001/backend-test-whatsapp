from django.contrib import admin

from .models import Device


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("user", "platform", "device_id", "app_version", "updated_at")
    list_filter = ("platform",)
    search_fields = ("user__phone_number", "device_id")
