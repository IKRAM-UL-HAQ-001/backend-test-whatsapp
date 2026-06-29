from django.db import migrations


def backfill_devices(apps, schema_editor):
    """Copy each user's existing single fcm_token into a legacy Android Device row.

    The real device_id is unknown for pre-existing tokens, so use a stable
    synthetic id. The client re-registers with its true device_id on next launch,
    which upserts a fresh row; this legacy row just keeps push working until then.
    """
    User = apps.get_model("users", "User")
    Device = apps.get_model("users", "Device")
    for user in User.objects.exclude(fcm_token__isnull=True).exclude(fcm_token="").iterator():
        Device.objects.get_or_create(
            user_id=user.id,
            device_id="legacy-android",
            defaults={"platform": "android", "fcm_token": user.fcm_token},
        )


def noop_reverse(apps, schema_editor):
    Device = apps.get_model("users", "Device")
    Device.objects.filter(device_id="legacy-android").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0011_device"),
    ]

    operations = [
        migrations.RunPython(backfill_devices, noop_reverse),
    ]
