# Copyright (c) 2026, Magnatlas and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from marketplace_integrations.bol.process_status import run_handler

RETRY_ROLES = ("System Manager", "Bol Manager")


class BolProcess(Document):
	# Rows are written only by bol/process_status.py; the UI is read-only.

	@frappe.whitelist()
	def retry_handler(self):
		# Checked here, not with frappe.only_for, which lets everyone through in tests.
		if set(RETRY_ROLES).isdisjoint(frappe.get_roles()):
			frappe.throw(_("Only System Manager or Bol Manager can retry a handler."), frappe.PermissionError)
		if self.status != "HANDLER_ERROR":
			frappe.throw(_("Only a process whose handler failed can be retried."), title=_("Not Allowed"))
		error = run_handler(self)
		if error:
			frappe.throw(
				_("The handler failed again: {0}").format(error), title=_("Retry Failed")
			)
		return {"status": self.status}
