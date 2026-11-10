# Pure shipping label mapper tests (story 09): no database.

import copy
import json
import unittest
from datetime import date
from pathlib import Path

from marketplace_integrations.bol.api.errors import BolMappingError
from marketplace_integrations.bol.mappers.label import choose_option, delivery_options_request, label_request

FIXTURES = Path(__file__).parent / "fixtures"
# The demo's answer: a recommended TNT parcel (4.41) and a TNT_BRIEF mailbox label (2.88), valid until 2020.
OPTIONS = json.loads((FIXTURES / "delivery_options.json").read_text())["deliveryOptions"]
PARCEL, MAILBOX = "32c4a88c-3c64-41ad-83a1-24450b341747", "fb7f20c0-d847-4d62-9c57-7aba64fb9b52"
TODAY = date(2020, 10, 8)


def options(recommended: bool = True, valid_until: tuple = ("2020-10-17", "2020-10-08")) -> list[dict]:
	"""The demo's options (parcel, mailbox), with the parcel's recommendation and both end dates changed."""
	result = copy.deepcopy(OPTIONS)
	result[0]["recommended"] = recommended
	for demo_option, end_date in zip(result, valid_until, strict=True):
		demo_option["validUntilDate"] = end_date
	return result


def option(offer_id: str, transporter: str, price, valid_until: str | None = "2020-10-17", recommended=False):
	return {
		"shippingLabelOfferId": offer_id,
		"recommended": recommended,
		"validUntilDate": valid_until,
		"transporterCode": transporter,
		"labelPrice": {"totalPrice": price},
	}


class TestChooseOption(unittest.TestCase):
	def test_recommended_wins_over_cheaper(self):
		self.assertEqual(choose_option(options(), "TNT_BRIEF", today=TODAY)["shippingLabelOfferId"], PARCEL)

	def test_preferred_transporter_without_a_recommendation(self):
		chosen = choose_option(
			[option("a", "TNT_BRIEF", 2.0), option("b", "DHL", 5.0), option("c", "dhl", 4.5)],
			" dhl ",
			today=TODAY,
		)

		self.assertEqual(chosen["shippingLabelOfferId"], "c")

	def test_cheapest_without_recommendation_or_preference(self):
		self.assertEqual(
			choose_option(options(recommended=False), None, today=TODAY)["shippingLabelOfferId"], MAILBOX
		)

	def test_cheapest_when_the_preferred_transporter_has_no_option(self):
		chosen = choose_option(options(recommended=False), "DHL", today=TODAY)

		self.assertEqual(chosen["shippingLabelOfferId"], MAILBOX)

	def test_expired_options_are_left_out(self):
		# The recommended parcel ran out yesterday; the mailbox label holds through today.
		chosen = choose_option(options(valid_until=("2020-10-07", "2020-10-08")), None, today=TODAY)

		self.assertEqual(chosen["shippingLabelOfferId"], MAILBOX)

	def test_option_without_end_date_stays(self):
		chosen = choose_option([option("a", "TNT", 3.0, valid_until=None)], None, today=date(2030, 1, 1))

		self.assertEqual(chosen["shippingLabelOfferId"], "a")

	def test_no_date_check_without_today(self):
		self.assertEqual(choose_option(options(), None, today=None)["shippingLabelOfferId"], PARCEL)

	def test_all_expired_is_a_mapping_error(self):
		with self.assertRaises(BolMappingError) as raised:
			choose_option(options(), None, today=date(2026, 11, 10))

		self.assertEqual(raised.exception.reason, "no_option")

	def test_no_options_is_a_mapping_error(self):
		with self.assertRaises(BolMappingError) as raised:
			choose_option([], "TNT", today=TODAY)

		self.assertEqual(raised.exception.reason, "no_option")


class TestRequests(unittest.TestCase):
	LINES = [
		{"order_item_id": "2012345678", "qty": 1, "distribution_party": "BOL"},
		{"order_item_id": "2012345679", "qty": 1, "distribution_party": "BOL"},
		{"order_item_id": "2012345678", "qty": 1, "distribution_party": "BOL"},
	]
	ITEMS = [{"orderItemId": "2012345678", "quantity": 2}, {"orderItemId": "2012345679", "quantity": 1}]

	def test_delivery_options_add_up_quantities_per_order_item(self):
		self.assertEqual(delivery_options_request(self.LINES), {"orderItems": self.ITEMS})

	def test_label_names_the_chosen_offer(self):
		self.assertEqual(
			label_request(self.LINES, PARCEL), {"orderItems": self.ITEMS, "shippingLabelOfferId": PARCEL}
		)

	def test_part_units_are_a_mapping_error(self):
		with self.assertRaises(BolMappingError):
			delivery_options_request([{"order_item_id": "2012345678", "qty": 1.5}])
