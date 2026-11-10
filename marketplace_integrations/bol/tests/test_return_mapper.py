# Pure return mapper tests (story 11): no database.

import copy
import json
import unittest
from pathlib import Path

from marketplace_integrations.bol.mappers.returns import (
	HANDLING_RESULTS,
	REASON_MAX,
	handling_body,
	is_handled,
	pick_line,
	reason_text,
	unhandled_items,
)

FIXTURES = Path(__file__).parent / "fixtures"
RETURNS = json.loads((FIXTURES / "returns_list.json").read_text())
RETURN = json.loads((FIXTURES / "return.json").read_text())


class TestUnhandledItems(unittest.TestCase):
	def test_list_entry_becomes_one_item_per_rma(self):
		self.assertEqual(
			unhandled_items(RETURNS["returns"]),
			[
				{
					"return_id": "15897410",
					"rma_id": "60283608",
					"order_id": "1044796550",
					"ean": "0634154562956",
					"expected_qty": 1,
					"reason": "Anders, namelijk: Cadeau vonden ze niet leuk",
				}
			],
		)

	def test_handled_items_are_left_out(self):
		# The detail has one handled and one unhandled item.
		self.assertEqual([item["rma_id"] for item in unhandled_items([RETURN])], ["60283608"])

	def test_nothing_listed(self):
		self.assertEqual(unhandled_items([]), [])
		self.assertEqual(unhandled_items(None), [])


class TestReasonText(unittest.TestCase):
	def test_all_parts(self):
		reason = {"mainReason": "Verkeerde maat", "detailedReason": "Te klein", "customerComments": "Valt klein"}
		self.assertEqual(reason_text(reason), "Verkeerde maat — Te klein: Valt klein")

	def test_comment_repeating_the_reason_is_left_out(self):
		reason = {"mainReason": "Verkeerd artikel ontvangen", "customerComments": "Verkeerd artikel ontvangen"}
		self.assertEqual(reason_text(reason), "Verkeerd artikel ontvangen")

	def test_empty_parts(self):
		self.assertEqual(reason_text({"mainReason": "Anders", "detailedReason": None, "customerComments": ""}), "Anders")
		self.assertEqual(reason_text(None), "")

	def test_long_text_is_truncated(self):
		text = reason_text({"mainReason": "Anders", "customerComments": "x" * 500})
		self.assertEqual(len(text), REASON_MAX)
		self.assertTrue(text.endswith("…"))


class TestPickLine(unittest.TestCase):
	def line(self, dn: str, available: float) -> dict:
		return {"delivery_note": dn, "dn_detail": f"{dn}-1", "available": available}

	def test_single_line(self):
		self.assertEqual(pick_line([self.line("DN-1", 2)], 2)["delivery_note"], "DN-1")

	def test_first_line_that_covers_the_quantity(self):
		lines = [self.line("DN-1", 1), self.line("DN-2", 2), self.line("DN-3", 2)]
		self.assertEqual(pick_line(lines, 2)["delivery_note"], "DN-2")

	def test_none_covers_it(self):
		self.assertIsNone(pick_line([self.line("DN-1", 1), self.line("DN-2", 1)], 2))
		self.assertIsNone(pick_line([], 1))


class TestBodyAndState(unittest.TestCase):
	def test_body(self):
		self.assertEqual(
			handling_body("RETURN_RECEIVED", 2), {"handlingResult": "RETURN_RECEIVED", "quantityReturned": 2}
		)

	def test_handling_results_match_the_spec(self):
		spec = json.loads((FIXTURES.parent.parent.parent.parent / "docs/bol-openapi/retailer-api-v10.json").read_text())
		enum = spec["components"]["schemas"]["ReturnRequest"]["properties"]["handlingResult"]["enum"]
		self.assertEqual(list(HANDLING_RESULTS), enum)

	def test_is_handled(self):
		self.assertTrue(is_handled(RETURN, "60283607"))
		self.assertFalse(is_handled(RETURN, "60283608"))
		self.assertFalse(is_handled(RETURN, "1"))
		self.assertFalse(is_handled(None, "60283607"))

	def test_rma_id_compared_as_text(self):
		detail = copy.deepcopy(RETURN)
		detail["returnItems"][0]["rmaId"] = 60283607
		self.assertTrue(is_handled(detail, "60283607"))
