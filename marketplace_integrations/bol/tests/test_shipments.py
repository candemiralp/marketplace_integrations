# Shipment confirmation (story 08) with bol's shipment, transport and process-status endpoints faked in memory.
# Everything stays in the test transaction.

import copy
import html
import itertools
import json
from pathlib import Path
from unittest.mock import patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from marketplace_integrations.bol import orders, process_status, shipments
from marketplace_integrations.bol.api.errors import (
	BolAuthError,
	BolNotFoundError,
	BolServerError,
	BolTransientError,
	BolValidationError,
)
from marketplace_integrations.bol.mappers.shipment import TRANSPORTER_CODES
from marketplace_integrations.bol.tests.utils import (
	BOL_MANAGER,
	discard_pending_logs,
	make_bol_manager,
	make_catalog_masters,
	make_item,
	make_order_masters,
)
from marketplace_integrations.setup.custom_fields import CUSTOM_FIELDS

FIXTURES = Path(__file__).parent / "fixtures"
ORDER = json.loads((FIXTURES / "order.json").read_text())
ACCEPTED = json.loads((FIXTURES / "shipment_process_status.json").read_text())
SHIRT, BAG = "_Test bol ship shirt", "_Test bol ship bag"
SHIRT_EAN, BAG_EAN = "4006381333931", "8712345678906"
SHIRT_ITEM, BAG_ITEM = "2012345678", "2012345679"
TRACK = "3SBOL0987654321"
TZ = "Europe/Amsterdam"
DN = "Delivery Note"


class FakeShipments:
	"""bol's shipment, transport and process-status endpoints in memory, called like BolClient.request.

	bol creates a shipment when it accepts the POST (in reality a little later; the tests don't need the gap).
	"""

	def __init__(self):
		self.settings = frappe._dict(environment="Production")
		self.shipments = {}  # shipmentId -> Shipment detail
		self.processes = {}  # processStatusId -> ProcessStatus
		self.entities = {}  # processStatusId -> entityId bol reports on SUCCESS
		self.order_of = {}  # orderItemId -> orderId
		# (method, path) -> (error, done): raised before handling, or after it when `done` (bol did the work).
		self.errors = {}
		# The demo's list leaves out shipmentReference.
		self.list_references = True
		self.calls = []

	def request(self, method, path, *, version, json=None, params=None, **kwargs):
		assert version == shipments.VERSION, version
		if (method, path) == ("POST", process_status.PATH):
			ids = [query["processStatusId"] for query in json["processStatusQueries"]]
			return {"processStatuses": [copy.deepcopy(self.processes[i]) for i in ids if i in self.processes]}
		self.calls.append((method, path, copy.deepcopy(json), params))
		error, done = self.errors.pop((method, path), (None, False))
		if error and not done:
			raise error
		result = self._handle(method, path, json, params)
		if error:
			raise error
		return result

	def ship(self, body: dict, **transport) -> str:
		shipment_id = str(953266576 + len(self.shipments))
		items = body["orderItems"]
		self.shipments[shipment_id] = {
			"shipmentId": shipment_id,
			"shipmentReference": body.get("shipmentReference"),
			"order": {"orderId": self.order_of[items[0]["orderItemId"]]},
			"shipmentItems": [
				{"orderItemId": i["orderItemId"], "quantity": i["quantity"], "quantityShipped": i["quantity"]}
				for i in items
			],
			"transport": {
				"transportId": str(356988715 + len(self.shipments)),
				**(body.get("transport") or {}),
				**transport,
			},
		}
		return shipment_id

	def finish(self, status: str = "SUCCESS", error_message: str | None = None) -> str:
		"""bol finishes the latest process. Returns its id."""
		process_id = list(self.processes)[-1]
		self.processes[process_id].update(
			status=status,
			entityId=self.entities[process_id] if status == "SUCCESS" else None,
			errorMessage=error_message,
		)
		return process_id

	def posts(self) -> list[dict]:
		return [
			body for method, path, body, _params in self.calls if (method, path) == ("POST", shipments.PATH)
		]

	def puts(self) -> list[tuple[str, dict]]:
		return [(path, body) for method, path, body, _params in self.calls if method == "PUT"]

	def _handle(self, method, path, body, params):
		if (method, path) == ("GET", shipments.PATH):
			return {
				"shipments": [
					self._reduced(s)
					for s in self.shipments.values()
					if s["order"]["orderId"] == params["order-id"]
				]
			}
		if method == "GET":
			shipment_id = path.rsplit("/", 1)[1]
			if shipment_id not in self.shipments:
				raise BolNotFoundError("Not found")
			return copy.deepcopy(self.shipments[shipment_id])
		if (method, path) == ("POST", shipments.PATH):
			return self._accept("CREATE_SHIPMENT", self.ship(body))
		if method == "PUT":
			transport_id = path.rsplit("/", 1)[1]
			for shipment in self.shipments.values():
				if shipment["transport"]["transportId"] == transport_id:
					shipment["transport"].update({k: v for k, v in body.items() if v})
			return self._accept("CHANGE_TRANSPORT", transport_id)
		raise AssertionError(f"Unexpected call {method} {path}")

	def _reduced(self, shipment: dict) -> dict:
		reduced = {
			"shipmentId": shipment["shipmentId"],
			"order": shipment["order"],
			"shipmentItems": [
				{"orderItemId": i["orderItemId"], "ean": "8712345678906"} for i in shipment["shipmentItems"]
			],
			"transport": {"transportId": shipment["transport"]["transportId"]},
		}
		if self.list_references:
			reduced["shipmentReference"] = shipment["shipmentReference"]
		return reduced

	def _accept(self, event_type: str, entity_id: str) -> dict:
		process_id = f"ps-ship-{len(self.processes) + 1}"
		self.processes[process_id] = {**ACCEPTED, "processStatusId": process_id, "eventType": event_type}
		self.entities[process_id] = entity_id
		return copy.deepcopy(self.processes[process_id])


class ShipmentTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.masters = make_order_masters()

	def setUp(self):
		self.bol = FakeShipments()
		self.settings = frappe._dict(
			enabled=1, push_shipments=1, default_transporter_code="TNT", require_track_and_trace=1
		)
		self._start(patch.object(shipments, "get_client", return_value=self.bol))
		self._start(patch.object(process_status, "get_client", return_value=self.bol))
		self._start(patch.object(shipments, "_settings", return_value=self.settings))
		self.enqueued = self._start(patch.object(shipments, "enqueue_confirm"))
		self.transport_enqueued = self._start(patch.object(shipments, "enqueue_transport"))
		# Jobs commit per record; keep the work in this transaction so the rollback undoes it.
		self._start(patch.object(frappe.db, "commit"))
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		make_catalog_masters()
		make_item(SHIRT, SHIRT_EAN, valuation_rate=10)
		make_item(BAG, BAG_EAN, valuation_rate=5)
		self.order_ids = itertools.count(1)

	def _start(self, patcher):
		self.addCleanup(patcher.stop)
		return patcher.start()

	def _sales_order(
		self, order_id: str | None = None, party: str = "RETAILER", second_party: str | None = None
	) -> str:
		order = copy.deepcopy(ORDER)
		order["orderId"] = order_id or f"A{next(self.order_ids)}"
		for line, line_party in zip(order["orderItems"], (party, second_party or party), strict=True):
			line["fulfilment"]["distributionParty"] = line_party
			self.bol.order_of[line["orderItemId"]] = order["orderId"]
		settings = frappe._dict(import_orders_placed_after=None, submit_orders=1, **self.masters)
		with patch.object(orders, "get_system_timezone", return_value=TZ):
			return orders.create_sales_order(order, settings)

	def _delivery_note(
		self, so_name: str | None = None, submit: bool = True, track_and_trace=TRACK, **values
	):
		dn = make_delivery_note(so_name or self._sales_order())
		dn.update({"bol_track_and_trace": track_and_trace, **values})
		dn.insert()
		for kind in ("shipment", "shipment_unknown"):
			self.addCleanup(frappe.cache.delete_value, f"bol:failed:{kind}:{dn.name}")
		if submit:
			dn.submit()
		return dn

	def _confirmed_dn(self, **values):
		dn = self._delivery_note(**values)
		shipments.confirm_shipment(dn.name)
		self._finish()
		return self._reload(dn)

	def _finish(self, status: str = "SUCCESS", error_message: str | None = None):
		process_status.check_one(self.bol.finish(status, error_message))

	def _reload(self, dn):
		dn.reload()
		return dn

	def _state(self, dn) -> tuple:
		return frappe.db.get_value(DN, dn.name, ["bol_shipment_status", "bol_shipment_id"])

	def _comments(self, dn) -> list[str]:
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": DN, "reference_name": dn.name, "comment_type": "Comment"},
			order_by="creation asc",
			pluck="content",
		)
		return [html.unescape(content) for content in comments]

	def _error_logs(self) -> list[dict]:
		return [log for log in frappe.local.bol_pending_logs or [] if log["status"] == "Error"]

	def _make_old(self, dn, minutes: int = shipments.SWEEP_AFTER_MINUTES + 1):
		frappe.db.set_value(
			DN, dn.name, "modified", add_to_date(now_datetime(), minutes=-minutes), update_modified=False
		)


