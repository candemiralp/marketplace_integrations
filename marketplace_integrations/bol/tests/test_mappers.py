# Pure mapper tests: no database.

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from marketplace_integrations.bol.api.errors import BolMappingError
from marketplace_integrations.bol.mappers.ean import get_item_ean, gtin_forms, is_valid_gtin13
from marketplace_integrations.bol.mappers.offer import (
	HOLD,
	build_offer,
	diff_offer,
	format_not_for_sale_reasons,
	format_violations,
	offer_status,
)

FIXTURES = Path(__file__).parent / "fixtures"
OFFERS = json.loads((FIXTURES / "offers_page_1.json").read_text())["offers"]


def item(*barcodes):
	rows = [SimpleNamespace(barcode=code, barcode_type=barcode_type) for code, barcode_type in barcodes]
	return SimpleNamespace(name="ITEM-1", barcodes=rows)


class TestGtin13(unittest.TestCase):
	def test_valid(self):
		for code in ("4006381333931", "8712345678906", "5901234123457"):
			self.assertTrue(is_valid_gtin13(code), code)

	def test_wrong_check_digit(self):
		self.assertFalse(is_valid_gtin13("4006381333932"))

	def test_wrong_length(self):
		for code in ("400638133393", "40063813339310", ""):
			self.assertFalse(is_valid_gtin13(code), code)

	def test_non_digits(self):
		for code in ("40063813339X1", "4006381 33931", "４００６３８１３３３９３１", None, 4006381333931):
			self.assertFalse(is_valid_gtin13(code), code)

	def test_leading_zeros_kept(self):
		self.assertTrue(is_valid_gtin13("0000007740404"))
		self.assertFalse(is_valid_gtin13("7740404"))
		self.assertEqual(get_item_ean(item(("0000007740404", "EAN"))), "0000007740404")


class TestGetItemEan(unittest.TestCase):
	def test_one_ean(self):
		self.assertEqual(
			get_item_ean(item(("4006381333931", "EAN"), ("ABC-123", "CODE-39"))), "4006381333931"
		)

	def test_no_ean(self):
		for barcodes in ((), (("012345678905", "UPC-A"),), (("4006381333931", ""),)):
			with self.assertRaises(BolMappingError) as ctx:
				get_item_ean(item(*barcodes))
			self.assertEqual(ctx.exception.reason, "missing")

	def test_two_eans(self):
		with self.assertRaises(BolMappingError) as ctx:
			get_item_ean(item(("4006381333931", "EAN"), ("8712345678906", "EAN")))
		self.assertEqual(ctx.exception.reason, "several")
		self.assertIn("8712345678906", str(ctx.exception))

	def test_invalid_ean(self):
		for code in ("4006381333932", "12345678"):
			with self.assertRaises(BolMappingError) as ctx:
				get_item_ean(item((code, "EAN")))
			self.assertEqual(ctx.exception.reason, "invalid")


class TestGtinForms(unittest.TestCase):
	def test_zero_padded_forms(self):
		self.assertEqual(gtin_forms("0012345678905"), ["00012345678905", "0012345678905", "012345678905"])

	def test_full_length_code_has_no_shorter_form(self):
		self.assertEqual(gtin_forms("4006381333931"), ["04006381333931", "4006381333931"])


class TestOfferStatus(unittest.TestCase):
	def test_for_sale_in_any_country(self):
		self.assertEqual(offer_status(OFFERS[0]), "For sale")

	def test_on_hold_wins(self):
		self.assertEqual(offer_status({**OFFERS[0], "onHoldByRetailer": True}), "On hold")

	def test_not_for_sale(self):
		offer = {**OFFERS[0], "countryAvailabilities": [{"countryCode": "NL", "forSale": False}]}
		self.assertEqual(offer_status(offer), "Not for sale")
		self.assertEqual(offer_status({"offerId": "x", "onHoldByRetailer": False}), "Not for sale")


OPERATOR = "0c6573a2-a80c-48b7-a03e-d5939f1173fa"
SHIRT = {"item_code": "SHIRT-RED-M", "ean": "0000007740404"}


def offer(**overrides) -> dict:
	return build_offer(
		**{
			"item": SHIRT,
			"price": 9.99,
			"eo_id": OPERATOR,
			"min_days": 1,
			"max_days": 2,
			"stock": 0,
			**overrides,
		}
	)


