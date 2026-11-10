# Offer sync (story 05) with bol's offer endpoints faked in memory. Everything stays in the test transaction.

import copy
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from marketplace_integrations.bol import offers
from marketplace_integrations.bol.api.errors import (
	BolNotFoundError,
	BolServerError,
	BolTransientError,
	BolValidationError,
)
from marketplace_integrations.bol.tests.utils import (
	OPERATOR_ID,
	TEST_BRAND,
	TEST_PRICE_LIST,
	discard_pending_logs,
	make_brand,
	make_catalog_masters,
	make_item,
	make_item_price,
	make_price_list,
)

EAN = "4006381333931"
OTHER_EAN = "8712345678906"
THIRD_EAN = "8712345678913"
PATH = "/retailer/offers"
REASONS = {
	"offerId": "x",
	"countries": [
		{"countryCode": "NL", "reasons": [{"category": "PRICE", "code": "4000-1", "description": "Price too high"}]}
	],
}


class FakeBol:
	"""bol's offer endpoints in memory, called like BolClient.request."""

	def __init__(self):
		self.settings = frappe._dict(environment="Production")
		self.offers = {}
		self.calls = []
		self.errors = []
		self.created = 0

	def add(self, ean: str, for_sale: bool = True, **values) -> str:
		self.created += 1
		offer_id = f"00000000-0000-4000-8000-{self.created:012d}"
		self.offers[offer_id] = {
			"offerId": offer_id,
			"ean": ean,
			"onHoldByRetailer": False,
			"countryAvailabilities": [{"countryCode": "NL", "forSale": for_sale}],
			**copy.deepcopy(values),
		}
		return offer_id

	def fail(self, method: str, error: Exception, path: str | None = None, after_effect: bool = False):
		"""Raise `error` on the next matching call; `after_effect`: bol processed it all the same."""
		self.errors.append((method, path, error, after_effect))

	def writes(self) -> list[tuple]:
		return [call for call in self.calls if call[0] != "GET"]

	def request(self, method, path, *, version, json=None, params=None, reference=None, **kwargs):
		self.calls.append((method, path, copy.deepcopy(json), params))
		error, after_effect = self._error(method, path)
		if error and not after_effect:
			raise error
		result = self._handle(method, path, copy.deepcopy(json), params or {})
		if error:
			raise error
		return result

	def _error(self, method, path):
		for i, (m, p, error, after_effect) in enumerate(self.errors):
			if m == method and p in (None, path):
				del self.errors[i]
				return error, after_effect
		return None, False

	def _handle(self, method, path, body, params):
		parts = path.removeprefix(PATH).strip("/").split("/")
		if parts == [""]:
			if method == "POST":
				values = {key: value for key, value in body.items() if key not in ("ean", "condition")}
				return {"offerId": self.add(body["ean"], **values)}
			eans = params.get("eans")
			found = [offer for offer in self.offers.values() if not eans or offer["ean"] in eans]
			return {"offers": copy.deepcopy(found), "page": {"pageSize": 100, "nextCursor": None}}
		offer_id = parts[0]
		if offer_id not in self.offers:
			raise BolNotFoundError("Not found")
		if len(parts) == 2:
			return copy.deepcopy(REASONS)
		if method == "GET":
			return copy.deepcopy(self.offers[offer_id])
		if method == "PATCH":
			self.offers[offer_id].update(body)
			return None
		del self.offers[offer_id]
		return None


def desired_offer(item_code: str, ean: str, price: float = 9.99) -> dict:
	"""What sync_offer would send for a test item (Settings days 1–2, the test brand's operator)."""
	return {
		"reference": item_code,
		"economicOperatorId": OPERATOR_ID,
		"onHoldByRetailer": False,
		"pricing": {"bundlePrices": [{"quantity": 1, "unitPrice": price}]},
		"fulfilment": {
			"method": "FBR",
			"schedule": "MY_DELIVERY_PROMISE",
			"deliveryPromise": {"minimumDaysToCustomer": 1, "maximumDaysToCustomer": 2},
		},
		"stock": {"amount": 0, "managedByRetailer": False},
	}