class TestValidation(ShipmentTestCase):
	def test_draft_gets_the_bol_order_and_the_default_transporter(self):
		dn = self._delivery_note(submit=False)

		self.assertEqual((dn.bol_order_id, dn.bol_transporter_code), ("A1", "TNT"))
		self.assertFalse(dn.bol_shipment_status)

	def test_switched_off_checks_and_sends_nothing(self):
		self.settings.push_shipments = 0

		dn = self._delivery_note(track_and_trace=None)

		self.assertEqual(dn.bol_order_id, "A1")
		self.assertEqual(self._state(dn), ("", None))
		self.enqueued.assert_not_called()

	def test_two_bol_orders_are_refused(self):
		dn = make_delivery_note(self._sales_order("A1"))
		dn = make_delivery_note(self._sales_order("A2"), target_doc=dn)

		with self.assertRaisesRegex(frappe.ValidationError, "one Delivery Note per bol order"):
			dn.insert()

	def test_bol_and_other_items_are_refused(self):
		other = self._sales_order("A2")
		frappe.db.set_value("Sales Order", other, "bol_order_id", None)
		dn = make_delivery_note(other, target_doc=make_delivery_note(self._sales_order("A1")))

		with self.assertRaisesRegex(frappe.ValidationError, "Create separate Delivery Notes"):
			dn.insert()

	def test_mixed_distribution_parties_are_refused(self):
		so = self._sales_order(party="RETAILER", second_party="BOL")

		with self.assertRaisesRegex(frappe.ValidationError, "shipped by bol"):
			self._delivery_note(so, submit=False)

	def test_line_without_a_bol_order_item_is_refused(self):
		dn = make_delivery_note(self._sales_order())
		frappe.db.set_value("Sales Order Item", dn.items[1].so_detail, "bol_order_item_id", None)

		with self.assertRaisesRegex(frappe.ValidationError, "isn't linked to a line of bol order A1"):
			dn.insert()

	def test_part_units_are_refused(self):
		dn = make_delivery_note(self._sales_order())
		dn.items[0].qty = 1.5

		with self.assertRaisesRegex(frappe.ValidationError, "whole units"):
			dn.insert()

	def test_missing_track_and_trace_blocks_submit(self):
		dn = self._delivery_note(submit=False, track_and_trace=" ")

		with self.assertRaisesRegex(frappe.ValidationError, "Track & Trace"):
			dn.submit()

	def test_track_and_trace_can_be_optional(self):
		self.settings.require_track_and_trace = 0

		dn = self._delivery_note(track_and_trace=None)

		self.assertEqual(self._state(dn), ("Pending", None))

	def test_items_bol_ships_need_no_track_and_trace(self):
		dn = self._delivery_note(self._sales_order(party="BOL"), track_and_trace=None)

		self.assertEqual(self._state(dn)[0], "Pending")
		# No transporter of ours either: bol's label (story 09) names it.
		self.assertFalse(dn.bol_transporter_code)

	def test_returns_are_left_alone(self):
		dn = self._delivery_note()
		self.enqueued.reset_mock()

		ret = make_sales_return(dn.name)
		ret.insert()
		ret.submit()

		self.assertFalse(ret.bol_shipment_status)
		self.enqueued.assert_not_called()

	def test_transporter_options_match_the_settings(self):
		fields = {f["fieldname"]: f for f in CUSTOM_FIELDS[DN]}
		settings_field = frappe.get_meta("Bol Settings").get_field("default_transporter_code")

		self.assertEqual(fields["bol_transporter_code"]["options"].split("\n")[1:], list(TRANSPORTER_CODES))
		self.assertEqual(settings_field.options.split("\n"), list(TRANSPORTER_CODES))


