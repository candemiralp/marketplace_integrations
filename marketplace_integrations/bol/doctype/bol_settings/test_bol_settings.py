# Copyright (c) 2026, Can Demiralp and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from marketplace_integrations.bol.api.errors import BolAuthError, BolTransientError
from marketplace_integrations.bol.doctype.bol_settings import bol_settings
from marketplace_integrations.bol.mappers.offer import DELIVERY_TIMES, ORDER_BEFORE_TIMES
from marketplace_integrations.bol.tests.utils import (
	TEST_COMPANY_ABBR,
	make_accounting_masters,
	make_catalog_masters,
	make_item,
	make_order_masters,
	make_price_list,
	make_tax_template,
	make_warehouse,
)

# These tests need no Company; ERPNext's Company test records need ERPNext's full test setup.
# Warehouse links to Company too: the stock tests create a warehouse of their own (tests/utils.make_warehouse).
# The order import settings link Customer and tax templates: tests/utils.make_order_masters creates them.
# The accounting settings link Accounts, a Supplier, a purchase template and Items: make_accounting_masters creates them.
test_ignore = [
	"Company",
	"Warehouse",
	"Customer",
	"Sales Taxes and Charges Template",
	"Account",
	"Supplier",
	"Purchase Taxes and Charges Template",
	"Item",
]


class TestBolSettings(FrappeTestCase):
	def _settings(self, **values):
		doc = frappe.get_single("Bol Settings")
		doc.update({"enabled": 0, "environment": "Demo", "client_id": "settings-test-client", **values})
		return doc

	def test_enabling_requires_credentials_and_company(self):
		doc = self._settings(enabled=1, client_id=None, client_secret=None, company=None)

		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.validate()
		for label in ("Client ID", "Client Secret", "Company"):
			self.assertIn(label, str(ctx.exception))

	def test_disabled_settings_need_nothing(self):
		self._settings(client_id=None, company=None).validate()

	def test_saving_never_calls_bol(self):
		with patch("requests.Session.request") as request:
			self._settings(client_secret="not-a-real-secret").save()

		request.assert_not_called()

	def test_connection_ok(self):
		with patch.object(bol_settings, "BolClient") as client:
			self.assertEqual(self._settings().test_connection(), {"ok": True})

		args, kwargs = client.return_value.request.call_args
		self.assertEqual(args, ("GET", "/retailer/orders"))
		self.assertEqual(kwargs["version"], 10)

	def test_connection_works_before_enabling(self):
		with patch.object(bol_settings, "BolClient"):
			self.assertEqual(self._settings(enabled=0).test_connection(), {"ok": True})

	def test_invalid_credentials_give_a_clear_error_and_a_log(self):
		error = BolAuthError("Invalid bol credentials", {"method": "POST", "status": 401})
		with (
			patch.object(bol_settings, "BolClient") as client,
			patch.object(bol_settings, "log_call") as log_call,
		):
			client.return_value.request.side_effect = error
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._settings().test_connection()

		self.assertIn("bol rejected the credentials", str(ctx.exception))
		self.assertNotIn("Traceback", str(ctx.exception))
		log_call.assert_called_once_with(
			method="POST", status=401, error=error, reference=("Bol Settings", "Bol Settings")
		)

	def test_unreachable_bol_gives_a_clear_error(self):
		with (
			patch.object(bol_settings, "BolClient") as client,
			patch.object(bol_settings, "log_call") as log_call,
		):
			client.return_value.request.side_effect = BolTransientError("bol unavailable (HTTP 503)")
			with self.assertRaisesRegex(frappe.ValidationError, "Connection to bol failed"):
				self._settings().test_connection()
		log_call.assert_called_once()

	def test_connection_requires_permission(self):
		doc = self._settings()
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		with patch.object(bol_settings, "BolClient") as client, self.assertRaises(frappe.PermissionError):
			doc.test_connection()
		client.assert_not_called()


