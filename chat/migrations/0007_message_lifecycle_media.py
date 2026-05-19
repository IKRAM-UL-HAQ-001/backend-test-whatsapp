# Generated manually for WhatsApp-style one-to-one message lifecycle and media metadata.

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0006_rename_chat_chat_last_ac_164ad4_idx_chat_chat_last_ac_2aa3f0_idx_and_more"),
        ("users", "0009_contact_phone_and_invitelog"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="MessageStatus",
            new_name="MessageReceipt",
        ),
        migrations.RenameIndex(
            model_name="messagereceipt",
            new_name="chat_messag_user_id_8ceef9_idx",
            old_name="chat_messag_user_id_03f6f7_idx",
        ),
        migrations.AddField(
            model_name="message",
            name="status",
            field=models.CharField(
                choices=[
                    ("sending", "Sending"),
                    ("sent", "Sent"),
                    ("delivered", "Delivered"),
                    ("read", "Read"),
                    ("failed", "Failed"),
                ],
                default="sent",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="message",
            name="delivered_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="read_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="edited_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="file_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="message",
            name="file_size",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="file_type",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="message",
            name="duration",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="thumbnail",
            field=models.ImageField(blank=True, null=True, upload_to="chat_thumbnails/"),
        ),
        migrations.AddField(
            model_name="message",
            name="width",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="height",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="deleted_for_users",
            field=models.ManyToManyField(
                blank=True,
                related_name="deleted_messages",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
