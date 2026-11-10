from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from marketplace_integrations import install
from marketplace_integrations.setup.custom_fields import CUSTOM_FIELDS

# A Section Break has no DB column, so creating it runs no DDL (which would commit implicitly).
TEST_FIELDS = {
	"ToDo": [{"fieldname": "bol_test_section", "fieldtype": "Section Break", "label": "Bol Test"}]
}


class TestInstall(FrappeTestCase):
	def test_ensure_role_is_idempotent(self):
		frappe.delete_doc("Role", install.BOL_MANAGER, force=True, ignore_missing=True)

		install.ensure_role()
		install.ensure_role()

		self.assertEqual(frappe.db.count("Role", {"name": install.BOL_MANAGER}), 1)
		self.assertEqual(frappe.db.get_value("Role", install.BOL_MANAGER, "desk_access"), 1)

	def test_ensure_custom_fields_is_idempotent(self):
		self.addCleanup(_delete_test_fields)

		with patch.dict(CUSTOM_FIELDS, TEST_FIELDS, clear=True):
			install.ensure_custom_fields()
			install.ensure_custom_fields()

		self.assertEqual(frappe.db.count("Custom Field", {"dt": "ToDo", "fieldname": "bol_test_section"}), 1)

	def test_ensure_module_defs_is_idempotent(self):
		install.ensure_module_defs()
		install.ensure_module_defs()

		self.assertEqual(frappe.db.count("Module Def", {"name": "Bol"}), 1)

	def test_after_migrate_does_not_swallow_errors(self):
		with (
			patch.object(install, "create_custom_fields", side_effect=frappe.ValidationError),
			self.assertRaises(frappe.ValidationError),
		):
			install.after_migrate()

	def test_custom_fields_use_bol_prefix(self):
		for doctype, fields in CUSTOM_FIELDS.items():
			for field in fields:
				self.assertTrue(field["fieldname"].startswith("bol_"), f"{doctype}.{field['fieldname']}")


def _delete_test_fields():
	# create_custom_fields commits (frappe.db.updatedb), so the test rollback can't undo it.
	frappe.db.delete("Custom Field", {"dt": "ToDo", "fieldname": "bol_test_section"})
	frappe.clear_cache(doctype="ToDo")
	frappe.db.commit()
