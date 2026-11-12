# Monitoring (story 15): the workspace counts, stuck orders, the daily digest and the health timestamps.
# Everything stays in the test transaction.

import copy
import itertools
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from marketplace_integrations.bol import labels, monitoring, orders, shipments
from marketplace_integrations.bol.api.errors import BolTransientError
from marketplace_integrations.bol.log import LOG_DOCTYPE
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
SHIRT, BAG = "_Test bol monitoring shirt", "_Test bol monitoring bag"
SHIRT_EAN, BAG_EAN = "4006381333931", "8712345678906"
TZ = "Europe/Amsterdam"
DN = "Delivery Note"
PLAIN_USER = "no.bol.role.test@example.com"
# The buyer in order.json: none of it may reach the digest.
LONG_AGO = "2020-01-01 00:00:00"
BUYER_DATA = ("Billie", "Jansen", "Dorpstraat", "1111ZZ", "billie@verkopen.bol.com", "012123456")


class MonitoringTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.masters = make_order_masters()

	def setUp(self):
		self.settings = frappe._dict(enabled=1, notify_user=None)
		self._start(patch.object(monitoring, "_settings", return_value=self.settings))
		off = frappe._dict(enabled=0)
		self._start(patch.object(shipments, "_settings", return_value=off))
		self._start(patch.object(labels, "_settings", return_value=off))
		self.sendmail = self._start(patch.object(frappe, "sendmail"))
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.cache.delete_keys("bol:failed:digest")
		self.addCleanup(frappe.cache.delete_keys, "bol:failed:digest")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		make_catalog_masters()
		make_item(SHIRT, SHIRT_EAN, valuation_rate=10)
		make_item(BAG, BAG_EAN, valuation_rate=5)
		self.ids = itertools.count(1)

	def _start(self, patcher):
		self.addCleanup(patcher.stop)
		return patcher.start()

	def _delivery_note(self, submit: bool = True):
		"""A Delivery Note of a newly imported bol order (buyer Billie Jansen)."""
		order = copy.deepcopy(ORDER)
		order["orderId"] = f"M{next(self.ids)}"
		settings = frappe._dict(import_orders_placed_after=None, submit_orders=1, **self.masters)
		with patch.object(orders, "get_system_timezone", return_value=TZ):
			so_name = orders.create_sales_order(order, settings)
		dn = make_delivery_note(so_name)
		dn.insert()
		if submit:
			dn.submit()
		return dn

	def _log(self, status: str = "Error", method: str = "GET /retailer/offers", hours_ago: float = 0) -> str:
		log = frappe.get_doc(
			{"doctype": LOG_DOCTYPE, "integration": "Bol", "status": status, "method": method, "message": "x"}
		).insert(ignore_permissions=True)
		if hours_ago:
			self._backdate(LOG_DOCTYPE, log.name, hours=hours_ago)
		return log.name

	def _process(self, status: str, hours_ago: float = 0) -> str:
		name = f"ps-monitoring-{next(self.ids)}"
		frappe.get_doc(
			{
				"doctype": monitoring.PROCESS,
				"process_status_id": name,
				"handler": "shipment",
				"status": status,
				"reference_doctype": "Bol Settings",
				"reference_name": "Bol Settings",
			}
		).insert(ignore_permissions=True)
		if hours_ago:
			self._backdate(monitoring.PROCESS, name, hours=hours_ago)
		return name

	def _bol_item(self, item_code: str, status: str | None, detail: str | None = None, **values):
		make_item(item_code)
		frappe.db.set_value(
			"Item",
			item_code,
			{"bol_enabled": 1, "bol_offer_status": status, "bol_status_detail": detail, **values},
		)

	def _backdate(self, doctype: str, name: str, **delta):
		frappe.db.set_value(
			doctype,
			name,
			"creation",
			add_to_date(now_datetime(), **{k: -v for k, v in delta.items()}),
			update_modified=False,
		)


