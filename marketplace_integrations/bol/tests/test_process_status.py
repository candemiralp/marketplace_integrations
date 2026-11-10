import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from marketplace_integrations.bol import process_status
from marketplace_integrations.bol.api.errors import BolError, BolTransientError
from marketplace_integrations.bol.process_status import DOCTYPE
from marketplace_integrations.bol.tests.utils import discard_pending_logs

FIXTURES = Path(__file__).parent / "fixtures"
BULK = json.loads((FIXTURES / "process_status_bulk.json").read_text())
PAYLOAD = {"order_item_ids": ["2012345678"]}


def bol_status(process_id: str, status: str, **fields) -> dict:
	"""A ProcessStatus shaped like bol's demo examples."""
	example = next(s for s in BULK["processStatuses"] if s["status"] == status)
	return {**example, "processStatusId": process_id, **fields}


class TestBackoff(FrappeTestCase):
	def test_schedule(self):
		self.assertEqual(
			[process_status.backoff(attempts) for attempts in range(8)],
			[10, 30, 60, 120, 300, 600, 600, 600],
		)

	def test_timeout_after_24_hours(self):
		now = get_datetime("2026-11-10 12:00:00")

		self.assertFalse(process_status.timed_out(now - timedelta(hours=23, minutes=59), now))
		self.assertTrue(process_status.timed_out(now - timedelta(hours=24, minutes=1), now))


class ProcessTestCase(FrappeTestCase):
	"""Bol Process flows with BolClient mocked. Everything stays in the test transaction."""

	def setUp(self):
		self.on_success = MagicMock(name="on_success")
		self.on_failure = MagicMock(name="on_failure")
		self.client = MagicMock(name="BolClient")
		self.client.request.return_value = {"processStatuses": []}
		self._start(patch.dict(process_status.HANDLERS, {"shipment": (self.on_success, self.on_failure)}))
		self._start(patch.object(process_status, "get_client", return_value=self.client))
		# poll commits per row; keep the work in this transaction so the rollback below undoes it.
		self._start(patch.object(frappe.db, "commit"))
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		for key in ("fetch", "ps-1", "ps-2"):
			self.addCleanup(frappe.cache.delete_value, f"bol:failed:process_status:{key}")

		frappe.db.set_single_value("Bol Settings", "enabled", 1)
		self.reference = frappe.get_doc({"doctype": "ToDo", "description": "bol process test"}).insert()

	def _start(self, patcher):
		self.addCleanup(patcher.stop)
		return patcher.start()

	def _track(self, process_id="ps-1", due=True) -> str:
		name = process_status.track({"processStatusId": process_id}, "shipment", self.reference, PAYLOAD)
		if due:
			_make_due(name)
		return name

	def _bol_returns(self, *statuses):
		self.client.request.return_value = {"processStatuses": list(statuses)}

	def _comments(self, doctype="ToDo", name=None):
		return frappe.get_all(
			"Comment",
			filters={"reference_doctype": doctype, "reference_name": name or self.reference.name},
			pluck="content",
		)


class TestTrack(ProcessTestCase):
	def test_creates_a_pending_row(self):
		before = now_datetime()

		name = self._track("019bbcbc-1a21-77c9-9de6-0726d1379c91fff", due=False)

		doc = frappe.get_doc(DOCTYPE, name)
		self.assertEqual(name, "019bbcbc-1a21-77c9-9de6-0726d1379c91fff")
		self.assertEqual(doc.process_status_id, name)
		self.assertEqual((doc.status, doc.attempts, doc.handler), ("PENDING", 0, "shipment"))
		self.assertEqual((doc.reference_doctype, doc.reference_name), ("ToDo", self.reference.name))
		self.assertEqual(frappe.parse_json(doc.payload), PAYLOAD)
		delay = (get_datetime(doc.next_check_at) - before).total_seconds()
		self.assertTrue(9 <= delay <= 11, delay)

	def test_duplicate_track_gives_one_row(self):
		first = self._track()
		second = self._track()

		self.assertEqual(first, second)
		self.assertEqual(frappe.db.count(DOCTYPE, {"process_status_id": "ps-1"}), 1)

	def test_concurrent_duplicate_insert_counts_as_tracked(self):
		with (
			patch.object(process_status.frappe.db, "exists", return_value=False),
			patch("frappe.model.document.Document.insert", side_effect=frappe.DuplicateEntryError),
		):
			self.assertEqual(self._track(due=False), "ps-1")

	def test_unknown_handler_is_rejected(self):
		with self.assertRaisesRegex(ValueError, "Unknown bol process handler: nope"):
			process_status.track({"processStatusId": "ps-x"}, "nope", self.reference)
		self.assertFalse(frappe.db.exists(DOCTYPE, "ps-x"))

	def test_response_without_process_status_id_is_rejected(self):
		with self.assertRaisesRegex(BolError, "no processStatusId"):
			process_status.track({"status": "PENDING"}, "shipment", self.reference)

	def test_register(self):
		on_success, on_failure = MagicMock(), MagicMock()

		process_status.register("content", on_success, on_failure)

		self.assertEqual(process_status.HANDLERS["content"], (on_success, on_failure))


