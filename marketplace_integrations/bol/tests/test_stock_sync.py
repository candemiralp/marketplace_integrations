# Stock sync (story 06) against the in-memory bol of the offer sync tests. Everything stays in the test transaction.

from unittest.mock import patch

import frappe
from frappe.utils import get_datetime

from marketplace_integrations.bol import offers, stock
from marketplace_integrations.bol.api.errors import BolServerError, BolTransientError
from marketplace_integrations.bol.mappers.stock import stock_body
from marketplace_integrations.bol.tests.test_offer_sync import (
	EAN,
	OTHER_EAN,
	PATH,
	OfferSyncTestCase,
	desired_offer,
)
from marketplace_integrations.bol.tests.utils import TEST_WAREHOUSE, make_warehouse

WAREHOUSE = TEST_WAREHOUSE
OTHER_WAREHOUSE = "_Test bol Other Warehouse"
NEVER = get_datetime("1970-01-01")


class StockSyncTestCase(OfferSyncTestCase):
	def setUp(self):
		super().setUp()
		self.settings.update(sync_stock=1, warehouse=WAREHOUSE, stock_buffer=1)
		self._start(patch.object(stock, "get_client", return_value=self.bol))
		self._start(patch.object(stock, "_settings", return_value=self.settings))
		self.pushes = self._start(patch.object(stock, "enqueue_push"))
		make_warehouse()
		make_warehouse(OTHER_WAREHOUSE)

	def _item(self, item_code: str, ean: str = EAN, rate: float | None = 9.99, **values):
		marker = f"bol:failed:stock:{item_code}"
		frappe.cache.delete_value(marker)
		self.addCleanup(frappe.cache.delete_value, marker)
		return super()._item(item_code, ean, rate, **values)

	def _offer(self, item_code: str, ean: str = EAN, actual_qty: float = 5, **values) -> str:
		"""A test item with stock, whose offer was created by sync_offer. bol's call history starts empty."""
		self._item(item_code, ean, **values)
		self._bin(item_code, actual_qty=actual_qty)
		offers.sync_offer(item_code)
		self.bol.calls.clear()
		return self._offer_id(item_code)

	def _bin(self, item_code: str, warehouse: str = WAREHOUSE, **qty):
		frappe.get_doc({"doctype": "Bin", "item_code": item_code, "warehouse": warehouse, **qty}).insert()

	def _set_bin(self, item_code: str, warehouse: str = WAREHOUSE, **qty):
		# The way ERPNext changes a Bin: no document events.
		frappe.db.set_value("Bin", {"item_code": item_code, "warehouse": warehouse}, qty)

	def _pushed(self, item_code: str) -> tuple[int, object]:
		amount, synced_on = frappe.db.get_value(
			"Ecommerce Item",
			{"integration": "Bol", "erpnext_item_code": item_code},
			["bol_last_stock_pushed", "inventory_synced_on"],
		)
		return amount, get_datetime(synced_on) > NEVER

	def _patches(self) -> list[tuple[str, dict]]:
		return [(path, body) for method, path, body, _params in self.bol.writes() if method == "PATCH"]


class TestAvailableStock(StockSyncTestCase):
	def test_missing_bin_is_zero(self):
		self._item("_Test bol stock no bin")
		self.assertEqual(stock.available_stock("_Test bol stock no bin", self.settings), 0)

	def test_reservations_and_settings_buffer(self):
		self._item("_Test bol stock reserved")
		self._bin("_Test bol stock reserved", actual_qty=10, reserved_qty=3, reserved_stock=2)
		self.assertEqual(stock.available_stock("_Test bol stock reserved", self.settings), 4)

	def test_item_buffer_overrides_the_settings(self):
		self._item("_Test bol stock buffer", bol_stock_buffer=3)
		self._bin("_Test bol stock buffer", actual_qty=5)
		self.assertEqual(stock.available_stock("_Test bol stock buffer", self.settings), 2)

	def test_other_warehouse_does_not_count(self):
		self._item("_Test bol stock elsewhere")
		self._bin("_Test bol stock elsewhere", OTHER_WAREHOUSE, actual_qty=50)
		self.assertEqual(stock.available_stock("_Test bol stock elsewhere", self.settings), 0)


class TestCreateWithStock(StockSyncTestCase):
	def test_new_offer_starts_with_its_stock(self):
		offer_id = self._offer("_Test bol stock create", actual_qty=5)

		self.assertEqual(self.bol.offers[offer_id]["stock"], stock_body(4))
		self.assertEqual(self._pushed("_Test bol stock create"), (4, True))

	def test_without_stock_sync_new_offer_has_none(self):
		self.settings.sync_stock = 0

		offer_id = self._offer("_Test bol stock create off", actual_qty=5)

		self.assertEqual(self.bol.offers[offer_id]["stock"], stock_body(0))
		self.assertEqual(self._pushed("_Test bol stock create off"), (0, False))