class OfferSyncTestCase(FrappeTestCase):
	def setUp(self):
		self.bol = FakeBol()
		self._start(patch.object(offers, "get_client", return_value=self.bol))
		self.settings = frappe._dict(
			enabled=1, sync_offers=1, price_list=TEST_PRICE_LIST, default_min_days=1, default_max_days=2
		)
		self._start(patch.object(offers, "_settings", return_value=self.settings))
		self.enqueued = self._start(patch.object(offers, "enqueue_sync"))
		# The job commits after creating an offer; keep the work in this transaction so the rollback undoes it.
		self._start(patch.object(frappe.db, "commit"))
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		self.addCleanup(frappe.set_user, "Administrator")
		make_catalog_masters()
		make_price_list()

	def _start(self, patcher):
		self.addCleanup(patcher.stop)
		return patcher.start()

	def _item(self, item_code: str, ean: str = EAN, rate: float | None = 9.99, **values):
		marker = f"bol:failed:offer:{item_code}"
		frappe.cache.delete_value(marker)
		self.addCleanup(frappe.cache.delete_value, marker)
		item = make_item(item_code, ean, **{"bol_enabled": 1, **values})
		if rate is not None:
			make_item_price(item_code, rate)
		return item

	def _offer_id(self, item_code: str) -> str | None:
		return frappe.db.get_value(
			"Ecommerce Item", {"integration": "Bol", "erpnext_item_code": item_code}, "integration_item_code"
		)

	def _mappings(self) -> list[str]:
		return frappe.get_all("Ecommerce Item", filters={"integration": "Bol"}, pluck="integration_item_code")

	def _status(self, item_code: str) -> tuple:
		return frappe.db.get_value("Item", item_code, ["bol_offer_status", "bol_status_detail"])

	def _price(self, item_code: str, rate: float):
		name = frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": TEST_PRICE_LIST})
		frappe.db.set_value("Item Price", name, "price_list_rate", rate)

	def _synced(self, item_code: str, ean: str = EAN, **values) -> str:
		"""A test item whose offer was created by sync_offer. bol's call history starts empty afterwards."""
		self._item(item_code, ean, **values)
		offers.sync_offer(item_code)
		self.bol.calls.clear()
		return self._offer_id(item_code)

	def _error_logs(self) -> list[dict]:
		return [log for log in frappe.local.bol_pending_logs or [] if log["status"] == "Error"]

	def _comments(self, item_code: str) -> list[str]:
		return frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Item", "reference_name": item_code, "comment_type": "Comment"},
			pluck="content",
		)