class TestPoll(ProcessTestCase):
	def test_bulk_request(self):
		self._track("ps-1")
		self._track("ps-2")
		self._track("ps-later", due=False)

		process_status.poll_pending()

		self.client.request.assert_called_once_with(
			"POST",
			"/shared/process-status",
			version=10,
			json={"processStatusQueries": [{"processStatusId": "ps-1"}, {"processStatusId": "ps-2"}]},
			safe=True,
		)

	def test_pending_then_success_runs_on_success_once(self):
		name = self._track()
		self._bol_returns(bol_status(name, "PENDING"))

		before = now_datetime()
		process_status.poll_pending()

		doc = frappe.get_doc(DOCTYPE, name)
		self.assertEqual((doc.status, doc.attempts), ("PENDING", 1))
		delay = (get_datetime(doc.next_check_at) - before).total_seconds()
		self.assertTrue(29 <= delay <= 31, delay)
		self.on_success.assert_not_called()

		_make_due(name)
		self._bol_returns(bol_status(name, "SUCCESS", entityId="987654321"))
		process_status.poll_pending()
		_make_due(name)
		process_status.poll_pending()

		doc = frappe.get_doc(DOCTYPE, name)
		self.assertEqual((doc.status, doc.bol_status, doc.entity_id), ("SUCCESS", "SUCCESS", "987654321"))
		self.on_success.assert_called_once()
		handler_doc, entity_id, payload = self.on_success.call_args.args
		self.assertEqual((handler_doc.name, entity_id, payload), (name, "987654321", PAYLOAD))
		self.on_failure.assert_not_called()

	def test_failure_runs_on_failure_logs_and_comments(self):
		name = self._track()
		self._bol_returns(bol_status(name, "FAILURE", errorMessage="Order item already shipped."))

		process_status.poll_pending()

		doc = frappe.get_doc(DOCTYPE, name)
		self.assertEqual((doc.status, doc.error_message), ("FAILURE", "Order item already shipped."))
		self.on_failure.assert_called_once()
		_, message, payload = self.on_failure.call_args.args
		self.assertEqual((message, payload), ("Order item already shipped.", PAYLOAD))
		self.assertEqual(self._comments(), ["bol shipment failed: Order item already shipped."])
		[entry] = frappe.local.bol_pending_logs
		self.assertEqual(entry["status"], "Error")
		self.assertIn("Order item already shipped.", entry["message"])
		self.assertEqual(entry["bol_reference_name"], self.reference.name)

	def test_bol_timeout_runs_on_failure(self):
		name = self._track()
		self._bol_returns(bol_status(name, "TIMEOUT", errorMessage=None))

		process_status.poll_pending()

		self.assertEqual(frappe.db.get_value(DOCTYPE, name, "status"), "TIMEOUT")
		self.assertEqual(self.on_failure.call_args.args[1], "bol reported TIMEOUT")

	def test_pending_for_more_than_24_hours_times_out(self):
		name = self._track()
		frappe.db.set_value(DOCTYPE, name, "creation", add_to_date(now_datetime(), hours=-25))
		self._bol_returns(bol_status(name, "PENDING"))

		process_status.poll_pending()

		doc = frappe.get_doc(DOCTYPE, name)
		self.assertEqual((doc.status, doc.bol_status), ("TIMEOUT", "TIMEOUT"))
		self.assertIn("24 hours", self.on_failure.call_args.args[1])
		self.assertEqual(len(self._comments()), 1)

	def test_handler_error_rolls_back_the_handler_and_other_rows_continue(self):
		failing, other = self._track("ps-1"), self._track("ps-2")

		def broken_handler(doc, entity_id, payload):
			if doc.name == failing:
				frappe.get_doc({"doctype": "ToDo", "description": "half-done handler work"}).insert()
				raise RuntimeError("handler bug")

		self.on_success.side_effect = broken_handler
		self._bol_returns(bol_status(failing, "SUCCESS"), bol_status(other, "SUCCESS"))

		process_status.poll_pending()

		failed = frappe.get_doc(DOCTYPE, failing)
		self.assertEqual((failed.status, failed.bol_status), ("HANDLER_ERROR", "SUCCESS"))
		self.assertEqual(frappe.db.get_value(DOCTYPE, other, "status"), "SUCCESS")
		self.assertFalse(frappe.db.exists("ToDo", {"description": "half-done handler work"}))
		self.assertEqual(self._comments(), ["bol shipment handler failed: handler bug"])
		self.assertEqual(self._comments(DOCTYPE, failing), ["bol shipment handler failed: handler bug"])
		self.assertIn("RuntimeError: handler bug", frappe.local.bol_pending_logs[0]["traceback"])

		# bol's state is final: a HANDLER_ERROR row is never polled again.
		_make_due(failing)
		self.client.request.reset_mock()
		process_status.poll_pending()
		self.client.request.assert_not_called()

	def test_unregistered_handler_is_a_handler_error(self):
		name = self._track()
		self._bol_returns(bol_status(name, "SUCCESS"))

		with patch.dict(process_status.HANDLERS, clear=True):
			process_status.poll_pending()

		self.assertEqual(frappe.db.get_value(DOCTYPE, name, "status"), "HANDLER_ERROR")

	def test_transient_fetch_error_keeps_rows_pending_with_backoff(self):
		name = self._track()
		self.client.request.side_effect = BolTransientError("bol unavailable (HTTP 503)")

		before = now_datetime()
		process_status.poll_pending()

		doc = frappe.get_doc(DOCTYPE, name)
		self.assertEqual((doc.status, doc.attempts), ("PENDING", 1))
		self.assertGreaterEqual((get_datetime(doc.next_check_at) - before).total_seconds(), 29)
		self.assertEqual(len(frappe.local.bol_pending_logs), 1)
		self.on_success.assert_not_called()
		self.on_failure.assert_not_called()

	def test_repeated_fetch_errors_are_logged_once(self):
		self.client.request.side_effect = BolTransientError("bol unavailable (HTTP 503)")
		for process_id in ("ps-1", "ps-2"):
			self._track(process_id)
			process_status.poll_pending()

		self.assertEqual(len(frappe.local.bol_pending_logs), 1)

	def test_id_missing_from_bulk_response_stays_pending(self):
		name = self._track()

		process_status.poll_pending()

		self.assertEqual(frappe.db.get_value(DOCTYPE, name, ["status", "attempts"]), ("PENDING", 1))

	def test_row_error_is_rolled_back_and_does_not_stop_other_rows(self):
		first, second = self._track("ps-1"), self._track("ps-2")

		with patch.object(process_status, "timed_out", side_effect=[RuntimeError("bug"), False]):
			process_status.poll_pending()

		self.assertEqual(frappe.db.get_value(DOCTYPE, first, "attempts"), 0)
		self.assertEqual(frappe.db.get_value(DOCTYPE, second, "attempts"), 1)
		[entry] = frappe.local.bol_pending_logs
		self.assertEqual((entry["bol_reference_doctype"], entry["bol_reference_name"]), (DOCTYPE, first))

	def test_nothing_due_makes_no_call(self):
		self._track(due=False)

		process_status.poll_pending()

		self.client.request.assert_not_called()

	def test_disabled_settings_make_no_call(self):
		self._track()
		frappe.db.set_single_value("Bol Settings", "enabled", 0)

		process_status.poll_pending()

		self.client.request.assert_not_called()

	def test_check_one_ignores_next_check_at(self):
		name = self._track(due=False)
		self._bol_returns(bol_status(name, "SUCCESS"))

		process_status.check_one(name)

		self.assertEqual(frappe.db.get_value(DOCTYPE, name, "status"), "SUCCESS")


