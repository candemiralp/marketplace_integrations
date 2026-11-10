# Returns (Returns API v10, story 11, ADR-014). Every unhandled FBR return becomes a draft return Delivery Note
# against the Delivery Note that shipped it. Submitting it after inspection reports bol's handling result, and
# process_status follows that to a final state. Never creates financial documents (story 12).
# Every state change on the submitted return goes through db_set: it's never saved again.

import frappe
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return
from frappe import _
from frappe.utils import add_to_date, cint, flt, now_datetime

from marketplace_integrations.bol import offers, process_status
from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import (
	BolError,
	BolMappingError,
	BolRateLimitError,
	BolServerError,
	BolTransientError,
	BolValidationError,
)
from marketplace_integrations.bol.log import add_comment, log_call, log_once
from marketplace_integrations.bol.mappers.offer import format_violations
from marketplace_integrations.bol.mappers.returns import (
	HANDLING_RESULTS,
	handling_body,
	is_handled,
	pick_line,
	unhandled_items,
)
from marketplace_integrations.bol.permissions import check_bol_role

PATH = "/retailer/returns"
VERSION = 10
FBR = "FBR"
DOCTYPE = "Delivery Note"
ITEM_DOCTYPE = "Delivery Note Item"
HANDLER = "return_handling"
IMPORT_METHOD = "import_return"
REPORT_METHOD = "marketplace_integrations.bol.returns.report_handling"
IMPORT_SAVEPOINT = "bol_import_return"
REPORT_SAVEPOINT = "bol_report_return"
OPEN, REPORTED, FAILED = "Open", "Reported", "Failed"
# Statuses of a Bol Process that still has to finish: bol is busy, or our handler needs a Retry.
UNFINISHED = ("PENDING", "HANDLER_ERROR")
# A report bol couldn't take is sent again by the returns poll once things had time to settle.
SWEEP_AFTER_MINUTES = 10
SWEEP_BATCH = 50
# Failures a later attempt may not have: the return stays Open and the sweep reports it again.
PASSING = (BolTransientError, BolRateLimitError, BolServerError)


def returns_on(settings=None) -> bool:
	settings = settings or _settings()
	return bool(settings.enabled and settings.handle_returns)


# --- From bol: the returns poll ----------------------------------------------------------------------------


def poll_returns():
	"""Every 30 minutes: a draft return Delivery Note for each unhandled FBR return without one.

	Frappe's scheduler never runs this job twice at once (ADR-006); the unique bol_rma_id catches the rest.
	"""
	if not returns_on():
		return
	client = get_client()
	try:
		items = unhandled_items(unhandled_returns(client))
	except BolError as e:
		# The next poll tries again; one log per 6 hours is enough.
		log_once("returns", "list", **e.details, error=e)
		raise
	known = _known_rmas([item["rma_id"] for item in items])
	for item in items:
		if item["rma_id"] not in known:
			import_return(item)
			# One return per transaction (overview §4.1 rule 4).
			frappe.db.commit()  # nosemgrep
	sweep_reports()


def unhandled_returns(client) -> list[dict]:
	"""bol's unhandled FBR returns (ReducedReturn), each once. Pages until bol sends an empty page."""
	listed = {}
	page = 1
	while True:
		response = client.request(
			"GET", PATH, version=VERSION, params={"handled": "false", "fulfilment-method": FBR, "page": page}
		)
		page_returns = [ret for ret in (response or {}).get("returns") or [] if ret.get("returnId")]
		for ret in page_returns:
			# A new return can shift the pages between two requests.
			listed.setdefault(ret["returnId"], ret)
		if not page_returns or client.settings.environment == "Demo":
			# The demo has only page 1 (page 2 is a 400, "resource not available").
			return list(listed.values())
		page += 1


