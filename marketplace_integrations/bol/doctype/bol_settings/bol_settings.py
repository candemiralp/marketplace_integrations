# Copyright (c) 2026, Magnatlas and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from marketplace_integrations.bol.api.auth import invalidate_token
from marketplace_integrations.bol.api.client import BolClient
from marketplace_integrations.bol.api.errors import BolAuthError, BolError
from marketplace_integrations.bol.log import log_call
from marketplace_integrations.bol.offers import enqueue_adoption
from marketplace_integrations.bol.permissions import check_bol_role

REQUIRED_WHEN_ENABLED = ("client_id", "client_secret", "company")
BOL_CURRENCY = "EUR"


class BolSettings(Document):
	# Saving never calls bol; only the Test Connection button does.

	def validate(self):
		self.validate_delivery_days()
		self.validate_offer_sync()
		self.validate_stock_sync()
		if not self.enabled:
			return
		missing = [_(self.meta.get_label(field)) for field in REQUIRED_WHEN_ENABLED if not self.get(field)]
		if missing:
			frappe.throw(
				_("Set {0} before enabling the bol integration.").format(", ".join(missing)),
				title=_("Missing Settings"),
			)

	def validate_delivery_days(self):
		min_days, max_days = cint(self.default_min_days), cint(self.default_max_days)
		if min_days < 0 or max_days < 0:
			frappe.throw(_("Delivery days can't be negative."))
		if max_days and min_days > max_days:
			frappe.throw(
				_(
					"Default Min Days to Customer ({0}) can't be more than Default Max Days to Customer ({1})."
				).format(min_days, max_days)
			)

	def validate_offer_sync(self):
		if not self.sync_offers:
			return
		if not cint(self.default_max_days):
			# Otherwise items without their own days would promise delivery on the day of the order.
			frappe.throw(
				_("Set Default Max Days to Customer before switching on Sync Offers."),
				title=_("Missing Settings"),
			)
		if not self.price_list:
			frappe.throw(_("Set a Price List before switching on Sync Offers."), title=_("Missing Settings"))
		selling, currency = frappe.db.get_value("Price List", self.price_list, ["selling", "currency"]) or (
			None,
			None,
		)
		if not selling:
			frappe.throw(_("Price List {0} isn't a selling price list.").format(self.price_list))
		if currency != BOL_CURRENCY:
			frappe.throw(
				_("bol sells in {0}, but Price List {1} is in {2}.").format(
					BOL_CURRENCY, self.price_list, currency
				)
			)

	def validate_stock_sync(self):
		if not self.sync_stock:
			return
		if not self.sync_offers:
			# Offers missing at bol are recreated by the offer sync, with their stock.
			frappe.throw(_("Switch on Sync Offers before Sync Stock."), title=_("Missing Settings"))
		if not self.warehouse:
			frappe.throw(_("Set a Warehouse before switching on Sync Stock."), title=_("Missing Settings"))
		if frappe.db.get_value("Warehouse", self.warehouse, "is_group"):
			frappe.throw(
				_("Warehouse {0} is a group warehouse. Pick the warehouse that holds the stock.").format(
					self.warehouse
				)
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

	@frappe.whitelist()
	def adopt_existing_offers(self):
		"""Queue the job that maps bol's existing offers to Items (it never creates offers)."""
		check_bol_role()
		if not self.enabled:
			frappe.throw(_("Enable the bol integration first."), title=_("bol Disabled"))
		return {"queued": enqueue_adoption()}