class TestOfferSyncSettings(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)

	def _settings(self, **values):
		doc = frappe.get_single("Bol Settings")
		doc.update(
			{
				"enabled": 0,
				"sync_offers": 1,
				"price_list": make_price_list(),
				"default_delivery_time": "1-2 working days",
				**values,
			}
		)
		return doc

	def test_valid(self):
		self._settings().validate()

	def test_off_needs_nothing(self):
		self._settings(sync_offers=0, price_list=None, default_delivery_time=None).validate()

	def test_needs_a_price_list(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Price List"):
			self._settings(price_list=None).validate()

	def test_needs_a_default_delivery_time(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Default Delivery Time"):
			self._settings(default_delivery_time=None).validate()

	def test_within_24_hours_needs_an_order_time(self):
		with self.assertRaisesRegex(frappe.ValidationError, "If Ordered Before"):
			self._settings(default_delivery_time="Within 24 hours", default_order_before=None).validate()
		self._settings(default_delivery_time="Within 24 hours", default_order_before="22:00").validate()

	def test_dropdowns_match_the_mapper(self):
		# The DocType JSON can't import the mapper's lists; the Item custom fields do.
		meta = frappe.get_meta("Bol Settings")
		self.assertEqual(meta.get_options("default_delivery_time").split("\n")[1:], list(DELIVERY_TIMES))
		self.assertEqual(meta.get_options("default_order_before").split("\n")[1:], list(ORDER_BEFORE_TIMES))

	def test_buying_price_list_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "selling"):
			self._settings(price_list=make_price_list("_Test bol Buying", selling=0)).validate()

	def test_price_list_in_another_currency_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "EUR"):
			self._settings(price_list=make_price_list("_Test bol USD", currency="USD")).validate()


