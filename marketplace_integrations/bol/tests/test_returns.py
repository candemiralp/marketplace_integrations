# Returns (story 11) with bol's returns and process-status endpoints faked in memory.
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

from marketplace_integrations.bol import labels, orders, process_status, returns, shipments
from marketplace_integrations.bol.api.errors import (
	BolAuthError,
	BolServerError,
	BolTransientError,
	BolValidationError,
)
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
RETURN = json.loads((FIXTURES / "return.json").read_text())
SHIRT, BAG = "_Test bol return shirt", "_Test bol return bag"
SHIRT_EAN, BAG_EAN = "4006381333931", "8712345678906"
TZ = "Europe/Amsterdam"
DN = "Delivery Note"
REASON = {"mainReason": "Verkeerde maat of formaat", "customerComments": "Te klein"}


class FakeReturns:
	"""bol's returns and process-status endpoints in memory, called like BolClient.request."""

	def __init__(self):
		self.settings = frappe._dict(environment="Production")
		self.returns = {}  # returnId -> Return detail
		self.processes = {}  # processStatusId -> ProcessStatus
		self.handling = {}  # processStatusId -> rmaId
		self.errors = {}  # (method, path) -> error raised instead of answering
		self.calls = []
		self.ids = itertools.count(1)

	def request(self, method, path, *, version, json=None, params=None, **kwargs):
		assert version == returns.VERSION, version
		if (method, path) == ("POST", process_status.PATH):
			ids = [query["processStatusId"] for query in json["processStatusQueries"]]
			return {"processStatuses": [copy.deepcopy(self.processes[i]) for i in ids if i in self.processes]}
		self.calls.append((method, path, copy.deepcopy(json)))
		error = self.errors.pop((method, path), None)
		if error:
			raise error
		if (method, path) == ("GET", returns.PATH):
			assert params["handled"] == "false" and params["fulfilment-method"] == "FBR", params
			# Everything fits on page 1.
			listed = self.returns.values() if params["page"] == 1 else ()
			return {"returns": [self.reduced(ret) for ret in listed if not self._handled(ret)]}
		if method == "GET":
			return copy.deepcopy(self.returns[path.rsplit("/", 1)[1]])
		if method == "PUT":
			rma_id = path.rsplit("/", 1)[1]
			if self.item(rma_id)["handled"]:
				raise BolValidationError("Bad Request", "Return item is already handled.")
			process_id = f"ps-return-{len(self.processes) + 1}"
			self.processes[process_id] = {
				"processStatusId": process_id,
				"eventType": "HANDLE_RETURN_ITEM",
				"status": "PENDING",
				"createTimestamp": "2026-11-10T20:07:04+01:00",
			}
			self.handling[process_id] = rma_id
			return copy.deepcopy(self.processes[process_id])
		raise AssertionError(f"Unexpected call {method} {path}")

	def add(self, order_id: str, ean: str = SHIRT_EAN, quantity: int = 1) -> dict:
		"""A return of one item, registered by the buyer. Returns the item."""
		ret = copy.deepcopy(RETURN)
		ret["returnId"] = str(next(self.ids) + 90000)
		item = copy.deepcopy(ret["returnItems"][1])
		item.update(
			rmaId=str(next(self.ids) + 60000000),
			orderId=order_id,
			ean=ean,
			expectedQuantity=quantity,
			returnReason=dict(REASON),
			handled=False,
		)
		ret["returnItems"] = [item]
		self.returns[ret["returnId"]] = ret
		return item

	def return_id(self, rma_id: str) -> str:
		return next(rid for rid, ret in self.returns.items() if ret["returnItems"][0]["rmaId"] == rma_id)

	def item(self, rma_id: str) -> dict:
		return next(i for ret in self.returns.values() for i in ret["returnItems"] if i["rmaId"] == rma_id)

	def finish(self, process_id: str, status: str = "SUCCESS", error_message: str | None = None):
		rma_id = self.handling[process_id]
		if status == "SUCCESS":
			self.item(rma_id)["handled"] = True
		self.processes[process_id].update(status=status, entityId=rma_id, errorMessage=error_message)

	def reduced(self, ret: dict) -> dict:
		keys = ("rmaId", "orderId", "ean", "expectedQuantity", "returnReason", "handled")
		return {
			"returnId": ret["returnId"],
			"registrationDateTime": ret["registrationDateTime"],
			"fulfilmentMethod": "FBR",
			"returnItems": [{key: item[key] for key in keys} for item in ret["returnItems"]],
		}

	def puts(self) -> list[tuple[str, dict]]:
		return [(path, body) for method, path, body in self.calls if method == "PUT"]

	def _handled(self, ret: dict) -> bool:
		return all(item["handled"] for item in ret["returnItems"])


class ReturnTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.masters = make_order_masters()

	def setUp(self):
		self.bol = FakeReturns()
		self.settings = frappe._dict(enabled=1, handle_returns=1, notify_user="Administrator")
		self._start(patch.object(returns, "get_client", return_value=self.bol))
		self._start(patch.object(process_status, "get_client", return_value=self.bol))
		self._start(patch.object(returns, "_settings", return_value=self.settings))
		# Shipments and labels stay out of it: these Delivery Notes are shipped already.
		off = frappe._dict(enabled=0)
		self._start(patch.object(shipments, "_settings", return_value=off))
		self._start(patch.object(labels, "_settings", return_value=off))
		self.enqueued = self._start(patch.object(returns, "enqueue_report"))
		# Jobs commit per record; keep the work in this transaction so the rollback undoes it.
		self._start(patch.object(frappe.db, "commit"))
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.cache.delete_keys("bol:failed:return")
		self.addCleanup(frappe.cache.delete_keys, "bol:failed:return")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		make_catalog_masters()
		make_item(SHIRT, SHIRT_EAN, valuation_rate=10)
		make_item(BAG, BAG_EAN, valuation_rate=5)
		self.order_ids = itertools.count(1)

	def _start(self, patcher):
		self.addCleanup(patcher.stop)
		return patcher.start()

	def _sales_order(self, shirts: int = 2) -> tuple[str, str]:
		"""A submitted Sales Order for a new bol order (shirts, 1 bag). Returns (order_id, its name)."""
		order = copy.deepcopy(ORDER)
		order["orderId"] = f"R{next(self.order_ids)}"
		order["orderItems"][0].update(quantity=shirts, totalPrice=round(24.95 * shirts, 2))
		settings = frappe._dict(import_orders_placed_after=None, submit_orders=1, **self.masters)
		with patch.object(orders, "get_system_timezone", return_value=TZ):
			return order["orderId"], orders.create_sales_order(order, settings)

	def _deliver(self, so_name: str, shirts: int | None = None):
		"""Submit a Delivery Note of the Sales Order; with `shirts`, only that many shirts."""
		dn = make_delivery_note(so_name)
		if shirts is not None:
			dn.items = [row for row in dn.items if row.item_code == SHIRT]
			dn.items[0].qty = shirts
		dn.insert()
		dn.submit()
		return dn

	def _shipped_order(self) -> tuple[str, object]:
		order_id, so_name = self._sales_order()
		return order_id, self._deliver(so_name)

	def _returns(self, rma_id: str | None = None) -> list:
		filters = {"bol_rma_id": rma_id} if rma_id else {"bol_rma_id": ("is", "set")}
		return [frappe.get_doc(DN, name) for name in frappe.get_all(DN, filters=filters, pluck="name")]

	def _draft(self, quantity: int = 1):
		"""A draft bol return for one shirt of a shipped order. Returns (return DN, original DN, rma id)."""
		order_id, dn = self._shipped_order()
		rma_id = self.bol.add(order_id, quantity=quantity)["rmaId"]
		returns.poll_returns()
		(ret,) = self._returns(rma_id)
		return ret, dn, rma_id

	def _submitted(self, handling_result: str = "RETURN_RECEIVED"):
		ret, dn, rma_id = self._draft()
		ret.bol_handling_result = handling_result
		ret.submit()
		return ret, dn, rma_id

	def _finish(self, status: str = "SUCCESS", error_message: str | None = None):
		for process_id in list(self.bol.processes):
			if self.bol.processes[process_id]["status"] == "PENDING":
				self.bol.finish(process_id, status, error_message)
				process_status.check_one(process_id)

	def _status(self, name: str) -> str:
		return frappe.db.get_value(DN, name, "bol_return_status")

	def _comments(self, name: str) -> list[str]:
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": DN, "reference_name": name, "comment_type": "Comment"},
			order_by="creation asc",
			pluck="content",
		)
		return [html.unescape(content) for content in comments]

	def _todos(self, rma_id: str) -> list[dict]:
		return frappe.get_all(
			"ToDo",
			filters={"description": ("like", f"%{rma_id}%"), "status": "Open"},
			fields=["allocated_to", "reference_type", "reference_name", "description"],
		)

	def _error_logs(self) -> list[dict]:
		return [log for log in self._logs() if log["status"] == "Error"]

	def _logs(self) -> list[dict]:
		return getattr(frappe.local, "bol_pending_logs", None) or []

	def _make_old(self, name: str, minutes: int = returns.SWEEP_AFTER_MINUTES + 1):
		frappe.db.set_value(DN, name, "modified", add_to_date(now_datetime(), minutes=-minutes), update_modified=False)