class TestPushStock(StockSyncTestCase):
	def test_unchanged_stock_makes_no_call(self):
		self._offer("_Test bol stock same")

		stock.push_stock("_Test bol stock same")

		self.assertEqual(self.bol.calls, [])

	def test_changed_stock_is_patched_and_stored(self):
		offer_id = self._offer("_Test bol stock changed")
		self._set_bin("_Test bol stock changed", actual_qty=8)

		stock.push_stock("_Test bol stock changed")

		self.assertEqual(self._patches(), [(f"{PATH}/{offer_id}", {"stock": stock_body(7)})])
		self.assertEqual(self._pushed("_Test bol stock changed"), (7, True))

	def test_zero_is_pushed_once(self):
		self._offer("_Test bol stock zero")
		self._set_bin("_Test bol stock zero", actual_qty=0)

		stock.push_stock("_Test bol stock zero")
		self._set_bin("_Test bol stock zero", reserved_qty=2)
		stock.push_stock("_Test bol stock zero")
		stock.poll_stock()

		self.assertEqual([body for _path, body in self._patches()], [{"stock": stock_body(0)}])
		self.pushes.assert_not_called()

	def test_failure_is_not_stored(self):
		self._offer("_Test bol stock down")
		self._set_bin("_Test bol stock down", actual_qty=8)

		for _attempt in range(2):
			self.bol.fail("PATCH", BolTransientError("bol unavailable (HTTP 503)"))
			with self.assertRaises(BolTransientError):
				stock.push_stock("_Test bol stock down")

		self.assertEqual(self._pushed("_Test bol stock down"), (4, True))
		# Logged and commented once while the retries go on.
		self.assertEqual(len(self._error_logs()), 1)
		self.assertEqual(len(self._comments("_Test bol stock down")), 1)

	def test_offer_gone_at_bol_is_handed_to_the_offer_sync(self):
		offer_id = self._offer("_Test bol stock gone")
		del self.bol.offers[offer_id]
		self._set_bin("_Test bol stock gone", actual_qty=8)
		self.enqueued.reset_mock()

		stock.push_stock("_Test bol stock gone")

		self.enqueued.assert_called_once_with("_Test bol stock gone")
		self.assertEqual(self._pushed("_Test bol stock gone"), (4, True))
		self.assertEqual(self._error_logs(), [])

	def test_adopted_offer_is_pushed_although_its_stock_is_zero(self):
		offer_id = self.bol.add(
			EAN,
			**{
				**desired_offer("_Test bol stock adopted", EAN),
				"stock": {"amount": 6, "managedByRetailer": True},
			},
		)
		self._item("_Test bol stock adopted")
		offers.sync_offer("_Test bol stock adopted")
		self.bol.calls.clear()

		stock.push_stock("_Test bol stock adopted")

		self.assertEqual(self._patches(), [(f"{PATH}/{offer_id}", {"stock": stock_body(0)})])

	def test_item_not_for_bol_or_without_offer_does_nothing(self):
		self._item("_Test bol stock unmapped")
		self._offer("_Test bol stock unticked", OTHER_EAN)
		frappe.db.set_value("Item", "_Test bol stock unticked", "bol_enabled", 0)
		self._set_bin("_Test bol stock unticked", actual_qty=8)

		stock.push_stock("_Test bol stock unmapped")
		stock.push_stock("_Test bol stock unticked")

		self.assertEqual(self.bol.calls, [])

	def test_feature_toggle_off(self):
		self._offer("_Test bol stock toggle")
		self._set_bin("_Test bol stock toggle", actual_qty=8)
		for values in ({"sync_stock": 0}, {"sync_offers": 0}, {"enabled": 0}):
			with self.subTest(**values):
				self.settings.update({"enabled": 1, "sync_offers": 1, "sync_stock": 1, **values})
				stock.push_stock("_Test bol stock toggle")
		self.assertEqual(self.bol.calls, [])


