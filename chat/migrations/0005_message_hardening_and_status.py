import uuid

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0007_hardening_fields_and_websocketticket"),
        ("chat", "0004_message_is_delivered_message_is_read"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="client_uuid",
            field=models.UUIDField(db_index=True, default=uuid.uuid4),
        ),
        migrations.AlterField(
            model_name="message",
            name="created_at",
            field=models.DateTimeField(db_index=True, default=django.utils.timezone.now),
        ),
        migrations.AddIndex(
            model_name="chat",
            index=models.Index(fields=["last_activity"], name="chat_chat_last_ac_164ad4_idx"),
        ),
        migrations.AddIndex(
            model_name="message",
            index=models.Index(fields=["chat", "created_at"], name="chat_messag_chat_id_28616d_idx"),
        ),
        migrations.AddIndex(
            model_name="message",
            index=models.Index(fields=["sender", "created_at"], name="chat_messag_sender__5a7cfe_idx"),
        ),
        migrations.RemoveField(
            model_name="message",
            name="is_delivered",
        ),
        migrations.RemoveField(
            model_name="message",
            name="is_read",
        ),
        migrations.CreateModel(
            name="MessageStatus",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                ("read_at", models.DateTimeField(blank=True, null=True)),
                (
                    "message",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="statuses", to="chat.message"),
                ),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="users.user")),
            ],
            options={"unique_together": {("message", "user")}},
        ),
        migrations.AddIndex(
            model_name="messagestatus",
            index=models.Index(fields=["user", "read_at"], name="chat_messag_user_id_4cb2e0_idx"),
        ),
    ]
