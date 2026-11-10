import copy
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from marketplace_integrations.bol import offers
from marketplace_integrations.bol.api.errors import BolError, BolTransientError
from marketplace_integrations.bol.tests.utils import discard_pending_logs, make_catalog_masters, make_item

FIXTURES = Path(__file__).parent / "fixtures"
PAGE_1 = json.loads((FIXTURES / "offers_page_1.json").read_text())
PAGE_2 = json.loads((FIXTURES / "offers_page_2.json").read_text())
FOR_SALE, ON_HOLD = PAGE_1["offers"]
NOT_FOR_SALE = PAGE_2["offers"][0]
CURSOR = PAGE_1["page"]["nextCursor"]


def single_page(*offer_list) -> dict:
	return {"offers": list(offer_list), "page": {"pageSize": 100, "nextCursor": None}}


class AdoptTestCase(FrappeTestCase):
	"""adopt_existing_offers with BolClient mocked. Everything stays in the test transaction."""

	def setUp(self):
		self.client = MagicMock(name="BolClient")
		self._start(patch.object(offers, "get_client", return_value=self.client))
		self.realtime = self._start(patch.object(offers.frappe, "publish_realtime"))
		# The job commits per offer; keep the work in this transaction so the rollback below undoes it.
		self._start(patch.object(frappe.db, "commit"))
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		make_catalog_masters()

	def _start(self, patcher):
		self.addCleanup(patcher.stop)
		return patcher.start()

	def _bol_returns(self, *pages):
		self.client.request.side_effect = [copy.deepcopy(page) for page in pages]

	def _adopt(self, *pages) -> dict:
		self._bol_returns(*pages)
		return offers.adopt_existing_offers()

	def _mappings(self, **filters):
		return frappe.get_all(
			"Ecommerce Item",
			filters={"integration": "Bol", **filters},
			fields=["erpnext_item_code", "integration_item_code", "sku"],
		)

	def _map(self, item_code: str, offer_id: str, ean: str):
		frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": "Bol",
				"erpnext_item_code": item_code,
				"integration_item_code": offer_id,
				"sku": ean,
			}
		).insert()

	def _item_flags(self, item_code):
		return frappe.db.get_value("Item", item_code, ["bol_enabled", "bol_offer_status"])

	def _logs(self):
		return frappe.local.bol_pending_logs or []