class TestConfirm(ShipmentTestCase):
	def test_submit_marks_pending_and_queues_the_confirmation(self):
		dn = self._delivery_note()

		self.assertEqual(self._state(dn), ("Pending", None))
		self.enqueued.assert_called_once_with(dn.name)
		self.assertEqual(self.bol.calls, [])

	def test_success_confirms_with_bols_shipment_id(self):
		dn = self._delivery_note()

		shipments.confirm_shipment(dn.name)

		(body,) = self.bol.posts()
		self.assertEqual(
			body,
			{
				"orderItems": [
					{"orderItemId": SHIRT_ITEM, "quantity": 2},
					{"orderItemId": BAG_ITEM, "quantity": 1},
				],
				"shipmentReference": dn.name,
				"transport": {"transporterCode": "TNT", "trackAndTrace": TRACK},
			},
		)
		process = frappe.get_doc(process_status.DOCTYPE, "ps-ship-1")
		self.assertEqual(
			(process.handler, process.reference_doctype, process.reference_name), ("shipment", DN, dn.name)
		)
		self.assertEqual(
			frappe.parse_json(process.payload),
			{"order_item_ids": [SHIRT_ITEM, BAG_ITEM], "track_and_trace": TRACK},
		)
		self.assertEqual(self._state(dn), ("Pending", None))

		self._finish()

		shipment_id = self.bol.entities["ps-ship-1"]
		self.assertEqual(self._state(dn), ("Confirmed", shipment_id))
		self.assertIn(shipment_id, self._comments(dn)[-1])
		self.assertEqual(self._error_logs(), [])

	def test_lines_of_one_order_item_are_sent_as_one(self):
		frappe.db.set_single_value("Selling Settings", "allow_multiple_items", 1)
		dn = make_delivery_note(self._sales_order())
		dn.items[0].qty = 1
		dn.append("items", {**dn.items[0].as_dict(), "name": None, "idx": None})
		dn.bol_track_and_trace = TRACK
		dn.insert()
		dn.submit()

		shipments.confirm_shipment(dn.name)

		(body,) = self.bol.posts()
		self.assertEqual(
			body["orderItems"],
			[{"orderItemId": SHIRT_ITEM, "quantity": 2}, {"orderItemId": BAG_ITEM, "quantity": 1}],
		)

	def test_failure_at_bol_marks_failed_with_bols_message(self):
		dn = self._delivery_note()
		shipments.confirm_shipment(dn.name)

		self._finish("FAILURE", "Quantity exceeds open quantity")

		self.assertEqual(self._state(dn), ("Failed", None))
		self.assertTrue(any("Quantity exceeds open quantity" in c for c in self._comments(dn)))
		self.assertEqual(len(self._error_logs()), 1)

	def test_rejected_request_is_failed_with_the_violations(self):
		dn = self._delivery_note()
		violations = [{"name": "orderItems[0].quantity", "reason": "Quantity exceeds the open quantity."}]
		self.bol.errors[("POST", shipments.PATH)] = (
			BolValidationError("Bad Request", "Request contains invalid value(s)", violations),
			False,
		)

		shipments.confirm_shipment(dn.name)

		self.assertEqual(self._state(dn), ("Failed", None))
		self.assertIn("orderItems[0].quantity: Quantity exceeds the open quantity.", self._comments(dn)[-1])
		self.assertEqual(len(self._error_logs()), 1)
		self.assertFalse(frappe.db.exists(process_status.DOCTYPE, {"reference_name": dn.name}))

	def test_auth_error_is_failed_never_confirmed(self):
		dn = self._delivery_note()
		self.bol.errors[("POST", shipments.PATH)] = (BolAuthError("bol denied access (HTTP 403)"), False)

		shipments.confirm_shipment(dn.name)

		self.assertEqual(self._state(dn), ("Failed", None))
		(log,) = self._error_logs()
		self.assertIn("BolAuthError", log["traceback"])
		self.assertEqual(log["bol_reference_name"], dn.name)

	def test_ambiguous_post_then_reconcile_finds_the_shipment(self):
		dn = self._delivery_note()
		self.bol.errors[("POST", shipments.PATH)] = (BolTransientError("ReadTimeout", ambiguous=True), True)

		shipments.confirm_shipment(dn.name)

		self.assertEqual(self._state(dn), ("Unknown", None))
		self.assertIn("bol may have received the shipment", self._comments(dn)[-1])

		shipments.confirm_shipment(dn.name)

		(shipment_id,) = self.bol.shipments
		self.assertEqual(self._state(dn), ("Confirmed", shipment_id))
		self.assertEqual(len(self.bol.posts()), 1)

	def test_ambiguous_post_that_bol_never_got_is_sent_again(self):
		dn = self._delivery_note()
		self.bol.errors[("POST", shipments.PATH)] = (BolTransientError("ReadTimeout", ambiguous=True), False)
		shipments.confirm_shipment(dn.name)

		shipments.confirm_shipment(dn.name)

		self.assertEqual(len(self.bol.posts()), 2)
		self.assertEqual(len(self.bol.shipments), 1)
		self.assertEqual(self._state(dn), ("Pending", None))

	def test_shipment_already_at_bol_is_not_sent_again(self):
		dn = self._delivery_note()
		shipment_id = self.bol.ship(
			{"orderItems": [{"orderItemId": SHIRT_ITEM, "quantity": 2}], "shipmentReference": dn.name}
		)

		shipments.confirm_shipment(dn.name)

		self.assertEqual(self._state(dn), ("Confirmed", shipment_id))
		self.assertEqual(self.bol.posts(), [])

	def test_list_without_references_matches_on_items_and_quantities(self):
		dn = self._delivery_note()
		self.bol.list_references = False
		shipment_id = self.bol.ship(
			{
				"orderItems": [
					{"orderItemId": SHIRT_ITEM, "quantity": 2},
					{"orderItemId": BAG_ITEM, "quantity": 1},
				],
				"shipmentReference": "shipped in the seller dashboard",
			}
		)

		shipments.confirm_shipment(dn.name)

		self.assertEqual(self._state(dn), ("Confirmed", shipment_id))
		self.assertEqual(self.bol.posts(), [])

	def test_other_quantities_are_not_this_shipment(self):
		dn = self._delivery_note()
		self.bol.ship(
			{
				"orderItems": [
					{"orderItemId": SHIRT_ITEM, "quantity": 1},
					{"orderItemId": BAG_ITEM, "quantity": 1},
				]
			}
		)

		shipments.confirm_shipment(dn.name)

		self.assertEqual(len(self.bol.posts()), 1)

	def test_shipment_of_another_delivery_note_is_never_taken(self):
		so = self._sales_order()
		first = make_delivery_note(so)
		first.items[0].qty = 1
		del first.items[1]
		first.bol_track_and_trace = TRACK
		first.insert()
		first.submit()
		shipments.confirm_shipment(first.name)
		self._finish()
		second = make_delivery_note(so)
		del second.items[1]
		second.bol_track_and_trace = TRACK
		second.insert()
		second.submit()

		# The same items and quantity as the first shipment, which bol lists for this order.
		shipments.confirm_shipment(second.name)

		self.assertEqual(len(self.bol.posts()), 2)

	def test_bol_unreachable_stays_pending_and_is_logged_once(self):
		dn = self._delivery_note()
		for _run in range(2):
			self.bol.errors[("GET", shipments.PATH)] = (BolServerError("bol server error (HTTP 500)"), False)
			shipments.confirm_shipment(dn.name)

		self.assertEqual(self._state(dn), ("Pending", None))
		self.assertEqual(len(self._error_logs()), 1)
		self.assertEqual(len([c for c in self._comments(dn) if "couldn't be reached" in c]), 1)
		self.assertEqual(self.bol.posts(), [])

	def test_no_process_status_id_is_unknown(self):
		dn = self._delivery_note()

		with patch.object(self.bol, "_accept", return_value={}):
			shipments.confirm_shipment(dn.name)

		self.assertEqual(self._state(dn), ("Unknown", None))

	def test_running_twice_sends_once(self):
		dn = self._delivery_note()

		shipments.confirm_shipment(dn.name)
		shipments.confirm_shipment(dn.name)

		self.assertEqual(len(self.bol.posts()), 1)

	def test_confirmed_makes_no_call(self):
		dn = self._confirmed_dn()
		self.bol.calls.clear()

		shipments.confirm_shipment(dn.name)

		self.assertEqual(self.bol.calls, [])

	def test_unexpected_error_marks_failed_and_is_raised(self):
		dn = self._delivery_note()

		with (
			patch.object(shipments, "build_shipment", side_effect=KeyError("transport")),
			self.assertRaises(KeyError),
		):
			shipments.confirm_shipment(dn.name)

		self.assertEqual(self._state(dn), ("Failed", None))
		(log,) = self._error_logs()
		self.assertIn("KeyError", log["traceback"])

	def test_unexpected_error_after_the_post_is_unknown(self):
		dn = self._delivery_note()

		with (
			patch.object(process_status, "track", side_effect=RuntimeError("db gone")),
			self.assertRaises(RuntimeError),
		):
			shipments.confirm_shipment(dn.name)

		self.assertEqual(self._state(dn), ("Unknown", None))


