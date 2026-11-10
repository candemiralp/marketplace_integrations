# Verzenden via bol labels (story 09) with bol's shipping label, shipment and process-status endpoints faked in memory.
# Everything stays in the test transaction; label files are deleted from disk afterwards.

import copy
import json
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from marketplace_integrations.bol import labels, process_status, shipments
from marketplace_integrations.bol.api.errors import (
	BolAuthError,
	BolNotFoundError,
	BolTransientError,
	BolValidationError,
)
from marketplace_integrations.bol.tests.test_shipments import (
	BAG_ITEM,
	DN,
	SHIRT_ITEM,
	FakeShipments,
	ShipmentTestCase,
)
from marketplace_integrations.bol.tests.utils import BOL_MANAGER, make_bol_manager

FIXTURES = Path(__file__).parent / "fixtures"
OPTIONS = json.loads((FIXTURES / "delivery_options.json").read_text())["deliveryOptions"]
PARCEL = "32c4a88c-3c64-41ad-83a1-24450b341747"
PDF = (FIXTURES / "label.pdf").read_bytes()
# What the demo serves for its label.
DEMO_PDF = b"THIS IS A FAKE PDF FILE AND IS INTENDED FOR TESTING PURPOSES ONLY."
ITEMS = [{"orderItemId": SHIRT_ITEM, "quantity": 2}, {"orderItemId": BAG_ITEM, "quantity": 1}]


class FakeLabels(FakeShipments):
	"""FakeShipments plus bol's shipping label endpoints. bol makes the label when it accepts the POST."""

	def __init__(self):
		super().__init__()
		self.options = copy.deepcopy(OPTIONS)
		for option in self.options:
			option["validUntilDate"] = add_days(nowdate(), 1)
		self.labels = {}  # shippingLabelId -> PDF

	def label_posts(self) -> list[dict]:
		return [body for method, path, body, _params in self.calls if (method, path) == ("POST", labels.PATH)]

	def _handle(self, method, path, body, params):
		if (method, path) == ("POST", labels.OPTIONS_PATH):
			return {"deliveryOptions": copy.deepcopy(self.options)}
		if (method, path) == ("POST", labels.PATH):
			label_id = f"label-{len(self.labels) + 1}"
			self.labels[label_id] = PDF
			return self._accept("CREATE_SHIPPING_LABEL", label_id)
		if method == "GET" and path.startswith(f"{labels.PATH}/"):
			label_id = path.rsplit("/", 1)[1]
			if label_id not in self.labels:
				raise BolNotFoundError("Not found")
			return self.labels[label_id]
		return super()._handle(method, path, body, params)


class LabelTestCase(ShipmentTestCase):
	def setUp(self):
		super().setUp()
		self.bol = FakeLabels()
		for module in (shipments, process_status, labels):
			self._start(patch.object(module, "get_client", return_value=self.bol))
		self.settings.update(
			environment="Production", use_vvb=1, vvb_for_retailer_items=0, vvb_preferred_transporter=None
		)
		self._start(patch.object(labels, "_settings", return_value=self.settings))
		self.realtime = self._start(patch.object(frappe, "publish_realtime"))
		# Runs before the rollback (cleanups run last-in, first-out): File.on_trash removes the file from disk.
		self.addCleanup(self._delete_label_files)

	def _bol_dn(self, party: str = "BOL", **values):
		return self._delivery_note(
			self._sales_order(party=party), submit=False, track_and_trace=None, **values
		)

	def _requested_dn(self, **values):
		dn = self._bol_dn(**values)
		labels.request_label(dn.name)
		return dn

	def _ready_dn(self):
		dn = self._requested_dn()
		self._finish()
		return self._reload(dn)

	def _label_state(self, dn) -> tuple:
		return frappe.db.get_value(
			DN, dn.name, ["bol_label_status", "bol_shipping_label_id", "bol_label_transporter"]
		)

	def _label_files(self, dn) -> list[dict]:
		return frappe.get_all(
			"File",
			filters={"attached_to_doctype": DN, "attached_to_name": dn.name},
			fields=["name", "file_name", "is_private", "file_url"],
		)

	def _messages(self) -> list[dict]:
		return [c.args[1] for c in self.realtime.call_args_list if c.args[0] == "msgprint"]

	def _delete_label_files(self):
		for name in frappe.get_all("File", filters={"file_name": ("like", "bol-label-%")}, pluck="name"):
			frappe.delete_doc("File", name, ignore_permissions=True, force=True)


