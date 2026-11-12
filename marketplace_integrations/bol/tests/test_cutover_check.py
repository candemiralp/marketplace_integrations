# The cutover order check (smoke.compare, story 15): bol's orders vs the Sales Orders. No DB, no bol.

import unittest

from marketplace_integrations.bol.smoke import compare


def _order(order_id: str, *items: tuple[int, int]) -> dict:
	"""A ReducedOrder with (quantity, quantityCancelled) per item."""
	return {
		"orderId": order_id,
		"orderItems": [{"quantity": qty, "quantityCancelled": cancelled} for qty, cancelled in items],
	}


def _so(name: str, order_id: str, docstatus: int = 1) -> dict:
	return {"name": name, "bol_order_id": order_id, "docstatus": docstatus}


class TestCompare(unittest.TestCase):
	def test_every_order_imported_once(self):
		report = compare(
			{"A": _order("A", (1, 0)), "B": _order("B", (2, 0))}, [_so("SO-1", "A"), _so("SO-2", "B")]
		)
		self.assertEqual(report["imported"], ["A", "B"])
		for key in ("missing", "cancelled_before_import", "extra"):
			self.assertEqual(report[key], [], key)
		self.assertEqual(report["duplicates"], {})

	def test_missing_orders_and_orders_cancelled_before_import(self):
		report = compare(
			{"A": _order("A", (2, 1)), "C": _order("C", (1, 1), (2, 2))},
			[],
		)
		self.assertEqual(report["missing"], ["A"])
		self.assertEqual(report["cancelled_before_import"], ["C"])

	def test_duplicates_ignore_cancelled_sales_orders(self):
		report = compare(
			{"A": _order("A", (1, 0)), "B": _order("B", (1, 0))},
			[
				_so("SO-1", "A"),
				_so("SO-2", "A", docstatus=0),
				_so("SO-3", "B", docstatus=2),
				_so("SO-4", "B"),
			],
		)
		self.assertEqual(report["duplicates"], {"A": ["SO-1", "SO-2"]})
		# A Sales Order cancelled because bol cancelled the order still counts as imported.
		self.assertEqual(report["imported"], ["A", "B"])

	def test_extra_sales_orders_bol_does_not_list(self):
		report = compare({"A": _order("A", (1, 0))}, [_so("SO-1", "A"), _so("SO-9", "Z")])
		self.assertEqual(report["extra"], ["Z"])