def import_return(item: dict) -> str | None:
	"""Create the draft return for one return item (mappers.returns.unhandled_items), inside a savepoint.

	Returns its name; None when there's no Delivery Note to return against, or on failure. A failure never
	raises: it's rolled back, logged once per 6 hours, and the next poll tries again. The caller commits.
	"""
	frappe.db.savepoint(IMPORT_SAVEPOINT)
	try:
		return create_return(item)
	except Exception as e:
		frappe.db.rollback(save_point=IMPORT_SAVEPOINT)
		if not _imported_concurrently(item["rma_id"], e):
			log_once(
				"return",
				item["rma_id"],
				method=IMPORT_METHOD,
				path=PATH,
				request=item,
				error=e,
				message=_("bol return {0} not imported: {1}").format(item["rma_id"], str(e) or type(e).__name__),
			)
	return None


def create_return(item: dict) -> str | None:
	line = find_line(item["order_id"], item["ean"], item["expected_qty"])
	if not line:
		_not_found(item)
		return None
	ret = make_sales_return(line["delivery_note"])
	# Only the returned line, with bol's expected quantity; inspection may lower it before submitting.
	ret.set("items", [row for row in ret.items if row.dn_detail == line["dn_detail"]])
	row = ret.items[0]
	row.idx = 1
	row.qty = -item["expected_qty"]
	row.stock_qty = row.qty * flt(row.conversion_factor or 1)
	ret.update(
		{
			"bol_rma_id": item["rma_id"],
			"bol_return_id": item["return_id"],
			"bol_return_reason": item["reason"],
			"bol_expected_qty": item["expected_qty"],
			"bol_return_status": OPEN,
		}
	)
	ret.flags.ignore_permissions = True
	ret.insert()
	_assign(
		_("bol return {0}: inspect and submit").format(item["rma_id"]),
		(DOCTYPE, ret.name),
		item["rma_id"],
	)
	return ret.name


def find_line(order_id: str, ean: str, qty: int) -> dict | None:
	"""The shipped line of bol order `order_id` that can take back `qty` units of `ean`: {delivery_note,
	dn_detail, available}, from the oldest Delivery Note that covers it. None when there's none."""
	so_names = frappe.get_all("Sales Order", filters={"bol_order_id": order_id, "docstatus": 1}, pluck="name")
	item_codes = offers.items_by_ean(ean) if ean else []
	if not (so_names and item_codes):
		return None
	rows = frappe.get_all(
		ITEM_DOCTYPE,
		filters={
			"parenttype": DOCTYPE,
			"docstatus": 1,
			"against_sales_order": ("in", so_names),
			"item_code": ("in", item_codes),
		},
		fields=["name", "parent", "qty", "returned_qty"],
	)
	if not rows:
		return None
	# Return Delivery Notes carry the Sales Order too: only what was shipped counts.
	shipped = dict(
		frappe.get_all(
			DOCTYPE,
			filters={"name": ("in", list({row.parent for row in rows})), "is_return": 0},
			fields=["name", "creation"],
			as_list=True,
		)
	)
	drafts = _draft_return_qty([row.name for row in rows])
	candidates = [
		{
			"delivery_note": row.parent,
			"dn_detail": row.name,
			"available": flt(row.qty) - flt(row.returned_qty) - drafts.get(row.name, 0),
		}
		for row in rows
		if row.parent in shipped
	]
	candidates.sort(key=lambda line: (shipped[line["delivery_note"]], line["delivery_note"]))
	return pick_line(candidates, qty)


def _draft_return_qty(dn_details: list[str]) -> dict[str, float]:
	"""Units already on draft returns, per shipped line: ERPNext only counts submitted returns."""
	pending = {}
	for row in frappe.get_all(
		ITEM_DOCTYPE,
		filters={"parenttype": DOCTYPE, "docstatus": 0, "dn_detail": ("in", dn_details)},
		fields=["dn_detail", "qty"],
	):
		pending[row.dn_detail] = pending.get(row.dn_detail, 0) + abs(flt(row.qty))
	return pending


