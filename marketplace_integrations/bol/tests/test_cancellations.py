# Cancellations (story 10) with bol's order, cancellation and process-status endpoints faked in memory.
# Everything stays in the test transaction.

import copy
import html
import itertools
import json
from pathlib import Path
from unittest.mock import patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from marketplace_integrations.bol import cancellations, orders, process_status
from marketplace_integrations.bol.api.errors import BolServerError, BolValidationError
from marketplace_integrations.bol.tests.utils import (
	BOL_MANAGER,
	discard_pending_logs,
	make_bol_manager,
	make_catalog_masters,
	make_item,
	make_order_masters,
)

FIXTURES = Path(__file__).parent / "fixtures"
ORDER = json.loads((FIXTURES / "order.json").read_text())
SHIRT, BAG = "_Test bol cancel shirt", "_Test bol cancel bag"
SHIRT_EAN, BAG_EAN = "4006381333931", "8712345678906"
SHIRT_ITEM, BAG_ITEM = "2012345678", "2012345679"
TZ = "Europe/Amsterdam"
SO = "Sales Order"


class FakeCancellations:
	"""bol's order, cancellation and process-status endpoints in memory, called like BolClient.request."""

	def __init__(self):
		self.settings = frappe._dict(environment="Production")
		self.orders = {}  # orderId -> Order detail
		self.processes = {}  # processStatusId -> ProcessStatus
		self.cancelling = {}  # processStatusId -> orderItemId
		self.errors = {}  # (method, path) -> error raised instead of answering
		# The demo's list has no order items: the detail has to be fetched.
		self.list_fields = True
		self.calls = []

	def request(self, method, path, *, version, json=None, params=None, **kwargs):
		assert version == orders.VERSION, version
		if (method, path) == ("POST", process_status.PATH):
			ids = [query["processStatusId"] for query in json["processStatusQueries"]]
			return {"processStatuses": [copy.deepcopy(self.processes[i]) for i in ids if i in self.processes]}
		self.calls.append((method, path, copy.deepcopy(json)))
		error = self.errors.pop((method, path), None)
		if error:
			raise error
		if (method, path) == ("GET", orders.PATH):
			# Everything fits on page 1.
			listed = self.orders if params["page"] == 1 else {}
			return {"orders": [self.reduced(order_id) for order_id in listed]}
		if method == "GET":
			return copy.deepcopy(self.orders[path.rsplit("/", 1)[1]])
		if (method, path) == ("PUT", cancellations.PATH):
			(item,) = json["orderItems"]
			process_id = f"ps-cancel-{len(self.processes) + 1}"
			self.processes[process_id] = {
				"processStatusId": process_id,
				"eventType": "CANCEL_ORDER",
				"status": "PENDING",
				"createTimestamp": "2026-11-10T20:07:04+01:00",
			}
			self.cancelling[process_id] = item["orderItemId"]
			return copy.deepcopy(self.processes[process_id])
		raise AssertionError(f"Unexpected call {method} {path}")

	def add(self, order_id: str) -> dict:
		order = copy.deepcopy(ORDER)
		order["orderId"] = order_id
		self.orders[order_id] = order
		return order

	def line(self, order_id: str, order_item_id: str) -> dict:
		return next(i for i in self.orders[order_id]["orderItems"] if i["orderItemId"] == order_item_id)

	def cancel(self, order_id: str, order_item_id: str, quantity: int | None = None):
		"""bol cancels (part of) a line: its own decision, the buyer's, or a seller's request."""
		line = self.line(order_id, order_item_id)
		line["quantityCancelled"] = (
			quantity if quantity is not None else line["quantity"] - line["quantityShipped"]
		)

	def finish(self, process_id: str, status: str = "SUCCESS", error_message: str | None = None):
		order_item_id = self.cancelling[process_id]
		if status == "SUCCESS":
			order_id = next(
				o
				for o, order in self.orders.items()
				if any(i["orderItemId"] == order_item_id for i in order["orderItems"])
			)
			self.cancel(order_id, order_item_id)
		self.processes[process_id].update(status=status, entityId=order_item_id, errorMessage=error_message)

	def reduced(self, order_id: str) -> dict:
		order = self.orders[order_id]
		items = [
			{
				key: item[key]
				for key in (
					"orderItemId",
					"quantity",
					"quantityShipped",
					"quantityCancelled",
					"cancellationRequest",
				)
			}
			for item in order["orderItems"]
		]
		return {"orderId": order_id, "orderItems": items if self.list_fields else []}

	def puts(self) -> list[dict]:
		return [body for method, _path, body in self.calls if method == "PUT"]

	def details(self) -> list[str]:
		return [
			path.rsplit("/", 1)[1]
			for method, path, _body in self.calls
			if method == "GET" and path != orders.PATH
		]


class CancellationTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.masters = make_order_masters()

	def setUp(self):
		self.bol = FakeCancellations()
		self.settings = frappe._dict(
			enabled=1,
			import_orders=1,
			submit_orders=1,
			import_orders_placed_after=None,
			notify_user=None,
			**self.masters,
		)
		for module in (cancellations, orders, process_status):
			self._start(patch.object(module, "get_client", return_value=self.bol))
		self._start(patch.object(cancellations, "_settings", return_value=self.settings))
		self._start(patch.object(orders, "_settings", return_value=self.settings))
		self._start(patch.object(orders, "get_system_timezone", return_value=TZ))
		# Jobs commit per record; keep the work in this transaction so the rollback undoes it.
		self._start(patch.object(frappe.db, "commit"))
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.cache.delete_keys("bol:failed:cancellation")
		self.addCleanup(frappe.cache.delete_keys, "bol:failed:cancellation")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		make_catalog_masters()
		make_item(SHIRT, SHIRT_EAN, valuation_rate=10)
		make_item(BAG, BAG_EAN, valuation_rate=5)
		self.order_ids = itertools.count(1)

	def _start(self, patcher):
		self.addCleanup(patcher.stop)
		return patcher.start()

	def _sales_order(self, submit: bool = True):
		"""A Sales Order for a new bol order (2 shirts, 1 bag). Returns (order_id, Sales Order)."""
		order = self.bol.add(f"C{next(self.order_ids)}")
		self.settings.submit_orders = int(submit)
		name = orders.create_sales_order(copy.deepcopy(order), self.settings)
		return order["orderId"], frappe.get_doc(SO, name)

	def _poll(self, *order_ids):
		cancellations.sync_existing_order_changes([self.bol.reduced(o) for o in order_ids], self.bol)

	def _row(self, so, order_item_id: str):
		so.reload()
		return next((row for row in so.items if row.bol_order_item_id == order_item_id), None)

	def _deliver(self, so, shirts: int = 0):
		dn = make_delivery_note(so.name)
		dn.items = [row for row in dn.items if row.item_code == SHIRT]
		dn.items[0].qty = shirts
		dn.insert()
		dn.submit()
		return dn

	def _comments(self, so) -> list[str]:
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": SO, "reference_name": so.name, "comment_type": "Comment"},
			order_by="creation asc",
			pluck="content",
		)
		return [html.unescape(content) for content in comments]

	def _requests(self, so) -> list[str]:
		# The import comments too (the fixture's offer references aren't these items' codes).
		return [comment for comment in self._comments(so) if "The buyer asked" in comment]

	def _todos(self, so) -> list[str]:
		return frappe.get_all(
			"ToDo", filters={"reference_type": SO, "reference_name": so.name}, pluck="allocated_to"
		)

	def _error_logs(self) -> list[dict]:
		return [log for log in frappe.local.bol_pending_logs or [] if log["status"] == "Error"]

	def _finish_all(self, status: str = "SUCCESS", error_message: str | None = None):
		for process_id in list(self.bol.processes):
			self.bol.finish(process_id, status, error_message)
			process_status.check_one(process_id)


