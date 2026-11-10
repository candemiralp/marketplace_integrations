# Copyright (c) 2026, Magnatlas and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from marketplace_integrations.bol.api.auth import invalidate_token
from marketplace_integrations.bol.api.client import BolClient
from marketplace_integrations.bol.api.errors import BolAuthError, BolError
from marketplace_integrations.bol.log import log_call

REQUIRED_WHEN_ENABLED = ("client_id", "client_secret", "company")


class BolSettings(Document):
	# Saving never calls bol; only the Test Connection button does.

	def validate(self):
		if not self.enabled:
			return
		missing = [_(self.meta.get_label(field)) for field in REQUIRED_WHEN_ENABLED if not self.get(field)]
		if missing:
			frappe.throw(
				_("Set {0} before enabling the bol integration.").format(", ".join(missing)),
				title=_("Missing Settings"),
			)

	def on_update(self):
		# Credentials or environment may have changed; the next call fetches a fresh token.
		invalidate_token(self)

	@frappe.whitelist()
	def test_connection(self):
		frappe.has_permission(self.doctype, "write", throw=True)
		invalidate_token(self)
		try:
			# One cheap GET that the demo environment also answers.
			BolClient(self).request(
				"GET", "/retailer/orders", version=10, params={"fulfilment-method": "FBR", "status": "OPEN"}
			)
		except BolAuthError as e:
			log_call(**e.details, error=e, reference=(self.doctype, self.name))
			frappe.throw(_("bol rejected the credentials: {0}").format(str(e)), title=_("Connection Failed"))
		except BolError as e:
			log_call(**e.details, error=e, reference=(self.doctype, self.name))
			frappe.throw(_("Connection to bol failed: {0}").format(str(e)), title=_("Connection Failed"))
		return {"ok": True}