def _not_found(item: dict):
	so_name = frappe.db.get_value("Sales Order", {"bol_order_id": item["order_id"]}, "name")
	reference = ("Sales Order", so_name) if so_name else None
	message = _(
		"bol return {0} ({1} × EAN {2} of bol order {3}, reason: {4}): no submitted Delivery Note of this order can take it back, e.g. it was shipped before the cutover. Handle it by hand, and report it in the bol seller dashboard."
	).format(item["rma_id"], item["expected_qty"], item["ean"], item["order_id"], item["reason"] or "-")
	if log_once(
		"return_not_found",
		item["rma_id"],
		method=IMPORT_METHOD,
		path=PATH,
		request=item,
		error=BolMappingError(message, reason="no_delivery_note"),
		reference=reference,
	):
		_assign(message, reference, item["rma_id"])


def _known_rmas(rma_ids: list[str]) -> set[str]:
	"""RMAs with a return Delivery Note (any state), or being reported to bol right now."""
	if not rma_ids:
		return set()
	known = set(frappe.get_all(DOCTYPE, filters={"bol_rma_id": ("in", rma_ids)}, pluck="bol_rma_id"))
	# Reported without a stock return: the draft is gone, but bol still lists it until it has processed it.
	payloads = frappe.get_all(
		process_status.DOCTYPE,
		filters={"handler": HANDLER, "status": ("in", UNFINISHED)},
		pluck="payload",
	)
	return known | {str(frappe.parse_json(payload or "{}").get("rma_id")) for payload in payloads}


def _imported_concurrently(rma_id: str, error: Exception) -> bool:
	"""True when another run created the return while this one was at it: already done, not a failure."""
	if isinstance(error, frappe.DuplicateEntryError):
		return True
	# Frappe raises UniqueValidationError(doctype, name, db_error), and the db error names the key.
	if isinstance(error, frappe.UniqueValidationError) and "bol_rma_id" in str(error):
		return True
	return bool(frappe.db.exists(DOCTYPE, {"bol_rma_id": rma_id}))


# --- Delivery Note events (doc_events) -------------------------------------------------------------------


def onload_delivery_note(doc, method=None):
	# Tells the form whether to offer Report to bol without stock return.
	if doc.docstatus == 0 and doc.is_return and doc.bol_rma_id and returns_on():
		doc.set_onload("bol_returns_on", True)


def before_submit_delivery_note(doc, method=None):
	# Checked even while Handle Returns is off: bol is told once it's switched on (sweep_reports).
	if not (doc.is_return and doc.bol_rma_id):
		return
	if not doc.bol_handling_result:
		frappe.throw(
			_("Choose the bol Handling Result before submitting: it's what bol is told about this return."),
			title=_("bol Return"),
		)
	quantity = returned_quantity(doc)
	if quantity != cint(quantity):
		frappe.throw(_("bol takes back whole units, not {0}.").format(quantity), title=_("bol Return"))
	if quantity > cint(doc.bol_expected_qty):
		frappe.throw(
			_("bol expects {0} units back for return {1}; this return has {2}.").format(
				cint(doc.bol_expected_qty), doc.bol_rma_id, _units(quantity)
			),
			title=_("bol Return"),
		)


def on_submit_delivery_note(doc, method=None):
	if doc.is_return and doc.bol_rma_id and returns_on():
		enqueue_report(doc.name)


def returned_quantity(doc) -> float:
	return sum(abs(flt(row.qty)) for row in doc.items)


# --- Reporting the handling result ---------------------------------------------------------------------------