class TestSyncOffer(OfferSyncTestCase):
	def test_new_item_is_looked_up_then_created(self):
		self._item("_Test bol sync new")

		offers.sync_offer("_Test bol sync new")

		methods = [(method, path) for method, path, _json, _params in self.bol.calls]
		offer_id = self._offer_id("_Test bol sync new")
		self.assertEqual(methods, [("GET", PATH), ("POST", PATH), ("GET", f"{PATH}/{offer_id}")])
		self.assertEqual(self.bol.calls[0][3], {"eans": [EAN]})
		self.assertEqual(
			self.bol.calls[1][2],
			{"ean": EAN, "condition": {"category": "NEW"}, **desired_offer("_Test bol sync new", EAN)},
		)
		self.assertEqual(self._mappings(), [offer_id])
		self.assertEqual(
			frappe.db.get_value("Ecommerce Item", {"integration_item_code": offer_id}, "sku"), EAN
		)
		self.assertEqual(self._status("_Test bol sync new"), ("For sale", ""))

	def test_existing_offer_is_adopted_by_ean(self):
		offer_id = self.bol.add(EAN, **desired_offer("_Test bol sync adopt", EAN, price=8.5))
		self._item("_Test bol sync adopt")

		offers.sync_offer("_Test bol sync adopt")

		self.assertEqual(self._offer_id("_Test bol sync adopt"), offer_id)
		self.assertEqual(
			self.bol.writes(),
			[("PATCH", f"{PATH}/{offer_id}", {"pricing": {"bundlePrices": [{"quantity": 1, "unitPrice": 9.99}]}}, None)],
		)

	def test_unchanged_offer_is_not_patched(self):
		self._synced("_Test bol sync same")

		offers.sync_offer("_Test bol sync same")

		self.assertEqual(self.bol.writes(), [])
		self.assertEqual(self._status("_Test bol sync same"), ("For sale", ""))

	def test_changed_price_patches_the_price_only(self):
		offer_id = self._synced("_Test bol sync price")
		self._price("_Test bol sync price", 12.5)

		offers.sync_offer("_Test bol sync price")

		self.assertEqual(
			self.bol.writes(),
			[("PATCH", f"{PATH}/{offer_id}", {"pricing": {"bundlePrices": [{"quantity": 1, "unitPrice": 12.5}]}}, None)],
		)

	def test_disabled_item_is_put_on_hold_without_needing_a_price(self):
		offer_id = self._synced("_Test bol sync hold")
		frappe.db.set_value("Item", "_Test bol sync hold", "disabled", 1)
		frappe.db.delete("Item Price", {"item_code": "_Test bol sync hold"})

		offers.sync_offer("_Test bol sync hold")

		self.assertEqual(self.bol.writes(), [("PATCH", f"{PATH}/{offer_id}", {"onHoldByRetailer": True}, None)])
		self.assertEqual(self._status("_Test bol sync hold"), ("On hold", ""))

	def test_unticked_item_is_put_on_hold(self):
		offer_id = self._synced("_Test bol sync untick")
		frappe.db.set_value("Item", "_Test bol sync untick", "bol_enabled", 0)

		offers.sync_offer("_Test bol sync untick")

		self.assertEqual(self.bol.writes(), [("PATCH", f"{PATH}/{offer_id}", {"onHoldByRetailer": True}, None)])
		self.assertIn(offer_id, self.bol.offers)

	def test_enabled_again_goes_back_on_sale(self):
		offer_id = self._synced("_Test bol sync back")
		frappe.db.set_value("Item", "_Test bol sync back", "disabled", 1)
		offers.sync_offer("_Test bol sync back")
		frappe.db.set_value("Item", "_Test bol sync back", "disabled", 0)
		self.bol.calls.clear()

		offers.sync_offer("_Test bol sync back")

		self.assertEqual(self.bol.writes(), [("PATCH", f"{PATH}/{offer_id}", {"onHoldByRetailer": False}, None)])
		self.assertEqual(self._status("_Test bol sync back"), ("For sale", ""))

	def test_item_not_for_bol_without_offer_does_nothing(self):
		self._item("_Test bol sync off", bol_enabled=0)

		offers.sync_offer("_Test bol sync off")

		self.assertEqual(self.bol.calls, [])

	def test_offer_deleted_at_bol_is_recreated(self):
		old_id = self._synced("_Test bol sync gone")
		del self.bol.offers[old_id]

		offers.sync_offer("_Test bol sync gone")

		new_id = self._offer_id("_Test bol sync gone")
		self.assertNotEqual(new_id, old_id)
		self.assertEqual(self._mappings(), [new_id])
		self.assertEqual([call[0] for call in self.bol.writes()], ["POST"])

	def test_offer_deleted_at_bol_for_an_item_off_sale(self):
		old_id = self._synced("_Test bol sync gone off")
		del self.bol.offers[old_id]
		frappe.db.set_value("Item", "_Test bol sync gone off", "disabled", 1)

		offers.sync_offer("_Test bol sync gone off")

		self.assertEqual(self._mappings(), [])
		self.assertEqual(self.bol.writes(), [])
		self.assertEqual(self._status("_Test bol sync gone off")[0], "Deleted")

	def test_ambiguous_post_is_not_retried_and_never_duplicated(self):
		self._item("_Test bol sync ambiguous")
		# bol created the offer but the answer never arrived (read timeout).
		self.bol.fail("POST", BolTransientError("ReadTimeout", ambiguous=True), after_effect=True)

		offers.sync_offer("_Test bol sync ambiguous")

		self.assertEqual(self._mappings(), [])
		self.assertEqual(self._status("_Test bol sync ambiguous")[0], "Not synced")
		self.assertEqual(len(self._error_logs()), 1)

		offers.sync_offer("_Test bol sync ambiguous")

		self.assertEqual([call[0] for call in self.bol.writes()], ["POST"])
		self.assertEqual(len(self.bol.offers), 1)
		self.assertEqual(self._mappings(), list(self.bol.offers))
		self.assertEqual(self._status("_Test bol sync ambiguous"), ("For sale", ""))

	def test_create_without_offer_id_counts_as_ambiguous(self):
		self._item("_Test bol sync no id")
		with patch.object(self.bol, "_handle", return_value={}):
			offers.sync_offer("_Test bol sync no id")

		self.assertEqual(self._mappings(), [])
		self.assertEqual(self._status("_Test bol sync no id")[0], "Not synced")

	def test_validation_violations_are_shown_on_the_item(self):
		self._synced("_Test bol sync invalid")
		self._price("_Test bol sync invalid", 12.5)
		violations = [{"name": "pricing.bundlePrices[0].unitPrice", "reason": "Price is not allowed"}]
		self.bol.fail("PATCH", BolValidationError("Bad Request", "Validation failed", violations))

		offers.sync_offer("_Test bol sync invalid")

		self.assertEqual(
			self._status("_Test bol sync invalid"),
			("Not synced", "pricing.bundlePrices[0].unitPrice: Price is not allowed"),
		)
		self.assertEqual(len(self._error_logs()), 1)
		self.assertEqual(len(self._comments("_Test bol sync invalid")), 1)

	def test_missing_price_never_creates_an_offer(self):
		self._item("_Test bol sync no price", rate=None)

		offers.sync_offer("_Test bol sync no price")
		offers.sync_offer("_Test bol sync no price")

		self.assertEqual(self.bol.writes(), [])
		status, detail = self._status("_Test bol sync no price")
		self.assertEqual(status, "Not synced")
		self.assertIn(TEST_PRICE_LIST, detail)
		# The repeat is logged (and commented) once.
		self.assertEqual(len(self._error_logs()), 1)
		self.assertEqual(len(self._comments("_Test bol sync no price")), 1)

	def test_missing_price_leaves_the_live_offer_alone(self):
		offer_id = self._synced("_Test bol sync price gone")
		frappe.db.delete("Item Price", {"item_code": "_Test bol sync price gone"})

		offers.sync_offer("_Test bol sync price gone")

		self.assertEqual(self.bol.writes(), [])
		self.assertFalse(self.bol.offers[offer_id]["onHoldByRetailer"])
		self.assertEqual(self._status("_Test bol sync price gone")[0], "Not synced")

	def test_price_out_of_range(self):
		self._item("_Test bol sync expensive", rate=10000)

		offers.sync_offer("_Test bol sync expensive")

		self.assertEqual(self.bol.writes(), [])
		self.assertIn("10000", self._status("_Test bol sync expensive")[1])

	def test_brand_without_economic_operator(self):
		self._item("_Test bol sync no operator")
		frappe.db.set_value("Brand", TEST_BRAND, "bol_economic_operator_id", None)

		offers.sync_offer("_Test bol sync no operator")

		self.assertEqual(self.bol.writes(), [])
		self.assertEqual(self._status("_Test bol sync no operator")[0], "Not synced")

	def test_ean_already_linked_to_another_items_offer(self):
		self._item("_Test bol sync linked")
		self._item("_Test bol sync other", OTHER_EAN)
		frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": "Bol",
				"erpnext_item_code": "_Test bol sync other",
				"integration_item_code": "11111111-0000-4000-8000-000000000000",
				"sku": EAN,
			}
		).insert()

		offers.sync_offer("_Test bol sync linked")

		self.assertEqual(self.bol.calls, [])
		self.assertEqual(self._status("_Test bol sync linked")[0], "Not synced")

	def test_several_offers_for_the_ean(self):
		self.bol.add(EAN)
		self.bol.add(EAN)
		self._item("_Test bol sync twice")

		offers.sync_offer("_Test bol sync twice")

		self.assertEqual(self.bol.writes(), [])
		self.assertEqual(self._mappings(), [])
		self.assertEqual(self._status("_Test bol sync twice")[0], "Not synced")

	def test_transient_error_fails_the_job_and_is_logged(self):
		offer_id = self._synced("_Test bol sync down")
		self.bol.fail("GET", BolTransientError("bol unavailable (HTTP 503)"))

		with self.assertRaises(BolTransientError):
			offers.sync_offer("_Test bol sync down")

		self.assertEqual(self._offer_id("_Test bol sync down"), offer_id)
		self.assertEqual(self._status("_Test bol sync down")[0], "For sale")
		self.assertEqual(len(self._error_logs()), 1)
		self.assertEqual(len(self._comments("_Test bol sync down")), 1)

	def test_not_for_sale_reasons_are_written(self):
		self.bol.add(EAN, for_sale=False, **desired_offer("_Test bol sync reasons", EAN))
		self._item("_Test bol sync reasons")

		offers.sync_offer("_Test bol sync reasons")

		self.assertEqual(self._status("_Test bol sync reasons"), ("Not for sale", "NL: Price too high (4000-1)"))

	def test_item_days_override_the_settings(self):
		offer_id = self._synced("_Test bol sync days")
		frappe.db.set_value("Item", "_Test bol sync days", {"bol_min_days": 2, "bol_max_days": 4})

		offers.sync_offer("_Test bol sync days")

		patch_body = self.bol.writes()[0][2]
		self.assertEqual(
			patch_body["fulfilment"]["deliveryPromise"], {"minimumDaysToCustomer": 2, "maximumDaysToCustomer": 4}
		)
		self.assertEqual(self.bol.offers[offer_id]["pricing"]["bundlePrices"][0]["unitPrice"], 9.99)

	def test_feature_toggle_off(self):
		self._item("_Test bol sync toggle")
		for values in ({"sync_offers": 0}, {"enabled": 0}):
			with self.subTest(**values):
				self.settings.update({"enabled": 1, "sync_offers": 1, **values})
				offers.sync_offer("_Test bol sync toggle")
		self.assertEqual(self.bol.calls, [])