class TestTrackAndTrace(ShipmentTestCase):
	def setUp(self):
		super().setUp()
		self.settings.require_track_and_trace = 0

	def test_added_after_submit_is_sent_to_the_transport(self):
		dn = self._confirmed_dn(track_and_trace=None)
		transport_id = next(iter(self.bol.shipments.values()))["transport"]["transportId"]

		dn.bol_track_and_trace = TRACK
		dn.save()
		self.transport_enqueued.assert_called_once_with(dn.name)
		shipments.update_transport(dn.name)

		self.assertEqual(
			self.bol.puts(),
			[
				(
					f"{shipments.TRANSPORT_PATH}/{transport_id}",
					{"transporterCode": "TNT", "trackAndTrace": TRACK},
				)
			],
		)
		self._finish()
		self.assertIn(f"Track & trace {TRACK} added at bol.", self._comments(dn)[-1])

	def test_bol_keeps_the_code_it_has(self):
		dn = self._confirmed_dn(track_and_trace="3SOLD")

		dn.bol_track_and_trace = TRACK
		dn.save()
		shipments.update_transport(dn.name)

		self.assertEqual(self.bol.puts(), [])
		self.assertIn("can't change it", self._comments(dn)[-1])

	def test_changed_while_pending_is_sent_once_bol_has_the_shipment(self):
		dn = self._delivery_note(track_and_trace=None)
		shipments.confirm_shipment(dn.name)
		dn.reload()

		dn.bol_track_and_trace = TRACK
		dn.save()
		self.transport_enqueued.assert_not_called()

		self._finish()
		self.transport_enqueued.assert_called_once_with(dn.name)

	def test_transport_failure_is_commented(self):
		dn = self._confirmed_dn(track_and_trace=None)
		dn.db_set("bol_track_and_trace", TRACK)
		shipments.update_transport(dn.name)

		self._finish("FAILURE", "Track and trace already set")

		self.assertTrue(any("Track and trace already set" in c for c in self._comments(dn)))

	def test_bol_error_is_logged_and_commented(self):
		dn = self._confirmed_dn(track_and_trace=None)
		dn.db_set("bol_track_and_trace", TRACK)
		self.bol.errors[("GET", f"{shipments.PATH}/{dn.bol_shipment_id}")] = (
			BolServerError("HTTP 500"),
			False,
		)

		shipments.update_transport(dn.name)

		self.assertIn("Track & trace not sent to bol", self._comments(dn)[-1])
		self.assertEqual(len(self._error_logs()), 1)


