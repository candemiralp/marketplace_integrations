import frappe
from frappe.tests.utils import FrappeTestCase


class TestSmoke(FrappeTestCase):
	"""Proves the test runner picks up tests in this app and the app is installed on the test site."""

	def test_app_is_installed(self):
		self.assertIn("marketplace_integrations", frappe.get_installed_apps())

	def test_bol_module_exists(self):
		self.assertEqual(frappe.db.get_value("Module Def", "Bol", "app_name"), "marketplace_integrations")