class TestBuildOffer(unittest.TestCase):
	def test_payload_shape(self):
		self.assertEqual(
			offer(),
			{
				"ean": "0000007740404",
				"reference": "SHIRT-RED-M",
				"economicOperatorId": OPERATOR,
				"onHoldByRetailer": False,
				"condition": {"category": "NEW"},
				"pricing": {"bundlePrices": [{"quantity": 1, "unitPrice": 9.99}]},
				"fulfilment": {
					"method": "FBR",
					"schedule": "MY_DELIVERY_PROMISE",
					"deliveryPromise": {"minimumDaysToCustomer": 1, "maximumDaysToCustomer": 2},
				},
				"stock": {"amount": 0, "managedByRetailer": False},
			},
		)

	def test_one_bundle_price_rounded_to_cents(self):
		self.assertEqual(
			offer(price=12.3456)["pricing"], {"bundlePrices": [{"quantity": 1, "unitPrice": 12.35}]}
		)

	def test_price_limits(self):
		self.assertEqual(offer(price=1)["pricing"]["bundlePrices"][0]["unitPrice"], 1)
		self.assertEqual(offer(price=9999)["pricing"]["bundlePrices"][0]["unitPrice"], 9999)

	def test_price_out_of_range(self):
		for price in (0, 0.99, 9999.01, None):
			with self.subTest(price=price), self.assertRaises(BolMappingError) as ctx:
				offer(price=price)
			self.assertEqual(ctx.exception.reason, "price_range")

	def test_min_days_above_max_days(self):
		with self.assertRaises(BolMappingError) as ctx:
			offer(min_days=3, max_days=2)
		self.assertEqual(ctx.exception.reason, "delivery_days")

	def test_long_item_code_is_cut_to_bols_reference_limit(self):
		self.assertEqual(offer(item={**SHIRT, "item_code": "X" * 140})["reference"], "X" * 100)


class TestDiffOffer(unittest.TestCase):
	def current(self, **overrides) -> dict:
		# As bol returns it: with its own extra fields.
		current = {
			**offer(),
			"offerId": "6ff736b5-cdd0-4150-8c67-78269ee986f5",
			"stock": {"amount": 42, "correctedStock": 40, "managedByRetailer": False},
			"countryAvailabilities": [{"countryCode": "NL", "forSale": True}],
			"fulfilment": {
				"method": "FBR",
				"schedule": "MY_DELIVERY_PROMISE",
				"profileId": "0c6573a2-a80c-48b7-a03e-d5939f1173f1",
				"deliveryPromise": {
					"minimumDaysToCustomer": 1,
					"maximumDaysToCustomer": 2,
					"ultimateOrderTime": "14:00",
				},
			},
		}
		current.update(overrides)
		return current

	def test_unchanged_offer_needs_no_patch(self):
		self.assertEqual(diff_offer(self.current(), offer()), {})

	def test_changed_price_only(self):
		self.assertEqual(
			diff_offer(self.current(), offer(price=11.5)),
			{"pricing": {"bundlePrices": [{"quantity": 1, "unitPrice": 11.5}]}},
		)

	def test_same_price_as_int_and_float(self):
		current = self.current(pricing={"bundlePrices": [{"quantity": 1, "unitPrice": 10}]})
		self.assertEqual(diff_offer(current, offer(price=10.0)), {})

	def test_changed_operator_and_delivery_promise(self):
		patch = diff_offer(self.current(economicOperatorId=None), offer(max_days=3))
		self.assertEqual(set(patch), {"economicOperatorId", "fulfilment"})
		self.assertEqual(patch["fulfilment"]["deliveryPromise"]["maximumDaysToCustomer"], 3)

	def test_bol_delivery_promise_is_replaced(self):
		current = self.current(fulfilment={"method": "FBR", "schedule": "BOL_DELIVERY_PROMISE"})
		self.assertEqual(set(diff_offer(current, offer())), {"fulfilment"})

	def test_stock_ean_and_condition_are_never_patched(self):
		current = self.current(ean="other", condition={"category": "SECONDHAND"})
		self.assertEqual(diff_offer(current, offer(stock=7)), {})

	def test_hold(self):
		self.assertEqual(diff_offer(self.current(), HOLD), {"onHoldByRetailer": True})
		self.assertEqual(diff_offer(self.current(onHoldByRetailer=True), HOLD), {})

	def test_back_on_sale(self):
		self.assertEqual(
			diff_offer(self.current(onHoldByRetailer=True), offer()), {"onHoldByRetailer": False}
		)


class TestFormatting(unittest.TestCase):
	def test_not_for_sale_reasons(self):
		response = {
			"offerId": "x",
			"countries": [
				{
					"countryCode": "NL",
					"reasons": [{"category": "PRICE", "code": "4000-1", "description": "Price too high"}],
				},
				{
					"countryCode": "BE",
					"reasons": [
						{
							"category": "CONTENT",
							"code": "4004-2",
							"description": "Essential product information is missing",
							"missingAttributes": [{"name": "Title"}, {"name": "Colour"}],
						}
					],
				},
			],
		}
		self.assertEqual(
			format_not_for_sale_reasons(response),
			"NL: Price too high (4000-1)\nBE: Essential product information is missing (4004-2) – Title, Colour",
		)

	def test_no_reasons(self):
		self.assertEqual(format_not_for_sale_reasons(None), "")
		self.assertEqual(format_not_for_sale_reasons({"offerId": "x"}), "")

	def test_violations(self):
		violations = [
			{"name": "pricing.bundlePrices[0].unitPrice", "reason": "must be at least 1"},
			{"name": "ean", "reason": "invalid"},
		]
		self.assertEqual(
			format_violations(violations),
			"pricing.bundlePrices[0].unitPrice: must be at least 1\nean: invalid",
		)
