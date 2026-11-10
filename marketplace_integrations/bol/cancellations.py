# Cancellations (Orders API v10, story 10, ADR-013), in both directions. bol's cancellations and buyers' requests
# reach the Sales Order on the order poll, plus a daily check for orders that left bol's OPEN list. A seller cancels
# lines with Cancel on bol, and the Sales Order follows once bol has processed them (process_status).
# The Sales Order line's qty is always net of bol_qty_cancelled: only what bol cancels beyond it is taken off.

import json

import frappe
from erpnext.controllers.accounts_controller import update_child_qty_rate
from frappe import _
from frappe.utils import add_to_date, flt, now_datetime

from marketplace_integrations.bol import orders, process_status
from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import BolError, BolMappingError, BolValidationError
from marketplace_integrations.bol.log import add_comment, log_call, log_once
from marketplace_integrations.bol.mappers.cancellation import (
	REASON_CODES,
	cancellation_body,
	changes,
	has_cancellation_fields,
)
from marketplace_integrations.bol.mappers.offer import format_violations
from marketplace_integrations.bol.permissions import check_bol_role

PATH = "/retailer/orders/cancellation"
DOCTYPE = "Sales Order"
ITEM_DOCTYPE = "Sales Order Item"
HANDLER = "cancellation"
REQUEST_SAVEPOINT = "bol_cancellation_request"
APPLY_SAVEPOINT = "bol_cancellation_apply"
# Orders that left bol's OPEN list are checked this long after import.
RECENT_DAYS = 30
# Statuses of a Bol Process that still has to finish: bol is busy, or our handler needs a Retry.
UNFINISHED = ("PENDING", "HANDLER_ERROR")


# --- From bol: the order poll and the daily check ----------------------------------------------------------


def sync_existing_order_changes(listed: list[dict], client=None):
	"""Apply bol's cancellations and buyers' requests to the Sales Orders of imported open orders (order poll).

	`listed` are bol's list entries (ReducedOrder); an entry without the cancellation fields is fetched in full.
	Commits after each Sales Order.
	"""
	if not listed:
		return
	so_names = dict(
		frappe.get_all(
			DOCTYPE,
			filters={"bol_order_id": ("in", [order["orderId"] for order in listed]), "docstatus": ("<", 2)},
			fields=["bol_order_id", "name"],
			as_list=True,
		)
	)
	for order in listed:
		so_name = so_names.get(order["orderId"])
		if not so_name:
			continue
		if not has_cancellation_fields(order):
			order = _fetch(order["orderId"], so_name, client)
		if order:
			sync_order(so_name, order)
		# One record per transaction (overview §4.1 rule 4).
		frappe.db.commit()  # nosemgrep


def sync_recent_orders():
	"""Daily: check the recent bol Sales Orders that aren't fully delivered, which the OPEN list may no longer show
	(bol cancelled every open unit, or the rest was shipped)."""
	if not orders.orders_on(_settings()):
		return
	rows = frappe.get_all(
		DOCTYPE,
		filters={
			"docstatus": ("<", 2),
			"bol_order_id": ("is", "set"),
			"per_delivered": ("<", 100),
			"status": ("!=", "Closed"),
			"creation": (">=", add_to_date(now_datetime(), days=-RECENT_DAYS)),
		},
		fields=["name", "bol_order_id"],
		order_by="creation asc",
	)
	client = get_client() if rows else None
	for row in rows:
		order = _fetch(row.bol_order_id, row.name, client)
		if order:
			sync_order(row.name, order)
		frappe.db.commit()  # nosemgrep


def resync(so_name: str, client=None):
	"""GET the Sales Order's bol order and apply it. A bol error is logged, not raised: the poll catches up."""
	order_id = frappe.db.get_value(DOCTYPE, so_name, "bol_order_id")
	order = _fetch(order_id, so_name, client) if order_id else None
	if order:
		sync_order(so_name, order)


def sync_order(so_name: str, order: dict):
	"""Bring one Sales Order in line with its bol order (list entry or detail). The caller commits.

	Never raises: a failure is rolled back to its savepoint, logged once per 6 hours and retried on the next poll.
	"""
	if frappe.db.get_value(DOCTYPE, so_name, "docstatus") not in (0, 1):
		return
	rows = _bol_rows(so_name)
	cancelled, requested = changes(order, rows)
	if requested:
		_safely(REQUEST_SAVEPOINT, "cancellation_request", so_name, _record_requests, so_name, requested, rows)
	if cancelled:
		_safely(APPLY_SAVEPOINT, "cancellation", so_name, apply_cancelled_qty, so_name, cancelled)


