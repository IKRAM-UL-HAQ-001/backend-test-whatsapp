from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0012_backfill_devices_from_fcm_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="devicelinktoken",
            name="device_name",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
    ]