class TestTriggers(OfferSyncTestCase):
	def test_new_bol_item(self):
		self._item("_Test bol trigger new")

		self.enqueued.assert_any_call("_Test bol trigger new")

	def test_relevant_item_change(self):
		item = self._item("_Test bol trigger change")
		for field, value in (("disabled", 1), ("bol_max_days", 5), ("bol_enabled", 0)):
			with self.subTest(field=field):
				self.enqueued.reset_mock()
				item.set(field, value)
				item.save()
				self.enqueued.assert_called_once_with("_Test bol trigger change")

	def test_unrelated_item_change(self):
		item = self._item("_Test bol trigger other")
		self.enqueued.reset_mock()

		item.description = "New description"
		item.save()

		self.enqueued.assert_not_called()

	def test_item_not_for_bol(self):
		item = self._item("_Test bol trigger none", bol_enabled=0)
		item.disabled = 1
		item.save()

		self.enqueued.assert_not_called()

	def test_item_price_in_the_bol_price_list(self):
		self._item("_Test bol trigger price", rate=None)
		self.enqueued.reset_mock()

		price = make_item_price("_Test bol trigger price", 10)
		price.price_list_rate = 11
		price.save()
		price.delete()

		self.assertEqual(self.enqueued.call_count, 3)

	def test_item_price_in_another_price_list(self):
		self._item("_Test bol trigger shop", rate=None)
		self.enqueued.reset_mock()

		make_item_price("_Test bol trigger shop", 10, make_price_list("_Test bol Shop Prices"))

		self.enqueued.assert_not_called()

	def test_item_price_of_an_item_not_for_bol(self):
		self._item("_Test bol trigger price none", rate=None, bol_enabled=0)

		make_item_price("_Test bol trigger price none", 10)

		self.enqueued.assert_not_called()

	def test_brand_operator_change_queues_its_bol_items(self):
		self._item("_Test bol trigger brand A")
		self._item("_Test bol trigger brand B", OTHER_EAN)
		self._item("_Test bol trigger brand C", THIRD_EAN, bol_enabled=0)
		self.enqueued.reset_mock()

		brand = frappe.get_doc("Brand", TEST_BRAND)
		brand.bol_economic_operator_id = "11111111-2222-3333-4444-555555555555"
		brand.save()

		queued = {call.args[0] for call in self.enqueued.call_args_list}
		self.assertTrue({"_Test bol trigger brand A", "_Test bol trigger brand B"} <= queued)
		self.assertNotIn("_Test bol trigger brand C", queued)

	def test_brand_saved_without_operator_change(self):
		self._item("_Test bol trigger brand same")
		self.enqueued.reset_mock()

		brand = frappe.get_doc("Brand", TEST_BRAND)
		brand.description = "Embroidered"
		brand.save()

		self.enqueued.assert_not_called()

	def test_toggle_off(self):
		self.settings.sync_offers = 0
		item = self._item("_Test bol trigger toggle", rate=None)
		item.disabled = 1
		item.save()
		make_item_price("_Test bol trigger toggle", 12)
		brand = make_brand(TEST_BRAND, OPERATOR_ID)
		brand.bol_economic_operator_id = "11111111-2222-3333-4444-555555555555"
		brand.save()

		self.enqueued.assert_not_called()