def _record_requests(so_name: str, requested: list[str], rows: dict[str, dict]):
	for order_item_id in requested:
		frappe.db.set_value(
			ITEM_DOCTYPE, rows[order_item_id]["name"], "bol_cancellation_requested", 1, update_modified=False
		)
	lines = ", ".join(_line_label(rows[order_item_id]) for order_item_id in requested)
	so = frappe.get_doc(DOCTYPE, so_name)
	so.add_comment(
		"Comment",
		_(
			"The buyer asked bol to cancel {0}. Not shipped yet: use bol → Cancel on bol with reason REQUESTED_BY_CUSTOMER. Already shipped: nothing to do, bol asks the buyer to return it."
		).format(lines),
	)
	_assign_request(so, lines)


def _assign_request(so, lines: str):
	"""One open ToDo per Sales Order for the Notify User; none when no user is set."""
	user = _settings().notify_user
	if not user or frappe.db.exists(
		"ToDo",
		{"reference_type": DOCTYPE, "reference_name": so.name, "allocated_to": user, "status": "Open"},
	):
		return
	frappe.get_doc(
		{
			"doctype": "ToDo",
			"allocated_to": user,
			"reference_type": DOCTYPE,
			"reference_name": so.name,
			"priority": "High",
			"description": _("bol: the buyer asked to cancel {0} of Sales Order {1}.").format(lines, so.name),
		}
	).insert(ignore_permissions=True)


# --- Applying a cancellation ----------------------------------------------------------------------------------


def apply_cancelled_qty(so_name: str, cancelled: dict[str, float]):
	"""Take bol's cancelled units ({order_item_id: bol's quantityCancelled}) off the Sales Order. Idempotent.

	Every unit cancelled with nothing delivered → the Sales Order is cancelled (a draft is deleted). Otherwise
	ERPNext's update_child_qty_rate lowers the lines and removes those with nothing left. A line is never taken
	below its delivered qty: that is logged instead. Raises on any other failure; the caller rolls back.
	"""
	so = frappe.get_doc(DOCTYPE, so_name, for_update=True)
	if so.docstatus == 2:
		return
	remaining = {row.name: flt(row.qty) for row in so.items}
	applied = []  # (row, bol's quantityCancelled, units taken off)
	for row in so.items:
		new_cancelled = cancelled.get(row.bol_order_item_id) if row.bol_order_item_id else None
		delta = flt(new_cancelled) - flt(row.bol_qty_cancelled)
		if new_cancelled is None or delta <= 0:
			continue
		qty = flt(flt(row.qty) - delta, row.precision("qty"))
		if qty < flt(row.delivered_qty):
			_below_delivered(so, row, delta)
			continue
		remaining[row.name] = qty
		applied.append((row, new_cancelled, delta))
	if not applied:
		return

	lines = ", ".join(_line_label(row, delta) for row, _new, delta in applied)
	if not any(remaining.values()) and not any(flt(row.delivered_qty) for row in so.items):
		_cancel_whole(so, applied, lines)
		return
	if so.docstatus == 1:
		_update_items(so, remaining)
	else:
		_update_draft(so, remaining)
	for row, new_cancelled, _delta in applied:
		if remaining[row.name]:
			frappe.db.set_value(ITEM_DOCTYPE, row.name, "bol_qty_cancelled", new_cancelled, update_modified=False)
	so.add_comment("Comment", _("bol cancelled {0}. The Sales Order was adjusted.").format(lines))


def _cancel_whole(so, applied: list[tuple], lines: str):
	if so.docstatus == 0:
		frappe.delete_doc(DOCTYPE, so.name, ignore_permissions=True)
		# The draft's timeline goes with it: the log keeps what happened.
		log_call(
			method="apply_cancelled_qty",
			path=f"{orders.PATH}/{so.bol_order_id}",
			request={"cancelled": {row.bol_order_item_id: new for row, new, _delta in applied}},
			message=_("bol cancelled order {0} completely ({1}): draft Sales Order {2} deleted.").format(
				so.bol_order_id, lines, so.name
			),
		)
		return
	for row, new_cancelled, _delta in applied:
		row.db_set("bol_qty_cancelled", new_cancelled, update_modified=False)
	so.flags.bol_cancelled = True
	so.flags.ignore_permissions = True
	so.cancel()
	so.add_comment("Comment", _("bol cancelled the whole order ({0}). The Sales Order was cancelled.").format(lines))


def _update_items(so, remaining: dict[str, float]):
	"""ERPNext's Update Items: keeps reservations, billing and totals consistent. Rows left out are removed."""
	trans_items = [
		{
			"docname": row.name,
			"idx": row.idx,
			"item_code": row.item_code,
			"qty": remaining[row.name],
			"rate": row.rate,
			"uom": row.uom,
			"conversion_factor": row.conversion_factor,
			"delivery_date": row.delivery_date,
		}
		for row in so.items
		if remaining[row.name]
	]
	update_child_qty_rate(DOCTYPE, json.dumps(trans_items, default=str), so.name)


