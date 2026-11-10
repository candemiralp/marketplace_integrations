# Pure bol invoice mapper tests (story 12): no database.

import copy
import json
import unittest
from datetime import date
from pathlib import Path

from marketplace_integrations.bol.api.errors import BolMappingError
from marketplace_integrations.bol.mappers.bol_invoice import (
	anchor_date,
	differences,
	group_lines,
	invoice_totals,
	invoice_windows,
	issue_date,
	requested_shipments,
	to_purchase_invoice,
	window_start,
)

FIXTURES = Path(__file__).parent / "fixtures"
INVOICES = json.loads((FIXTURES / "bol_invoices.json").read_text())
SPECIFICATION = json.loads((FIXTURES / "bol_invoice_specification.json").read_text())
REQUESTS = json.loads((FIXTURES / "invoice_requests.json").read_text())
TZ = "Europe/Amsterdam"
DEFAULT = "Miscellaneous Expenses - TBOL"
MAP = {"COMMISSION": {"expense_account": "Commission on Sales - TBOL", "item": None}}


def line(kind: str, net: float, vat: float = 0.0, description: str | None = None) -> dict:
	"""A specification line shaped like the demo's."""
	entry = copy.deepcopy(SPECIFICATION["invoiceSpecification"][1])
	entry["item"]["Name"]["value"] = kind
	entry["item"]["Description"] = [{"value": description or f"{kind} omschrijving"}]
	entry["lineExtensionAmount"]["value"] = net
	entry["taxTotal"]["TaxAmount"]["value"] = vat
	return entry


def invoice(net: float, vat: float, **values) -> dict:
	"""A listed invoice (InvoiceListItem) with these totals."""
	item = copy.deepcopy(INVOICES["invoiceListItems"][0])
	total = item["legalMonetaryTotal"]
	total["taxExclusiveAmount"]["amount"] = net
	total["taxInclusiveAmount"]["amount"] = round(net + vat, 2)
	item.update(values)
	return item


def mapped(item: dict, lines: list[dict], account_map: dict = MAP) -> dict:
	return to_purchase_invoice(item, lines, account_map, DEFAULT, precision=2, timezone=TZ)


class TestGrouping(unittest.TestCase):
	def test_demo_specification_is_summed_per_type(self):
		groups = group_lines(SPECIFICATION["invoiceSpecification"])

		self.assertEqual(
			set(groups), {"TURNOVER", "COMMISSION", "OUTBOUND", "PICK_PACK", "STOCK", "SHIPMENT_LABEL"}
		)
		self.assertEqual(groups["COMMISSION"]["count"], 5)
		self.assertAlmostEqual(groups["COMMISSION"]["net"], 210.041322, places=6)
		self.assertAlmostEqual(groups["COMMISSION"]["vat"], 44.108678, places=6)
		self.assertAlmostEqual(groups["TURNOVER"]["net"], -1344.46, places=6)
		self.assertEqual(groups["COMMISSION"]["description"], "Bemiddelingsbijdrage")

	def test_line_without_a_type_is_unknown(self):
		entry = line("COMMISSION", 1)
		del entry["item"]["Name"]

		self.assertEqual(list(group_lines([entry])), ["UNKNOWN"])


