# Copyright (c) 2026, Magnatlas and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from marketplace_integrations.bol.api.errors import BolAuthError, BolTransientError
from marketplace_integrations.bol.doctype.bol_settings import bol_settings

# These tests need no Company; ERPNext's Company test records need ERPNext's full test setup.
test_ignore = ["Company"]


class TestBolSettings(FrappeTestCase):
	def _settings(self, **values):
		doc = frappe.get_single("Bol Settings")
		doc.update({"enabled": 0, "environment": "Demo", "client_id": "settings-test-client", **values})
		return doc

	def test_enabling_requires_credentials_and_company(self):
		doc = self._settings(enabled=1, client_id=None, client_secret=None, company=None)

		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.validate()
		for label in ("Client ID", "Client Secret", "Company"):
			self.assertIn(label, str(ctx.exception))

	def test_disabled_settings_need_nothing(self):
		self._settings(client_id=None, company=None).validate()

	def test_saving_never_calls_bol(self):
		with patch("requests.Session.request") as request:
			self._settings(client_secret="not-a-real-secret").save()

		request.assert_not_called()

	def test_connection_ok(self):
		with patch.object(bol_settings, "BolClient") as client:
			self.assertEqual(self._settings().test_connection(), {"ok": True})

		args, kwargs = client.return_value.request.call_args
		self.assertEqual(args, ("GET", "/retailer/orders"))
		self.assertEqual(kwargs["version"], 10)

	def test_connection_works_before_enabling(self):
		with patch.object(bol_settings, "BolClient"):
			self.assertEqual(self._settings(enabled=0).test_connection(), {"ok": True})

	def test_invalid_credentials_give_a_clear_error_and_a_log(self):
		error = BolAuthError("Invalid bol credentials", {"method": "POST", "status": 401})
		with (
			patch.object(bol_settings, "BolClient") as client,
			patch.object(bol_settings, "log_call") as log_call,
		):
			client.return_value.request.side_effect = error
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._settings().test_connection()

		self.assertIn("bol rejected the credentials", str(ctx.exception))
		self.assertNotIn("Traceback", str(ctx.exception))
		log_call.assert_called_once_with(
			method="POST", status=401, error=error, reference=("Bol Settings", "Bol Settings")
		)

	def test_unreachable_bol_gives_a_clear_error(self):
		with (
			patch.object(bol_settings, "BolClient") as client,
			patch.object(bol_settings, "log_call") as log_call,
		):
			client.return_value.request.side_effect = BolTransientError("bol unavailable (HTTP 503)")
			with self.assertRaisesRegex(frappe.ValidationError, "Connection to bol failed"):
				self._settings().test_connection()
		log_call.assert_called_once()

	def test_connection_requires_permission(self):
		doc = self._settings()
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		with patch.object(bol_settings, "BolClient") as client, self.assertRaises(frappe.PermissionError):
			doc.test_connection()
		client.assert_not_called()
