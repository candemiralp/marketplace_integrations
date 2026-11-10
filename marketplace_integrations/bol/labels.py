# Verzenden via bol labels (Shipping Labels API v10, story 09, ADR-012). Items bol ships itself (distribution party
# BOL) need a bol label: it's bought from the draft Delivery Note, its PDF attached privately, and the shipment is
# confirmed with its shippingLabelId (shipments.py). bol charges a label only when a shipment uses it.

import frappe
from frappe import _
from frappe.utils import flt, fmt_money, getdate, nowdate

from marketplace_integrations.bol import process_status
from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import (
	BolAuthError,
	BolError,
	BolMappingError,
	BolValidationError,
)
from marketplace_integrations.bol.log import log_call
from marketplace_integrations.bol.mappers.label import choose_option, delivery_options_request, label_request
from marketplace_integrations.bol.mappers.offer import format_violations
from marketplace_integrations.bol.permissions import check_bol_role
from marketplace_integrations.bol.shipments import shipment_lines, shipments_on, ships_itself

PATH = "/retailer/shipping-labels"
OPTIONS_PATH = f"{PATH}/delivery-options"
VERSION = 10
PDF = "application/vnd.retailer.v10+pdf"
PDF_SIGNATURE = b"%PDF-"
DOCTYPE = "Delivery Note"
HANDLER = "shipping_label"
REQUESTED, READY, FAILED = "Requested", "Ready", "Failed"
BOL = "BOL"


def labels_on(settings=None) -> bool:
	settings = settings or _settings()
	return bool(settings.enabled and settings.use_vvb)


# --- Delivery Note events (doc_events) -------------------------------------------------------------------


def onload_delivery_note(doc, method=None):
	# Tells the form whether to offer Get bol label.
	if doc.docstatus != 0 or doc.is_return or not doc.bol_order_id or not labels_on():
		return
	doc.set_onload("bol_label_allowed", BOL in _parties(doc) or bool(_settings().vvb_for_retailer_items))


def validate_delivery_note(doc, method=None):
	if doc.is_return or doc.bol_label_status not in (REQUESTED, READY):
		return
	before = doc.get_doc_before_save()
	if before and _packed(before) != _packed(doc):
		frappe.throw(
			_(
				"The bol label was bought for these items and quantities, so they can't change. To ship others, delete this Delivery Note and make a new one: an unused label isn't charged."
			),
			title=_("bol Label"),
		)


def before_submit_delivery_note(doc, method=None):
	if doc.is_return or not doc.bol_order_id or not shipments_on() or doc.bol_label_status == READY:
		return
	if doc.bol_label_status == REQUESTED:
		frappe.throw(
			_(
				"The bol label isn't attached yet. Submit once it is; if bol made it but the download failed, use bol → Download bol label again."
			),
			title=_("bol Label"),
		)
	if not ships_itself(shipment_lines(doc)):
		message = _("bol ships these items (Verzenden via bol). Get the bol label (bol → Get bol label) first.")
		if not labels_on():
			message += " " + _("Switch on Use Verzenden via bol in Bol Settings for that.")
		frappe.throw(message, title=_("bol Label"))


