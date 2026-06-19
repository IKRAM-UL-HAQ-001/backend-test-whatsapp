from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from users.models import DeviceLinkToken, OTP, User, UserContact
from users.validation import normalize_contact_phone


class AuthFlowTests(APITestCase):
    @override_settings(ENABLE_DEV_OTP=True, DEV_OTP_CODE="000000")
    def test_request_and_verify_otp_for_new_user(self):
        response = self.client.post(
            "/auth/request-otp/",
            {"country_code": "+1", "phone_number": "2025550133"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["otp"], "000000")

        verify_response = self.client.post(
            "/auth/verify-otp/",
            {
                "country_code": "+1",
                "phone_number": "2025550133",
                "otp": "000000",
            },
            format="json",
        )
        self.assertEqual(verify_response.status_code, 200)
        self.assertIn("access", verify_response.data)

    def test_request_otp_rejects_invalid_phone(self):
        response = self.client.post(
            "/auth/request-otp/",
            {"country_code": "+1", "phone_number": "abc"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(ENABLE_DEV_OTP=True, DEV_OTP_CODE="000000")
    def test_request_otp_rate_limit(self):
        for _ in range(5):
            self.client.post(
                "/auth/request-otp/",
                {"country_code": "+1", "phone_number": "2025550144"},
                format="json",
            )
        response = self.client.post(
            "/auth/request-otp/",
            {"country_code": "+1", "phone_number": "2025550144"},
            format="json",
        )
        self.assertEqual(response.status_code, 429)

    def test_verify_otp_rejects_expired_code(self):
        OTP.objects.create(phone_number="+15555555555", otp_code="123456")
        otp = OTP.objects.get(phone_number="+15555555555")
        otp.created_at = otp.created_at.replace(year=otp.created_at.year - 1)
        otp.save(update_fields=["created_at"])
        response = self.client.post(
            "/auth/verify-otp/",
            {"phone_number": "+15555555555", "otp": "123456"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class AccountAndLinkingTests(APITestCase):
    def setUp(self):
        self.user = User.all_objects.create(
            country_code="+1",
            phone_number="+16666666666",
            name="Owner",
            is_verified=True,
        )

    def test_delete_account_soft_deletes_user(self):
        OTP.objects.create(phone_number=self.user.phone_number, otp_code="654321")
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.post("/auth/delete-account/", {"otp": "654321"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_deleted)
        self.assertIsNotNone(self.user.deleted_at)

    def test_profile_can_be_read_and_picture_removed(self):
        self.user.profile_picture.name = "profiles/avatar.jpg"
        self.user.save(update_fields=["profile_picture"])
        client = APIClient()
        client.force_authenticate(user=self.user)

        profile_response = client.get("/auth/complete-profile/")
        self.assertEqual(profile_response.status_code, 200)
        self.assertIn("profiles/avatar.jpg", profile_response.data["user"]["profile_picture"])

        remove_response = client.post(
            "/auth/complete-profile/",
            {"remove_profile_picture": True},
            format="json",
        )
        self.assertEqual(remove_response.status_code, 200)
        self.assertIsNone(remove_response.data["user"]["profile_picture"])
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile_picture)

    @override_settings(LINK_STATUS_POLL_SECONDS=0)
    def test_device_link_token_consumed_on_first_success(self):
        token_obj = DeviceLinkToken.objects.create(
            token="token-1",
            user=self.user,
            is_active=True,
            access_token="a",
            refresh_token="b",
        )
        response = self.client.get("/auth/check-link-status/token-1/")
        self.assertEqual(response.status_code, 200)
        token_obj.refresh_from_db()
        self.assertIsNotNone(token_obj.consumed_at)
        second = self.client.get("/auth/check-link-status/token-1/")
        self.assertEqual(second.status_code, 400)


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "contact-sync-tests",
        }
    }
)
class ContactSyncTests(APITestCase):
    def setUp(self):
        self.owner = User.all_objects.create(
            country_code="+92",
            phone_number="+923009999999",
            name="Owner",
            is_verified=True,
        )
        self.contact_user = User.all_objects.create(
            country_code="+92",
            phone_number="+923001234567",
            name="Ali",
            is_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_normalize_contact_phone_handles_pakistan_mobile_formats(self):
        cases = [
            "+923001234567",
            "03001234567",
            "3001234567",
            "923001234567",
            "+92 (300) 123-4567",
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(normalize_contact_phone(value), "+923001234567")

    def test_list_users_matches_submitted_contact_formats(self):
        for value in ["+923001234567", "03001234567", "3001234567", "923001234567"]:
            with self.subTest(value=value):
                response = self.client.post(
                    "/auth/list-users/",
                    {"contacts": [{"phone_number": value, "name": "Ali Local"}]},
                    format="json",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.data), 1)
                self.assertEqual(response.data[0]["id"], str(self.contact_user.id))
                self.assertEqual(response.data[0]["phone"], "+923001234567")

    def test_sync_contacts_collapses_duplicate_formats(self):
        response = self.client.post(
            "/auth/sync-contacts/",
            {
                "contacts": [
                    {"phone_number": "03001234567", "name": "Ali"},
                    {"phone_number": "3001234567", "name": "Ali Duplicate"},
                    {"phone_number": "+92 300 1234567", "name": "Ali Plus"},
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        contacts = UserContact.objects.filter(user=self.owner, phone_number="+923001234567")
        self.assertEqual(contacts.count(), 1)
        self.assertEqual(contacts.first().contact_id, self.contact_user.id)

    def test_newly_created_user_appears_in_list_users_immediately(self):
        self.contact_user.delete()
        first = self.client.post(
            "/auth/list-users/",
            {"contacts": [{"phone_number": "03001234567", "name": "Ali"}]},
            format="json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.data), 0)

        new_user = User.all_objects.create(
            country_code="+92",
            phone_number="+923001234567",
            name="Ali",
            is_verified=True,
        )
        second = self.client.post(
            "/auth/list-users/",
            {"contacts": [{"phone_number": "3001234567", "name": "Ali"}]},
            format="json",
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(second.data), 1)
        self.assertEqual(second.data[0]["id"], str(new_user.id))
