import base64
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from marketplace_integrations.bol import log
from marketplace_integrations.bol.api import auth
from marketplace_integrations.bol.api.client import BolClient
from marketplace_integrations.bol.api.errors import BolError, BolValidationError
from marketplace_integrations.bol.tests.utils import (
	SECRET,
	TOKEN,
	FakeSession,
	make_response,
	make_settings,
)

LOG_FIELDS = ["name", "status", "method", "message", "request_data", "response_data", "traceback"]


class TestRedact(FrappeTestCase):
	def test_masks_secret_keys_at_any_depth_case_insensitively(self):
		data = {
			"headers": {"Authorization": "Bearer x", "Accept": "application/json"},
			"items": [{"ACCESS_TOKEN": "y", "ok": 1}, ["z", {"client_secret": "s"}]],
			"keep": "value",
		}

		self.assertEqual(
			log.redact(data),
			{
				"headers": {"Authorization": log.MASK, "Accept": "application/json"},
				"items": [{"ACCESS_TOKEN": log.MASK, "ok": 1}, ["z", {"client_secret": log.MASK}]],
				"keep": "value",
			},
		)
		self.assertEqual(data["headers"]["Authorization"], "Bearer x", "input must not be modified")


class TestLogCall(FrappeTestCase):
	def setUp(self):
		self.addCleanup(_discard_pending_logs)

	def test_policy(self):
		cases = [
			({"method": "GET", "error": None}, 0, False),
			({"method": "GET", "error": None}, 1, True),
			({"method": "POST", "error": None}, 0, True),
			({"method": "GET", "error": BolError("boom")}, 0, True),
		]
		for kwargs, debug, expected in cases:
			with (
				self.subTest(**kwargs, debug=debug),
				patch.object(frappe.db, "get_single_value", return_value=debug),
				patch.object(log, "_write_after_transaction") as write,
			):
				log.log_call(path="/retailer/orders", status=200, **kwargs)
				self.assertEqual(write.called, expected)

	def test_entry_fields(self):
		error = BolValidationError("Invalid", "Bad price")
		with patch.object(log, "_write_after_transaction") as write:
			log.log_call(
				"PUT",
				"/retailer/offers/1/price",
				400,
				request={"json": {"price": 1}},
				response={"body": "x"},
				error=error,
				reference=("Item", "SKU-1"),
			)

		entry = write.call_args.args[0]
		self.assertEqual(entry["integration"], "Bol")
		self.assertEqual(entry["status"], "Error")
		self.assertEqual(entry["method"], "PUT /retailer/offers/1/price")
		self.assertEqual(entry["message"], "BolValidationError: Invalid: Bad price")
		self.assertIn("BolValidationError", entry["traceback"])
		self.assertEqual((entry["bol_reference_doctype"], entry["bol_reference_name"]), ("Item", "SKU-1"))

	def test_log_is_written_when_the_transaction_rolls_back(self):
		self.addCleanup(_delete_logs, "POST /retailer/rollback-test")

		log.log_call("POST", "/retailer/rollback-test", 500, error=BolError("boom"), reference=("ToDo", "x"))
		self.assertFalse(_logs("POST /retailer/rollback-test"), "must not be written mid-transaction")

		frappe.db.rollback()

		logs = _logs("POST /retailer/rollback-test", fields=["status", "bol_reference_doctype"])
		self.assertEqual(logs, [{"status": "Error", "bol_reference_doctype": "ToDo"}])

	def test_log_is_written_on_commit(self):
		self.addCleanup(_delete_logs, "POST /retailer/commit-test")

		log.log_call("POST", "/retailer/commit-test", 202)
		# The commit is the behaviour under test.
		frappe.db.commit()  # nosemgrep

		self.assertEqual(len(_logs("POST /retailer/commit-test")), 1)

	def test_logging_keeps_the_callers_savepoint(self):
		frappe.db.savepoint("bol_record")
		todo = frappe.get_doc({"doctype": "ToDo", "description": "half-processed record"}).insert()

		log.log_call("POST", "/retailer/savepoint-test", 500, error=BolError("boom"))
		frappe.db.rollback(save_point="bol_record")

		self.assertFalse(frappe.db.exists("ToDo", todo.name))
		self.assertEqual(len(frappe.local.bol_pending_logs), 1)

	def test_log_once(self):
		marker = "bol:failed:offer:SKU-ONCE"
		frappe.cache.delete_value(marker)
		self.addCleanup(frappe.cache.delete_value, marker)

		with patch.object(log, "log_call") as log_call:
			self.assertTrue(log.log_once("offer", "SKU-ONCE", method="POST", error=BolError("boom")))
			self.assertFalse(log.log_once("offer", "SKU-ONCE", method="POST", error=BolError("boom")))

		log_call.assert_called_once()
		ttl = frappe.cache.ttl(frappe.cache.make_key(marker))
		self.assertTrue(6 * 3600 - 10 <= ttl <= 6 * 3600, ttl)


class TestNoSecretsInLogs(FrappeTestCase):
	def test_full_request_and_response_logs_contain_no_secret_or_token(self):
		settings = make_settings(client_id="no-secrets-client")
		auth.invalidate_token(settings)
		self.addCleanup(auth.invalidate_token, settings)
		methods = ["GET /retailer/orders", "POST /retailer/offers", "PUT /retailer/offers/1"]
		_delete_logs(methods)
		self.addCleanup(_delete_logs, methods)
		session = FakeSession(
			make_response(401),  # forces a second token request
			make_response(200, {"orders": []}),
			make_response(202, {"processStatusId": "1", "access_token": TOKEN}),
			make_response(400, {"title": "Invalid", "detail": "Bad", "client_secret": SECRET}),
		)
		bol = BolClient(settings, session)

		with patch.object(frappe.db, "get_single_value", return_value=1):  # debug logging on
			bol.request("GET", "/retailer/orders", version=10)
			bol.request("POST", "/retailer/offers", version=11, json={"client_secret": SECRET})
			try:
				bol.request("PUT", "/retailer/offers/1", version=11, json={})
			except BolValidationError as e:
				log.log_call(**e.details, error=e)
		frappe.db.rollback()

		logs = _logs(methods, fields=LOG_FIELDS)
		self.assertEqual(len(logs), 3)
		basic = base64.b64encode(f"no-secrets-client:{SECRET}".encode()).decode()
		for entry in logs:
			for field, value in entry.items():
				for secret in (SECRET, TOKEN, basic):
					self.assertNotIn(secret, value or "", f"{field} of {entry['method']}")


def _logs(method: str | list[str], fields=None):
	methods = [method] if isinstance(method, str) else method
	filters = {"integration": "Bol", "method": ["in", methods]}
	return frappe.get_all("Ecommerce Integration Log", filters=filters, fields=fields or ["name"])


def _delete_logs(method):
	# The logs were committed by the flush, so the test rollback can't remove them.
	for entry in _logs(method):
		frappe.delete_doc("Ecommerce Integration Log", entry.name, force=True, ignore_permissions=True)
	frappe.db.commit()  # nosemgrep


def _discard_pending_logs():
	frappe.local.bol_pending_logs = None
