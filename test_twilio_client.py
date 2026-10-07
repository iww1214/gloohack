"""Offline tests for Twilio credential selection and sender configuration."""

import os
import unittest
from unittest.mock import MagicMock, patch

import twilio_client


class TwilioClientConfigTests(unittest.TestCase):
    def test_api_key_credentials_take_precedence(self):
        fake_client = MagicMock()
        environment = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_API_KEY_SID": "SK_test",
            "TWILIO_API_KEY_SECRET": "secret_test",
            "TWILIO_AUTH_TOKEN": "legacy_test",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            twilio_client, "Client", return_value=fake_client
        ) as factory:
            self.assertIs(twilio_client.create_twilio_client(), fake_client)
        factory.assert_called_once_with("SK_test", "secret_test", account_sid="AC_test")

    def test_documented_twilio_api_key_variable_names_are_supported(self):
        fake_client = MagicMock()
        environment = {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_API_KEY": "SK_test",
            "TWILIO_API_SECRET": "secret_test",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            twilio_client, "Client", return_value=fake_client
        ) as factory:
            self.assertIs(twilio_client.create_twilio_client(), fake_client)
        factory.assert_called_once_with("SK_test", "secret_test", account_sid="AC_test")

    def test_legacy_auth_token_is_supported_as_fallback(self):
        fake_client = MagicMock()
        with patch.dict(os.environ, {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_AUTH_TOKEN": "legacy_test",
        }, clear=True), patch.object(twilio_client, "Client", return_value=fake_client) as factory:
            self.assertIs(twilio_client.create_twilio_client(), fake_client)
        factory.assert_called_once_with("AC_test", "legacy_test")

    def test_partial_api_key_configuration_fails_closed(self):
        with patch.dict(os.environ, {"TWILIO_API_KEY_SID": "SK_test"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Set both Twilio API key"):
                twilio_client.create_twilio_client()

    def test_api_key_requires_account_sid(self):
        with patch.dict(os.environ, {
            "TWILIO_API_KEY_SID": "SK_test",
            "TWILIO_API_KEY_SECRET": "secret_test",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TWILIO_ACCOUNT_SID"):
                twilio_client.create_twilio_client()

    def test_account_sid_must_not_be_api_key_sid(self):
        with patch.dict(os.environ, {
            "TWILIO_ACCOUNT_SID": "SK_misplaced",
            "TWILIO_API_KEY_SID": "SK_key",
            "TWILIO_API_KEY_SECRET": "secret_test",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Account SID"):
                twilio_client.create_twilio_client()

    def test_api_key_secret_must_not_be_key_sid(self):
        with patch.dict(os.environ, {
            "TWILIO_ACCOUNT_SID": "AC_test",
            "TWILIO_API_KEY_SID": "SK_key",
            "TWILIO_API_KEY_SECRET": "SK_misplaced",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "32-character secret"):
                twilio_client.create_twilio_client()

    def test_sender_accepts_both_supported_variable_names(self):
        with patch.dict(os.environ, {"TWILIO_FROM_PHONE": "+10000000001"}, clear=True):
            self.assertEqual(twilio_client.get_twilio_from_phone(), "+10000000001")
        with patch.dict(os.environ, {"TWILIO_PHONE_NUMBER": "+10000000002"}, clear=True):
            self.assertEqual(twilio_client.get_twilio_from_phone(), "+10000000002")


if __name__ == "__main__":
    unittest.main()