class TestCounts(MonitoringTestCase):
	def test_log_errors_count_bol_errors_of_the_last_24_hours(self):
		before = monitoring.count_log_errors()
		self._log()
		self._log(status="Success")
		self._log(hours_ago=25)
		self.assertEqual(monitoring.count_log_errors(), before + 1)

	def test_stuck_processes_are_old_pending_or_handler_errors(self):
		before = monitoring.count_stuck_processes()
		self._process("PENDING", hours_ago=2)
		self._process("PENDING")
		self._process("HANDLER_ERROR")
		self._process("SUCCESS", hours_ago=5)
		self._process("FAILURE", hours_ago=5)
		self.assertEqual(monitoring.count_stuck_processes(), before + 2)

	def test_failed_shipments_are_failed_or_unknown_submitted_delivery_notes(self):
		before = monitoring.count_failed_shipments()
		failed, unknown, confirmed = (self._delivery_note() for _ in range(3))
		failed.db_set("bol_shipment_status", "Failed")
		unknown.db_set("bol_shipment_status", "Unknown")
		confirmed.db_set("bol_shipment_status", "Confirmed")
		draft = self._delivery_note(submit=False)
		draft.db_set("bol_shipment_status", "Failed")
		self.assertEqual(monitoring.count_failed_shipments(), before + 2)

	def test_offers_not_for_sale_include_items_without_a_status(self):
		before = monitoring.count_offers_not_for_sale()
		self._bol_item("_Test bol nfs for sale", "For sale")
		self._bol_item("_Test bol nfs not for sale", "Not for sale")
		self._bol_item("_Test bol nfs on hold", "On hold")
		self._bol_item("_Test bol nfs no offer", None)
		self._bol_item("_Test bol nfs disabled", "Not for sale", disabled=1)
		make_item("_Test bol nfs not on bol")
		self.assertEqual(monitoring.count_offers_not_for_sale(), before + 3)

	def test_open_returns_are_bol_returns_not_reported(self):
		before = monitoring.count_open_returns()
		rows = [self._delivery_note(submit=False) for _ in range(4)]
		for dn, (rma, status) in zip(
			rows, (("R1", "Open"), ("R2", "Failed"), ("R3", "Reported"), (None, "Open")), strict=True
		):
			dn.db_set({"bol_rma_id": rma, "bol_return_status": status})
		self.assertEqual(monitoring.count_open_returns(), before + 2)


class TestNumberCards(MonitoringTestCase):
	def test_card_returns_the_count_and_the_filtered_list(self):
		make_bol_manager()
		frappe.set_user(BOL_MANAGER)
		card = monitoring.failed_shipments_card()
		self.assertEqual(card["value"], monitoring.count_failed_shipments())
		self.assertEqual(card["fieldtype"], "Int")
		self.assertEqual(card["route"], ["List", DN])
		self.assertEqual(card["route_options"]["bol_shipment_status"], ["in", ["Failed", "Unknown"]])
		# Datetimes go to the list view as text.
		frappe.as_json(monitoring.log_errors_card())

	def test_card_refuses_a_user_without_a_bol_role(self):
		if not frappe.db.exists("User", PLAIN_USER):
			frappe.get_doc({"doctype": "User", "email": PLAIN_USER, "first_name": "plain"}).insert()
		frappe.set_user(PLAIN_USER)
		for card in (
			monitoring.log_errors_card,
			monitoring.stuck_processes_card,
			monitoring.failed_shipments_card,
			monitoring.offers_not_for_sale_card,
			monitoring.open_returns_card,
		):
			with self.assertRaises(frappe.PermissionError):
				card()


class TestStuckOrders(MonitoringTestCase):
	def _failing(self, order_id: str, first: float, latest: float, method: str = "GET"):
		self._log(method=f"{method} /retailer/orders/{order_id}", hours_ago=first)
		return self._log(method=f"{method} /retailer/orders/{order_id}", hours_ago=latest)

	def _stuck(self) -> list[str]:
		return [order.order_id for order in monitoring.stuck_orders()]

	def test_failing_for_more_than_a_day_and_still_failing(self):
		latest = self._failing("S-OLD", first=30, latest=2)
		self._failing("S-MAPPING", first=40, latest=4, method="import_order")
		(stuck,) = (o for o in monitoring.stuck_orders() if o.order_id == "S-OLD")
		self.assertEqual(stuck.log, latest)
		self.assertIn("S-MAPPING", self._stuck())

	def test_not_stuck_when_recent_stopped_imported_or_not_an_import(self):
		self._failing("S-NEW", first=10, latest=1)
		# Not logged again for 10 hours: bol no longer lists it (cancelled, shipped elsewhere).
		self._failing("S-STOPPED", first=40, latest=10)
		self._failing("cancellation", first=40, latest=1, method="PUT")
		self._failing("S-SUCCESS", first=40, latest=1)
		self._log(status="Success", method="GET /retailer/orders/S-SUCCESS")
		dn = self._delivery_note()
		self._failing(dn.bol_order_id, first=40, latest=1)
		stuck = self._stuck()
		for order_id in ("S-NEW", "S-STOPPED", "cancellation", dn.bol_order_id):
			self.assertNotIn(order_id, stuck)
		# A Success log doesn't clear an order: only a Sales Order does.
		self.assertIn("S-SUCCESS", stuck)


