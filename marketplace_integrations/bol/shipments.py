# Shipment confirmation (Shipments and Transports API v10, story 08, ADR-011). Submitting a Delivery Note for a
# bol order confirms its shipment at bol exactly once; process_status follows it to a final state.
# Every state change on the submitted Delivery Note goes through db_set: it's never saved again.

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, now_datetime

# invoices imports this module too; each uses the other's attributes only at call time.
from marketplace_integrations.bol import invoices, process_status
from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import (
	BolError,
	BolRateLimitError,
	BolServerError,
	BolTransientError,
	BolValidationError,
)
from marketplace_integrations.bol.log import log_call, log_once
from marketplace_integrations.bol.mappers.offer import format_violations
from marketplace_integrations.bol.mappers.shipment import build_shipment, candidates, is_ours, quantities
from marketplace_integrations.bol.permissions import check_bol_role

PATH = "/retailer/shipments"
TRANSPORT_PATH = "/retailer/transports"
VERSION = 10
DOCTYPE = "Delivery Note"
CONFIRM_METHOD = "marketplace_integrations.bol.shipments.confirm_shipment"
TRANSPORT_METHOD = "marketplace_integrations.bol.shipments.update_transport"
CONFIRM_SAVEPOINT = "bol_confirm_shipment"
PENDING, CONFIRMED, FAILED, UNKNOWN = "Pending", "Confirmed", "Failed", "Unknown"
RETAILER = "RETAILER"
# Statuses of a Bol Process that still has to finish: bol is busy, or our handler needs a Retry.
UNFINISHED = ("PENDING", "HANDLER_ERROR")
# bol couldn't be reached, or may have got the POST: tried again once things had time to settle.
SWEEP_AFTER_MINUTES = 10
# Each confirmation first reads GET /retailer/shipments, which bol limits to 25 a minute (story 15).
SWEEP_BATCH = 20
# Failures a later attempt may not have: the Delivery Note stays Pending and the sweep retries it.
PASSING = (BolTransientError, BolRateLimitError, BolServerError)


def shipments_on(settings=None) -> bool:
	settings = settings or _settings()
	return bool(settings.enabled and settings.push_shipments)


# --- Delivery Note events (doc_events) -------------------------------------------------------------------


def validate_delivery_note(doc, method=None):
	if doc.is_return:
		return
	line_orders = _line_orders(doc)
	order_ids = sorted({order_id for order_id in line_orders if order_id})
	doc.bol_order_id = order_ids[0] if len(order_ids) == 1 else None
	if not order_ids or not shipments_on():
		return
	if len(order_ids) > 1:
		frappe.throw(
			_("The items come from bol orders {0}. Create one Delivery Note per bol order.").format(
				", ".join(order_ids)
			)
		)
	if None in line_orders:
		frappe.throw(
			_(
				"Items of bol order {0} can't share a Delivery Note with other items. Create separate Delivery Notes."
			).format(order_ids[0])
		)
	lines = shipment_lines(doc)
	if len({_party(line) for line in lines}) > 1:
		frappe.throw(
			_(
				"Some items of bol order {0} are shipped by bol (Verzenden via bol) and others by you. Create separate Delivery Notes."
			).format(order_ids[0])
		)
	if not doc.bol_transporter_code and ships_itself(lines):
		doc.bol_transporter_code = _settings().default_transporter_code


def before_submit_delivery_note(doc, method=None):
	if doc.is_return or not doc.bol_order_id or not shipments_on():
		return
	# A bol label names the carrier and its own track & trace. Items bol ships always need one (labels.py, story 09).
	if doc.bol_shipping_label_id or not ships_itself(shipment_lines(doc)):
		return
	if not doc.bol_transporter_code:
		frappe.throw(_("Set the bol Transporter before submitting."), title=_("bol Shipment"))
	if _settings().require_track_and_trace and not (doc.bol_track_and_trace or "").strip():
		frappe.throw(
			_("Fill in the bol Track & Trace before submitting: bol needs it to confirm the shipment."),
			title=_("bol Shipment"),
		)