class TestPurchaseInvoice(unittest.TestCase):
	def test_one_line_per_type_with_the_mapped_or_default_account(self):
		result = mapped(
			invoice(14.5, 3.05),
			[line("COMMISSION", 10, 2.1), line("COMMISSION", 2.5, 0.53), line("SHIPMENT_LABEL", 2, 0.42)],
		)

		self.assertEqual(
			result["lines"],
			[
				{
					"line_type": "COMMISSION",
					"description": "COMMISSION omschrijving",
					"count": 2,
					"item": None,
					"expense_account": "Commission on Sales - TBOL",
					"qty": 1,
					"rate": 12.5,
				},
				{
					"line_type": "SHIPMENT_LABEL",
					"description": "SHIPMENT_LABEL omschrijving",
					"count": 1,
					"item": None,
					"expense_account": DEFAULT,
					"qty": 1,
					"rate": 2.0,
				},
			],
		)
		self.assertEqual(result["unmapped"], ["SHIPMENT_LABEL"])
		self.assertFalse(result["is_return"])
		self.assertEqual(result["expected"], {"net": 14.5, "vat": 3.05})

	def test_mapped_item_is_passed_on(self):
		account_map = {"STOCK": {"expense_account": "Stock Fees - TBOL", "item": "bol fee"}}

		(result,) = mapped(invoice(1, 0.21), [line("STOCK", 1, 0.21)], account_map)["lines"]

		self.assertEqual((result["item"], result["expense_account"]), ("bol fee", "Stock Fees - TBOL"))

	def test_turnover_stays_off_and_out_of_the_expected_totals(self):
		lines = [line("TURNOVER", -100), line("CORRECTION_TURNOVER", 4.5), line("COMMISSION", 12, 2.52)]

		result = mapped(invoice(-83.5, 2.52), lines)

		self.assertEqual([entry["line_type"] for entry in result["lines"]], ["COMMISSION"])
		self.assertEqual(result["settled"], {"types": ["CORRECTION_TURNOVER", "TURNOVER"], "net": -95.5, "vat": 0})
		self.assertEqual(result["expected"], {"net": 12.0, "vat": 2.52})
		# The fees are what bol charges: a normal invoice, though bol pays out on balance.
		self.assertFalse(result["is_return"])

	def test_rounded_per_type_in_the_currency_precision(self):
		lines = [line("STOCK", 0.00658, 0.001382), line("STOCK", 0.0031, 0.0007)]

		(result,) = mapped(invoice(0.01, 0), lines)["lines"]

		self.assertEqual(result["rate"], 0.01)

	def test_negative_total_is_a_debit_note(self):
		lines = [line("COMPENSATION", -20), line("COMMISSION", 5, 1.05)]

		result = mapped(invoice(-15, 1.05), lines)

		self.assertTrue(result["is_return"])
		# qty -1 on every line; the rate keeps each type's sign relative to the debit note.
		self.assertEqual(
			[(entry["line_type"], entry["qty"], entry["rate"]) for entry in result["lines"]],
			[("COMMISSION", -1, -5.0), ("COMPENSATION", -1, 20.0)],
		)

	def test_only_turnover_has_nothing_to_book(self):
		with self.assertRaises(BolMappingError) as ctx:
			mapped(invoice(-10, 0), [line("TURNOVER", -10)])
		self.assertEqual(ctx.exception.reason, "nothing_to_book")

	def test_demo_invoice(self):
		result = mapped(INVOICES["invoiceListItems"][0], SPECIFICATION["invoiceSpecification"])

		self.assertEqual(result["invoice_id"], "4500022543921")
		self.assertEqual(result["invoice_type"], "ALL_IN_ONE")
		self.assertEqual(result["bill_date"], date(2018, 5, 1))
		self.assertEqual(result["currency"], "EUR")
		self.assertTrue(result["has_pdf"])
		# The demo's list totals don't add up to its specification: bol's -1290.66 minus the turnover.
		self.assertEqual(result["expected"], {"net": 53.8, "vat": 0.0})
		self.assertAlmostEqual(sum(entry["rate"] for entry in result["lines"]), 218.04, places=6)

	def test_no_pdf_media_type(self):
		item = invoice(1, 0.21, invoiceMediaTypes={"availableMediaTypes": ["application/vnd.retailer.v10+json"]})

		self.assertFalse(mapped(item, [line("COMMISSION", 1, 0.21)])["has_pdf"])

	def test_missing_totals_are_a_mapping_error(self):
		item = invoice(1, 0)
		del item["legalMonetaryTotal"]

		with self.assertRaises(BolMappingError) as ctx:
			mapped(item, [line("COMMISSION", 1)])
		self.assertEqual(ctx.exception.reason, "no_totals")

	def test_totals_from_the_list(self):
		self.assertEqual(invoice_totals(invoice(100, 21)), {"net": 100.0, "vat": 21.0, "currency": "EUR"})


class TestDifferences(unittest.TestCase):
	def test_equal_and_within_a_cent_match(self):
		expected = {"net": 12.0, "vat": 2.52}

		self.assertEqual(differences(expected, {"net": 12.0, "vat": 2.52}), [])
		self.assertEqual(differences(expected, {"net": 12.01, "vat": 2.51}), [])

	def test_more_than_a_cent_is_flagged(self):
		self.assertEqual(
			differences({"net": 12.0, "vat": 2.52}, {"net": 12.0, "vat": 2.55}), [("vat", 2.55, 2.52)]
		)
		self.assertEqual(
			differences({"net": -15.0, "vat": 1.05}, {"net": -14.0, "vat": 1.05}), [("net", -14.0, -15.0)]
		)


class TestWindows(unittest.TestCase):
	def test_first_import_reads_30_days_and_the_overlap(self):
		today = date(2026, 11, 10)
		anchor = anchor_date(None, today)

		self.assertEqual(anchor, date(2026, 10, 11))
		self.assertEqual(window_start(anchor), date(2026, 10, 4))

	def test_later_imports_start_7_days_before_the_last(self):
		self.assertEqual(window_start(anchor_date(date(2026, 11, 9), date(2026, 11, 10))), date(2026, 11, 2))

	def test_short_window_is_one_period(self):
		self.assertEqual(
			invoice_windows(date(2026, 11, 2), date(2026, 11, 10)), [(date(2026, 11, 2), date(2026, 11, 10))]
		)

	def test_long_window_is_split_into_31_day_periods_that_share_their_boundary(self):
		windows = invoice_windows(date(2026, 8, 1), date(2026, 11, 10))

		self.assertEqual(
			windows,
			[
				(date(2026, 8, 1), date(2026, 8, 31)),
				(date(2026, 8, 31), date(2026, 9, 30)),
				(date(2026, 9, 30), date(2026, 10, 30)),
				(date(2026, 10, 30), date(2026, 11, 10)),
			],
		)
		self.assertTrue(all((end - start).days <= 30 for start, end in windows))


class TestIssueDate(unittest.TestCase):
	def test_formats(self):
		self.assertEqual(issue_date("2026-11-01", TZ), date(2026, 11, 1))
		self.assertEqual(issue_date("2026-10-31T23:30:00+00:00", TZ), date(2026, 11, 1))
		# Epoch seconds (the demo's issueDate) and milliseconds (its invoicePeriod), in the system time zone.
		self.assertEqual(issue_date(1525125600, TZ), date(2018, 5, 1))
		self.assertEqual(issue_date(1556575200000, TZ), date(2019, 4, 30))
		self.assertIsNone(issue_date(None, TZ))


class TestRequestedShipments(unittest.TestCase):
	def test_shipments_without_the_buyer_details(self):
		self.assertEqual(
			requested_shipments(REQUESTS),
			[
				{"shipment_id": "987654", "order_id": "4691862037"},
				{"shipment_id": "665578", "order_id": "564734839"},
			],
		)

	def test_empty(self):
		self.assertEqual(requested_shipments(None), [])
		self.assertEqual(requested_shipments({"invoiceRequests": [{"status": "OPEN"}]}), [])