class TestRequest(LabelTestCase):
	def test_request_buys_the_chosen_label_and_tracks_it(self):
		dn = self._bol_dn()

		self.assertEqual(labels.request_label(dn.name), {"status": "Requested"})

		(options_call,) = [c for c in self.bol.calls if c[1] == labels.OPTIONS_PATH]
		self.assertEqual(options_call[2], {"orderItems": ITEMS})
		self.assertEqual(self.bol.label_posts(), [{"orderItems": ITEMS, "shippingLabelOfferId": PARCEL}])
		process = frappe.get_doc(process_status.DOCTYPE, {"reference_name": dn.name})
		self.assertEqual((process.handler, process.reference_doctype), ("shipping_label", DN))
		self.assertEqual(frappe.parse_json(process.payload), {"user": "Administrator", "transporter": "TNT"})
		self.assertEqual(self._label_state(dn), ("Requested", None, None))
		self.assertIn("PostNL & bol.com - Pakket (TNT)", self._comments(dn)[-1])

	def test_success_attaches_the_pdf_privately(self):
		dn = self._requested_dn()

		self._finish()

		self.assertEqual(self._label_state(dn), ("Ready", "label-1", "TNT"))
		(label_file,) = self._label_files(dn)
		self.assertEqual((label_file.file_name, label_file.is_private), (f"bol-label-{dn.name}.pdf", 1))
		self.assertTrue(label_file.file_url.startswith("/private/files/"))
		self.assertEqual(frappe.get_doc("File", label_file.name).get_content(), PDF)
		(message,) = self._messages()
		self.assertEqual(message["indicator"], "green")
		self.assertTrue(any(c.kwargs.get("user") == "Administrator" for c in self.realtime.call_args_list))
		self.assertEqual(self._error_logs(), [])

	def test_failure_marks_failed_and_a_new_label_can_be_requested(self):
		dn = self._requested_dn()

		self._finish("FAILURE", "Order item already shipped")

		self.assertEqual(self._label_state(dn)[0], "Failed")
		self.assertTrue(any("Order item already shipped" in c for c in self._comments(dn)))
		self.assertEqual(self._messages()[-1]["indicator"], "red")

		labels.request_label(dn.name)

		self.assertEqual(len(self.bol.label_posts()), 2)
		self.assertEqual(self._label_state(dn)[0], "Requested")

	def test_double_click_buys_one_label(self):
		dn = self._requested_dn()

		with self.assertRaisesRegex(frappe.ValidationError, "already has a bol label"):
			labels.request_label(dn.name)

		self._finish()
		with self.assertRaisesRegex(frappe.ValidationError, "already has a bol label"):
			labels.request_label(dn.name)
		self.assertEqual(len(self.bol.label_posts()), 1)

	def test_account_without_vvb_gets_a_clear_message(self):
		dn = self._bol_dn()
		self.bol.errors[("POST", labels.OPTIONS_PATH)] = (
			BolAuthError("bol denied access (HTTP 403)", {"method": "POST", "status": 403}),
			False,
		)

		with self.assertRaisesRegex(frappe.ValidationError, "not registered for Verzenden via bol"):
			labels.request_label(dn.name)

		(log,) = self._error_logs()
		self.assertEqual(log["bol_reference_name"], dn.name)
		self.assertEqual(self.bol.label_posts(), [])

	def test_no_valid_option(self):
		dn = self._bol_dn()
		for option in self.bol.options:
			option["validUntilDate"] = add_days(nowdate(), -1)

		with self.assertRaisesRegex(frappe.ValidationError, "No VVB option available"):
			labels.request_label(dn.name)

		self.assertEqual(self.bol.label_posts(), [])
		self.assertEqual(len(self._error_logs()), 1)

	def test_rejected_request_shows_the_violations(self):
		dn = self._bol_dn()
		violations = [{"name": "orderItems[0].quantity", "reason": "Quantity exceeds the open quantity."}]
		self.bol.errors[("POST", labels.PATH)] = (BolValidationError("Bad Request", None, violations), False)

		with self.assertRaisesRegex(frappe.ValidationError, "Quantity exceeds the open quantity"):
			labels.request_label(dn.name)

	def test_ambiguous_post_can_be_tried_again(self):
		dn = self._bol_dn()
		self.bol.errors[("POST", labels.PATH)] = (BolTransientError("ReadTimeout", ambiguous=True), True)

		with self.assertRaisesRegex(frappe.ValidationError, "an unused label isn't charged"):
			labels.request_label(dn.name)
		self.assertEqual(self._label_state(dn)[0], "")

		labels.request_label(dn.name)

		self.assertEqual(self._label_state(dn)[0], "Requested")

	def test_vvb_switched_off(self):
		dn = self._bol_dn()
		self.settings.use_vvb = 0

		with self.assertRaisesRegex(frappe.ValidationError, "Use Verzenden via bol"):
			labels.request_label(dn.name)
		self.assertEqual(self.bol.calls, [])

	def test_own_items_need_the_setting(self):
		dn = self._bol_dn(party="RETAILER")

		with self.assertRaisesRegex(frappe.ValidationError, "bol Labels for Own Shipments"):
			labels.request_label(dn.name)

		self.settings.vvb_for_retailer_items = 1
		labels.request_label(dn.name)
		self.assertEqual(self._label_state(dn)[0], "Requested")

	def test_submitted_delivery_note_is_refused(self):
		dn = self._delivery_note(self._sales_order(party="RETAILER"))

		with self.assertRaisesRegex(frappe.ValidationError, "draft Delivery Note"):
			labels.request_label(dn.name)

	def test_needs_a_bol_role(self):
		dn = self._bol_dn()
		frappe.set_user("Guest")

		with self.assertRaises(frappe.PermissionError):
			labels.request_label(dn.name)
		self.assertEqual(self.bol.calls, [])

	def test_needs_write_permission_on_the_delivery_note(self):
		dn = self._bol_dn()
		make_bol_manager()
		frappe.set_user(BOL_MANAGER)

		with self.assertRaises(frappe.PermissionError):
			labels.request_label(dn.name)