class TestReconcile(OfferSyncTestCase):
	def test_statuses_and_reasons_are_refreshed(self):
		sale_id = self._synced("_Test bol reconcile sale")
		off_id = self._synced("_Test bol reconcile off", OTHER_EAN)
		self.bol.offers[sale_id]["onHoldByRetailer"] = True
		self.bol.offers[off_id]["countryAvailabilities"] = [{"countryCode": "NL", "forSale": False}]

		offers.reconcile_offers()

		self.assertEqual(self._status("_Test bol reconcile sale"), ("On hold", ""))
		self.assertEqual(self._status("_Test bol reconcile off"), ("Not for sale", "NL: Price too high (4000-1)"))
		gets = [path for method, path, _json, _params in self.bol.calls if method == "GET"]
		# One list page and the reasons of the one offer that isn't for sale: no GET per offer.
		self.assertEqual(gets, [PATH, f"{PATH}/{off_id}/not-for-sale-reasons"])
		self.assertEqual(self.bol.writes(), [])

	def test_offer_out_of_line_is_queued(self):
		self._synced("_Test bol reconcile drift")
		self._synced("_Test bol reconcile fine", OTHER_EAN)
		self._price("_Test bol reconcile drift", 15)
		self.enqueued.reset_mock()

		offers.reconcile_offers()

		self.enqueued.assert_called_once_with("_Test bol reconcile drift")

	def test_offer_gone_at_bol_and_unmapped_item_are_queued(self):
		gone_id = self._synced("_Test bol reconcile gone")
		del self.bol.offers[gone_id]
		self._item("_Test bol reconcile unmapped", OTHER_EAN)
		self.enqueued.reset_mock()

		offers.reconcile_offers()

		queued = {call.args[0] for call in self.enqueued.call_args_list}
		self.assertEqual(queued, {"_Test bol reconcile gone", "_Test bol reconcile unmapped"})

	def test_one_failing_offer_does_not_stop_the_others(self):
		bad_id = self._synced("_Test bol reconcile bad")
		good_id = self._synced("_Test bol reconcile good", OTHER_EAN)
		for offer_id in (bad_id, good_id):
			self.bol.offers[offer_id]["countryAvailabilities"] = [{"countryCode": "NL", "forSale": False}]
		self.bol.fail("GET", BolServerError("bol server error (HTTP 500)"), path=f"{PATH}/{bad_id}/not-for-sale-reasons")

		offers.reconcile_offers()

		self.assertEqual(self._status("_Test bol reconcile bad")[0], "For sale")
		self.assertEqual(self._status("_Test bol reconcile good")[0], "Not for sale")
		self.assertEqual(len(self._error_logs()), 1)

	def test_list_failure_queues_nothing(self):
		gone_id = self._synced("_Test bol reconcile list")
		del self.bol.offers[gone_id]
		self.enqueued.reset_mock()
		self.bol.fail("GET", BolTransientError("bol unavailable (HTTP 503)"), path=PATH)

		with self.assertRaises(BolTransientError):
			offers.reconcile_offers()

		self.enqueued.assert_not_called()
		self.assertEqual(len(self._error_logs()), 1)

	def test_toggle_off(self):
		self.settings.sync_offers = 0

		offers.reconcile_offers()

		self.assertEqual(self.bol.calls, [])