class TestRequests(CancellationTestCase):
	def test_request_flags_the_line_and_makes_one_todo(self):
		make_bol_manager()
		self.settings.notify_user = BOL_MANAGER
		order_id, so = self._sales_order()
		self.bol.line(order_id, SHIRT_ITEM)["cancellationRequest"] = True

		self._poll(order_id)
		self._poll(order_id)

		self.assertTrue(self._row(so, SHIRT_ITEM).bol_cancellation_requested)
		self.assertFalse(self._row(so, BAG_ITEM).bol_cancellation_requested)
		self.assertEqual(self._todos(so), [BOL_MANAGER])
		comments = self._requests(so)
		self.assertEqual(len(comments), 1)
		self.assertIn("The buyer asked bol to cancel 2 × _Test bol cancel shirt", comments[0])
		# The seller decides: nothing is cancelled yet.
		self.assertEqual(self._row(so, SHIRT_ITEM).qty, 2)
		self.assertEqual(self.bol.puts(), [])

	def test_second_request_on_the_order_keeps_the_open_todo(self):
		make_bol_manager()
		self.settings.notify_user = BOL_MANAGER
		order_id, so = self._sales_order()
		self.bol.line(order_id, SHIRT_ITEM)["cancellationRequest"] = True
		self._poll(order_id)
		self.bol.line(order_id, BAG_ITEM)["cancellationRequest"] = True

		self._poll(order_id)

		self.assertTrue(self._row(so, BAG_ITEM).bol_cancellation_requested)
		self.assertEqual(self._todos(so), [BOL_MANAGER])
		self.assertEqual(len(self._requests(so)), 2)

	def test_without_notify_user_only_a_comment(self):
		order_id, so = self._sales_order()
		self.bol.line(order_id, SHIRT_ITEM)["cancellationRequest"] = True

		self._poll(order_id)

		self.assertEqual(self._todos(so), [])
		self.assertEqual(len(self._requests(so)), 1)


class TestApply(CancellationTestCase):
	def test_part_of_a_line_lowers_its_qty(self):
		order_id, so = self._sales_order()
		total = so.grand_total
		self.bol.cancel(order_id, SHIRT_ITEM, 1)

		self._poll(order_id)

		shirt = self._row(so, SHIRT_ITEM)
		self.assertEqual((shirt.qty, shirt.bol_qty_cancelled), (1, 1))
		self.assertEqual(so.docstatus, 1)
		self.assertAlmostEqual(so.grand_total, total - 24.95, places=2)
		self.assertIn("bol cancelled 1 × _Test bol cancel shirt", self._comments(so)[-1])

	def test_applied_again_is_a_no_op(self):
		order_id, so = self._sales_order()
		self.bol.cancel(order_id, SHIRT_ITEM, 1)
		self._poll(order_id)
		comments = self._comments(so)

		self._poll(order_id)
		cancellations.apply_cancelled_qty(so.name, {SHIRT_ITEM: 1})

		self.assertEqual(self._row(so, SHIRT_ITEM).qty, 1)
		self.assertEqual(self._comments(so), comments)

	def test_a_whole_line_is_removed_and_the_order_stays(self):
		order_id, so = self._sales_order()
		self.bol.cancel(order_id, BAG_ITEM)

		self._poll(order_id)

		self.assertIsNone(self._row(so, BAG_ITEM))
		self.assertEqual(self._row(so, SHIRT_ITEM).qty, 2)
		self.assertEqual(so.docstatus, 1)
		self.assertAlmostEqual(so.grand_total, 49.9, places=2)

	def test_whole_order_cancels_the_sales_order(self):
		order_id, so = self._sales_order()
		self.bol.cancel(order_id, SHIRT_ITEM)
		self.bol.cancel(order_id, BAG_ITEM)

		self._poll(order_id)

		so.reload()
		self.assertEqual(so.docstatus, 2)
		self.assertEqual([row.bol_qty_cancelled for row in so.items], [2, 1])
		self.assertIn("The Sales Order was cancelled", self._comments(so)[-1])
		# Cancelled orders are left alone from then on.
		self._poll(order_id)
		self.assertEqual(self._error_logs(), [])

	def test_never_below_the_delivered_qty(self):
		order_id, so = self._sales_order()
		self._deliver(so, shirts=2)
		self.bol.cancel(order_id, SHIRT_ITEM, 1)

		self._poll(order_id)
		self._poll(order_id)

		shirt = self._row(so, SHIRT_ITEM)
		self.assertEqual((shirt.qty, shirt.bol_qty_cancelled), (2, 0))
		(log,) = self._error_logs()
		self.assertIn("are delivered already", log["message"])
		self.assertEqual(log["bol_reference_name"], so.name)
		self.assertEqual(sum("are delivered already" in c for c in self._comments(so)), 1)

	def test_delivered_lines_stay_while_the_rest_is_removed(self):
		order_id, so = self._sales_order()
		self._deliver(so, shirts=2)
		self.bol.cancel(order_id, BAG_ITEM)

		self._poll(order_id)

		so.reload()
		self.assertEqual(so.docstatus, 1)
		self.assertEqual([row.item_code for row in so.items], [SHIRT])
		self.assertEqual(so.per_delivered, 100)

	def test_update_items_failure_is_rolled_back_and_logged_once(self):
		order_id, so = self._sales_order()
		self.bol.cancel(order_id, SHIRT_ITEM, 1)

		with patch.object(
			cancellations, "update_child_qty_rate", side_effect=frappe.ValidationError("Stock reserved")
		):
			self._poll(order_id)
			self._poll(order_id)

		shirt = self._row(so, SHIRT_ITEM)
		self.assertEqual((shirt.qty, shirt.bol_qty_cancelled), (2, 0))
		(log,) = self._error_logs()
		self.assertIn("Stock reserved", log["message"])
		self.assertEqual(sum("not applied" in c for c in self._comments(so)), 1)

		# Retried on the next poll.
		self._poll(order_id)
		self.assertEqual(self._row(so, SHIRT_ITEM).qty, 1)

	def test_draft_is_edited(self):
		order_id, so = self._sales_order(submit=False)
		self.bol.cancel(order_id, SHIRT_ITEM, 1)
		self.bol.cancel(order_id, BAG_ITEM)

		self._poll(order_id)

		so.reload()
		self.assertEqual(so.docstatus, 0)
		self.assertEqual(
			[(row.item_code, row.qty, row.bol_qty_cancelled) for row in so.items], [(SHIRT, 1, 1)]
		)

	def test_draft_cancelled_completely_is_deleted(self):
		order_id, so = self._sales_order(submit=False)
		self.bol.cancel(order_id, SHIRT_ITEM)
		self.bol.cancel(order_id, BAG_ITEM)

		self._poll(order_id)

		self.assertFalse(frappe.db.exists(SO, so.name))
		(log,) = [log for log in frappe.local.bol_pending_logs or [] if so.name in log["message"]]
		self.assertEqual(log["status"], "Success")
		self.assertIn("deleted", log["message"])


