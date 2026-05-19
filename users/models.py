from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

    def create_user(self, phone_number, password=None, **extra_fields):
        if not phone_number:
            raise ValueError("Phone number is required")
        user = self.model(phone_number=phone_number, **extra_fields)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        user = self.model(phone_number=phone_number, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user


class AllUserManager(BaseUserManager):
    def get_queryset(self):
        return super().get_queryset()


class User(AbstractBaseUser):
    country_code = models.CharField(max_length=5)
    phone_number = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100, blank=True)
    about = models.CharField(max_length=139, default="Available", blank=True)
    profile_picture = models.ImageField(upload_to="profiles/", null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    fcm_token = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()
    all_objects = AllUserManager()

    USERNAME_FIELD = "phone_number"

    def __str__(self):
        return self.phone_number

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.is_active = False
        self.fcm_token = None
        self.name = self.name or "Deleted User"
        self.save(update_fields=["is_deleted", "deleted_at", "is_active", "fcm_token", "name"])

    @property
    def last_login_display(self):
        return self.last_login

    def has_perm(self, perm, obj=None):
        return self.is_superuser

    def has_module_perms(self, app_label):
        return self.is_superuser


class OTP(models.Model):
    phone_number = models.CharField(max_length=20)
    otp_code = models.CharField(max_length=10)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["phone_number", "is_used", "created_at"]),
        ]

    def is_expired(self):
        return timezone.now() > self.created_at + timedelta(minutes=10)


class DeviceLinkToken(models.Model):
    token = models.CharField(max_length=100, unique=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    access_token = models.TextField(null=True, blank=True)
    refresh_token = models.TextField(null=True, blank=True)

    def is_expired(self):
        return timezone.now() > self.created_at + timedelta(minutes=5)

    def mark_consumed(self):
        self.consumed_at = timezone.now()
        self.save(update_fields=["consumed_at"])

    def __str__(self):
        return f"Token: {self.token} - Active: {self.is_active}"


class WebSocketTicket(models.Model):
    ticket = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="websocket_tickets")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["ticket"]),
            models.Index(fields=["user", "expires_at"]),
        ]

    def is_valid(self):
        return self.consumed_at is None and timezone.now() <= self.expires_at

    def consume(self):
        self.consumed_at = timezone.now()
        self.save(update_fields=["consumed_at"])


class UserContact(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="contacts")
    contact = models.ForeignKey(User, on_delete=models.CASCADE, related_name="contact_of", null=True, blank=True)
    phone_number = models.CharField(max_length=20)
    contact_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "phone_number"], name="unique_user_contact_phone"),
        ]

    def __str__(self):
        return f"{self.user.phone_number} has {self.phone_number} as contact"


class InviteLog(models.Model):
    invited_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="invite_logs")
    phone_number = models.CharField(max_length=20)
    contact_name = models.CharField(max_length=100, blank=True)
    invited_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default="pending")

    class Meta:
        indexes = [
            models.Index(fields=["invited_by", "invited_at"]),
            models.Index(fields=["phone_number"]),
        ]

    def __str__(self):
        return f"{self.invited_by.phone_number} invited {self.phone_number}"
