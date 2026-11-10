# Who may use bol actions (overview §4.2: Bol Manager and System Manager only).

import frappe
from frappe import _

BOL_ROLES = ("System Manager", "Bol Manager")


def check_bol_role():
	# Not frappe.only_for: it lets everyone through when frappe.flags.in_test is set (ADR-006).
	if set(BOL_ROLES).isdisjoint(frappe.get_roles()):
		frappe.throw(_("Only System Manager or Bol Manager can do this."), frappe.PermissionError)
