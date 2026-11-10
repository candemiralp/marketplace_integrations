# Errors are deliberately not caught here: a failing after_migrate must abort the migration.

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.installer import add_module_defs

from marketplace_integrations.setup.custom_fields import CUSTOM_FIELDS

APP_NAME = "marketplace_integrations"
BOL_MANAGER = "Bol Manager"


def after_install():
	ensure_role()
	ensure_custom_fields()


def after_migrate():
	ensure_module_defs()
	ensure_role()
	ensure_custom_fields()


def ensure_module_defs():
	# install-app creates Module Defs from modules.txt, but migrate doesn't, so a module added
	# after install would be missing (Ecommerce Item / Integration Log link to Module Def).
	add_module_defs(APP_NAME, ignore_if_duplicate=True)


def ensure_role():
	if frappe.db.exists("Role", BOL_MANAGER):
		return
	frappe.get_doc({"doctype": "Role", "role_name": BOL_MANAGER, "desk_access": 1}).insert(
		ignore_permissions=True
	)


def ensure_custom_fields():
	create_custom_fields(CUSTOM_FIELDS, update=True)