class TestImport(ReturnTestCase):
	def test_return_becomes_a_draft_with_only_the_returned_line(self):
		order_id, dn = self._shipped_order()
		item = self.bol.add(order_id, quantity=1)

		returns.poll_returns()

		(ret,) = self._returns(item["rmaId"])
		self.assertEqual((ret.docstatus, ret.is_return, ret.return_against), (0, 1, dn.name))
		shirt = next(row for row in dn.items if row.item_code == SHIRT)
		self.assertEqual([(row.item_code, row.qty, row.dn_detail) for row in ret.items], [(SHIRT, -1, shirt.name)])
		self.assertEqual(ret.items[0].stock_qty, -1)
		self.assertEqual(
			(ret.bol_return_id, ret.bol_expected_qty, ret.bol_return_status, ret.bol_return_reason),
			(self.bol.return_id(item["rmaId"]), 1, "Open", "Verkeerde maat of formaat: Te klein"),
		)
		(todo,) = self._todos(item["rmaId"])
		self.assertEqual((todo.allocated_to, todo.reference_type, todo.reference_name), ("Administrator", DN, ret.name))
		self.assertIn("inspect and submit", todo.description)
		# No financial documents (story 12), and bol is told nothing yet.
		self.assertEqual(self.bol.puts(), [])

	def test_several_delivery_notes_take_the_one_that_covers_it(self):
		order_id, so_name = self._sales_order(shirts=3)
		self._deliver(so_name, shirts=1)
		second = self._deliver(so_name, shirts=2)
		item = self.bol.add(order_id, quantity=2)

		returns.poll_returns()

		(ret,) = self._returns(item["rmaId"])
		self.assertEqual((ret.return_against, ret.items[0].qty), (second.name, -2))

	def test_oldest_delivery_note_first(self):
		order_id, so_name = self._sales_order(shirts=2)
		first = self._deliver(so_name, shirts=1)
		self._deliver(so_name, shirts=1)
		item = self.bol.add(order_id, quantity=1)

		returns.poll_returns()

		self.assertEqual(self._returns(item["rmaId"])[0].return_against, first.name)

	def test_open_drafts_count_against_what_can_come_back(self):
		order_id, dn = self._shipped_order()
		first = self.bol.add(order_id)
		second = self.bol.add(order_id)
		third = self.bol.add(order_id)

		returns.poll_returns()

		# Two shirts were shipped: the third return has nothing left to take back.
		self.assertEqual(len(self._returns(first["rmaId"]) + self._returns(second["rmaId"])), 2)
		self.assertEqual(self._returns(third["rmaId"]), [])
		self.assertIn(third["rmaId"], self._error_logs()[0]["message"])

	def test_not_found_creates_nothing_and_is_logged_once_with_a_todo(self):
		order_id, _so_name = self._sales_order()  # never shipped
		item = self.bol.add(order_id)
		unknown = self.bol.add("1044796550")  # shipped before the cutover

		returns.poll_returns()
		returns.poll_returns()

		self.assertEqual(self._returns(), [])
		logs = self._error_logs()
		self.assertEqual(len(logs), 2)
		self.assertIn(item["rmaId"], logs[0]["message"])
		self.assertIn("no submitted Delivery Note", logs[0]["message"])
		(todo,) = self._todos(item["rmaId"])
		self.assertEqual(todo.reference_name, _so_name)
		(todo,) = self._todos(unknown["rmaId"])
		self.assertIsNone(todo.reference_name)
		self.assertIn("1044796550", todo.description)

	def test_no_notify_user_no_todo(self):
		self.settings.notify_user = None
		order_id, _dn = self._shipped_order()
		item = self.bol.add(order_id)

		returns.poll_returns()

		self.assertEqual(len(self._returns(item["rmaId"])), 1)
		self.assertEqual(self._todos(item["rmaId"]), [])

	def test_duplicate_poll_makes_one_return(self):
		order_id, _dn = self._shipped_order()
		item = self.bol.add(order_id)

		returns.poll_returns()
		returns.poll_returns()

		self.assertEqual(len(self._returns(item["rmaId"])), 1)
		self.assertEqual(len(self._todos(item["rmaId"])), 1)

	def test_created_concurrently_is_already_done(self):
		order_id, _dn = self._shipped_order()
		self.bol.add(order_id)
		error = frappe.UniqueValidationError(DN, "MAT-DN-1", Exception("Duplicate entry for key 'bol_rma_id'"))

		with patch.object(returns, "create_return", side_effect=error):
			returns.poll_returns()

		self.assertEqual(self._error_logs(), [])

	def test_failure_is_rolled_back_logged_once_and_retried(self):
		order_id, _dn = self._shipped_order()
		item = self.bol.add(order_id)

		with patch.object(returns, "make_sales_return", side_effect=frappe.ValidationError("Return blocked")):
			returns.poll_returns()
			returns.poll_returns()

		self.assertEqual(self._returns(), [])
		(log,) = self._error_logs()
		self.assertIn("Return blocked", log["message"])
		self.assertEqual(self._todos(item["rmaId"]), [])

		returns.poll_returns()
		self.assertEqual(len(self._returns(item["rmaId"])), 1)

	def test_rma_being_reported_is_skipped(self):
		order_id, dn = self._shipped_order()
		item = self.bol.add(order_id)
		process_status.track(
			{"processStatusId": "ps-elsewhere"}, returns.HANDLER, dn, payload={"rma_id": item["rmaId"]}
		)

		returns.poll_returns()

		self.assertEqual(self._returns(), [])

	def test_handled_returns_are_left_out(self):
		order_id, _dn = self._shipped_order()
		self.bol.add(order_id)["handled"] = True

		returns.poll_returns()

		self.assertEqual(self._returns(), [])

	def test_list_failure_is_logged_once_and_raised(self):
		self.bol.errors[("GET", returns.PATH)] = BolServerError("bol answered 500")

		with self.assertRaises(BolServerError):
			returns.poll_returns()

		(log,) = self._error_logs()
		self.assertIn("bol answered 500", log["message"])

	def test_switched_off_makes_no_call(self):
		self.settings.handle_returns = 0

		returns.poll_returns()

		self.assertEqual(self.bol.calls, [])

	def test_demo_reads_only_page_one(self):
		self.bol.settings.environment = "Demo"

		returns.poll_returns()

		self.assertEqual(len([call for call in self.bol.calls if call[0] == "GET"]), 1)