# --- Buttons ------------------------------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def request_label(dn_name: str) -> dict:
	"""Buy a bol label for the draft Delivery Note. bol makes it in the background; on_label_success attaches it."""
	# Frappe enforces the type hints of whitelisted functions.
	check_bol_role()
	frappe.has_permission(DOCTYPE, "write", dn_name, throw=True)
	settings = _settings()
	if not labels_on(settings):
		frappe.throw(_("Switch on Use Verzenden via bol in Bol Settings first."), title=_("bol Disabled"))
	# The row lock makes a second click wait for the first, which then finds the label Requested.
	dn = frappe.get_doc(DOCTYPE, dn_name, for_update=True)
	lines = _label_lines(dn, settings)
	try:
		client = get_client()
		response = client.request(
			"POST",
			OPTIONS_PATH,
			version=VERSION,
			json=delivery_options_request(lines),
			reference=_reference(dn),
			safe=True,
		)
		option = choose_option(
			(response or {}).get("deliveryOptions") or [],
			settings.vvb_preferred_transporter,
			# The demo's options expired in 2020.
			today=None if settings.environment == "Demo" else getdate(nowdate()),
		)
		response = client.request(
			"POST",
			PATH,
			version=VERSION,
			json=label_request(lines, option["shippingLabelOfferId"]),
			reference=_reference(dn),
		)
		if not (response or {}).get("processStatusId"):
			raise BolError("bol accepted the label without a processStatusId", ambiguous=True)
		process_status.track(
			response,
			HANDLER,
			dn,
			payload={"user": frappe.session.user, "transporter": option.get("transporterCode")},
		)
	except BolError as e:
		# The log is written after the rollback of frappe.throw; the Delivery Note stays as it was.
		_log_failure(dn, e, "request_label")
		frappe.throw(_request_failed(e), title=_("bol Label"))

	dn.db_set("bol_label_status", REQUESTED, notify=True)
	dn.add_comment(
		"Comment",
		_("bol label requested: {0} ({1}), {2} excl. VAT. bol charges it only when the shipment is confirmed.").format(
			option.get("labelDisplayName") or option.get("labelType"),
			option.get("transporterCode"),
			fmt_money((option.get("labelPrice") or {}).get("totalPrice"), currency="EUR"),
		),
	)
	return {"status": REQUESTED}


@frappe.whitelist(methods=["POST"])
def download_label(dn_name: str) -> dict:
	"""Fetch the PDF of a label bol made but that couldn't be downloaded. Never buys a label."""
	check_bol_role()
	frappe.has_permission(DOCTYPE, "write", dn_name, throw=True)
	dn = frappe.get_doc(DOCTYPE, dn_name, for_update=True)
	if dn.docstatus != 0 or dn.bol_label_status != REQUESTED or not dn.bol_shipping_label_id:
		frappe.throw(
			_("Only a bol label that bol made but that wasn't downloaded can be downloaded again."),
			title=_("Not Allowed"),
		)
	try:
		_download(dn)
	except BolError as e:
		_log_failure(dn, e, "download_label")
		frappe.throw(_("bol label not downloaded: {0}").format(str(e)), title=_("bol Label"))
	return {"status": READY}


# --- Process handlers (process_status) ----------------------------------------------------------------------


def on_label_success(process, entity_id, payload):
	# A draft deleted meanwhile needs nothing: an unused label isn't charged.
	if not frappe.db.exists(DOCTYPE, process.reference_name):
		return
	dn = frappe.get_doc(DOCTYPE, process.reference_name)
	if dn.bol_label_status == READY:
		return
	user = payload.get("user")
	if not entity_id:
		_failed(dn, _("bol made the label but didn't say its id. Get a new bol label: an unused label isn't charged."), user)
		return
	dn.db_set({"bol_shipping_label_id": entity_id, "bol_label_transporter": payload.get("transporter")})
	try:
		_download(dn, user)
	except BolError as e:
		# The label is bought: it stays Requested, and Download bol label again fetches it without buying another.
		_log_failure(dn, e, "on_label_success")
		message = _(
			"bol label {0} is made but couldn't be downloaded ({1}). Use bol → Download bol label again on {2}."
		).format(entity_id, str(e), dn.name)
		dn.add_comment("Comment", message)
		_notify(user, message, "red")


def on_label_failure(process, message, payload):
	# process_status has logged bol's errorMessage and commented it on the Delivery Note already.
	if not frappe.db.exists(DOCTYPE, process.reference_name):
		return
	dn = frappe.get_doc(DOCTYPE, process.reference_name)
	if dn.bol_label_status != READY:
		dn.db_set("bol_label_status", FAILED, notify=True)
		_notify(payload.get("user"), _("bol couldn't make the label for {0}: {1}").format(dn.name, message), "red")


process_status.register(HANDLER, on_label_success, on_label_failure)


# --- Helpers ------------------------------------------------------------------------------------------------