class TestCancel(ShipmentTestCase):
	def test_confirmed_pending_and_unknown_are_blocked(self):
		dn = self._delivery_note()
		for status in ("Pending", "Confirmed", "Unknown"):
			dn.db_set("bol_shipment_status", status)
			dn.reload()

			with self.assertRaises(frappe.ValidationError, msg=status):
				dn.cancel()

	def test_failed_can_be_cancelled(self):
		dn = self._delivery_note()
		dn.db_set("bol_shipment_status", "Failed")
		dn.reload()

		dn.cancel()

		self.assertEqual(dn.docstatus, 2)


class TestRetry(ShipmentTestCase):
	def _failed_dn(self):
		dn = self._delivery_note()
		self.bol.errors[("POST", shipments.PATH)] = (BolAuthError("bol denied access (HTTP 403)"), False)
		shipments.confirm_shipment(dn.name)
		return dn

	def test_needs_a_bol_role(self):
		dn = self._failed_dn()
		frappe.set_user("Guest")

		with self.assertRaises(frappe.PermissionError):
			shipments.retry_shipment(dn.name)
		self.assertEqual(len(self.bol.posts()), 1)

	def test_needs_write_permission_on_the_delivery_note(self):
		dn = self._failed_dn()
		make_bol_manager()
		frappe.set_user(BOL_MANAGER)

		with self.assertRaises(frappe.PermissionError):
			shipments.retry_shipment(dn.name)

	def test_only_failed_or_unknown(self):
		dn = self._delivery_note()

		with self.assertRaisesRegex(frappe.ValidationError, "Only a Failed or Unknown"):
			shipments.retry_shipment(dn.name)

	def test_retry_checks_bol_then_sends_again(self):
		dn = self._failed_dn()

		self.assertEqual(shipments.retry_shipment(dn.name), {"status": "Pending"})

		self.assertEqual(len(self.bol.posts()), 2)
		self.assertEqual([call[0] for call in self.bol.calls].count("GET"), 2)

	def test_unexpected_error_is_a_message(self):
		dn = self._failed_dn()

		with (
			patch.object(shipments, "build_shipment", side_effect=KeyError("transport")),
			self.assertRaisesRegex(frappe.ValidationError, "Retrying failed"),
		):
			shipments.retry_shipment(dn.name)