class TestPolling(CancellationTestCase):
	def test_list_data_is_enough(self):
		order_id, so = self._sales_order()
		self.bol.cancel(order_id, SHIRT_ITEM, 1)

		orders.poll_orders()

		self.assertEqual(self._row(so, SHIRT_ITEM).qty, 1)
		self.assertEqual(self.bol.details(), [])

	def test_list_without_the_fields_fetches_the_detail(self):
		order_id, so = self._sales_order()
		self.bol.list_fields = False
		self.bol.cancel(order_id, SHIRT_ITEM, 1)

		orders.poll_orders()

		self.assertEqual(self._row(so, SHIRT_ITEM).qty, 1)
		self.assertEqual(self.bol.details(), [order_id])

	def test_detail_failure_is_logged_and_the_next_order_synced(self):
		first_id, _first = self._sales_order()
		second_id, second = self._sales_order()
		self.bol.list_fields = False
		self.bol.cancel(second_id, SHIRT_ITEM, 1)
		self.bol.errors[("GET", f"{orders.PATH}/{first_id}")] = BolServerError("bol answered 500")

		self._poll(first_id, second_id)

		self.assertEqual(self._row(second, SHIRT_ITEM).qty, 1)
		(log,) = self._error_logs()
		self.assertIn("bol answered 500", log["message"])

	def test_daily_check_covers_recent_open_orders(self):
		recent_id, recent = self._sales_order()
		old_id, old = self._sales_order()
		frappe.db.set_value(SO, old.name, "creation", add_to_date(now_datetime(), days=-31))
		gone_id, _gone = self._sales_order()
		self.bol.cancel(gone_id, SHIRT_ITEM)
		self.bol.cancel(gone_id, BAG_ITEM)
		self._poll(gone_id)
		self.bol.calls.clear()
		# bol cancelled every open unit: the order is no longer in the OPEN list.
		self.bol.cancel(recent_id, SHIRT_ITEM)
		self.bol.cancel(recent_id, BAG_ITEM)

		cancellations.sync_recent_orders()

		self.assertEqual(self.bol.details(), [recent_id])
		self.assertEqual(frappe.db.get_value(SO, recent.name, "docstatus"), 2)

	def test_switched_off_does_nothing(self):
		self._sales_order()
		self.settings.import_orders = 0

		cancellations.sync_recent_orders()

		self.assertEqual(self.bol.calls, [])


