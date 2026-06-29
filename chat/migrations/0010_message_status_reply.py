from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0009_message_is_forwarded"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="status_reply",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