class TestDigest(MonitoringTestCase):
	def test_nothing_to_report_sends_nothing(self):
		with self._nothing_counted():
			self.assertIsNone(monitoring.build_digest())
			monitoring.send_daily_digest()
		self.sendmail.assert_not_called()

	def test_disabled_integration_sends_nothing(self):
		self.settings.enabled = 0
		self._log()
		monitoring.send_daily_digest()
		self.sendmail.assert_not_called()

	def test_digest_lists_problems_with_links_and_no_buyer_data(self):
		make_bol_manager()
		dn = self._delivery_note()
		dn.db_set("bol_shipment_status", "Failed")
		self._bol_item("_Test bol digest item", "Not for sale", "Ontbrekende content")
		self._log(method="GET /retailer/orders/STUCK-1", hours_ago=30)
		# The error message of an order can name the buyer: it must stay in the log.
		frappe.db.set_value(
			LOG_DOCTYPE,
			self._log(method="import_order /retailer/orders/STUCK-1", hours_ago=1),
			"message",
			"Billie Jansen",
		)

		monitoring.send_daily_digest()

		self.sendmail.assert_called_once()
		kwargs = self.sendmail.call_args.kwargs
		self.assertIn(BOL_MANAGER, kwargs["recipients"])
		text = kwargs["subject"] + kwargs["message"]
		for needle in (
			dn.name,
			dn.bol_order_id,
			"STUCK-1",
			"_Test bol digest item",
			"Ontbrekende content",
			"/app/",
		):
			self.assertIn(needle, text)
		for personal in BUYER_DATA:
			self.assertNotIn(personal, text)

	def test_digest_lists_are_capped(self):
		for n in range(monitoring.LIST_LIMIT + 1):
			self._bol_item(f"_Test bol digest cap {n:02}", "Not for sale")
		_subject, html = monitoring.build_digest()
		self.assertIn("_Test bol digest cap 19", html)
		self.assertNotIn("_Test bol digest cap 20", html)
		self.assertIn(">More</a>", html)

	def test_recipients_fall_back_to_notify_user(self):
		frappe.db.delete("Has Role", {"role": monitoring.BOL_MANAGER, "parenttype": "User"})
		self.settings.notify_user = "Administrator"
		self.assertEqual(monitoring.digest_recipients(self.settings), ["Administrator"])

	def test_no_recipient_is_logged_once(self):
		frappe.db.delete("Has Role", {"role": monitoring.BOL_MANAGER, "parenttype": "User"})
		self._log()
		monitoring.send_daily_digest()
		monitoring.send_daily_digest()
		self.sendmail.assert_not_called()
		pending = [log for log in frappe.local.bol_pending_logs or [] if "digest not sent" in log["message"]]
		self.assertEqual(len(pending), 1)

	def _nothing_counted(self) -> ExitStack:
		stack = ExitStack()
		for name in (
			"count_log_errors",
			"count_stuck_processes",
			"count_failed_shipments",
			"count_offers_not_for_sale",
			"count_open_returns",
		):
			stack.enter_context(patch.object(monitoring, name, return_value=0))
		stack.enter_context(patch.object(monitoring, "stuck_orders", return_value=[]))
		return stack


class TestHealth(MonitoringTestCase):
	def setUp(self):
		super().setUp()
		self.client = frappe._dict(settings=frappe._dict(environment="Production"))
		self._start(patch.object(orders, "get_client", return_value=self.client))
		self._start(patch.object(orders, "_settings", return_value=frappe._dict(enabled=1, import_orders=1)))
		self._start(patch.object(frappe.db, "commit"))
		frappe.db.set_single_value("Bol Settings", "last_order_poll", LONG_AGO, update_modified=False)

	def test_order_poll_records_its_run_without_touching_modified(self):
		self.client.request = lambda *args, **kwargs: {"orders": []}
		modified = self._modified()
		orders.poll_orders()
		last = get_datetime(frappe.db.get_single_value("Bol Settings", "last_order_poll"))
		self.assertLess(abs((now_datetime() - last).total_seconds()), 60)
		self.assertEqual(self._modified(), modified)

	def test_failed_order_list_records_nothing(self):
		def fail(*args, **kwargs):
			raise BolTransientError("bol unavailable (HTTP 503)")

		self.client.request = fail
		frappe.cache.delete_keys("bol:failed:orders")
		self.addCleanup(frappe.cache.delete_keys, "bol:failed:orders")
		with self.assertRaises(BolTransientError):
			orders.poll_orders()
		self.assertEqual(
			get_datetime(frappe.db.get_single_value("Bol Settings", "last_order_poll")),
			get_datetime(LONG_AGO),
		)

	def _modified(self):
		# get_single_value only reads the DocType's own fields.
		return frappe.db.get_singles_dict("Bol Settings").get("modified")