def _label_lines(dn, settings) -> list[dict]:
	"""The Delivery Note's lines, once it's sure a label may be bought for them."""
	if dn.docstatus != 0 or dn.is_return or not dn.bol_order_id:
		frappe.throw(
			_("A bol label can only be bought for a draft Delivery Note of a bol order."), title=_("Not Allowed")
		)
	if dn.bol_label_status in (REQUESTED, READY):
		frappe.throw(
			_("This Delivery Note already has a bol label ({0}).").format(_(dn.bol_label_status)),
			title=_("Not Allowed"),
		)
	lines = shipment_lines(dn)
	if ships_itself(lines) and not settings.vvb_for_retailer_items:
		frappe.throw(
			_(
				"You ship these items yourself. To use bol labels for them too, tick bol Labels for Own Shipments in Bol Settings."
			),
			title=_("Not Allowed"),
		)
	return lines


def _download(dn, user: str | None = None):
	pdf = get_client().request(
		"GET", f"{PATH}/{dn.bol_shipping_label_id}", version=VERSION, accept=PDF, reference=_reference(dn)
	)
	# Anything else would fail Frappe's PDF check with a raw parser error (the demo sends plain text).
	if not (isinstance(pdf, bytes) and pdf.startswith(PDF_SIGNATURE)):
		raise BolError("bol's label isn't a PDF")
	file_name = f"bol-label-{dn.name}.pdf"
	# Private: the label shows the buyer's address. Readable by whoever may read the Delivery Note.
	frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"attached_to_doctype": DOCTYPE,
			"attached_to_name": dn.name,
			"is_private": 1,
			"content": pdf,
		}
	).insert(ignore_permissions=True)
	dn.db_set("bol_label_status", READY, notify=True)
	message = _("bol label ready for {0} ({1}): {2} is attached.").format(
		dn.name, dn.bol_label_transporter or _("bol's transporter"), file_name
	)
	dn.add_comment("Comment", message)
	_notify(user, message, "green")


def _failed(dn, message: str, user: str | None):
	dn.db_set("bol_label_status", FAILED, notify=True)
	dn.add_comment("Comment", message)
	_notify(user, message, "red")


def _request_failed(error: BolError) -> str:
	if isinstance(error, BolAuthError) and (error.details or {}).get("status") == 403:
		return _(
			"bol account not registered for Verzenden via bol. Register in the bol seller dashboard (Instellingen → Diensten)."
		)
	if isinstance(error, BolMappingError) and error.reason == "no_option":
		return _("No VVB option available — check the package restrictions.")
	if isinstance(error, BolValidationError):
		return _("bol rejected the label request: {0}").format(format_violations(error.violations) or str(error))
	if error.ambiguous:
		return _("bol may have made the label, but its answer didn't arrive. Try again: an unused label isn't charged.")
	return _("bol label not requested: {0}").format(str(error))


def _parties(doc) -> set[str]:
	"""Distribution parties of the Delivery Note's bol lines. Unlike shipment_lines, it never throws."""
	so_details = [item.so_detail for item in doc.items if item.so_detail]
	if not so_details:
		return set()
	return set(
		frappe.get_all(
			"Sales Order Item", filters={"name": ("in", so_details)}, pluck="bol_distribution_party"
		)
	)


def _packed(doc) -> list[tuple]:
	return sorted((item.so_detail or "", flt(item.qty)) for item in doc.items)


def _notify(user: str | None, message: str, indicator: str):
	if user:
		frappe.publish_realtime(
			"msgprint",
			{"message": message, "title": _("bol Label"), "indicator": indicator},
			user=user,
			after_commit=True,
		)


def _log_failure(dn, error: Exception, method: str):
	details = getattr(error, "details", None) or {"method": method, "path": PATH}
	log_call(**details, error=error, reference=_reference(dn))


def _reference(dn) -> tuple[str, str]:
	return (DOCTYPE, dn.name)


def _settings():
	return frappe.get_cached_doc("Bol Settings")