class TestSubmit(ReturnTestCase):
	def test_handling_result_is_required(self):
		ret, _dn, _rma_id = self._draft()

		with self.assertRaisesRegex(frappe.ValidationError, "bol Handling Result"):
			ret.submit()

	def test_more_than_expected_is_blocked(self):
		ret, _dn, _rma_id = self._draft()
		ret.bol_handling_result = "RETURN_RECEIVED"
		ret.items[0].qty = -2

		with self.assertRaisesRegex(frappe.ValidationError, "bol expects 1 units back"):
			ret.submit()

	def test_part_units_are_blocked(self):
		ret, _dn, _rma_id = self._draft()
		ret.bol_handling_result = "RETURN_RECEIVED"
		ret.items[0].qty = -0.5

		with self.assertRaisesRegex(frappe.ValidationError, "whole units"):
			ret.submit()

	def test_submit_queues_the_report(self):
		ret, _dn, _rma_id = self._submitted()

		self.enqueued.assert_called_once_with(ret.name)

	def test_switched_off_submits_without_a_report(self):
		ret, _dn, _rma_id = self._draft()
		self.settings.handle_returns = 0
		ret.bol_handling_result = "RETURN_RECEIVED"

		ret.submit()

		self.enqueued.assert_not_called()

	def test_fewer_than_expected_is_reported_as_inspected(self):
		ret, _dn, _rma_id = self._draft(quantity=2)
		ret.bol_handling_result = "RETURN_DOES_NOT_MEET_CONDITIONS"
		ret.items[0].qty = -1
		ret.submit()

		returns.report_handling(ret.name)

		self.assertEqual(self.bol.puts()[0][1]["quantityReturned"], 1)

	def test_other_delivery_notes_are_left_alone(self):
		_order_id, dn = self._shipped_order()

		self.enqueued.assert_not_called()
		self.assertFalse(dn.bol_return_status)


