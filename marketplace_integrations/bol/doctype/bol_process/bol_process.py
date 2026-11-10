# Copyright (c) 2026, Magnatlas and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from marketplace_integrations.bol.permissions import check_bol_role
from marketplace_integrations.bol.process_status import run_handler


class BolProcess(Document):
	# Rows are written only by bol/process_status.py; the UI is read-only.

	@frappe.whitelist()
	def retry_handler(self):
		check_bol_role()
		if self.status != "HANDLER_ERROR":
			frappe.throw(_("Only a process whose handler failed can be retried."), title=_("Not Allowed"))
		error = run_handler(self)
		if error:
			frappe.throw(_("The handler failed again: {0}").format(str(error)), title=_("Retry Failed"))
		return {"status": self.status}
