# Copyright (c) 2026, Can Demiralp and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from marketplace_integrations.bol.api.auth import invalidate_token
from marketplace_integrations.bol.api.client import BolClient
from marketplace_integrations.bol.api.errors import BolAuthError, BolError
from marketplace_integrations.bol.log import log_call
from marketplace_integrations.bol.mappers.bol_invoice import SETTLED_TYPES
from marketplace_integrations.bol.mappers.offer import WITHIN_24_HOURS
from marketplace_integrations.bol.offers import enqueue_adoption
from marketplace_integrations.bol.permissions import check_bol_role

REQUIRED_WHEN_ENABLED = ("client_id", "client_secret", "company")
BOL_CURRENCY = "EUR"


class BolSettings(Document):
	# Saving never calls bol; only the Test Connection button does.

	def validate(self):
		self.validate_delivery_time()
		self.validate_offer_sync()
		self.validate_stock_sync()
		self.validate_order_import()
		self.validate_vvb()
		self.validate_invoicing()
		self.validate_invoice_import()
		if not self.enabled:
			return
		missing = [_(self.meta.get_label(field)) for field in REQUIRED_WHEN_ENABLED if not self.get(field)]
		if missing:
			frappe.throw(
				_("Set {0} before enabling the bol integration.").format(", ".join(missing)),
				title=_("Missing Settings"),
			)

	def validate_delivery_time(self):
		if self.default_delivery_time == WITHIN_24_HOURS and not self.default_order_before:
			frappe.throw(
				_("Set If Ordered Before for the delivery time {0}.").format(_(WITHIN_24_HOURS)),
				title=_("Missing Settings"),
			)

	def validate_offer_sync(self):
		if not self.sync_offers:
			return
		if not self.default_delivery_time:
			# Otherwise items without their own delivery time couldn't be offered.
			frappe.throw(
				_("Set a Default Delivery Time before switching on Sync Offers."),
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
		self.validate_warehouse()

	def validate_order_import(self):
		if not self.import_orders:
			return
		missing = [
			_(self.meta.get_label(field))
			for field in ("customer", "warehouse", "tax_template_nl", "tax_template_be")
			if not self.get(field)
		]
		if missing:
			frappe.throw(
				_("Set {0} before switching on Import Orders.").format(", ".join(missing)),
				title=_("Missing Settings"),
			)
		self.validate_warehouse()
		for field in ("tax_template_nl", "tax_template_be"):
			self.validate_tax_template(self.get(field))

	def validate_vvb(self):
		# A label is used by confirming the shipment with it through the API (story 08).
		if self.use_vvb and not self.push_shipments:
			frappe.throw(
				_("Switch on Push Shipments before Use Verzenden via bol."), title=_("Missing Settings")
			)

	def validate_invoicing(self):
		if self.upload_invoices and not self.auto_invoice:
			frappe.throw(
				_("Switch on Invoice Shipments and Returns before Upload Requested Invoices."),
				title=_("Missing Settings"),
			)
		if not self.auto_invoice:
			return
		if not self.push_shipments:
			# Shipments are invoiced once bol has confirmed them.
			frappe.throw(
				_("Switch on Push Shipments before Invoice Shipments and Returns."),
				title=_("Missing Settings"),
			)
		if not self.receivable_account:
			frappe.throw(
				_("Set a Receivable Account before switching on Invoice Shipments and Returns."),
				title=_("Missing Settings"),
			)
		if frappe.db.get_value("Account", self.receivable_account, "account_type") != "Receivable":
			frappe.throw(_("Account {0} isn't a Receivable account.").format(self.receivable_account))
		self.validate_account(self.receivable_account)

	def validate_invoice_import(self):
		if not self.import_bol_invoices:
			return
		missing = [
			_(self.meta.get_label(field))
			for field in ("supplier", "purchase_tax_template", "default_expense_account")
			if not self.get(field)
		]
		if missing:
			frappe.throw(
				_("Set {0} before switching on Import bol Invoices.").format(", ".join(missing)),
				title=_("Missing Settings"),
			)
		company = frappe.db.get_value(
			"Purchase Taxes and Charges Template", self.purchase_tax_template, "company"
		)
		if self.company and company != self.company:
			frappe.throw(
				_("Tax template {0} belongs to company {1}, not {2}.").format(
					self.purchase_tax_template, company, self.company
				)
			)
		self.validate_account(self.default_expense_account)
		seen = set()
		for row in self.invoice_account_map:
			# bol writes its line types in capitals.
			row.bol_line_type = (row.bol_line_type or "").strip().upper()
			if row.bol_line_type in SETTLED_TYPES:
				frappe.throw(
					_(
						"Row {0}: {1} is never booked on a bol Purchase Invoice: it's the sales bol collected, which settle the bol receivable."
					).format(row.idx, row.bol_line_type)
				)
			if row.bol_line_type in seen:
				frappe.throw(
					_("Row {0}: {1} is in the account map twice.").format(row.idx, row.bol_line_type)
				)
			seen.add(row.bol_line_type)
			self.validate_account(row.expense_account)
			if row.item and frappe.db.get_value("Item", row.item, "is_stock_item"):
				# ERPNext books a stock item on Stock Received But Not Billed, not on the mapped account.
				frappe.throw(
					_("Row {0}: {1} is a stock item. Pick a non-stock item.").format(row.idx, row.item)
				)

	def validate_account(self, account: str):
		company, is_group = frappe.db.get_value("Account", account, ["company", "is_group"]) or (None, None)
		if is_group:
			frappe.throw(_("Account {0} is a group account.").format(account))
		if self.company and company != self.company:
			frappe.throw(
				_("Account {0} belongs to company {1}, not {2}.").format(account, company, self.company)
			)

	def validate_warehouse(self):
		if frappe.db.get_value("Warehouse", self.warehouse, "is_group"):
			frappe.throw(
				_("Warehouse {0} is a group warehouse. Pick the warehouse that holds the stock.").format(
					self.warehouse
				)
			)

	def validate_tax_template(self, template: str):
		company = frappe.db.get_value("Sales Taxes and Charges Template", template, "company")
		if self.company and company != self.company:
			frappe.throw(
				_("Tax template {0} belongs to company {1}, not {2}.").format(template, company, self.company)
			)
		rows = frappe.get_all(
			"Sales Taxes and Charges",
			filters={"parenttype": "Sales Taxes and Charges Template", "parent": template},
			pluck="included_in_print_rate",
		)
		# bol's prices include VAT: an exclusive row would add VAT on top of what the buyer paid.
		if not rows or not all(rows):
			frappe.throw(
				_(
					"Every row of tax template {0} must have Is this Tax included in Basic Rate? ticked: bol prices include VAT."
				).format(template)
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