def _update_draft(so, remaining: dict[str, float]):
	so.set("items", [row for row in so.items if remaining[row.name]])
	for row in so.items:
		row.qty = remaining[row.name]
	so.flags.ignore_permissions = True
	so.save()


def _below_delivered(so, row, delta: float):
	message = _(
		"bol cancelled {0} more of {1} (bol order item {2}), but {3} of the {4} on this line are delivered already. The line was left as it is: check the order with bol."
	).format(_units(delta), row.item_code, row.bol_order_item_id, _units(row.delivered_qty), _units(row.qty))
	if log_once(
		"cancellation_qty",
		row.name,
		method="apply_cancelled_qty",
		path=f"{orders.PATH}/{so.bol_order_id}",
		error=BolMappingError(message, reason="below_delivered"),
		reference=(DOCTYPE, so.name),
	):
		so.add_comment("Comment", message)


# --- From the seller: Cancel on bol ---------------------------------------------------------------------------


def onload_sales_order(doc, method=None):
	# Tells the form whether to offer Cancel on bol, and with which reasons.
	if doc.docstatus == 1 and doc.bol_order_id and orders.orders_on(_settings()):
		doc.set_onload("bol_cancel_reasons", REASON_CODES)


def before_cancel_sales_order(doc, method=None):
	if not doc.bol_order_id or doc.flags.bol_cancelled or not orders.orders_on(_settings()):
		return
	if any(row.bol_order_item_id and flt(row.qty) > flt(row.delivered_qty) for row in doc.items):
		frappe.throw(
			_(
				"This Sales Order has lines that are still open on bol. Use Cancel on bol: the Sales Order follows once bol has cancelled them."
			),
			title=_("Use Cancel on bol"),
		)


@frappe.whitelist(methods=["POST"])
def cancel_bol_items(sales_order: str, so_details: str | list, reason_code: str) -> dict:
	"""Cancel Sales Order lines on bol, one request per line (bol takes one order item at a time).

	The Sales Order follows when bol has processed each one. Returns {sent, failed: [{so_detail, message}], resynced}.
	"""
	# Frappe enforces the type hints of whitelisted functions.
	check_bol_role()
	frappe.has_permission(DOCTYPE, "write", sales_order, throw=True)
	if not orders.orders_on(_settings()):
		frappe.throw(_("Switch on Import Orders in Bol Settings first."), title=_("bol Disabled"))
	if reason_code not in REASON_CODES:
		frappe.throw(_("Unknown bol cancellation reason: {0}").format(reason_code), title=_("Not Allowed"))
	so_details = _parse_lines(so_details)
	# Locked: a double click waits here, then finds the lines being cancelled already.
	so = frappe.get_doc(DOCTYPE, sales_order, for_update=True)
	rows = _cancellable_rows(so, so_details)

	client = get_client()
	sent, failed, rejected = [], [], False
	for row in rows:
		try:
			_send(client, so, row, reason_code)
			sent.append(row.name)
		except BolError as e:
			log_call(**(e.details or {"method": "PUT", "path": PATH}), error=e, reference=(DOCTYPE, so.name))
			failed.append({"so_detail": row.name, "message": _send_failed(row, e)})
			# Usually bol cancelled or shipped the item already: the Sales Order is brought in line with bol.
			rejected = rejected or isinstance(e, BolValidationError)
	if rejected:
		resync(so.name, client)
	if not sent and not rejected:
		# Nothing changed: the error is the answer (the logs are kept, ADR-005).
		frappe.throw("<br>".join(failure["message"] for failure in failed), title=_("Not Cancelled on bol"))
	return {"sent": sent, "failed": failed, "resynced": rejected}


def _send(client, so, row, reason_code: str):
	# PUT is retried by the client on a transient error; sent twice, bol refuses the second (400) and we resync.
	response = client.request(
		"PUT",
		PATH,
		version=orders.VERSION,
		json=cancellation_body(row.bol_order_item_id, reason_code),
		reference=(DOCTYPE, so.name),
	)
	process_status.track(
		response, HANDLER, so, payload={"so_detail": row.name, "order_item_id": row.bol_order_item_id}
	)


def _parse_lines(so_details) -> list[str]:
	if isinstance(so_details, str):
		try:
			so_details = json.loads(so_details)
		except ValueError:
			so_details = None
	if not (isinstance(so_details, list) and so_details and all(isinstance(d, str) for d in so_details)):
		frappe.throw(_("Choose the lines to cancel on bol."), title=_("Not Allowed"))
	return list(dict.fromkeys(so_details))