class TestReport(ReturnTestCase):
	def test_success_marks_reported(self):
		ret, _dn, rma_id = self._submitted()

		returns.report_handling(ret.name)

		self.assertEqual(
			self.bol.puts(),
			[(f"{returns.PATH}/{rma_id}", {"handlingResult": "RETURN_RECEIVED", "quantityReturned": 1})],
		)
		self.assertEqual(self._status(ret.name), "Open")
		self.assertTrue(
			frappe.db.exists(
				process_status.DOCTYPE, {"reference_name": ret.name, "handler": returns.HANDLER, "status": "PENDING"}
			)
		)

		self._finish()

		self.assertEqual(self._status(ret.name), "Reported")
		self.assertIn(f"bol processed return {rma_id}: RETURN_RECEIVED, 1 units.", self._comments(ret.name))

	def test_running_twice_sends_once(self):
		ret, _dn, _rma_id = self._submitted()

		returns.report_handling(ret.name)
		returns.report_handling(ret.name)
		self._finish()
		returns.report_handling(ret.name)

		self.assertEqual(len(self.bol.puts()), 1)

	def test_failure_at_bol_marks_failed(self):
		ret, _dn, _rma_id = self._submitted()
		returns.report_handling(ret.name)

		self._finish("FAILURE", "Quantity returned is invalid")

		self.assertEqual(self._status(ret.name), "Failed")
		self.assertTrue(any("Quantity returned is invalid" in c for c in self._comments(ret.name)))

	def test_failure_while_bol_has_it_handled_is_reported(self):
		ret, _dn, rma_id = self._submitted()
		returns.report_handling(ret.name)
		# Handled in the seller dashboard meanwhile.
		self.bol.item(rma_id)["handled"] = True

		self._finish("FAILURE", "Return item is already handled")

		self.assertEqual(self._status(ret.name), "Reported")

	def test_already_handled_is_reported_not_failed(self):
		ret, _dn, rma_id = self._submitted()
		self.bol.item(rma_id)["handled"] = True

		returns.report_handling(ret.name)

		self.assertEqual(self._status(ret.name), "Reported")
		self.assertIn("Marked reported without sending it again", self._comments(ret.name)[-1])
		self.assertEqual(self._error_logs(), [])
		self.assertFalse(frappe.db.exists(process_status.DOCTYPE, {"reference_name": ret.name}))

	def test_rejected_marks_failed_with_the_violations(self):
		ret, _dn, rma_id = self._submitted()
		self.bol.errors[("PUT", f"{returns.PATH}/{rma_id}")] = BolValidationError(
			"Bad Request", None, [{"name": "quantityReturned", "reason": "must be less than or equal to 1"}]
		)

		returns.report_handling(ret.name)

		self.assertEqual(self._status(ret.name), "Failed")
		self.assertIn("must be less than or equal to 1", self._comments(ret.name)[-1])
		self.assertEqual(len(self._error_logs()), 1)

	def test_rejected_and_bol_unreachable_for_the_check_is_failed(self):
		ret, _dn, rma_id = self._submitted()
		self.bol.errors[("PUT", f"{returns.PATH}/{rma_id}")] = BolValidationError("Bad Request", None)
		self.bol.errors[("GET", f"{returns.PATH}/{ret.bol_return_id}")] = BolServerError("bol answered 500")

		returns.report_handling(ret.name)

		self.assertEqual(self._status(ret.name), "Failed")
		self.assertEqual(len(self._error_logs()), 2)

	def test_bol_unreachable_stays_open_logged_once_and_is_swept(self):
		ret, _dn, rma_id = self._submitted()
		path = f"{returns.PATH}/{rma_id}"
		self.bol.errors[("PUT", path)] = BolTransientError("bol request failed: ConnectTimeout")

		returns.report_handling(ret.name)
		self.bol.errors[("PUT", path)] = BolTransientError("bol request failed: ConnectTimeout")
		returns.report_handling(ret.name)

		self.assertEqual(self._status(ret.name), "Open")
		self.assertEqual(len(self._error_logs()), 1)
		self.assertEqual(sum("tried again automatically" in c for c in self._comments(ret.name)), 1)

		self.enqueued.reset_mock()
		returns.sweep_reports()
		self.enqueued.assert_not_called()  # too recent

		self._make_old(ret.name)
		returns.poll_returns()
		self.enqueued.assert_called_once_with(ret.name)

	def test_sweep_skips_reported_failed_and_pending(self):
		reported, _dn, _rma = self._submitted()
		returns.report_handling(reported.name)
		self._finish()
		pending, _dn, _rma = self._submitted()
		returns.report_handling(pending.name)
		failed, _dn, rma_id = self._submitted()
		self.bol.errors[("PUT", f"{returns.PATH}/{rma_id}")] = BolAuthError("bol denied access (HTTP 403)")
		returns.report_handling(failed.name)
		for name in (reported.name, pending.name, failed.name):
			self._make_old(name)
		self.enqueued.reset_mock()

		returns.sweep_reports()

		self.enqueued.assert_not_called()

	def test_auth_error_marks_failed(self):
		ret, _dn, rma_id = self._submitted()
		self.bol.errors[("PUT", f"{returns.PATH}/{rma_id}")] = BolAuthError("bol denied access (HTTP 403)")

		returns.report_handling(ret.name)

		self.assertEqual(self._status(ret.name), "Failed")
		self.assertIn("HTTP 403", self._comments(ret.name)[-1])
		self.assertEqual(len(self._error_logs()), 1)

	def test_unexpected_error_marks_failed_and_is_raised(self):
		ret, _dn, _rma_id = self._submitted()

		with (
			patch.object(process_status, "track", side_effect=RuntimeError("tracker broke")),
			self.assertRaises(RuntimeError),
		):
			returns.report_handling(ret.name)

		self.assertEqual(self._status(ret.name), "Failed")
		self.assertIn("tracker broke", self._comments(ret.name)[-1])
		self.assertEqual(len(self._error_logs()), 1)