def enqueue_report(dn_name: str):
	frappe.enqueue(
		REPORT_METHOD,
		dn_name=dn_name,
		job_id=f"bol_return::{dn_name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def report_handling(dn_name: str):
	"""Report the submitted return's handling result to bol, unless it's reported or being reported already.

	bol errors are handled here (status, log, comment); anything else marks it Failed and is raised.
	"""
	if not returns_on():
		return
	dn = frappe.get_doc(DOCTYPE, dn_name, for_update=True)
	if (
		dn.docstatus != 1
		or not dn.bol_rma_id
		or dn.bol_return_status == REPORTED
		or _unfinished_process(dn.name)
	):
		return
	frappe.db.savepoint(REPORT_SAVEPOINT)
	try:
		_report(dn)
	except BolError as e:
		frappe.db.rollback(save_point=REPORT_SAVEPOINT)
		_report_failed(dn, e)
	except Exception as e:
		frappe.db.rollback(save_point=REPORT_SAVEPOINT)
		_set_status(dn, FAILED)
		_log_failure(dn, e)
		dn.add_comment("Comment", _("Return not reported to bol: {0}").format(str(e) or type(e).__name__))
		# Keep the status, log and comment: the job's rollback would drop them.
		frappe.db.commit()  # nosemgrep
		raise


def _report(dn):
	quantity = cint(returned_quantity(dn))
	# A PUT is retried by the client on a transient error; sent twice, bol refuses the second (400): _refused.
	response = get_client().request(
		"PUT",
		f"{PATH}/{dn.bol_rma_id}",
		version=VERSION,
		json=handling_body(dn.bol_handling_result, quantity),
		reference=_reference(dn),
	)
	process_status.track(response, HANDLER, dn, payload=_payload(dn, dn.bol_handling_result, quantity))
	if dn.bol_return_status != OPEN:
		_set_status(dn, OPEN)


def _report_failed(dn, error: BolError):
	if isinstance(error, BolValidationError):
		_refused(dn, error)
		return
	if isinstance(error, PASSING):
		# Open again (also after a Retry), so the returns poll sends it once more.
		_set_status(dn, OPEN)
		if _log_failure(dn, error, once="return_report"):
			dn.add_comment(
				"Comment",
				_("bol couldn't be reached to report this return ({0}). It's tried again automatically.").format(
					str(error)
				),
			)
		return
	# Credentials, an unexpected answer: a person acts.
	_set_status(dn, FAILED)
	_log_failure(dn, error)
	dn.add_comment("Comment", _("Return not reported to bol: {0}").format(str(error)))


def _refused(dn, error: BolValidationError):
	# Usually bol has the RMA handled already (a second PUT, or the seller dashboard): then it's done.
	if handled_at_bol(dn.bol_return_id, dn.bol_rma_id, _reference(dn)):
		_reported(
			dn,
			_("bol had handled return {0} already. Marked reported without sending it again.").format(
				dn.bol_rma_id
			),
		)
		return
	_set_status(dn, FAILED)
	_log_failure(dn, error)
	dn.add_comment(
		"Comment",
		_("bol refused the return report: {0}").format(format_violations(error.violations) or str(error)),
	)


def handled_at_bol(return_id: str | None, rma_id: str, reference: tuple[str, str]) -> bool:
	"""Whether bol has the RMA handled. A bol error counts as not handled (logged once per 6 hours)."""
	if not return_id:
		return False
	try:
		detail = get_client().request("GET", f"{PATH}/{return_id}", version=VERSION)
	except BolError as e:
		log_once("return_check", rma_id, **e.details, error=e, reference=reference)
		return False
	return is_handled(detail, rma_id)


def sweep_reports():
	"""Queue the submitted returns that bol hasn't been told about and that nothing else will finish: bol
	couldn't be reached, the job was lost, or Handle Returns was off at submit. Failed waits for a person."""
	names = frappe.get_all(
		DOCTYPE,
		filters={
			"docstatus": 1,
			"is_return": 1,
			"bol_rma_id": ("is", "set"),
			"bol_return_status": OPEN,
			"modified": ("<", add_to_date(now_datetime(), minutes=-SWEEP_AFTER_MINUTES)),
		},
		order_by="modified asc",
		limit=SWEEP_BATCH,
		pluck="name",
	)
	for name in names:
		if not _unfinished_process(name):
			enqueue_report(name)


# --- Buttons -----------------------------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def retry_return_report(dn_name: str) -> dict:
	"""Report a Failed return again. bol refusing it because it's handled already marks it reported."""
	# Frappe enforces the type hints of whitelisted functions.
	check_bol_role()
	frappe.has_permission(DOCTYPE, "write", dn_name, throw=True)
	_check_on()
	if frappe.db.get_value(DOCTYPE, dn_name, ["docstatus", "bol_return_status"]) != (1, FAILED):
		frappe.throw(_("Only a submitted bol return that Failed can be retried."), title=_("Not Allowed"))
	try:
		report_handling(dn_name)
	except Exception as e:
		# Logged and commented by report_handling; the user gets a message, not a traceback.
		frappe.throw(
			_("Retrying failed: {0}. Details are in the Ecommerce Integration Log.").format(
				str(e) or type(e).__name__
			),
			title=_("Retry Failed"),
		)
	return {"status": frappe.db.get_value(DOCTYPE, dn_name, "bol_return_status")}


@frappe.whitelist(methods=["POST"])
def report_without_return(dn_name: str, handling_result: str) -> dict:
	"""Report a draft bol return with nothing coming back (ERPNext can't submit a zero-qty return), then delete
	the draft (ADR-014). Returns {return_against}: the Delivery Note bol's answer is shown on."""
	check_bol_role()
	frappe.has_permission(DOCTYPE, "write", dn_name, throw=True)
	frappe.has_permission(DOCTYPE, "delete", dn_name, throw=True)
	_check_on()
	if handling_result not in HANDLING_RESULTS:
		frappe.throw(_("Unknown bol handling result: {0}").format(handling_result), title=_("Not Allowed"))
	# Locked: a double click waits here, then finds the draft gone.
	dn = frappe.get_doc(DOCTYPE, dn_name, for_update=True)
	if dn.docstatus != 0 or not dn.is_return or not dn.bol_rma_id:
		frappe.throw(_("Only a draft bol return can be reported without a stock return."), title=_("Not Allowed"))
	quantity = cint(dn.bol_expected_qty)
	original = frappe.get_doc(DOCTYPE, dn.return_against)
	try:
		response = get_client().request(
			"PUT",
			f"{PATH}/{dn.bol_rma_id}",
			version=VERSION,
			json=handling_body(handling_result, quantity),
			reference=_reference(dn),
		)
		# Followed on the original Delivery Note: the draft is deleted below.
		process_status.track(
			response, HANDLER, original, payload={**_payload(dn, handling_result, quantity), "without_return": True}
		)
	except BolError as e:
		log_call(**(e.details or {"method": "PUT", "path": PATH}), error=e, reference=_reference(dn))
		frappe.throw(_without_return_failed(dn, e), title=_("Not Reported to bol"))

	message = _(
		"bol return {0} reported as {1} ({2} units) without a stock return. Draft return {3} was deleted."
	).format(dn.bol_rma_id, handling_result, quantity, dn.name)
	original.add_comment("Comment", message)
	frappe.delete_doc(DOCTYPE, dn.name, ignore_permissions=True)
	# The draft's timeline goes with it: the log keeps what happened.
	log_call(method="report_without_return", path=f"{PATH}/{dn.bol_rma_id}", message=message, reference=_reference(original))
	return {"return_against": original.name}


def _without_return_failed(dn, error: BolError) -> str:
	if isinstance(error, BolValidationError) and handled_at_bol(dn.bol_return_id, dn.bol_rma_id, _reference(dn)):
		return _(
			"bol has handled return {0} already (e.g. in the seller dashboard). Submit this draft if stock came back, otherwise delete it."
		).format(dn.bol_rma_id)
	if isinstance(error, BolValidationError):
		return _("bol refused the return report: {0}").format(format_violations(error.violations) or str(error))
	return _("Return not reported to bol: {0}").format(str(error))


# --- Process handlers (process_status) ----------------------------------------------------------------------


def on_return_success(process, entity_id, payload):
	reference = (process.reference_doctype, process.reference_name)
	if payload.get("without_return"):
		add_comment(
			reference,
			_("bol processed return {0}: {1}, without a stock return.").format(
				payload.get("rma_id"), payload.get("handling_result")
			),
		)
		return
	if not frappe.db.exists(*reference):
		return
	dn = frappe.get_doc(*reference)
	if dn.bol_return_status != REPORTED:
		_reported(
			dn,
			_("bol processed return {0}: {1}, {2} units.").format(
				payload.get("rma_id"), payload.get("handling_result"), payload.get("quantity")
			),
		)


def on_return_failure(process, message, payload):
	# process_status has logged bol's errorMessage and commented it on the Delivery Note already.
	reference = (process.reference_doctype, process.reference_name)
	handled = handled_at_bol(payload.get("return_id"), payload.get("rma_id"), reference)
	if payload.get("without_return"):
		add_comment(
			reference,
			_("bol had handled return {0} already.").format(payload.get("rma_id"))
			if handled
			else _(
				"bol return {0} is still open at bol: it comes back as a draft return on the next bol returns poll."
			).format(payload.get("rma_id")),
		)
		return
	if not frappe.db.exists(*reference):
		return
	dn = frappe.get_doc(*reference)
	if handled:
		_reported(dn, _("bol had handled return {0} already. Marked reported.").format(payload.get("rma_id")))
	elif dn.bol_return_status != REPORTED:
		_set_status(dn, FAILED)


process_status.register(HANDLER, on_return_success, on_return_failure)


# --- Helpers ------------------------------------------------------------------------------------------------


def _payload(dn, handling_result: str, quantity: int) -> dict:
	return {
		"rma_id": dn.bol_rma_id,
		"return_id": dn.bol_return_id,
		"handling_result": handling_result,
		"quantity": quantity,
	}


def _assign(description: str, reference: tuple[str, str] | None, rma_id: str):
	"""One open ToDo per RMA for the Notify User; none when no user is set."""
	user = _settings().notify_user
	if not user or frappe.db.exists(
		"ToDo", {"allocated_to": user, "status": "Open", "description": ("like", f"%{rma_id}%")}
	):
		return
	reference_type, reference_name = reference or (None, None)
	frappe.get_doc(
		{
			"doctype": "ToDo",
			"allocated_to": user,
			"reference_type": reference_type,
			"reference_name": reference_name,
			"description": description,
		}
	).insert(ignore_permissions=True)


def _check_on():
	if not returns_on():
		frappe.throw(_("Switch on Handle Returns in Bol Settings first."), title=_("bol Disabled"))


def _unfinished_process(dn_name: str) -> bool:
	return bool(
		frappe.db.exists(
			process_status.DOCTYPE,
			{
				"reference_doctype": DOCTYPE,
				"reference_name": dn_name,
				"handler": HANDLER,
				"status": ("in", UNFINISHED),
			},
		)
	)


def _reported(dn, comment: str):
	dn.db_set("bol_return_status", REPORTED)
	dn.add_comment("Comment", comment)


def _set_status(dn, status: str):
	# Also bumps `modified`, which the sweep waits on.
	dn.db_set("bol_return_status", status)


def _log_failure(dn, error: Exception, once: str | None = None) -> bool:
	"""Log a failure for the return; with `once` (the failure kind), at most once per 6 hours."""
	details = getattr(error, "details", None) or {"method": "report_handling", "path": f"{PATH}/{dn.bol_rma_id}"}
	if once:
		return log_once(once, dn.name, **details, error=error, reference=_reference(dn))
	log_call(**details, error=error, reference=_reference(dn))
	return True


def _units(qty) -> str:
	# bol counts whole units: 2, not 2.0.
	return f"{flt(qty):g}"


def _reference(dn) -> tuple[str, str]:
	return (DOCTYPE, dn.name)


def _settings():
	return frappe.get_cached_doc("Bol Settings")