def on_submit_delivery_note(doc, method=None):
	if doc.is_return or not doc.bol_order_id or not shipments_on():
		return
	doc.db_set("bol_shipment_status", PENDING)
	enqueue_confirm(doc.name)


def on_update_after_submit_delivery_note(doc, method=None):
	if doc.is_return or not doc.bol_order_id or not shipments_on():
		return
	# While the shipment is still Pending, the handler sends a changed code once bol has the shipment.
	if (
		doc.bol_shipment_status == CONFIRMED
		and doc.has_value_changed("bol_track_and_trace")
		and (doc.bol_track_and_trace or "").strip()
	):
		enqueue_transport(doc.name)


def before_cancel_delivery_note(doc, method=None):
	if doc.bol_shipment_status in (PENDING, CONFIRMED):
		frappe.throw(
			_("Shipment already confirmed on bol; process a return instead."), title=_("bol Shipment")
		)
	if doc.bol_shipment_status == UNKNOWN:
		frappe.throw(
			_("bol may already have this shipment. Retry it first: that checks bol before anything is sent."),
			title=_("bol Shipment"),
		)


# --- Confirming ---------------------------------------------------------------------------------------------


def enqueue_confirm(dn_name: str):
	frappe.enqueue(
		CONFIRM_METHOD,
		dn_name=dn_name,
		job_id=f"bol_ship::{dn_name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def confirm_shipment(dn_name: str):
	"""Confirm the Delivery Note's shipment at bol, unless bol has it already. Never sends it twice.

	bol errors are handled here (status, log, comment); anything else marks it Failed and is raised.
	"""
	if not shipments_on():
		return
	dn = frappe.get_doc(DOCTYPE, dn_name, for_update=True)
	if dn.docstatus != 1 or dn.bol_shipment_status == CONFIRMED or _unfinished_process(dn.name, "shipment"):
		return
	frappe.db.savepoint(CONFIRM_SAVEPOINT)
	try:
		_confirm(dn)
	except BolError as e:
		frappe.db.rollback(save_point=CONFIRM_SAVEPOINT)
		_confirm_failed(dn, e)
	except Exception as e:
		frappe.db.rollback(save_point=CONFIRM_SAVEPOINT)
		# After the POST bol may have the shipment: a Retry must check bol first, and the DN must not be cancelled.
		_set_status(dn, UNKNOWN if dn.flags.bol_posted else FAILED)
		_log_failure(dn, e)
		dn.add_comment("Comment", _("Shipment not confirmed at bol: {0}").format(str(e) or type(e).__name__))
		# Keep the status, log and comment: the job's rollback would drop them.
		frappe.db.commit()  # nosemgrep
		raise


def _confirm(dn):
	client = get_client()
	lines = shipment_lines(dn)
	shipment = reconcile_shipment(dn, client, lines)
	if shipment:
		_confirmed(
			dn,
			shipment["shipmentId"],
			_("bol already has this shipment ({0}). Marked confirmed without sending it again.").format(
				shipment["shipmentId"]
			),
		)
		return

	body = build_shipment(
		lines, dn.name, dn.bol_transporter_code, dn.bol_track_and_trace, dn.get("bol_shipping_label_id")
	)
	response = client.request("POST", PATH, version=VERSION, json=body, reference=_reference(dn))
	dn.flags.bol_posted = True
	if not (response or {}).get("processStatusId"):
		raise BolError("bol accepted the shipment without a processStatusId", ambiguous=True)
	process_status.track(
		response,
		"shipment",
		dn,
		payload={
			"order_item_ids": [item["orderItemId"] for item in body["orderItems"]],
			"track_and_trace": (body.get("transport") or {}).get("trackAndTrace"),
		},
	)
	_set_status(dn, PENDING)


def _confirm_failed(dn, error: BolError):
	# The sweep retries these two every 10 minutes: logged and commented once per 6 hours.
	if error.ambiguous or dn.flags.bol_posted:
		_set_status(dn, UNKNOWN)
		if _log_failure(dn, error, once="shipment_unknown"):
			dn.add_comment(
				"Comment",
				_(
					"bol may have received the shipment ({0}). bol is checked before it's sent again: automatically within about {1} minutes, or with Retry."
				).format(str(error), SWEEP_AFTER_MINUTES),
			)
		return
	if isinstance(error, PASSING):
		_set_status(dn, PENDING)
		if _log_failure(dn, error, once="shipment"):
			dn.add_comment(
				"Comment",
				_(
					"bol couldn't be reached to confirm the shipment ({0}). It's tried again automatically."
				).format(str(error)),
			)
		return
	# Rejected data, credentials, our data (no whole units, no transporter), an unexpected answer: a person acts.
	if isinstance(error, BolValidationError):
		message = _("bol rejected the shipment: {0}").format(
			format_violations(error.violations) or str(error)
		)
	else:
		message = _("Shipment not confirmed at bol: {0}").format(str(error))
	_set_status(dn, FAILED)
	_log_failure(dn, error)
	dn.add_comment("Comment", message)


def reconcile_shipment(dn, client=None, lines: list[dict] | None = None) -> dict | None:
	"""The shipment at bol that is this Delivery Note's, or None. Asked before every POST (ADR-011)."""
	client = client or get_client()
	wanted = quantities(lines or shipment_lines(dn))
	others = frappe.get_all(
		DOCTYPE,
		filters={"bol_order_id": dn.bol_order_id, "name": ("!=", dn.name)},
		fields=["name", "bol_shipment_id"],
	)
	taken = {other.bol_shipment_id for other in others if other.bol_shipment_id}
	other_references = {other.name for other in others}
	for shipment in candidates(_order_shipments(client, dn.bol_order_id), dn.name, set(wanted), taken):
		if shipment.get("shipmentReference") == dn.name:
			return shipment
		# The list has no quantities (and the demo leaves out the reference): the detail has both.
		detail = client.request("GET", f"{PATH}/{shipment['shipmentId']}", version=VERSION)
		if is_ours(detail or {}, dn.name, wanted, other_references):
			return detail
	return None


def _order_shipments(client, order_id: str) -> list[dict]:
	# One order's shipments fit on the first page.
	try:
		response = client.request("GET", PATH, version=VERSION, params={"order-id": order_id})
	except BolValidationError:
		if client.settings.environment != "Demo":
			raise
		# The demo answers only its own example order; any other order gets a 400 there.
		return []
	return (response or {}).get("shipments") or []


# --- Process handlers (process_status) ----------------------------------------------------------------------


def on_shipment_success(process, entity_id, payload):
	dn = frappe.get_doc(DOCTYPE, process.reference_name)
	if dn.bol_shipment_status == CONFIRMED:
		return
	# bol's entityId is the shipment id; should it be missing, the shipment is looked up.
	shipment_id = entity_id or (reconcile_shipment(dn) or {}).get("shipmentId")
	_confirmed(
		dn,
		shipment_id,
		_("Shipment confirmed at bol ({0}).").format(shipment_id)
		if shipment_id
		else _("Shipment confirmed at bol."),
	)
	# Track & trace filled in while bol was still processing the shipment.
	track_and_trace = (dn.bol_track_and_trace or "").strip()
	if track_and_trace and track_and_trace != payload.get("track_and_trace"):
		enqueue_transport(dn.name)


def on_shipment_failure(process, message, payload):
	# process_status has logged bol's errorMessage and commented it on the Delivery Note already.
	dn = frappe.get_doc(DOCTYPE, process.reference_name)
	if dn.bol_shipment_status != CONFIRMED:
		_set_status(dn, FAILED)


def on_transport_success(process, entity_id, payload):
	frappe.get_doc(DOCTYPE, process.reference_name).add_comment(
		"Comment", _("Track & trace {0} added at bol.").format(payload.get("track_and_trace"))
	)


def on_transport_failure(process, message, payload):
	"""Nothing to undo: process_status has logged the failure and commented it on the Delivery Note."""


process_status.register("shipment", on_shipment_success, on_shipment_failure)
process_status.register("transport", on_transport_success, on_transport_failure)


# --- Track & trace after submit -----------------------------------------------------------------------------


def enqueue_transport(dn_name: str):
	frappe.enqueue(
		TRANSPORT_METHOD,
		dn_name=dn_name,
		job_id=f"bol_transport::{dn_name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def update_transport(dn_name: str):
	"""Add the Delivery Note's track & trace to its bol shipment. bol only accepts one while it has none."""
	if not shipments_on():
		return
	dn = frappe.get_doc(DOCTYPE, dn_name)
	track_and_trace = (dn.bol_track_and_trace or "").strip()
	if dn.bol_shipment_status != CONFIRMED or not track_and_trace:
		return
	try:
		client = get_client()
		shipment = _shipment(client, dn)
		transport = (shipment or {}).get("transport") or {}
		current = transport.get("trackAndTrace")
		if current == track_and_trace:
			return
		if current:
			dn.add_comment(
				"Comment",
				_(
					"bol already has track & trace {0} for this shipment, and bol's API can't change it. If {1} is right, correct it in the bol seller dashboard."
				).format(current, track_and_trace),
			)
			return
		if not transport.get("transportId"):
			raise BolError(f"bol shipment {(shipment or {}).get('shipmentId')} has no transport id")
		body = {"transporterCode": dn.bol_transporter_code or None, "trackAndTrace": track_and_trace}
		response = client.request(
			"PUT",
			f"{TRANSPORT_PATH}/{transport['transportId']}",
			version=VERSION,
			json=body,
			reference=_reference(dn),
		)
		process_status.track(response, "transport", dn, payload={"track_and_trace": track_and_trace})
	except BolError as e:
		_log_failure(dn, e)
		dn.add_comment("Comment", _("Track & trace not sent to bol: {0}").format(str(e)))


def _shipment(client, dn) -> dict | None:
	if dn.bol_shipment_id:
		return client.request("GET", f"{PATH}/{dn.bol_shipment_id}", version=VERSION)
	return reconcile_shipment(dn, client)


# --- Retry button and sweep ---------------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def retry_shipment(dn_name: str) -> dict:
	"""Confirm a Failed or Unknown shipment again. bol is checked first, so it's never sent twice."""
	# Frappe enforces the type hints of whitelisted functions.
	check_bol_role()
	frappe.has_permission(DOCTYPE, "write", dn_name, throw=True)
	if not shipments_on():
		frappe.throw(_("Switch on Push Shipments in Bol Settings first."), title=_("bol Disabled"))
	if frappe.db.get_value(DOCTYPE, dn_name, "bol_shipment_status") not in (FAILED, UNKNOWN):
		frappe.throw(_("Only a Failed or Unknown bol shipment can be retried."), title=_("Not Allowed"))
	try:
		confirm_shipment(dn_name)
	except Exception as e:
		# Logged and commented by confirm_shipment; the user gets a message, not a traceback.
		frappe.throw(
			_("Retrying failed: {0}. Details are in the Ecommerce Integration Log.").format(
				str(e) or type(e).__name__
			),
			title=_("Retry Failed"),
		)
	return {"status": frappe.db.get_value(DOCTYPE, dn_name, "bol_shipment_status")}


def sweep_shipments():
	"""Every 10 minutes: queue the confirmations nothing else will finish (ADR-011).

	Unknown (bol may have the shipment), and Pending without a Bol Process: bol couldn't be reached, or the
	job was lost. confirm_shipment checks bol before sending anything. Failed waits for a person.
	"""
	if not shipments_on():
		return
	names = frappe.get_all(
		DOCTYPE,
		filters={
			"docstatus": 1,
			"bol_shipment_status": ("in", (PENDING, UNKNOWN)),
			"modified": ("<", add_to_date(now_datetime(), minutes=-SWEEP_AFTER_MINUTES)),
		},
		order_by="modified asc",
		limit=SWEEP_BATCH,
		pluck="name",
	)
	for name in names:
		if not _unfinished_process(name, "shipment"):
			enqueue_confirm(name)


# --- Helpers ------------------------------------------------------------------------------------------------


def shipment_lines(doc) -> list[dict]:
	"""[{order_item_id, qty, distribution_party}] for each line, from its Sales Order line (so_detail)."""
	so_details = [item.so_detail for item in doc.items if item.so_detail]
	rows = (
		{
			row.name: row
			for row in frappe.get_all(
				"Sales Order Item",
				filters={"name": ("in", so_details)},
				fields=["name", "bol_order_item_id", "bol_distribution_party"],
			)
		}
		if so_details
		else {}
	)
	lines = []
	for item in doc.items:
		row = rows.get(item.so_detail)
		if not (row and row.bol_order_item_id):
			frappe.throw(
				_(
					"Row {0}: {1} isn't linked to a line of bol order {2}. Create the Delivery Note from the Sales Order."
				).format(item.idx, item.item_code, doc.bol_order_id)
			)
		if flt(item.qty) != cint(item.qty):
			frappe.throw(_("Row {0}: bol ships whole units, not {1}.").format(item.idx, item.qty))
		lines.append(
			{
				"order_item_id": row.bol_order_item_id,
				"qty": cint(item.qty),
				"distribution_party": row.bol_distribution_party,
			}
		)
	return lines


def _line_orders(doc) -> list[str | None]:
	"""The bol order id of each line's Sales Order; None for a line that isn't from a bol order."""
	so_names = list({item.against_sales_order for item in doc.items if item.against_sales_order})
	bol_orders = (
		dict(
			frappe.get_all(
				"Sales Order",
				filters={"name": ("in", so_names), "bol_order_id": ("is", "set")},
				fields=["name", "bol_order_id"],
				as_list=True,
			)
		)
		if so_names
		else {}
	)
	return [bol_orders.get(item.against_sales_order) for item in doc.items]


def _party(line: dict) -> str:
	# bol may leave the distribution party out; then the retailer ships.
	return line["distribution_party"] or RETAILER


def ships_itself(lines: list[dict]) -> bool:
	return all(_party(line) == RETAILER for line in lines)


def _unfinished_process(dn_name: str, handler: str) -> bool:
	return bool(
		frappe.db.exists(
			process_status.DOCTYPE,
			{
				"reference_doctype": DOCTYPE,
				"reference_name": dn_name,
				"handler": handler,
				"status": ("in", UNFINISHED),
			},
		)
	)


def _confirmed(dn, shipment_id: str | None, comment: str):
	dn.db_set({"bol_shipment_status": CONFIRMED, "bol_shipment_id": shipment_id})
	dn.add_comment("Comment", comment)
	# Invoice only what bol confirmed as shipped (story 12).
	invoices.on_shipment_confirmed(dn)


def _set_status(dn, status: str):
	# Also bumps `modified`, which the sweep waits on.
	dn.db_set("bol_shipment_status", status)


def _log_failure(dn, error: Exception, once: str | None = None) -> bool:
	"""Log a failure for the Delivery Note; with `once` (the failure kind), at most once per 6 hours."""
	details = getattr(error, "details", None) or {"method": "confirm_shipment", "path": PATH}
	if once:
		return log_once(once, dn.name, **details, error=error, reference=_reference(dn))
	log_call(**details, error=error, reference=_reference(dn))
	return True


def _reference(dn) -> tuple[str, str]:
	return (DOCTYPE, dn.name)


def _settings():
	return frappe.get_cached_doc("Bol Settings")