class TestRetry(ReturnTestCase):
	def _failed(self):
		ret, dn, rma_id = self._submitted()
		self.bol.errors[("PUT", f"{returns.PATH}/{rma_id}")] = BolAuthError("bol denied access (HTTP 403)")
		returns.report_handling(ret.name)
		return ret

	def test_retry_sends_it_again(self):
		ret = self._failed()

		result = returns.retry_return_report(ret.name)

		self.assertEqual(result, {"status": "Open"})
		self.assertEqual(len(self.bol.puts()), 2)
		self._finish()
		self.assertEqual(self._status(ret.name), "Reported")

	def test_only_failed_can_be_retried(self):
		ret, _dn, _rma_id = self._submitted()

		with self.assertRaisesRegex(frappe.ValidationError, "Only a submitted bol return that Failed"):
			returns.retry_return_report(ret.name)

	def test_needs_write_permission_on_the_delivery_note(self):
		ret = self._failed()
		make_bol_manager()
		frappe.set_user(BOL_MANAGER)

		with self.assertRaises(frappe.PermissionError):
			returns.retry_return_report(ret.name)

	def test_needs_a_bol_role(self):
		ret = self._failed()
		frappe.set_user("Guest")

		with self.assertRaises(frappe.PermissionError):
			returns.retry_return_report(ret.name)