class TestSweep(ShipmentTestCase):
	def test_queues_what_nothing_else_finishes(self):
		lost = self._delivery_note()
		unknown = self._delivery_note()
		unknown.db_set("bol_shipment_status", "Unknown")
		busy = self._delivery_note()
		shipments.confirm_shipment(busy.name)
		failed = self._delivery_note()
		failed.db_set("bol_shipment_status", "Failed")
		recent = self._delivery_note()
		for dn in (lost, unknown, busy, failed):
			self._make_old(dn)
		self.enqueued.reset_mock()

		shipments.sweep_shipments()

		self.assertEqual(
			sorted(c.args[0] for c in self.enqueued.call_args_list), sorted([lost.name, unknown.name])
		)
		self.assertNotIn(recent.name, [c.args[0] for c in self.enqueued.call_args_list])

	def test_switched_off_does_nothing(self):
		dn = self._delivery_note()
		self._make_old(dn)
		self.enqueued.reset_mock()
		self.settings.push_shipments = 0

		shipments.sweep_shipments()

		self.enqueued.assert_not_called()


class TestEnqueue(FrappeTestCase):
	def test_jobs_are_deduplicated_per_delivery_note(self):
		with patch.object(shipments.frappe, "enqueue") as enqueue:
			shipments.enqueue_confirm("DN-1")
			shipments.enqueue_transport("DN-1")

		confirm, transport = enqueue.call_args_list
		self.assertEqual(confirm.args, (shipments.CONFIRM_METHOD,))
		self.assertEqual(
			confirm.kwargs,
			{
				"dn_name": "DN-1",
				"job_id": "bol_ship::DN-1",
				"deduplicate": True,
				"enqueue_after_commit": True,
			},
		)
		self.assertEqual(transport.kwargs["job_id"], "bol_transport::DN-1")
