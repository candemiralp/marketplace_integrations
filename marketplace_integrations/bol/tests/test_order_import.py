# Order import (story 07) with bol's order endpoints faked in memory. Everything stays in the test transaction.

import copy
import json
from pathlib import Path
from unittest.mock import patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder, make_delivery_note, make_sales_invoice
from frappe.tests.utils import FrappeTestCase

from marketplace_integrations.bol import orders
from marketplace_integrations.bol.api.errors import BolError, BolServerError
from marketplace_integrations.bol.tests.utils import (
	discard_pending_logs,
	make_catalog_masters,
	make_item,
	make_order_masters,
)

ORDER = json.loads((Path(__file__).parent / "fixtures" / "order.json").read_text())
SHIRT, BAG = "_Test bol order shirt", "_Test bol order bag"
SHIRT_EAN, BAG_EAN = "4006381333931", "8712345678906"
UNKNOWN_EAN = "8712345678913"
BOL_TOTAL = 62.89  # 2 × 24.95 + 12.99
TZ = "Europe/Amsterdam"


class FakeOrders:
	"""bol's order endpoints in memory, called like BolClient.request."""

	def __init__(self):
		self.settings = frappe._dict(environment="Production")
		self.orders = {}
		# Order ids per list page; None = every order on page 1.
		self.pages = None
		self.errors = {}
		self.calls = []

	def add(self, order_id: str, **overrides) -> dict:
		order = copy.deepcopy(ORDER)
		order.update(copy.deepcopy(overrides), orderId=order_id)
		self.orders[order_id] = order
		return order

	def request(self, method, path, *, version, params=None, **kwargs):
		assert (method, version) == ("GET", orders.VERSION), (method, version)
		self.calls.append((path, params))
		error = self.errors.get(path)
		if error:
			raise error
		if path == orders.PATH:
			return {"orders": [self.reduced(order_id) for order_id in self._page(params)]}
		return copy.deepcopy(self.orders[path.rsplit("/", 1)[1]])

	def reduced(self, order_id: str) -> dict:
		"""The order as bol's list shows it (ReducedOrder)."""
		order = self.orders.get(order_id) or {"orderItems": []}
		return {
			"orderId": order_id,
			"orderPlacedDateTime": order.get("orderPlacedDateTime"),
			"orderItems": [
				{
					"orderItemId": item["orderItemId"],
					"ean": item["product"]["ean"],
					"fulfilmentMethod": item["fulfilment"]["method"],
					"fulfilmentStatus": "OPEN",
					"quantity": item["quantity"],
					"quantityShipped": item["quantityShipped"],
					"quantityCancelled": item["quantityCancelled"],
					"cancellationRequest": item["cancellationRequest"],
					"latestChangedDateTime": item["latestChangedDateTime"],
				}
				for item in order["orderItems"]
			],
		}

	def details(self) -> list[str]:
		return [path.rsplit("/", 1)[1] for path, _params in self.calls if path != orders.PATH]

	def pages_read(self) -> list[int]:
		return [params["page"] for path, params in self.calls if path == orders.PATH]

	def _page(self, params) -> list[str]:
		assert (params["fulfilment-method"], params["status"]) == ("FBR", "OPEN"), params
		pages = self.pages if self.pages is not None else [list(self.orders)]
		page = params["page"]
		return pages[page - 1] if page <= len(pages) else []


class OrderImportTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.masters = make_order_masters()

	def setUp(self):
		self.bol = FakeOrders()
		self._start(patch.object(orders, "get_client", return_value=self.bol))
		self.settings = frappe._dict(
			enabled=1, import_orders=1, submit_orders=1, import_orders_placed_after=None, **self.masters
		)
		self._start(patch.object(orders, "_settings", return_value=self.settings))
		self._start(patch.object(orders, "get_system_timezone", return_value=TZ))
		# The poll commits per order; keep the work in this transaction so the rollback undoes it.
		self._start(patch.object(frappe.db, "commit"))
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		make_catalog_masters()
		make_item(SHIRT, SHIRT_EAN)
		make_item(BAG, BAG_EAN)

	def _start(self, patcher):
		self.addCleanup(patcher.stop)
		return patcher.start()

	def _add(self, order_id: str, **overrides) -> dict:
		marker = f"bol:failed:order:{order_id}"
		frappe.cache.delete_value(marker)
		self.addCleanup(frappe.cache.delete_value, marker)
		return self.bol.add(order_id, **overrides)

	def _sales_orders(self, order_id: str) -> list[str]:
		return frappe.get_all("Sales Order", filters={"bol_order_id": order_id}, pluck="name")

	def _sales_order(self, order_id: str):
		(name,) = self._sales_orders(order_id)
		return frappe.get_doc("Sales Order", name)

	def _buyer_addresses(self) -> int:
		return frappe.db.count("Address", {"bol_buyer": 1})

	def _error_logs(self) -> list[dict]:
		return [log for log in frappe.local.bol_pending_logs or [] if log["status"] == "Error"]