class TestStockSyncSettings(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)

	def _settings(self, **values):
		doc = frappe.get_single("Bol Settings")
		doc.update(
			{
				"enabled": 0,
				"sync_offers": 1,
				"price_list": make_price_list(),
				"default_delivery_time": "1-2 working days",
				"sync_stock": 1,
				"warehouse": make_warehouse(),
				"stock_buffer": 1,
				**values,
			}
		)
		return doc

	def test_valid(self):
		self._settings().validate()

	def test_off_needs_nothing(self):
		self._settings(sync_stock=0, warehouse=None).validate()

	def test_needs_sync_offers(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Sync Offers"):
			self._settings(sync_offers=0).validate()

	def test_needs_a_warehouse(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Warehouse"):
			self._settings(warehouse=None).validate()

	def test_group_warehouse_rejected(self):
		group = make_warehouse("_Test bol Group Warehouse", is_group=1)
		with self.assertRaisesRegex(frappe.ValidationError, "group"):
			self._settings(warehouse=group).validate()


class TestOrderImportSettings(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.masters = make_order_masters()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)

	def _settings(self, **values):
		doc = frappe.get_single("Bol Settings")
		doc.update(
			{
				"enabled": 0,
				"sync_offers": 0,
				"sync_stock": 0,
				"import_orders": 1,
				"company": self.masters.company,
				"customer": self.masters.customer,
				"warehouse": self.masters.warehouse,
				"tax_template_nl": self.masters.tax_template_nl,
				"tax_template_be": self.masters.tax_template_be,
				**values,
			}
		)
		return doc

	def test_valid(self):
		self._settings().validate()

	def test_off_needs_nothing(self):
		self._settings(import_orders=0, customer=None, tax_template_nl=None, tax_template_be=None).validate()

	def test_needs_customer_warehouse_and_templates(self):
		doc = self._settings(customer=None, warehouse=None, tax_template_nl=None, tax_template_be=None)

		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.validate()
		for label in ("Customer", "Warehouse", "Tax Template NL", "Tax Template BE"):
			self.assertIn(label, str(ctx.exception))

	def test_template_with_vat_on_top_rejected(self):
		account = f"_Test bol VAT NL - {TEST_COMPANY_ABBR}"
		exclusive = make_tax_template("_Test bol VAT NL excl", account, included=0)

		with self.assertRaisesRegex(frappe.ValidationError, "included in Basic Rate"):
			self._settings(tax_template_nl=exclusive.name).validate()

	def test_template_of_another_company_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "belongs to company"):
			self._settings(company="_Test bol Other Company").validate()

	def test_group_warehouse_rejected(self):
		group = make_warehouse("_Test bol Group Warehouse", is_group=1)
		with self.assertRaisesRegex(frappe.ValidationError, "group"):
			self._settings(warehouse=group).validate()


class TestVvbSettings(FrappeTestCase):
	def _settings(self, **values):
		doc = frappe.get_single("Bol Settings")
		doc.update(
			{"enabled": 0, "use_vvb": 1, "push_shipments": 1, "default_transporter_code": "TNT", **values}
		)
		return doc

	def test_valid(self):
		self._settings().validate()

	def test_needs_push_shipments(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Push Shipments"):
			self._settings(push_shipments=0).validate()


class TestAccountingSettings(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.masters = make_order_masters()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.accounts = make_accounting_masters()

	def _settings(self, **values):
		doc = frappe.get_single("Bol Settings")
		doc.update(
			{
				"enabled": 0,
				"company": self.masters.company,
				"push_shipments": 1,
				"default_transporter_code": "TNT",
				"auto_invoice": 1,
				"receivable_account": self.accounts.receivable_account,
				"upload_invoices": 1,
				"import_bol_invoices": 1,
				"supplier": self.accounts.supplier,
				"purchase_tax_template": self.accounts.purchase_tax_template,
				"default_expense_account": self.accounts.default_expense_account,
				**values,
			}
		)
		doc.set("invoice_account_map", [])
		return doc

	def _map(self, doc, line_type: str, account: str | None = None, item: str | None = None):
		doc.append(
			"invoice_account_map",
			{
				"bol_line_type": line_type,
				"expense_account": account or self.accounts.commission_account,
				"item": item,
			},
		)

	def test_valid(self):
		doc = self._settings()
		self._map(doc, " commission ")

		doc.validate()

		self.assertEqual(doc.invoice_account_map[0].bol_line_type, "COMMISSION")

	def test_off_needs_nothing(self):
		self._settings(
			auto_invoice=0,
			upload_invoices=0,
			import_bol_invoices=0,
			receivable_account=None,
			supplier=None,
			purchase_tax_template=None,
			default_expense_account=None,
		).validate()

	def test_invoicing_needs_push_shipments(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Push Shipments"):
			self._settings(push_shipments=0).validate()

	def test_invoicing_needs_a_receivable_account(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Receivable Account"):
			self._settings(receivable_account=None).validate()
		with self.assertRaisesRegex(frappe.ValidationError, "isn't a Receivable account"):
			self._settings(receivable_account=self.accounts.default_expense_account).validate()

	def test_uploads_need_invoicing(self):
		with self.assertRaisesRegex(frappe.ValidationError, "before Upload Requested Invoices"):
			self._settings(auto_invoice=0).validate()

	def test_import_needs_supplier_template_and_account(self):
		doc = self._settings(supplier=None, purchase_tax_template=None, default_expense_account=None)

		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.validate()
		for label in ("Supplier", "Purchase Tax Template", "Default Expense Account"):
			self.assertIn(label, str(ctx.exception))

	def test_accounts_of_another_company_or_groups_are_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "belongs to company"):
			self._settings(company="_Test bol Other Company", import_bol_invoices=0).validate()
		group = frappe.db.get_value("Account", self.accounts.default_expense_account, "parent_account")
		with self.assertRaisesRegex(frappe.ValidationError, "group account"):
			self._settings(default_expense_account=group).validate()

	def test_turnover_cant_be_mapped(self):
		doc = self._settings()
		self._map(doc, "TURNOVER")

		with self.assertRaisesRegex(frappe.ValidationError, "settle the bol receivable"):
			doc.validate()

	def test_a_line_type_is_mapped_once(self):
		doc = self._settings()
		self._map(doc, "COMMISSION")
		self._map(doc, "commission")

		with self.assertRaisesRegex(frappe.ValidationError, "twice"):
			doc.validate()

	def test_mapped_item_must_not_be_a_stock_item(self):
		make_catalog_masters()
		item = make_item("_Test bol stock fee")
		doc = self._settings()
		self._map(doc, "STOCK", item=item.name)

		with self.assertRaisesRegex(frappe.ValidationError, "stock item"):
			doc.validate()
