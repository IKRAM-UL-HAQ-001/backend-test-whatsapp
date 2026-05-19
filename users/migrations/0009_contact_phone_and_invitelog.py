# Generated manually for contact discovery and invite logging.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_contact_phone_numbers(apps, schema_editor):
    UserContact = apps.get_model("users", "UserContact")
    for contact in UserContact.objects.select_related("contact").all():
        if contact.contact_id and not contact.phone_number:
            contact.phone_number = contact.contact.phone_number
            contact.save(update_fields=["phone_number"])


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0008_rename_users_otp_phone_i_4c040f_idx_users_otp_phone_n_33906b_idx_and_more"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="usercontact",
            unique_together=set(),
        ),
        migrations.AlterField(
            model_name="usercontact",
            name="contact",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="contact_of",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="usercontact",
            name="phone_number",
            field=models.CharField(default="", max_length=20),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_contact_phone_numbers, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="usercontact",
            constraint=models.UniqueConstraint(fields=("user", "phone_number"), name="unique_user_contact_phone"),
        ),
        migrations.CreateModel(
            name="InviteLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("phone_number", models.CharField(max_length=20)),
                ("contact_name", models.CharField(blank=True, max_length=100)),
                ("invited_at", models.DateTimeField(auto_now_add=True)),
                ("status", models.CharField(default="pending", max_length=20)),
                (
                    "invited_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="invite_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="invitelog",
            index=models.Index(fields=["invited_by", "invited_at"], name="users_invit_invited_420f8d_idx"),
        ),
        migrations.AddIndex(
            model_name="invitelog",
            index=models.Index(fields=["phone_number"], name="users_invit_phone_n_f39cab_idx"),
        ),
    ]