class TestSellerCancel(CancellationTestCase):
	def test_needs_a_bol_role(self):
		_order_id, so = self._sales_order()
		frappe.set_user("Guest")

		with self.assertRaises(frappe.PermissionError):
			cancellations.cancel_bol_items(so.name, [so.items[0].name], "OUT_OF_STOCK")
		self.assertEqual(self.bol.puts(), [])

	def test_needs_write_permission_on_the_sales_order(self):
		_order_id, so = self._sales_order()
		make_bol_manager()
		frappe.set_user(BOL_MANAGER)

		with self.assertRaises(frappe.PermissionError):
			cancellations.cancel_bol_items(so.name, [so.items[0].name], "OUT_OF_STOCK")

	def test_unknown_reason_is_refused(self):
		_order_id, so = self._sales_order()

		with self.assertRaisesRegex(frappe.ValidationError, "Unknown bol cancellation reason"):
			cancellations.cancel_bol_items(so.name, [so.items[0].name], "CHANGED_MY_MIND")
		self.assertEqual(self.bol.puts(), [])

	def test_delivered_line_is_refused(self):
		_order_id, so = self._sales_order()
		self._deliver(so, shirts=1)
		so.reload()

		with self.assertRaisesRegex(frappe.ValidationError, "delivered already"):
			cancellations.cancel_bol_items(so.name, [so.items[0].name], "OUT_OF_STOCK")
		self.assertEqual(self.bol.puts(), [])

	def test_line_of_another_order_or_no_lines_is_refused(self):
		_order_id, so = self._sales_order()
		_other_id, other = self._sales_order()

		with self.assertRaisesRegex(frappe.ValidationError, "isn't an open bol line"):
			cancellations.cancel_bol_items(so.name, [other.items[0].name], "OUT_OF_STOCK")
		with self.assertRaisesRegex(frappe.ValidationError, "Choose the lines"):
			cancellations.cancel_bol_items(so.name, "[]", "OUT_OF_STOCK")

	def test_line_being_cancelled_is_refused(self):
		_order_id, so = self._sales_order()
		cancellations.cancel_bol_items(so.name, [so.items[0].name], "OUT_OF_STOCK")

		with self.assertRaisesRegex(frappe.ValidationError, "being cancelled on bol already"):
			cancellations.cancel_bol_items(so.name, json.dumps([so.items[0].name]), "OUT_OF_STOCK")
		self.assertEqual(len(self.bol.puts()), 1)

	def test_one_request_per_line_then_the_sales_order_follows(self):
		_order_id, so = self._sales_order()
		shirt, bag = (row.name for row in so.items)

		result = cancellations.cancel_bol_items(so.name, json.dumps([shirt, bag]), "REQUESTED_BY_CUSTOMER")

		self.assertEqual(result, {"sent": [shirt, bag], "failed": [], "resynced": False})
		self.assertEqual(
			self.bol.puts(),
			[
				{"orderItems": [{"orderItemId": SHIRT_ITEM, "reasonCode": "REQUESTED_BY_CUSTOMER"}]},
				{"orderItems": [{"orderItemId": BAG_ITEM, "reasonCode": "REQUESTED_BY_CUSTOMER"}]},
			],
		)
		processes = frappe.get_all(
			process_status.DOCTYPE,
			filters={"reference_name": so.name, "handler": cancellations.HANDLER},
			pluck="payload",
		)
		self.assertEqual(len(processes), 2)
		# Nothing changes before bol has done it.
		so.reload()
		self.assertEqual(len(so.items), 2)

		self._finish_all()

		so.reload()
		self.assertEqual(so.docstatus, 2)

	def test_success_of_one_line_removes_it(self):
		order_id, so = self._sales_order()

		cancellations.cancel_bol_items(so.name, [self._row(so, BAG_ITEM).name], "OUT_OF_STOCK")
		self._finish_all()

		self.assertIsNone(self._row(so, BAG_ITEM))
		self.assertEqual(so.docstatus, 1)
		# The next poll sees bol's cancellation: nothing more to do.
		self._poll(order_id)
		self.assertEqual(self._row(so, SHIRT_ITEM).qty, 2)
		self.assertEqual(self._error_logs(), [])

	def test_failure_comments_and_resyncs_from_bol(self):
		order_id, so = self._sales_order()
		cancellations.cancel_bol_items(so.name, [self._row(so, SHIRT_ITEM).name], "OUT_OF_STOCK")
		# bol cancelled one shirt itself; it refuses the seller's request.
		self.bol.cancel(order_id, SHIRT_ITEM, 1)

		self._finish_all("FAILURE", "Order item was already cancelled")

		self.assertTrue(
			any("bol cancellation failed: Order item was already cancelled" in c for c in self._comments(so))
		)
		self.assertEqual(self._row(so, SHIRT_ITEM).qty, 1)
		self.assertEqual(self.bol.details(), [order_id])

	def test_rejected_request_resyncs_from_bol(self):
		order_id, so = self._sales_order()
		self.bol.cancel(order_id, BAG_ITEM)
		self.bol.errors[("PUT", cancellations.PATH)] = BolValidationError(
			"Bad request",
			None,
			[{"name": "orderItems[0].orderItemId", "reason": "Order item is already cancelled"}],
		)
		bag = self._row(so, BAG_ITEM).name

		result = cancellations.cancel_bol_items(so.name, [bag], "OUT_OF_STOCK")

		self.assertEqual(result["sent"], [])
		self.assertTrue(result["resynced"])
		self.assertIn("Order item is already cancelled", result["failed"][0]["message"])
		self.assertIsNone(self._row(so, BAG_ITEM))
		self.assertEqual(len(self._error_logs()), 1)

	def test_partial_failure_is_returned_not_thrown(self):
		_order_id, so = self._sales_order()
		shirt, bag = (row.name for row in so.items)
		# The first PUT goes through; the second fails after the client's retries.
		original = self.bol.request

		def fail_second_put(method, path, **kwargs):
			if method == "PUT" and self.bol.puts():
				self.bol.calls.append((method, path, kwargs.get("json")))
				raise BolServerError("bol answered 500")
			return original(method, path, **kwargs)

		with patch.object(self.bol, "request", side_effect=fail_second_put):
			result = cancellations.cancel_bol_items(so.name, [shirt, bag], "OUT_OF_STOCK")

		self.assertEqual(result["sent"], [shirt])
		self.assertEqual([f["so_detail"] for f in result["failed"]], [bag])
		self.assertTrue(frappe.db.exists(process_status.DOCTYPE, {"reference_name": so.name}))

	def test_nothing_sent_is_an_error_message(self):
		_order_id, so = self._sales_order()
		self.bol.errors[("PUT", cancellations.PATH)] = BolServerError("bol answered 500")

		with self.assertRaisesRegex(frappe.ValidationError, "not cancelled on bol: bol answered 500"):
			cancellations.cancel_bol_items(so.name, [so.items[0].name], "OUT_OF_STOCK")

	def test_form_offers_the_reasons(self):
		_order_id, so = self._sales_order()

		so.run_method("onload")

		self.assertEqual(so.get_onload().get("bol_cancel_reasons"), cancellations.REASON_CODES)


class TestCancelGuard(CancellationTestCase):
	def test_user_cancel_of_an_open_bol_order_is_blocked(self):
		_order_id, so = self._sales_order()

		with self.assertRaisesRegex(frappe.ValidationError, "Use Cancel on bol"):
			so.cancel()
		self.assertEqual(frappe.db.get_value(SO, so.name, "docstatus"), 1)

	def test_allowed_while_import_is_off(self):
		_order_id, so = self._sales_order()
		self.settings.import_orders = 0

		so.cancel()

		self.assertEqual(so.docstatus, 2)