class TestDownload(LabelTestCase):
	def _undownloaded_dn(self):
		dn = self._requested_dn()
		self.bol.errors[("GET", f"{labels.PATH}/label-1")] = (BolNotFoundError("Not found"), False)
		self._finish()
		return dn

	def test_failed_download_keeps_the_bought_label(self):
		dn = self._undownloaded_dn()

		self.assertEqual(self._label_state(dn), ("Requested", "label-1", "TNT"))
		self.assertEqual(
			frappe.db.get_value(process_status.DOCTYPE, {"reference_name": dn.name}, "status"), "SUCCESS"
		)
		self.assertIn("Download bol label again", self._comments(dn)[-1])
		self.assertEqual(len(self._error_logs()), 1)
		self.assertEqual(self._label_files(dn), [])

	def test_body_that_isnt_a_pdf_is_a_failed_download(self):
		dn = self._requested_dn()
		self.bol.labels["label-1"] = DEMO_PDF

		self._finish()

		self.assertEqual(self._label_state(dn)[0], "Requested")
		self.assertIn("isn't a PDF", self._comments(dn)[-1])
		self.assertEqual(self._label_files(dn), [])

	def test_download_again_fetches_without_buying(self):
		dn = self._undownloaded_dn()

		self.assertEqual(labels.download_label(dn.name), {"status": "Ready"})

		self.assertEqual(self._label_state(dn)[0], "Ready")
		self.assertEqual(len(self._label_files(dn)), 1)
		self.assertEqual(len(self.bol.label_posts()), 1)

	def test_download_again_failing_is_a_message(self):
		dn = self._undownloaded_dn()
		self.bol.errors[("GET", f"{labels.PATH}/label-1")] = (BolNotFoundError("Not found"), False)

		with self.assertRaisesRegex(frappe.ValidationError, "bol label not downloaded"):
			labels.download_label(dn.name)

	def test_only_a_bought_label_can_be_downloaded_again(self):
		dn = self._requested_dn()

		with self.assertRaisesRegex(frappe.ValidationError, "downloaded again"):
			labels.download_label(dn.name)

	def test_download_again_needs_a_bol_role(self):
		dn = self._undownloaded_dn()
		frappe.set_user("Guest")

		with self.assertRaises(frappe.PermissionError):
			labels.download_label(dn.name)

	def test_label_without_an_id_is_failed(self):
		dn = self._requested_dn()
		self.bol.entities[list(self.bol.processes)[-1]] = None

		self._finish()

		self.assertEqual(self._label_state(dn), ("Failed", None, None))
		self.assertIn("didn't say its id", self._comments(dn)[-1])

	def test_deleted_draft_needs_nothing(self):
		dn = self._requested_dn()

		frappe.delete_doc(DN, dn.name)
		self._finish()

		self.assertEqual(
			frappe.db.get_value(process_status.DOCTYPE, {"reference_name": dn.name}, "status"), "SUCCESS"
		)