class TestRetryHandler(ProcessTestCase):
	def _handler_error(self) -> "frappe.Document":
		name = self._track()
		self.on_success.side_effect = RuntimeError("handler bug")
		self._bol_returns(bol_status(name, "SUCCESS", entityId="555552"))
		process_status.poll_pending()
		self.on_success.reset_mock(side_effect=True)
		return frappe.get_doc(DOCTYPE, name)

	def test_retry_after_fix_completes(self):
		doc = self._handler_error()

		doc.retry_handler()

		self.assertEqual(frappe.db.get_value(DOCTYPE, doc.name, "status"), "SUCCESS")
		self.assertEqual(self.on_success.call_args.args[1:], ("555552", PAYLOAD))

	def test_retry_that_fails_again_raises_a_clear_message(self):
		doc = self._handler_error()
		self.on_success.side_effect = RuntimeError("still broken")

		with self.assertRaisesRegex(frappe.ValidationError, "The handler failed again: still broken"):
			doc.retry_handler()
		self.assertEqual(doc.status, "HANDLER_ERROR")

	def test_retry_on_failure_path_calls_on_failure(self):
		name = self._track()
		self.on_failure.side_effect = RuntimeError("handler bug")
		self._bol_returns(bol_status(name, "FAILURE", errorMessage="Invalid transporter."))
		process_status.poll_pending()
		self.on_failure.reset_mock(side_effect=True)

		frappe.get_doc(DOCTYPE, name).retry_handler()

		self.assertEqual(frappe.db.get_value(DOCTYPE, name, "status"), "FAILURE")
		self.assertEqual(self.on_failure.call_args.args[1], "Invalid transporter.")

	def test_only_handler_errors_can_be_retried(self):
		name = self._track()

		with self.assertRaisesRegex(frappe.ValidationError, "whose handler failed"):
			frappe.get_doc(DOCTYPE, name).retry_handler()
		self.on_success.assert_not_called()

	def test_requires_bol_manager_or_system_manager(self):
		doc = self._handler_error()

		with (
			patch.object(frappe, "get_roles", return_value=["Sales User"]),
			self.assertRaises(frappe.PermissionError),
		):
			doc.retry_handler()
		self.on_success.assert_not_called()