class TestAdoptExistingOffers(AdoptTestCase):
	def test_match_maps_the_offer_and_flags_the_item(self):
		make_item("_Test bol shirt", FOR_SALE["ean"])

		report = self._adopt(single_page(FOR_SALE))

		self.assertEqual(
			self._mappings(),
			[
				{
					"erpnext_item_code": "_Test bol shirt",
					"integration_item_code": FOR_SALE["offerId"],
					"sku": "0000007740404",
				}
			],
		)
		self.assertEqual(self._item_flags("_Test bol shirt"), (1, "For sale"))
		self.assertEqual(report["counts"]["adopted"], 1)
		self.client.request.assert_called_once_with(
			"GET", "/retailer/offers", version=11, params={"page-size": 100}
		)

	def test_adopting_skips_item_validation(self):
		# The Brand has no economic operator yet: adopting still works; fixing it comes later.
		item = make_item("_Test bol cap", ON_HOLD["ean"], brand=None)

		self._adopt(single_page(ON_HOLD))

		self.assertEqual(self._item_flags(item.name), (1, "On hold"))

	def test_no_match_is_reported(self):
		report = self._adopt(single_page(ON_HOLD))

		self.assertEqual(report["counts"]["unmatched"], 1)
		self.assertEqual(report["unmatched"], [{"offerId": ON_HOLD["offerId"], "ean": ON_HOLD["ean"]}])
		self.assertEqual(self._mappings(), [])

	def test_offer_without_ean_is_unmatched(self):
		offer = {key: value for key, value in FOR_SALE.items() if key != "ean"}

		report = self._adopt(single_page(offer))

		self.assertEqual(report["unmatched"], [{"offerId": FOR_SALE["offerId"], "ean": None}])

	def test_upc_a_barcode_matches_the_ean(self):
		make_item("_Test bol upc", ("012345678905", "UPC-A"))

		report = self._adopt(single_page(NOT_FOR_SALE))

		self.assertEqual(report["counts"]["adopted"], 1)
		self.assertEqual(self._item_flags("_Test bol upc"), (1, "Not for sale"))
		self.assertEqual(self._mappings()[0]["sku"], "0012345678905")

	def test_duplicate_ean_is_reported_and_nothing_is_picked(self):
		make_item("_Test bol bag A", "0012345678905")
		make_item("_Test bol bag B", ("012345678905", "UPC-A"))

		report = self._adopt(single_page(NOT_FOR_SALE))

		self.assertEqual(report["counts"]["ambiguous"], 1)
		self.assertEqual(report["ambiguous"][0]["items"], ["_Test bol bag A", "_Test bol bag B"])
		self.assertEqual(self._mappings(), [])
		self.assertEqual(self._item_flags("_Test bol bag A"), (0, None))
		self.assertEqual(self._item_flags("_Test bol bag B"), (0, None))

	def test_offer_mapped_to_another_item_is_a_conflict(self):
		make_item("_Test bol old", "8712345678906")
		self._map("_Test bol old", FOR_SALE["offerId"], "8712345678906")
		make_item("_Test bol new", FOR_SALE["ean"])

		report = self._adopt(single_page(FOR_SALE))

		self.assertEqual(report["counts"]["conflict"], 1)
		[conflict] = report["conflict"]
		self.assertEqual(conflict["item"], "_Test bol new")
		self.assertIn("_Test bol old", conflict["reason"])
		self.assertEqual(len(self._mappings()), 1)
		self.assertEqual(self._item_flags("_Test bol new"), (0, None))

	def test_item_mapped_to_another_offer_is_a_conflict(self):
		make_item("_Test bol twin", FOR_SALE["ean"])
		self._map("_Test bol twin", "99999999-0000-0000-0000-000000000000", FOR_SALE["ean"])

		report = self._adopt(single_page(FOR_SALE))

		self.assertEqual(report["counts"]["conflict"], 1)
		self.assertIn("99999999-0000-0000-0000-000000000000", report["conflict"][0]["reason"])
		self.assertEqual(self._mappings(integration_item_code=FOR_SALE["offerId"]), [])

	def test_rerun_is_idempotent(self):
		make_item("_Test bol shirt", FOR_SALE["ean"])
		self._adopt(single_page(FOR_SALE))
		on_hold_now = {**FOR_SALE, "onHoldByRetailer": True}

		report = self._adopt(single_page(on_hold_now))

		self.assertEqual(len(self._mappings()), 1)
		self.assertEqual((report["counts"]["adopted"], report["counts"]["already_mapped"]), (0, 1))
		self.assertEqual(self._item_flags("_Test bol shirt"), (1, "On hold"))

	def test_concurrent_insert_counts_as_already_mapped(self):
		make_item("_Test bol shirt", FOR_SALE["ean"])

		with patch("frappe.model.document.Document.insert", side_effect=frappe.DuplicateEntryError):
			report = self._adopt(single_page(FOR_SALE))

		self.assertEqual(report["counts"]["already_mapped"], 1)

	def test_multiple_pages_follow_the_cursor_and_only_read(self):
		make_item("_Test bol shirt", FOR_SALE["ean"])
		make_item("_Test bol cap", ON_HOLD["ean"])

		report = self._adopt(PAGE_1, PAGE_2)

		self.assertEqual(report["pages"], 2)
		self.assertEqual(report["counts"], {**report["counts"], "adopted": 2, "unmatched": 1})
		first, second = self.client.request.call_args_list
		self.assertEqual(first.kwargs["params"], {"page-size": 100})
		self.assertEqual(second.kwargs["params"], {"page-size": 100, "cursor": CURSOR})
		self.assertEqual({call.args[0] for call in self.client.request.call_args_list}, {"GET"})

	def test_one_bad_offer_does_not_stop_the_others(self):
		make_item("_Test bol shirt", FOR_SALE["ean"])
		make_item("_Test bol cap", ON_HOLD["ean"])

		with patch.object(offers, "offer_status", side_effect=[RuntimeError("bug"), "On hold"]):
			report = self._adopt(single_page(FOR_SALE, ON_HOLD))

		self.assertEqual((report["counts"]["failed"], report["counts"]["adopted"]), (1, 1))
		self.assertEqual(report["failed"][0]["error"], "bug")
		self.assertEqual(self._item_flags("_Test bol shirt"), (0, None))
		failure, summary = self._logs()
		self.assertIn("RuntimeError: bug", failure["traceback"])
		self.assertEqual(summary["status"], "Success")

	def test_result_is_one_log_entry_and_a_message_to_the_user(self):
		make_item("_Test bol shirt", FOR_SALE["ean"])

		self._adopt(PAGE_1, PAGE_2)

		[entry] = self._logs()
		self.assertEqual(entry["status"], "Success")
		self.assertIn("1 adopted", entry["message"])
		self.assertIn("2 without a matching item", entry["message"])
		report = json.loads(entry["response_data"])
		self.assertEqual(
			{row["offerId"] for row in report["unmatched"]}, {ON_HOLD["offerId"], NOT_FOR_SALE["offerId"]}
		)
		event, message = self.realtime.call_args.args
		self.assertEqual(event, "msgprint")
		self.assertEqual(message["indicator"], "orange")
		self.assertEqual(self.realtime.call_args.kwargs, {"user": frappe.session.user})

	def test_pagination_error_fails_the_job_and_logs_the_progress(self):
		make_item("_Test bol shirt", FOR_SALE["ean"])
		error = BolTransientError(
			"bol unavailable (HTTP 503)", {"method": "GET", "path": "/retailer/offers", "status": 503}
		)
		self.client.request.side_effect = [copy.deepcopy(PAGE_1), error]

		with self.assertRaises(BolTransientError):
			offers.adopt_existing_offers()

		# The first page is kept (committed per offer), so a rerun only has to catch up.
		self.assertEqual(len(self._mappings()), 1)
		[entry] = self._logs()
		self.assertEqual(entry["status"], "Error")
		self.assertIn("stopped after 1 pages", entry["message"])
		progress = json.loads(entry["response_data"])["report"]
		self.assertEqual((progress["pages"], progress["cursor"]), (1, CURSOR))
		self.assertEqual(progress["counts"]["adopted"], 1)
		self.assertEqual(self.realtime.call_args.args[1]["indicator"], "red")

	def test_repeated_cursor_stops_the_job(self):
		make_item("_Test bol shirt", FOR_SALE["ean"])

		with self.assertRaisesRegex(BolError, "same page cursor"):
			self._adopt(PAGE_1, PAGE_1)

		self.assertEqual(self.client.request.call_count, 2)
		self.assertEqual(len(self._mappings()), 1)
		[entry] = self._logs()
		self.assertEqual(entry["status"], "Error")


