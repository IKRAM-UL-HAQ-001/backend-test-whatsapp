"""Re-normalize existing UserContact phone numbers to E.164.

User.phone_number is already stored in E.164 (it is created via
users.validation.normalize_phone / libphonenumber at signup), so registered
users do NOT need backfilling. UserContact rows synced before the
normalization fix, however, may hold mis-normalized numbers (e.g. a contact
saved as "00923435149587" became "+920923435149587"), which is why those
contacts stopped matching. This command rewrites every UserContact.phone_number
through the corrected normalizer and re-links the contact FK to the matching
user.

Idempotent and safe to re-run:

    python manage.py backfill_normalized_contacts          # apply
    python manage.py backfill_normalized_contacts --dry-run  # preview only
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from users.models import User, UserContact
from users.validation import normalize_contact_phone


class Command(BaseCommand):
    help = "Re-normalize UserContact phone numbers to E.164 and re-link matches."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        default_cc = settings.CONTACT_DEFAULT_COUNTRY_CODE

        # Canonical phone -> user lookup so we can (re)link the contact FK.
        users_by_phone = {
            u.phone_number: u
            for u in User.objects.all().only("id", "phone_number")
        }

        updated = relinked = removed = 0
        # Track (user_id, normalized_phone) we've already kept to collapse the
        # duplicates that re-normalization can create under the unique
        # constraint (user, phone_number).
        seen = set()

        contacts = UserContact.objects.all().only(
            "id", "user_id", "phone_number", "contact_id"
        )
        for contact in contacts.iterator():
            normalized = normalize_contact_phone(contact.phone_number, default_cc)
            if not normalized:
                continue

            key = (contact.user_id, normalized)
            if key in seen:
                # Re-normalizing produced a duplicate for this user -> drop it.
                removed += 1
                if not dry_run:
                    contact.delete()
                continue
            seen.add(key)

            matched_user = users_by_phone.get(normalized)
            new_contact_id = matched_user.id if matched_user else None

            changed_fields = []
            if contact.phone_number != normalized:
                contact.phone_number = normalized
                changed_fields.append("phone_number")
            if contact.contact_id != new_contact_id:
                contact.contact_id = new_contact_id
                changed_fields.append("contact")
                relinked += 1

            if changed_fields:
                updated += 1
                if not dry_run:
                    with transaction.atomic():
                        contact.save(update_fields=changed_fields)

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}UserContacts updated={updated} relinked={relinked} "
                f"duplicates_removed={removed}"
            )
        )