class TestSubmit(LabelTestCase):
	def test_items_bol_ships_need_a_label(self):
		dn = self._bol_dn()

		with self.assertRaisesRegex(frappe.ValidationError, "Get the bol label"):
			dn.submit()

	def test_message_says_to_switch_vvb_on(self):
		dn = self._bol_dn()
		self.settings.use_vvb = 0

		with self.assertRaisesRegex(frappe.ValidationError, "Switch on Use Verzenden via bol"):
			dn.submit()

	def test_requested_label_blocks_submit(self):
		dn = self._requested_dn()
		dn.reload()

		with self.assertRaisesRegex(frappe.ValidationError, "isn't attached yet"):
			dn.submit()

	def test_shipment_is_confirmed_with_the_label(self):
		dn = self._ready_dn()

		dn.submit()
		shipments.confirm_shipment(dn.name)

		(body,) = self.bol.posts()
		self.assertEqual(
			body, {"orderItems": ITEMS, "shipmentReference": dn.name, "shippingLabelId": "label-1"}
		)

	def test_own_items_on_a_bol_label_need_no_track_and_trace(self):
		self.settings.vvb_for_retailer_items = 1
		dn = self._requested_dn(party="RETAILER")
		self._finish()
		dn.reload()

		dn.submit()
		shipments.confirm_shipment(dn.name)

		(body,) = self.bol.posts()
		self.assertEqual(body["shippingLabelId"], "label-1")
		self.assertNotIn("transport", body)

	def test_items_are_fixed_once_a_label_is_bought(self):
		dn = self._ready_dn()

		dn.items[0].qty = 1
		with self.assertRaisesRegex(frappe.ValidationError, "can't change"):
			dn.save()

	def test_other_changes_are_allowed(self):
		dn = self._ready_dn()

		dn.remarks = "Packed"
		dn.save()

	def test_draft_with_a_label_can_be_deleted(self):
		dn = self._ready_dn()

		frappe.delete_doc(DN, dn.name)

		self.assertFalse(frappe.db.exists(DN, dn.name))


class TestOnload(LabelTestCase):
	def _allowed(self, dn):
		dn = frappe.get_doc(DN, dn.name)
		dn.run_method("onload")
		return dn.get_onload().get("bol_label_allowed")

	def test_offered_for_items_bol_ships(self):
		self.assertTrue(self._allowed(self._bol_dn()))

	def test_own_items_only_with_the_setting(self):
		dn = self._bol_dn(party="RETAILER")

		self.assertFalse(self._allowed(dn))
		self.settings.vvb_for_retailer_items = 1
		self.assertTrue(self._allowed(dn))

	def test_not_offered_with_vvb_off(self):
		dn = self._bol_dn()
		self.settings.use_vvb = 0

		self.assertIsNone(self._allowed(dn))
