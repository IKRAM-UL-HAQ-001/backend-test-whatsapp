from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0006_usercontact"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="phone_number",
            field=models.CharField(max_length=20, unique=True),
        ),
        migrations.AddField(
            model_name="user",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="user",
            name="is_deleted",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="user",
            name="is_staff",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="user",
            name="is_superuser",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="otp",
            name="phone_number",
            field=models.CharField(max_length=20),
        ),
        migrations.AddIndex(
            model_name="otp",
            index=models.Index(fields=["phone_number", "is_used", "created_at"], name="users_otp_phone_i_4c040f_idx"),
        ),
        migrations.AddField(
            model_name="devicelinktoken",
            name="consumed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="WebSocketTicket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ticket", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="websocket_tickets",
                        to="users.user",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="websocketticket",
            index=models.Index(fields=["ticket"], name="users_webso_ticket_4c5442_idx"),
        ),
        migrations.AddIndex(
            model_name="websocketticket",
            index=models.Index(fields=["user", "expires_at"], name="users_webso_user_id_f97041_idx"),
        ),
    ]