class TestImport(OrderImportTestCase):
	def test_new_order_becomes_a_submitted_sales_order(self):
		self._add("A1")

		orders.poll_orders()

		so = self._sales_order("A1")
		self.assertEqual(so.docstatus, 1)
		self.assertEqual(
			(so.customer, so.company, so.po_no), (self.masters.customer, self.masters.company, "A1")
		)
		self.assertEqual(str(so.transaction_date), "2026-11-09")
		self.assertEqual(str(so.delivery_date), "2026-11-11")
		self.assertEqual(
			[(item.item_code, item.qty, item.rate, item.bol_order_item_id) for item in so.items],
			[(SHIRT, 2, 24.95, "2012345678"), (BAG, 1, 12.99, "2012345679")],
		)
		self.assertEqual([item.warehouse for item in so.items], [self.masters.warehouse] * 2)
		self.assertEqual(so.items[0].bol_distribution_party, "RETAILER")
		self.assertEqual(str(so.items[0].bol_latest_delivery_date), "2026-11-12")
		self.assertEqual(so.items[0].bol_commission, 5.18)
		self.assertEqual(self._error_logs(), [])

	def test_vat_is_included_in_bols_total(self):
		self._add("A1")

		orders.poll_orders()

		so = self._sales_order("A1")
		self.assertEqual(so.taxes_and_charges, self.masters.tax_template_nl)
		self.assertEqual(len(so.taxes), 1)
		self.assertTrue(so.taxes[0].included_in_print_rate)
		self.assertEqual(so.grand_total, BOL_TOTAL)
		self.assertEqual(so.rounded_total or so.grand_total, BOL_TOTAL)
		# VAT is taken out of bol's prices, not added on top.
		self.assertEqual(so.total, BOL_TOTAL)
		self.assertLess(so.net_total, BOL_TOTAL)
		self.assertGreater(so.total_taxes_and_charges, 0)

	def test_belgian_buyer_gets_the_be_template(self):
		order = self._add("A1", billingDetails=None)
		order["shipmentDetails"].update(countryCode="BE", zipCode="2000", city="Antwerpen")

		orders.poll_orders()

		self.assertEqual(self._sales_order("A1").taxes_and_charges, self.masters.tax_template_be)

	def test_discount_keeps_the_total_equal_to_bols(self):
		order = self._add("A1")
		order["orderItems"][0].update(discounts=[{"title": "2 for 45", "amount": 4.9}], totalPrice=45.0)

		orders.poll_orders()

		so = self._sales_order("A1")
		self.assertEqual((so.items[0].price_list_rate, so.items[0].rate), (24.95, 22.5))
		self.assertEqual(so.grand_total, 57.99)

	def test_shipping_address_is_a_bol_buyer_address_of_the_customer(self):
		self._add("A1")

		orders.poll_orders()

		so = self._sales_order("A1")
		self.assertEqual(so.customer_address, so.shipping_address_name)
		address = frappe.get_doc("Address", so.shipping_address_name)
		self.assertEqual(
			(address.address_line1, address.pincode, address.city), ("Dorpstraat 1 B", "1111ZZ", "Utrecht")
		)
		self.assertEqual(
			(address.country, address.address_type, address.bol_buyer), ("Netherlands", "Shipping", 1)
		)
		self.assertEqual(
			[(link.link_doctype, link.link_name) for link in address.links],
			[("Customer", self.masters.customer)],
		)

	def test_different_billing_address_is_a_second_address(self):
		order = self._add("A1")
		order["billingDetails"].update(company="Borduurwerk B.V.", streetName="Kerkstraat", houseNumber="12")

		orders.poll_orders()

		so = self._sales_order("A1")
		self.assertNotEqual(so.customer_address, so.shipping_address_name)
		billing = frappe.get_doc("Address", so.customer_address)
		self.assertEqual((billing.address_title, billing.address_type), ("Borduurwerk B.V.", "Billing"))
		self.assertEqual(self._buyer_addresses(), 2)

	def test_buyer_details_reach_the_delivery_note_and_invoice(self):
		order = self._add("A1")
		order["billingDetails"].update(
			company="Borduurwerk B.V.", vatNumber="NL999999999B99", kvkNumber="99887766"
		)
		buyer = {
			"bol_buyer_name": "Billie Jansen",
			"bol_buyer_company": "Borduurwerk B.V.",
			"bol_buyer_vat_number": "NL999999999B99",
			"bol_buyer_kvk_number": "99887766",
		}

		orders.poll_orders()

		so = self._sales_order("A1")
		self.assertEqual(so.customer, self.masters.customer)
		for doc in (so, make_delivery_note(so.name), make_sales_invoice(so.name)):
			self.assertEqual({key: doc.get(key) for key in buyer}, buyer, doc.doctype)

	def test_draft_when_submit_orders_is_off(self):
		self.settings.submit_orders = 0
		self._add("A1")

		orders.poll_orders()

		self.assertEqual(self._sales_order("A1").docstatus, 0)

	def test_reference_mismatch_is_a_comment_on_the_order(self):
		order = self._add("A1")
		order["orderItems"][0]["offer"]["reference"] = "OLD-SKU"

		orders.poll_orders()

		so = self._sales_order("A1")
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Sales Order",
				"reference_name": so.name,
				"comment_type": "Comment",
			},
			pluck="content",
		)
		self.assertEqual(len(comments), 1)
		self.assertIn("OLD-SKU", comments[0])
		self.assertEqual(so.items[0].item_code, SHIRT)

	def test_order_with_nothing_left_to_ship_is_skipped(self):
		order = self._add("A1")
		order["orderItems"][0]["quantityShipped"] = 2
		order["orderItems"][1]["quantityCancelled"] = 1

		orders.poll_orders()

		self.assertEqual(self._sales_orders("A1"), [])
		self.assertEqual(self._buyer_addresses(), 0)
		self.assertEqual(self._error_logs(), [])


class TestCancelledBeforeImport(OrderImportTestCase):
	def test_units_bol_cancelled_before_import_are_recorded(self):
		order = self._add("A1")
		order["orderItems"][0].update(quantity=3, quantityCancelled=1, totalPrice=74.85)

		orders.poll_orders()

		shirt = self._sales_order("A1").items[0]
		# qty is net of them already; story 10 takes off only what bol cancels after this.
		self.assertEqual((shirt.qty, shirt.bol_qty_cancelled), (2, 1))


