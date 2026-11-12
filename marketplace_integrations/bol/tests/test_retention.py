# Buyer address retention (story 15). Everything stays in the test transaction.

import copy
import itertools
import json
from pathlib import Path
from unittest.mock import patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from marketplace_integrations.bol import labels, orders, retention, shipments
from marketplace_integrations.bol.tests.utils import (
	discard_pending_logs,
	make_accounting_masters,
	make_catalog_masters,
	make_item,
	make_order_masters,
)

FIXTURES = Path(__file__).parent / "fixtures"
ORDER = json.loads((FIXTURES / "order.json").read_text())
SHIRT, BAG = "_Test bol retention shirt", "_Test bol retention bag"
SHIRT_EAN, BAG_EAN = "4006381333931", "8712345678906"
TZ = "Europe/Amsterdam"
ADDRESS, SO, DN, SI = "Address", "Sales Order", "Delivery Note", "Sales Invoice"
DAYS = 90


class TestRetention(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.masters = make_order_masters()

	def setUp(self):
		self.settings = frappe._dict(enabled=1, anonymize_buyer_addresses_after_days=DAYS)
		self._start(patch.object(retention, "_settings", return_value=self.settings))
		off = frappe._dict(enabled=0)
		self._start(patch.object(shipments, "_settings", return_value=off))
		self._start(patch.object(labels, "_settings", return_value=off))
		self._start(patch.object(frappe.db, "commit"))
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		frappe.cache.delete_keys("bol:failed:anonymize")
		self.addCleanup(frappe.cache.delete_keys, "bol:failed:anonymize")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		# Submitting a Sales Invoice needs the Party Types.
		make_accounting_masters()
		make_catalog_masters()
		make_item(SHIRT, SHIRT_EAN, valuation_rate=10)
		make_item(BAG, BAG_EAN, valuation_rate=5)
		self.ids = itertools.count(1)

	def _start(self, patcher):
		self.addCleanup(patcher.stop)
		return patcher.start()

	def _order(self, submit: int = 1) -> frappe._dict:
		"""A bol Sales Order for buyer Billie Jansen. Returns its names (so, address)."""
		order = copy.deepcopy(ORDER)
		order["orderId"] = f"P{next(self.ids)}"
		settings = frappe._dict(import_orders_placed_after=None, submit_orders=submit, **self.masters)
		with patch.object(orders, "get_system_timezone", return_value=TZ):
			so_name = orders.create_sales_order(order, settings)
		return frappe._dict(so=so_name, address=frappe.db.get_value(SO, so_name, "shipping_address_name"))

	def _delivered(self, order) -> str:
		dn = make_delivery_note(order.so)
		dn.insert()
		dn.submit()
		order.dn = dn.name
		return dn.name

	def _invoiced(self, order) -> str:
		self._delivered(order)
		si = make_sales_invoice(order.dn)
		si.insert()
		si.submit()
		order.si = si.name
		return si.name

	def _age(self, order, days: int = DAYS + 10):
		"""Make the order's address and documents `days` old."""
		old = add_days(now_datetime(), -days)
		for doctype, name in (
			(ADDRESS, order.address),
			(SO, order.so),
			(DN, order.get("dn")),
			(SI, order.get("si")),
		):
			if name:
				frappe.db.set_value(doctype, name, "creation", old, update_modified=False)

	def _address(self, order) -> frappe._dict | None:
		"""The order's shipping Address as it is now (after any rename)."""
		name = frappe.db.get_value(SO, order.so, "shipping_address_name")
		return frappe.db.get_value(ADDRESS, name, ["*"], as_dict=True)

	def test_off_by_default(self):
		field = frappe.get_meta("Bol Settings").get_field("anonymize_buyer_addresses_after_days")
		self.assertEqual(field.default, "0")
		order = self._order()
		self._invoiced(order)
		self._age(order)
		self.settings.anonymize_buyer_addresses_after_days = 0
		self.assertEqual(retention.anonymize_buyer_addresses(), 0)
		self.assertEqual(self._address(order).address_line1, "Dorpstraat 1 B")

	def test_old_finished_order_address_is_anonymized_and_renamed(self):
		order = self._order()
		self._invoiced(order)
		self.assertEqual(frappe.db.get_value(SO, order.so, "status"), "Completed")
		printed = frappe.db.get_value(SI, order.si, "address_display")
		self.assertIn("Dorpstraat", printed)
		self._age(order)

		self.assertEqual(retention.anonymize_buyer_addresses(), 1)

		self.assertFalse(frappe.db.exists(ADDRESS, order.address))
		address = self._address(order)
		self.assertTrue(address.name.startswith("bol-buyer-"), address.name)
		for field in retention.TEXT_FIELDS:
			self.assertEqual(address[field], "[removed]", field)
		for field in retention.CONTACT_FIELDS:
			self.assertFalse(address[field], field)
		self.assertEqual((address.country, address.pincode), ("Netherlands", "1111ZZ"))
		for doctype, name in ((SO, order.so), (DN, order.dn), (SI, order.si)):
			self.assertEqual(
				frappe.db.get_value(doctype, name, "shipping_address_name"), address.name, doctype
			)
		# The invoice as printed is a fiscal record: untouched.
		self.assertEqual(frappe.db.get_value(SI, order.si, "address_display"), printed)
		# Nothing left that holds the old name or values.
		self.assertFalse(
			frappe.get_all("Comment", filters={"reference_doctype": ADDRESS, "reference_name": address.name})
		)
		self.assertFalse(frappe.get_all("Version", filters={"ref_doctype": ADDRESS, "docname": address.name}))
		# Done once: the next run leaves it alone.
		self.assertEqual(retention.anonymize_buyer_addresses(), 0)

	def test_address_of_a_recent_document_is_kept(self):
		order = self._order()
		self._invoiced(order)
		self._age(order)
		frappe.db.set_value(SI, order.si, "creation", now_datetime(), update_modified=False)
		retention.anonymize_buyer_addresses()
		self.assertEqual(self._address(order).address_line1, "Dorpstraat 1 B")

	def test_address_of_an_unfinished_order_is_kept(self):
		delivered = self._order()
		self._delivered(delivered)  # still to be invoiced: the invoice would print the address
		self._age(delivered)
		draft = self._order(submit=0)
		self._age(draft)
		retention.anonymize_buyer_addresses()
		self.assertEqual(frappe.db.get_value(SO, delivered.so, "status"), "To Bill")
		for order in (delivered, draft):
			self.assertEqual(self._address(order).address_line1, "Dorpstraat 1 B")

	def test_other_addresses_are_kept(self):
		order = self._order()
		self._invoiced(order)
		self._age(order)
		frappe.db.set_value(ADDRESS, order.address, "bol_buyer", 0)
		retention.anonymize_buyer_addresses()
		self.assertEqual(self._address(order).address_line1, "Dorpstraat 1 B")

	def test_failure_is_rolled_back_and_logged(self):
		order = self._order()
		self._invoiced(order)
		self._age(order)
		with patch.object(retention, "rename_doc", side_effect=frappe.ValidationError("rename refused")):
			self.assertEqual(retention.anonymize_buyer_addresses(), 0)
		self.assertEqual(self._address(order).address_line1, "Dorpstraat 1 B")
		(log,) = [log for log in frappe.local.bol_pending_logs or [] if "not anonymized" in log["message"]]
		self.assertEqual(log["status"], "Error")
		self.assertIn("rename refused", log["message"])