def _cancellable_rows(so, so_details: list[str]) -> list:
	if so.docstatus != 1 or not so.bol_order_id:
		frappe.throw(
			_("Only a submitted Sales Order of a bol order can be cancelled on bol."), title=_("Not Allowed")
		)
	rows = {row.name: row for row in so.items}
	pending = _pending_lines(so.name)
	chosen = []
	for so_detail in so_details:
		row = rows.get(so_detail)
		if not (row and row.bol_order_item_id):
			frappe.throw(
				_("Line {0} isn't an open bol line of Sales Order {1}.").format(so_detail, so.name),
				title=_("Not Allowed"),
			)
		if flt(row.delivered_qty):
			frappe.throw(
				_("Row {0}: {1} is delivered already, so bol can't cancel it. Process a return instead.").format(
					row.idx, row.item_code
				),
				title=_("Not Allowed"),
			)
		if row.name in pending:
			frappe.throw(
				_("Row {0}: {1} is being cancelled on bol already.").format(row.idx, row.item_code),
				title=_("Not Allowed"),
			)
		chosen.append(row)
	return chosen


def _pending_lines(so_name: str) -> set[str]:
	payloads = frappe.get_all(
		process_status.DOCTYPE,
		filters={
			"reference_doctype": DOCTYPE,
			"reference_name": so_name,
			"handler": HANDLER,
			"status": ("in", UNFINISHED),
		},
		pluck="payload",
	)
	return {frappe.parse_json(payload or "{}").get("so_detail") for payload in payloads}


def _send_failed(row, error: BolError) -> str:
	if isinstance(error, BolValidationError):
		return _("Row {0}: bol refused to cancel {1}: {2}").format(
			row.idx, row.item_code, format_violations(error.violations) or str(error)
		)
	return _("Row {0}: {1} not cancelled on bol: {2}").format(row.idx, row.item_code, str(error))


# --- Process handlers (process_status) ----------------------------------------------------------------------


def on_cancellation_success(process, entity_id, payload):
	row = frappe.db.get_value(
		ITEM_DOCTYPE,
		payload.get("so_detail"),
		["parent", "bol_order_item_id", "qty", "bol_qty_cancelled"],
		as_dict=True,
	)
	if not row:
		# Removed already: the poll applied bol's cancellation first.
		return
	# bol cancels the whole order item: every unit still on the line.
	apply_cancelled_qty(row.parent, {row.bol_order_item_id: flt(row.bol_qty_cancelled) + flt(row.qty)})


def on_cancellation_failure(process, message, payload):
	# process_status has logged bol's errorMessage and commented it on the Sales Order already. The usual cause is
	# that bol had cancelled or shipped the item: bring the Sales Order in line with bol.
	resync(process.reference_name)


process_status.register(HANDLER, on_cancellation_success, on_cancellation_failure)


# --- Helpers ------------------------------------------------------------------------------------------------


def _fetch(order_id: str, so_name: str, client=None) -> dict | None:
	"""bol's order detail; None (logged once per 6 hours) when bol can't be asked. The next run tries again."""
	try:
		return (client or get_client()).request("GET", f"{orders.PATH}/{order_id}", version=orders.VERSION)
	except BolError as e:
		log_once("cancellation_sync", order_id, **e.details, error=e, reference=(DOCTYPE, so_name))
		return None


def _safely(savepoint: str, kind: str, so_name: str, fn, *args):
	frappe.db.savepoint(savepoint)
	try:
		fn(*args)
	except Exception as e:
		# update_child_qty_rate refused, a locked row, …: nothing half-done stays, and the next poll retries.
		frappe.db.rollback(save_point=savepoint)
		details = getattr(e, "details", None) or {"method": fn.__name__, "path": PATH, "request": list(args[1:])}
		if log_once(kind, so_name, **details, error=e, reference=(DOCTYPE, so_name)):
			add_comment(
				(DOCTYPE, so_name),
				_("bol cancellation not applied: {0}. It's tried again on the next bol order poll.").format(
					str(e) or type(e).__name__
				),
			)


def _bol_rows(so_name: str) -> dict[str, dict]:
	rows = frappe.get_all(
		ITEM_DOCTYPE,
		filters={"parent": so_name, "parenttype": DOCTYPE, "bol_order_item_id": ("is", "set")},
		fields=["name", "bol_order_item_id", "bol_qty_cancelled", "bol_cancellation_requested", "item_code", "qty"],
	)
	return {row.bol_order_item_id: row for row in rows}


def _line_label(row, qty: float | None = None) -> str:
	return _("{0} × {1} (bol order item {2})").format(
		_units(qty if qty is not None else row.qty), row.item_code, row.bol_order_item_id
	)


def _units(qty) -> str:
	# bol counts whole units: 2, not 2.0.
	return f"{flt(qty):g}"


def _settings():
	return frappe.get_cached_doc("Bol Settings")