class TestIdempotency(OrderImportTestCase):
	def test_polled_twice_gives_one_sales_order(self):
		self._add("A1")

		orders.poll_orders()
		orders.poll_orders()

		self.assertEqual(len(self._sales_orders("A1")), 1)
		# The second poll found the order in ERPNext and didn't fetch it again.
		self.assertEqual(self.bol.details(), ["A1"])

	def test_existing_orders_go_to_the_change_sync(self):
		self._add("A1")
		orders.poll_orders()
		self._add("A2")

		with patch.object(orders.cancellations, "sync_existing_order_changes") as sync:
			orders.poll_orders()

		# With bol's list entries: they carry what cancellations need.
		sync.assert_called_once_with([self.bol.reduced("A1")], self.bol)
		self.assertEqual(len(self._sales_orders("A2")), 1)

	def test_concurrent_import_already_committed_is_no_failure(self):
		self._add("A1")
		orders.import_order("A1")
		addresses = self._buyer_addresses()

		# Another run committed it after our existence check: ERPNext's one-order-per-PO check stops the insert.
		with patch.object(orders, "_imported", side_effect=[False, True]):
			self.assertIsNone(orders.import_order("A1"))

		self.assertEqual(len(self._sales_orders("A1")), 1)
		self.assertEqual(self._buyer_addresses(), addresses)
		self.assertEqual(self._error_logs(), [])

	def test_concurrent_import_not_yet_committed_is_no_failure(self):
		self._add("A1")
		orders.import_order("A1")
		addresses = self._buyer_addresses()

		# Another run's insert isn't visible yet: only the unique bol_order_id stops ours.
		with (
			patch.object(orders, "_imported", return_value=False),
			patch.object(SalesOrder, "validate_po"),
		):
			self.assertIsNone(orders.import_order("A1"))

		self.assertEqual(len(self._sales_orders("A1")), 1)
		self.assertEqual(self._buyer_addresses(), addresses)
		self.assertEqual(self._error_logs(), [])

	def test_import_of_an_imported_order_makes_no_call(self):
		self._add("A1")
		orders.import_order("A1")
		self.bol.calls.clear()

		self.assertIsNone(orders.import_order("A1"))
		self.assertEqual(self.bol.calls, [])


class TestFailures(OrderImportTestCase):
	def test_unknown_ean_rolls_back_and_the_next_order_is_imported(self):
		bad = self._add("BAD")
		bad["orderItems"][1]["product"]["ean"] = UNKNOWN_EAN
		self._add("GOOD")
		self.bol.pages = [["BAD", "GOOD"]]

		orders.poll_orders()

		self.assertEqual(self._sales_orders("BAD"), [])
		self.assertEqual(len(self._sales_orders("GOOD")), 1)
		# Only GOOD's address: BAD's was rolled back with its order.
		self.assertEqual(self._buyer_addresses(), 1)
		(log,) = self._error_logs()
		self.assertIn("BAD", log["message"])
		self.assertIn(UNKNOWN_EAN, log["message"])
		self.assertIn("BolMappingError", log["traceback"])
		self.assertEqual(json.loads(log["response_data"])["orderId"], "BAD")

	def test_unsupported_country_is_logged(self):
		order = self._add("A1", billingDetails=None)
		order["shipmentDetails"]["countryCode"] = "DE"

		orders.poll_orders()

		self.assertEqual(self._sales_orders("A1"), [])
		(log,) = self._error_logs()
		self.assertIn("Unsupported shipment country: DE", log["message"])

	def test_validation_error_on_submit_rolls_back_everything(self):
		self._add("A1")

		with patch.object(
			SalesOrder, "on_submit", side_effect=frappe.ValidationError("Credit limit reached")
		):
			orders.poll_orders()

		self.assertEqual(self._sales_orders("A1"), [])
		self.assertEqual(self._buyer_addresses(), 0)
		(log,) = self._error_logs()
		self.assertIn("Credit limit reached", log["message"])
		self.assertEqual(json.loads(log["response_data"])["orderId"], "A1")

	def test_bol_error_on_the_detail_is_logged_and_the_next_order_is_imported(self):
		self._add("A1")
		self._add("A2")
		details = {"method": "GET", "path": f"{orders.PATH}/A1", "status": 500, "response": {"status": 500}}
		self.bol.errors[f"{orders.PATH}/A1"] = BolServerError("bol server error (HTTP 500)", details)

		orders.poll_orders()

		self.assertEqual(self._sales_orders("A1"), [])
		self.assertEqual(len(self._sales_orders("A2")), 1)
		(log,) = self._error_logs()
		self.assertEqual(log["method"], f"GET {orders.PATH}/A1")
		self.assertIn("HTTP 500", log["message"])

	def test_a_recurring_failure_is_logged_once_and_retried(self):
		order = self._add("A1")
		order["orderItems"][1]["product"]["ean"] = UNKNOWN_EAN

		orders.poll_orders()
		orders.poll_orders()

		self.assertEqual(len(self._error_logs()), 1)
		self.assertEqual(self.bol.details(), ["A1", "A1"])

		# Fixed in ERPNext (the barcode was added): the next poll imports it.
		make_item("_Test bol order cap", UNKNOWN_EAN)
		orders.poll_orders()
		self.assertEqual(len(self._sales_orders("A1")), 1)

	def test_list_failure_fails_the_job_with_one_log(self):
		error = BolServerError("bol server error (HTTP 500)", {"method": "GET", "path": orders.PATH})
		self.bol.errors[orders.PATH] = error
		frappe.cache.delete_value("bol:failed:orders:list")
		self.addCleanup(frappe.cache.delete_value, "bol:failed:orders:list")

		for _run in range(2):
			with self.assertRaises(BolError):
				orders.poll_orders()

		self.assertEqual(len(self._error_logs()), 1)


