# Pure mapper tests: no database.

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from marketplace_integrations.bol.api.errors import BolMappingError
from marketplace_integrations.bol.mappers.ean import get_item_ean, gtin_forms, is_valid_gtin13
from marketplace_integrations.bol.mappers.offer import offer_status

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
		self.assertEqual(get_item_ean(item(("4006381333931", "EAN"), ("ABC-123", "CODE-39"))), "4006381333931")

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