class TestWithoutStockReturn(ReturnTestCase):
	def test_reports_the_expected_quantity_and_deletes_the_draft(self):
		ret, dn, rma_id = self._draft(quantity=2)

		result = returns.report_without_return(ret.name, "CUSTOMER_KEEPS_PRODUCT_PAID")

		self.assertEqual(result, {"return_against": dn.name})
		# bol's spec: quantityReturned is at least 1 (ADR-014).
		self.assertEqual(
			self.bol.puts(),
			[(f"{returns.PATH}/{rma_id}", {"handlingResult": "CUSTOMER_KEEPS_PRODUCT_PAID", "quantityReturned": 2})],
		)
		self.assertFalse(frappe.db.exists(DN, ret.name))
		self.assertEqual(self._todos(rma_id), [])
		(process,) = frappe.get_all(
			process_status.DOCTYPE,
			filters={"handler": returns.HANDLER},
			fields=["reference_doctype", "reference_name", "payload"],
		)
		self.assertEqual((process.reference_doctype, process.reference_name), (DN, dn.name))
		self.assertTrue(json.loads(process.payload)["without_return"])
		self.assertIn("without a stock return", self._comments(dn.name)[-1])
		self.assertTrue(any(ret.name in log["message"] for log in self._logs()))

		# bol still lists it while it processes the report: no new draft.
		returns.poll_returns()
		self.assertEqual(self._returns(rma_id), [])

		self._finish()
		self.assertIn(f"bol processed return {rma_id}: CUSTOMER_KEEPS_PRODUCT_PAID", self._comments(dn.name)[-1])
		returns.poll_returns()
		self.assertEqual(self._returns(rma_id), [])

	def test_failure_brings_the_draft_back(self):
		ret, dn, rma_id = self._draft()
		returns.report_without_return(ret.name, "CUSTOMER_KEEPS_PRODUCT_PAID")

		self._finish("FAILURE", "Handling result not allowed")

		self.assertIn("comes back as a draft return", self._comments(dn.name)[-1])
		returns.poll_returns()
		self.assertEqual(len(self._returns(rma_id)), 1)

	def test_bol_error_keeps_the_draft(self):
		ret, _dn, rma_id = self._draft()
		self.bol.errors[("PUT", f"{returns.PATH}/{rma_id}")] = BolServerError("bol answered 500")

		with self.assertRaisesRegex(frappe.ValidationError, "bol answered 500"):
			returns.report_without_return(ret.name, "CUSTOMER_KEEPS_PRODUCT_PAID")

		self.assertTrue(frappe.db.exists(DN, ret.name))
		self.assertEqual(len(self._error_logs()), 1)

	def test_already_handled_says_so_and_keeps_the_draft(self):
		ret, _dn, rma_id = self._draft()
		self.bol.item(rma_id)["handled"] = True

		with self.assertRaisesRegex(frappe.ValidationError, "has handled return .* already"):
			returns.report_without_return(ret.name, "CUSTOMER_KEEPS_PRODUCT_PAID")

		self.assertTrue(frappe.db.exists(DN, ret.name))

	def test_unknown_handling_result_is_refused(self):
		ret, _dn, _rma_id = self._draft()

		with self.assertRaisesRegex(frappe.ValidationError, "Unknown bol handling result"):
			returns.report_without_return(ret.name, "KEPT_IT")
		self.assertEqual(self.bol.puts(), [])

	def test_submitted_return_is_refused(self):
		ret, _dn, _rma_id = self._submitted()

		with self.assertRaisesRegex(frappe.ValidationError, "Only a draft bol return"):
			returns.report_without_return(ret.name, "CUSTOMER_KEEPS_PRODUCT_PAID")

	def test_needs_permission_on_the_delivery_note(self):
		ret, _dn, _rma_id = self._draft()
		make_bol_manager()
		frappe.set_user(BOL_MANAGER)

		with self.assertRaises(frappe.PermissionError):
			returns.report_without_return(ret.name, "CUSTOMER_KEEPS_PRODUCT_PAID")
		frappe.set_user("Administrator")
		self.assertTrue(frappe.db.exists(DN, ret.name))

	def test_switched_off_is_refused(self):
		ret, _dn, _rma_id = self._draft()
		self.settings.handle_returns = 0

		with self.assertRaisesRegex(frappe.ValidationError, "Handle Returns"):
			returns.report_without_return(ret.name, "CUSTOMER_KEEPS_PRODUCT_PAID")

	def test_form_offers_the_button_on_drafts(self):
		ret, _dn, _rma_id = self._draft()

		ret.run_method("onload")

		self.assertTrue(ret.get_onload().get("bol_returns_on"))