class TestPolling(OrderImportTestCase):
	def test_off_makes_no_calls(self):
		self._add("A1")
		for values in ({"enabled": 1, "import_orders": 0}, {"enabled": 0, "import_orders": 1}):
			self.settings.update(values)
			orders.poll_orders()

		self.assertEqual(self.bol.calls, [])

	def test_pages_until_an_empty_page(self):
		for order_id in ("A1", "A2", "A3"):
			self._add(order_id)
		self.bol.pages = [["A1", "A2"], ["A3"]]

		orders.poll_orders()

		self.assertEqual(self.bol.pages_read(), [1, 2, 3])
		self.assertEqual(self.bol.details(), ["A1", "A2", "A3"])

	def test_an_order_on_two_pages_is_imported_once(self):
		for order_id in ("A1", "A2"):
			self._add(order_id)
		# A new order at bol shifts the pages between two requests.
		self.bol.pages = [["A1"], ["A1", "A2"]]

		orders.poll_orders()

		self.assertEqual(self.bol.details(), ["A1", "A2"])

	def test_demo_has_only_one_page(self):
		self.bol.settings.environment = "Demo"
		self._add("A1")
		self._add("A2")
		self.bol.pages = [["A1"], ["A2"]]

		orders.poll_orders()

		self.assertEqual(self.bol.pages_read(), [1])
		self.assertEqual(len(self._sales_orders("A1")), 1)

	def test_cutover_guard_skips_older_orders(self):
		# Placed 2026-11-09 23:30 in Amsterdam.
		self.settings.import_orders_placed_after = "2026-11-10 00:00:00"
		self._add("A1")

		orders.poll_orders()

		self.assertEqual(self._sales_orders("A1"), [])
		self.assertEqual(self._error_logs(), [])

	def test_cutover_guard_compares_in_the_system_time_zone(self):
		# 23:30+01:00 is 22:30 UTC: read naively as UTC, the order would wrongly count as before 23:00.
		self.settings.import_orders_placed_after = "2026-11-09 23:00:00"
		self._add("A1")

		orders.poll_orders()

		self.assertEqual(len(self._sales_orders("A1")), 1)