class TestEnqueueAdoption(FrappeTestCase):
	def test_enqueues_one_deduplicated_long_job(self):
		with (
			patch.object(offers, "is_job_enqueued", return_value=False),
			patch.object(offers.frappe, "enqueue") as enqueue,
		):
			self.assertTrue(offers.enqueue_adoption())

		enqueue.assert_called_once_with(
			"marketplace_integrations.bol.offers.adopt_existing_offers",
			queue="long",
			timeout=3600,
			job_id="bol_adopt_existing_offers",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def test_already_running(self):
		with (
			patch.object(offers, "is_job_enqueued", return_value=True),
			patch.object(offers.frappe, "enqueue") as enqueue,
		):
			self.assertFalse(offers.enqueue_adoption())
		enqueue.assert_not_called()


class TestAdoptButton(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")
		self.settings = frappe.get_single("Bol Settings")
		self.settings.enabled = 1

	def test_queues_the_job(self):
		with patch("marketplace_integrations.bol.doctype.bol_settings.bol_settings.enqueue_adoption") as enqueue:
			enqueue.return_value = True
			self.assertEqual(self.settings.adopt_existing_offers(), {"queued": True})

	def test_requires_enabled_integration(self):
		self.settings.enabled = 0

		with (
			patch("marketplace_integrations.bol.doctype.bol_settings.bol_settings.enqueue_adoption") as enqueue,
			self.assertRaisesRegex(frappe.ValidationError, "Enable the bol integration"),
		):
			self.settings.adopt_existing_offers()
		enqueue.assert_not_called()

	def test_requires_a_bol_role(self):
		frappe.set_user("Guest")

		with (
			patch("marketplace_integrations.bol.doctype.bol_settings.bol_settings.enqueue_adoption") as enqueue,
			self.assertRaises(frappe.PermissionError),
		):
			self.settings.adopt_existing_offers()
		enqueue.assert_not_called()
