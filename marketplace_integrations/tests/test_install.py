from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from marketplace_integrations import install
from marketplace_integrations.setup.custom_fields import CUSTOM_FIELDS

# A Section Break has no DB column, so creating it runs no DDL (which would commit implicitly).
TEST_FIELDS = {"ToDo": [{"fieldname": "bol_test_section", "fieldtype": "Section Break", "label": "Bol Test"}]}


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

	def test_ensure_log_permission_is_idempotent(self):
		frappe.db.delete("Custom DocPerm", {"parent": install.INTEGRATION_LOG, "role": install.BOL_MANAGER})

		install.ensure_log_permission()
		install.ensure_log_permission()

		perms = frappe.get_all(
			"Custom DocPerm",
			filters={"parent": install.INTEGRATION_LOG, "role": install.BOL_MANAGER},
			fields=["read", "write", "create", "delete"],
		)
		self.assertEqual(perms, [{"read": 1, "write": 0, "create": 0, "delete": 0}])
		# The standard perms were copied over, so System Manager keeps its access.
		self.assertTrue(
			frappe.db.exists("Custom DocPerm", {"parent": install.INTEGRATION_LOG, "role": "System Manager"})
		)

	def test_integration_log_has_reference_fields(self):
		meta = frappe.get_meta(install.INTEGRATION_LOG)
		self.assertEqual(meta.get_field("bol_reference_name").options, "bol_reference_doctype")

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
	frappe.db.commit()  # nosemgrep
