# Pure shipment mapper tests (story 08): no database.

import json
import unittest
from pathlib import Path

from marketplace_integrations.bol.api.errors import BolMappingError
from marketplace_integrations.bol.mappers.shipment import build_shipment, candidates, is_ours, quantities

FIXTURES = Path(__file__).parent / "fixtures"
LIST = json.loads((FIXTURES / "shipments_list.json").read_text())["shipments"]
DETAIL = json.loads((FIXTURES / "shipment.json").read_text())


def line(order_item_id: str, qty, party: str = "RETAILER") -> dict:
	return {"order_item_id": order_item_id, "qty": qty, "distribution_party": party}


class TestBuildShipment(unittest.TestCase):
	def test_lines_of_one_order_item_are_added_up(self):
		lines = [line("2012345678", 1), line("2012345679", 1), line("2012345678", 1.0)]

		body = build_shipment(lines, "MAT-DN-2026-00001", "TNT", "3SBOL0987654321")

		self.assertEqual(
			body["orderItems"],
			[{"orderItemId": "2012345678", "quantity": 2}, {"orderItemId": "2012345679", "quantity": 1}],
		)
		self.assertIsInstance(body["orderItems"][0]["quantity"], int)

	def test_reference_is_the_delivery_note(self):
		body = build_shipment([line("2012345678", 1)], "MAT-DN-2026-00001", "TNT", None)

		self.assertEqual(body["shipmentReference"], "MAT-DN-2026-00001")

	def test_own_transport_with_track_and_trace(self):
		body = build_shipment([line("2012345678", 1)], "DN-1", "TNT", "3SBOL0987654321")

		self.assertEqual(body["transport"], {"transporterCode": "TNT", "trackAndTrace": "3SBOL0987654321"})
		self.assertNotIn("shippingLabelId", body)

	def test_empty_track_and_trace_is_sent_as_null(self):
		body = build_shipment([line("2012345678", 1)], "DN-1", "BRIEFPOST", "")

		self.assertEqual(body["transport"], {"transporterCode": "BRIEFPOST", "trackAndTrace": None})

	def test_bol_label_replaces_the_transport(self):
		label = "d4c50077-0c19-435f-9bee-1b30b9f4ba1a"

		body = build_shipment([line("2012345678", 1, "BOL")], "DN-1", "TNT", "3SBOL0987654321", label)

		self.assertEqual(body["shippingLabelId"], label)
		self.assertNotIn("transport", body)

	def test_no_transporter_and_no_label(self):
		with self.assertRaises(BolMappingError) as raised:
			build_shipment([line("2012345678", 1)], "DN-1", None, "3SBOL0987654321")
		self.assertEqual(raised.exception.reason, "transporter")


class TestQuantities(unittest.TestCase):
	def test_part_units_are_refused(self):
		with self.assertRaises(BolMappingError) as raised:
			quantities([line("2012345678", 1.5)])
		self.assertEqual(raised.exception.reason, "quantity")

	def test_zero_is_refused(self):
		with self.assertRaises(BolMappingError):
			quantities([line("2012345678", 0)])

	def test_at_most_100_order_items(self):
		quantities([line(str(i), 1) for i in range(100)])

		with self.assertRaises(BolMappingError) as raised:
			quantities([line(str(i), 1) for i in range(101)])
		self.assertEqual(raised.exception.reason, "too_many_items")


class TestMatching(unittest.TestCase):
	def test_candidates_by_reference_or_order_items(self):
		found = candidates(LIST, "WSR1096399593", {"6107434013"}, taken=set())

		self.assertEqual([s["shipmentId"] for s in found], ["914587795", "953316694"])

	def test_shipments_of_other_delivery_notes_are_no_candidates(self):
		self.assertEqual(candidates(LIST, "DN-1", {"6107434013"}, taken={"914587795"}), [])

	def test_other_order_items_are_no_candidates(self):
		self.assertEqual(candidates(LIST, "DN-1", {"6107434013", "6107434014"}, taken=set()), [])

	def test_ours_by_reference(self):
		self.assertTrue(is_ours(DETAIL, "Shipment2", {"9999": 5}, other_references=set()))

	def test_ours_by_items_and_quantities(self):
		self.assertTrue(is_ours(DETAIL, "DN-1", {"6107331383": 1}, other_references=set()))
		self.assertFalse(is_ours(DETAIL, "DN-1", {"6107331383": 2}, other_references=set()))

	def test_shipment_of_another_delivery_note_is_never_ours(self):
		self.assertFalse(is_ours(DETAIL, "DN-1", {"6107331383": 1}, other_references={"Shipment2"}))