class TestPollStock(StockSyncTestCase):
	def test_bin_change_in_the_bol_warehouse_is_queued(self):
		self._offer("_Test bol poll change")
		stock.poll_stock()
		self.pushes.assert_not_called()

		self._set_bin("_Test bol poll change", actual_qty=8)
		stock.poll_stock()

		self.pushes.assert_called_once_with("_Test bol poll change")
		self.assertEqual(self.bol.calls, [])

	def test_other_warehouse_is_ignored(self):
		self._offer("_Test bol poll elsewhere")
		self._bin("_Test bol poll elsewhere", OTHER_WAREHOUSE, actual_qty=50)

		stock.poll_stock()

		self.pushes.assert_not_called()

	def test_item_not_for_bol_is_ignored(self):
		self._offer("_Test bol poll unticked")
		frappe.db.set_value("Item", "_Test bol poll unticked", "bol_enabled", 0)
		self._set_bin("_Test bol poll unticked", actual_qty=8)

		stock.poll_stock()

		self.pushes.assert_not_called()

	def test_buffer_change_is_queued(self):
		self._offer("_Test bol poll settings buffer")
		self._offer("_Test bol poll item buffer", OTHER_EAN)
		frappe.db.set_value("Item", "_Test bol poll item buffer", "bol_stock_buffer", 2)

		stock.poll_stock()
		self.pushes.assert_called_once_with("_Test bol poll item buffer")

		self.pushes.reset_mock()
		self.settings.stock_buffer = 3
		stock.poll_stock()
		queued = {call.args[0] for call in self.pushes.call_args_list}
		self.assertEqual(queued, {"_Test bol poll settings buffer", "_Test bol poll item buffer"})

	def test_mapping_never_pushed_is_queued(self):
		self.settings.sync_stock = 0
		self._offer("_Test bol poll never", actual_qty=1)
		self.settings.sync_stock = 1

		stock.poll_stock()

		# Its stock is 0, like the field's default, but bol has never been told.
		self.pushes.assert_called_once_with("_Test bol poll never")

	def test_feature_toggle_off(self):
		self._offer("_Test bol poll toggle")
		self._set_bin("_Test bol poll toggle", actual_qty=8)
		for values in ({"sync_stock": 0}, {"sync_offers": 0}, {"enabled": 0}):
			with self.subTest(**values):
				self.settings.update({"enabled": 1, "sync_offers": 1, "sync_stock": 1, **values})
				stock.poll_stock()
		self.pushes.assert_not_called()


class TestReconcileStock(StockSyncTestCase):
	def _success_logs(self) -> list[dict]:
		return [log for log in frappe.local.bol_pending_logs or [] if log["status"] == "Success"]

	def test_difference_is_corrected_and_matches_are_skipped(self):
		drift_id = self._offer("_Test bol reconcile stock drift")
		self._offer("_Test bol reconcile stock fine", OTHER_EAN)
		self.bol.offers[drift_id]["stock"] = {"amount": 9, "correctedStock": 9, "managedByRetailer": False}

		counts = stock.reconcile_stock()

		self.assertEqual(counts, {"checked": 2, "corrected": 1, "failed": 0})
		self.assertEqual(self._patches(), [(f"{PATH}/{drift_id}", {"stock": stock_body(4)})])
		gets = [path for method, path, _json, _params in self.bol.calls if method == "GET"]
		self.assertEqual(gets, [PATH])
		self.assertEqual(
			self._success_logs()[-1]["message"], "bol stock reconcile: 2 checked, 1 corrected, 0 failed."
		)

	def test_healthy_system_reports_no_corrections(self):
		self._offer("_Test bol reconcile stock healthy")

		counts = stock.reconcile_stock()

		self.assertEqual(counts, {"checked": 1, "corrected": 0, "failed": 0})
		self.assertEqual(self.bol.writes(), [])

	def test_matching_offer_never_pushed_is_recorded(self):
		self.settings.sync_stock = 0
		self._offer("_Test bol reconcile stock record", actual_qty=1)
		self.settings.sync_stock = 1

		stock.reconcile_stock()

		self.assertEqual(self.bol.writes(), [])
		self.assertEqual(self._pushed("_Test bol reconcile stock record"), (0, True))

	def test_one_failing_offer_does_not_stop_the_others(self):
		bad_id = self._offer("_Test bol reconcile stock bad")
		good_id = self._offer("_Test bol reconcile stock good", OTHER_EAN)
		for offer_id in (bad_id, good_id):
			self.bol.offers[offer_id]["stock"] = {"amount": 9, "managedByRetailer": False}
		self.bol.fail("PATCH", BolServerError("bol server error (HTTP 500)"), path=f"{PATH}/{bad_id}")

		counts = stock.reconcile_stock()

		self.assertEqual(counts, {"checked": 2, "corrected": 1, "failed": 1})
		self.assertEqual(self.bol.offers[good_id]["stock"], stock_body(4))
		self.assertEqual(len(self._error_logs()), 1)

	def test_list_failure_is_logged_and_fails_the_job(self):
		self._offer("_Test bol reconcile stock list")
		self.bol.fail("GET", BolTransientError("bol unavailable (HTTP 503)"), path=PATH)

		with self.assertRaises(BolTransientError):
			stock.reconcile_stock()

		logs = self._error_logs()
		self.assertEqual(len(logs), 1)
		self.assertTrue(logs[0]["message"].startswith("bol stock reconcile stopped"))

	def test_feature_toggle_off(self):
		self._offer("_Test bol reconcile stock toggle")
		self.settings.sync_stock = 0

		self.assertIsNone(stock.reconcile_stock())
		self.assertEqual(self.bol.calls, [])