class TestHousekeeping(ProcessTestCase):
	def test_deletes_final_rows_older_than_90_days_with_their_comments(self):
		rows = {}
		for process_id, status, age_days in [
			("old-success", "SUCCESS", 91),
			("old-failure", "FAILURE", 91),
			("old-timeout", "TIMEOUT", 91),
			("old-handler-error", "HANDLER_ERROR", 91),
			("old-pending", "PENDING", 91),
			("recent-success", "SUCCESS", 10),
		]:
			rows[process_id] = self._track(process_id, due=False)
			modified = add_to_date(now_datetime(), days=-age_days)
			frappe.db.set_value(
				DOCTYPE, process_id, {"status": status, "modified": modified}, update_modified=False
			)
		frappe.get_doc(DOCTYPE, "old-success").add_comment("Comment", "old note")

		process_status.delete_old_processes()

		remaining = set(frappe.get_all(DOCTYPE, filters={"name": ("in", list(rows))}, pluck="name"))
		self.assertEqual(remaining, {"old-handler-error", "old-pending", "recent-success"})
		self.assertEqual(self._comments(DOCTYPE, "old-success"), [])


def _make_due(name: str):
	past = add_to_date(now_datetime(), seconds=-1)
	frappe.db.set_value(DOCTYPE, name, "next_check_at", past, update_modified=False)