class TestDeleteOffer(OfferSyncTestCase):
	def test_deletes_at_bol_and_unlinks_the_item(self):
		offer_id = self._synced("_Test bol delete")

		offers.delete_offer("_Test bol delete")

		self.assertEqual(self.bol.writes(), [("DELETE", f"{PATH}/{offer_id}", None, None)])
		self.assertEqual(self._mappings(), [])
		self.assertEqual(
			frappe.db.get_value("Item", "_Test bol delete", ["bol_enabled", "bol_offer_status"]), (0, "Deleted")
		)
		self.enqueued.reset_mock()
		frappe.get_doc("Item", "_Test bol delete").save()
		self.enqueued.assert_not_called()

	def test_already_deleted_at_bol(self):
		offer_id = self._synced("_Test bol delete gone")
		del self.bol.offers[offer_id]

		offers.delete_offer("_Test bol delete gone")

		self.assertEqual(self._mappings(), [])

	def test_bol_error_gives_a_clear_message_and_a_log(self):
		offer_id = self._synced("_Test bol delete error")
		self.bol.fail("DELETE", BolServerError("bol server error (HTTP 500)"))

		with self.assertRaises(frappe.ValidationError) as ctx:
			offers.delete_offer("_Test bol delete error")

		self.assertIn("HTTP 500", str(ctx.exception))
		self.assertEqual(self._mappings(), [offer_id])
		self.assertEqual(len(self._error_logs()), 1)

	def test_item_without_offer(self):
		self._item("_Test bol delete none")

		with self.assertRaises(frappe.ValidationError):
			offers.delete_offer("_Test bol delete none")
		self.assertEqual(self.bol.calls, [])

	def test_disabled_integration(self):
		self._synced("_Test bol delete disabled")
		self.settings.enabled = 0

		with self.assertRaises(frappe.ValidationError):
			offers.delete_offer("_Test bol delete disabled")
		self.assertEqual(self.bol.calls, [])

	def test_requires_a_bol_role(self):
		self._synced("_Test bol delete guest")
		frappe.set_user("Guest")

		with self.assertRaises(frappe.PermissionError):
			offers.delete_offer("_Test bol delete guest")
		self.assertEqual(self.bol.calls, [])

	def test_rejects_wrong_argument_types(self):
		with self.assertRaises(frappe.FrappeTypeError):
			offers.delete_offer(["_Test bol delete"])
