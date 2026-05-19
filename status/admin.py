from django.contrib import admin

from .models import StatusPrivacyException, StatusView, UserStatus


@admin.register(UserStatus)
class UserStatusAdmin(admin.ModelAdmin):
    list_display = ["user", "status_type", "privacy", "is_active", "created_at", "expires_at"]
    list_filter = ["status_type", "privacy", "is_active"]
    search_fields = ["user__name", "user__phone_number", "text_content"]
    readonly_fields = ["id", "created_at"]


@admin.register(StatusView)
class StatusViewAdmin(admin.ModelAdmin):
    list_display = ["status", "viewer", "viewed_at"]
    list_filter = ["viewed_at"]


@admin.register(StatusPrivacyException)
class StatusPrivacyExceptionAdmin(admin.ModelAdmin):
    list_display = ["status_owner", "excepted_user", "exception_type"]
    list_filter = ["exception_type"]
