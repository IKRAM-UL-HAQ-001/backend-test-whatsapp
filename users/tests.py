from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from users.models import DeviceLinkToken, OTP, User


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
