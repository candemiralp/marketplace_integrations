# Pure cancellation mapper tests (story 10): no database.

import copy
import json
import unittest
from pathlib import Path

from marketplace_integrations.bol.mappers.cancellation import (
	REASON_CODES,
	cancellation_body,
	changes,
	has_cancellation_fields,
)

ORDER = json.loads((Path(__file__).parent / "fixtures" / "order.json").read_text())
SHIRT_ITEM, BAG_ITEM = "2012345678", "2012345679"


def order(**lines) -> dict:
	"""The fixture order with line overrides: order(shirt={...}, bag={...})."""
	result = copy.deepcopy(ORDER)
	shirt, bag = result["orderItems"]
	shirt.update(lines.get("shirt") or {})
	bag.update(lines.get("bag") or {})
	return result


def rows(shirt_cancelled=0, shirt_requested=0, bag_cancelled=0, bag_requested=0) -> dict:
	return {
		SHIRT_ITEM: {"bol_qty_cancelled": shirt_cancelled, "bol_cancellation_requested": shirt_requested},
		BAG_ITEM: {"bol_qty_cancelled": bag_cancelled, "bol_cancellation_requested": bag_requested},
	}


class TestChanges(unittest.TestCase):
	def test_nothing_new(self):
		self.assertEqual(changes(order(), rows()), ({}, []))

	def test_more_cancelled_at_bol(self):
		self.assertEqual(changes(order(shirt={"quantityCancelled": 1}), rows()), ({SHIRT_ITEM: 1}, []))

	def test_cancelled_already_applied_is_no_change(self):
		self.assertEqual(changes(order(shirt={"quantityCancelled": 1}), rows(shirt_cancelled=1)), ({}, []))
		# Never adds units back.
		self.assertEqual(changes(order(), rows(shirt_cancelled=1)), ({}, []))

	def test_new_request_on_an_open_line(self):
		self.assertEqual(changes(order(bag={"cancellationRequest": True}), rows()), ({}, [BAG_ITEM]))

	def test_request_already_recorded(self):
		self.assertEqual(changes(order(bag={"cancellationRequest": True}), rows(bag_requested=1)), ({}, []))

	def test_request_on_a_line_with_nothing_open_is_no_request(self):
		cancelled = order(bag={"cancellationRequest": True, "quantityCancelled": 1})
		self.assertEqual(changes(cancelled, rows()), ({BAG_ITEM: 1}, []))
		shipped = order(bag={"cancellationRequest": True, "quantityShipped": 1})
		self.assertEqual(changes(shipped, rows()), ({}, []))

	def test_lines_not_on_the_sales_order_are_left_out(self):
		result = changes(order(bag={"quantityCancelled": 1, "cancellationRequest": True}), {SHIRT_ITEM: {}})
		self.assertEqual(result, ({}, []))

	def test_works_on_a_list_entry(self):
		listed = {
			"orderId": "A1",
			"orderItems": [
				{
					"orderItemId": SHIRT_ITEM,
					"quantity": 2,
					"quantityShipped": 0,
					"quantityCancelled": 2,
					"cancellationRequest": True,
				}
			],
		}
		self.assertEqual(changes(listed, rows()), ({SHIRT_ITEM: 2}, []))


class TestFields(unittest.TestCase):
	def test_detail_and_list_entry_have_them(self):
		self.assertTrue(has_cancellation_fields(ORDER))

	def test_missing_or_empty_items_have_not(self):
		self.assertFalse(has_cancellation_fields({"orderId": "A1", "orderItems": []}))
		self.assertFalse(has_cancellation_fields({"orderId": "A1"}))
		self.assertFalse(
			has_cancellation_fields({"orderItems": [{"orderItemId": SHIRT_ITEM, "quantity": 1}]})
		)


class TestBody(unittest.TestCase):
	def test_one_order_item_per_request(self):
		self.assertEqual(
			cancellation_body(SHIRT_ITEM, "OUT_OF_STOCK"),
			{"orderItems": [{"orderItemId": SHIRT_ITEM, "reasonCode": "OUT_OF_STOCK"}]},
		)

	def test_reason_codes_are_the_specs(self):
		self.assertEqual(len(REASON_CODES), 12)
		self.assertIn("REQUESTED_BY_CUSTOMER", REASON_CODES)
		self.assertIn("OTHER", REASON_CODES)
