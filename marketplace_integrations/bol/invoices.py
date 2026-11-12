# Accounting (Invoices and Shipments API v10, story 12, ADR-015).
# A. Each bol Delivery Note is invoiced on the bol receivable once bol has confirmed its shipment.
# B. Each bol return reported as received or exchanged gets a credit note against that invoice.
# C. bol's own invoices (commission, labels, other fees) become draft Purchase Invoices for a person to review.
# D. When a business buyer asks bol for an invoice, the Sales Invoice is uploaded as a PDF.
# No Payment Entries: bol pays out net amounts, matched against the receivable and the supplier in Bank Reconciliation.

import frappe
from erpnext.controllers.accounts_controller import get_taxes_and_charges
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice
from frappe import _
from frappe.utils import add_days, get_system_timezone, getdate, today

from marketplace_integrations.bol import monitoring, process_status, returns, shipments
from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import BolError, BolMappingError, BolNotFoundError
from marketplace_integrations.bol.log import add_comment, log_call, log_once
from marketplace_integrations.bol.mappers import bol_invoice
from marketplace_integrations.bol.permissions import check_bol_role

VERSION = 10
DN = "Delivery Note"
SI = "Sales Invoice"
PI = "Purchase Invoice"
INVOICE_METHOD = "marketplace_integrations.bol.invoices.invoice_delivery_note"
INVOICE_SAVEPOINT = "bol_sales_invoice"
# Return handling results that give the buyer their money back.
CREDITED_RESULTS = ("RETURN_RECEIVED", "EXCHANGE_PRODUCT")
# The sweep looks this far back for Delivery Notes that never got their invoice.
SWEEP_DAYS = 30

INVOICES_PATH = "/retailer/invoices"
IMPORT_METHOD = "import_bol_invoice"
IMPORT_SAVEPOINT = "bol_purchase_invoice"
PURCHASE_TEMPLATE = "Purchase Taxes and Charges Template"
PDF_SIGNATURE = b"%PDF-"

REQUESTS_PATH = "/retailer/shipments/invoices/requests"
UPLOAD_PATH = "/retailer/shipments/invoices"
UPLOAD_METHOD = "upload_invoice"
UPLOAD_SAVEPOINT = "bol_upload_invoice"
UPLOAD_HANDLER = "invoice_upload"
PRINT_FORMAT = "bol Sales Invoice"
# Open requests, and uploads bol rejected because it found a virus: those are uploaded again.
REQUEST_STATES = ("OPEN", "UPLOAD_ERROR")
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
# Statuses of a Bol Process that still has to finish: bol is busy, or our handler needs a Retry.
UNFINISHED = ("PENDING", "HANDLER_ERROR")


def invoicing_on(settings=None) -> bool:
	settings = settings or _settings()
	return bool(settings.enabled and settings.auto_invoice)


def imports_on(settings=None) -> bool:
	settings = settings or _settings()
	return bool(settings.enabled and settings.import_bol_invoices)


def uploads_on(settings=None) -> bool:
	settings = settings or _settings()
	return bool(invoicing_on(settings) and settings.upload_invoices)


# --- A/B. Sales Invoices and credit notes ------------------------------------------------------------------


def on_shipment_confirmed(dn):
	"""Called by shipments once bol has confirmed the Delivery Note's shipment."""
	if invoicing_on():
		enqueue_invoice(dn.name)


def on_return_reported(dn):
	"""Called by returns once bol has the return's handling result."""
	if invoicing_on():
		enqueue_invoice(dn.name)


def enqueue_invoice(dn_name: str):
	frappe.enqueue(
		INVOICE_METHOD,
		dn_name=dn_name,
		job_id=f"bol_invoice::{dn_name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def invoice_delivery_note(dn_name: str) -> str | None:
	"""Submit the Sales Invoice (or credit note) of a bol Delivery Note that's due one. Returns its name.

	A failure is rolled back, logged once per 6 hours and commented on the Delivery Note, whose form then offers
	Create bol Sales Invoice; the daily sweep tries again. It's never raised: the job keeps the log and comment.
	"""
	if not invoicing_on():
		return None
	dn = frappe.get_doc(DN, dn_name, for_update=True)
	if not invoice_due(dn) or invoiced(dn.name):
		return None
	frappe.db.savepoint(INVOICE_SAVEPOINT)
	try:
		return make_invoice(dn)
	except Exception as e:
		frappe.db.rollback(save_point=INVOICE_SAVEPOINT)
		message = _("{0} not created for {1}: {2}").format(_kind(dn), dn.name, str(e) or type(e).__name__)
		if log_once(
			"sales_invoice",
			dn.name,
			method="invoice_delivery_note",
			error=e,
			reference=(DN, dn.name),
			message=message,
		):
			dn.add_comment(
				"Comment", _("{0} Fix it, then use Create bol Sales Invoice (bol menu).").format(message)
			)
		return None


def invoice_due(dn) -> bool:
	"""A submitted shipment bol confirmed, or a return bol has as received or exchanged, with units back."""
	if dn.docstatus != 1 or not dn.bol_order_id:
		return False
	if not dn.is_return:
		return dn.bol_shipment_status == shipments.CONFIRMED
	return bool(
		dn.bol_rma_id
		and dn.bol_return_status == returns.REPORTED
		and dn.bol_handling_result in CREDITED_RESULTS
		and returns.returned_quantity(dn) > 0
	)


def invoiced(dn_name: str) -> bool:
	"""Whether a Sales Invoice that isn't cancelled bills this Delivery Note (a draft made by hand counts)."""
	return bool(
		frappe.get_all(
			"Sales Invoice Item",
			filters={"parenttype": SI, "delivery_note": dn_name, "docstatus": ("<", 2)},
			limit=1,
		)
	)


def make_invoice(dn) -> str:
	"""Make and submit the Sales Invoice (credit note for a return) with ERPNext's Delivery Note mapper."""
	si = make_sales_invoice(dn.name)
	si.debit_to = _settings().receivable_account
	# bol_order_id is no_copy on the invoice, so the mapper leaves it out.
	si.bol_order_id = dn.bol_order_id
	if dn.is_return:
		_against_original(si, dn)
	else:
		# Invoiced on the day it shipped.
		si.set_posting_time = 1
		si.posting_date, si.posting_time = dn.posting_date, dn.posting_time
	si.flags.ignore_permissions = True
	si.insert()
	si.submit()
	dn.add_comment("Comment", _("{0} {1} submitted.").format(_kind(dn), si.name))
	return si.name


def _against_original(credit_note, return_dn):
	"""Point the credit note and each row at the Sales Invoice of the Delivery Note the return came from."""
	original_rows = {
		row.dn_detail: row
		for row in frappe.get_all(
			"Sales Invoice Item",
			filters={"parenttype": SI, "delivery_note": return_dn.return_against, "docstatus": 1},
			fields=["name", "parent", "dn_detail"],
		)
		if not frappe.db.get_value(SI, row.parent, "is_return")
	}
	# The credit note's rows point at the return's rows (dn_detail), and those at the shipped rows.
	shipped_row = {row.name: row.dn_detail for row in return_dn.items}
	invoices = set()
	for row in credit_note.items:
		original = original_rows.get(shipped_row.get(row.dn_detail))
		if not original:
			raise BolMappingError(
				f"Delivery Note {return_dn.return_against} has no submitted Sales Invoice for {row.item_code}",
				reason="no_sales_invoice",
			)
		row.sales_invoice_item = original.name
		invoices.add(original.parent)
	if len(invoices) != 1:
		raise BolMappingError(
			f"The return's lines were invoiced on {len(invoices)} Sales Invoices", reason="several_invoices"
		)
	credit_note.return_against = invoices.pop()


def onload_delivery_note(doc, method=None):
	# Tells the form whether to offer Create bol Sales Invoice.
	if (
		doc.docstatus == 1
		and doc.bol_order_id
		and invoicing_on()
		and invoice_due(doc)
		and not invoiced(doc.name)
	):
		doc.set_onload("bol_invoice_missing", True)


@frappe.whitelist(methods=["POST"])
def retry_invoice(dn_name: str) -> dict:
	"""Make the Sales Invoice (or credit note) of a bol Delivery Note now, e.g. after its job failed."""
	# Frappe enforces the type hints of whitelisted functions.
	check_bol_role()
	frappe.has_permission(DN, "read", dn_name, throw=True)
	frappe.has_permission(SI, "create", throw=True)
	if not invoicing_on():
		frappe.throw(
			_("Switch on Invoice Shipments and Returns in Bol Settings first."), title=_("bol Disabled")
		)
	# Locked: a double click, or the job, waits here and then finds the invoice.
	dn = frappe.get_doc(DN, dn_name, for_update=True)
	if not invoice_due(dn):
		frappe.throw(
			_(
				"Only a bol shipment bol has confirmed, or a bol return reported as received or exchanged, gets a bol Sales Invoice."
			),
			title=_("Not Allowed"),
		)
	if invoiced(dn.name):
		frappe.throw(_("{0} is invoiced already.").format(dn.name), title=_("Not Allowed"))
	frappe.db.savepoint(INVOICE_SAVEPOINT)
	try:
		name = make_invoice(dn)
	except Exception as e:
		frappe.db.rollback(save_point=INVOICE_SAVEPOINT)
		# The log survives the rollback of this request (ADR-005); the user gets a message, not a traceback.
		log_call(method="retry_invoice", error=e, reference=(DN, dn.name))
		frappe.throw(
			_("{0} not created: {1}").format(_kind(dn), str(e) or type(e).__name__), title=_("Not Invoiced")
		)
	return {"sales_invoice": name}


def sweep_sales_invoices():
	"""Daily: queue the bol Delivery Notes of the last 30 days that are due an invoice and never had one.

	That covers lost jobs, failures since fixed, and credit notes whose original invoice came later. A Delivery
	Note with a cancelled invoice is left alone: someone cancelled it on purpose.
	"""
	if not invoicing_on():
		return
	since = add_days(today(), -SWEEP_DAYS)
	shipped = frappe.get_all(
		DN,
		filters={
			"docstatus": 1,
			"is_return": 0,
			"bol_shipment_status": shipments.CONFIRMED,
			"posting_date": (">=", since),
		},
		order_by="posting_date asc",
		pluck="name",
	)
	returned = frappe.get_all(
		DN,
		filters={
			"docstatus": 1,
			"is_return": 1,
			"bol_return_status": returns.REPORTED,
			"bol_handling_result": ("in", CREDITED_RESULTS),
			"posting_date": (">=", since),
		},
		order_by="posting_date asc",
		pluck="name",
	)
	# Shipments first: a credit note needs its original invoice.
	names = shipped + returned
	if not names:
		return
	billed = set(
		frappe.get_all(
			"Sales Invoice Item",
			filters={"parenttype": SI, "delivery_note": ("in", names)},
			pluck="delivery_note",
		)
	)
	for name in names:
		if name not in billed:
			enqueue_invoice(name)


def _kind(dn) -> str:
	return _("bol credit note") if dn.is_return else _("bol Sales Invoice")


# --- C. bol invoices -> draft Purchase Invoices ------------------------------------------------------------


def poll_bol_invoices():
	"""Daily: a draft Purchase Invoice for each bol invoice in the window that has none yet.

	One failing invoice never blocks the others. While any fails, the window's start stays where it was, so the
	next run tries it again; the unique bol_invoice_id makes reruns safe.
	"""
	settings = _settings()
	if not imports_on(settings):
		return
	client = get_client()
	end = getdate(today())
	anchor = bol_invoice.anchor_date(
		getdate(settings.bol_invoices_until) if settings.bol_invoices_until else None, end
	)
	all_done = True
	for item in listed_invoices(client, bol_invoice.window_start(anchor), end):
		if frappe.db.exists(PI, {"bol_invoice_id": str(item["invoiceId"])}):
			continue
		all_done = import_bol_invoice(client, item, settings) and all_done
		# One invoice per transaction (overview §4.1 rule 4).
		frappe.db.commit()  # nosemgrep
	frappe.db.set_single_value("Bol Settings", "bol_invoices_until", end if all_done else anchor)
	monitoring.mark_run("last_invoice_import")


def listed_invoices(client, start, end) -> list[dict]:
	"""bol's invoices issued from `start` to `end`, each once. A failing list is logged once per 6 hours and raised."""
	# The demo answers only the list without a period (404 with one): its single canned invoice.
	periods = [None] if client.settings.environment == "Demo" else bol_invoice.invoice_windows(start, end)
	listed = {}
	for period in periods:
		params = (
			{"period-start-date": period[0].isoformat(), "period-end-date": period[1].isoformat()}
			if period
			else None
		)
		try:
			response = client.request("GET", INVOICES_PATH, version=VERSION, params=params)
		except BolNotFoundError:
			# Not in the spec for the list; read as "no invoices in this period".
			continue
		except BolError as e:
			log_once("bol_invoices", "list", **e.details, error=e)
			raise
		for item in (response or {}).get("invoiceListItems") or []:
			if item.get("invoiceId"):
				listed.setdefault(str(item["invoiceId"]), item)
	return list(listed.values())


def import_bol_invoice(client, item: dict, settings) -> bool:
	"""Create the draft Purchase Invoice of one listed bol invoice inside a savepoint. False when it failed: then
	it's rolled back and logged once per 6 hours. The caller commits."""
	invoice_id = str(item["invoiceId"])
	frappe.db.savepoint(IMPORT_SAVEPOINT)
	try:
		create_purchase_invoice(client, item, settings)
		return True
	except Exception as e:
		frappe.db.rollback(save_point=IMPORT_SAVEPOINT)
		if _imported_concurrently(invoice_id, e):
			return True
		details = getattr(e, "details", None) or {
			"method": IMPORT_METHOD,
			"path": f"{INVOICES_PATH}/{invoice_id}",
			"request": item,
		}
		log_once(
			"bol_invoice",
			invoice_id,
			**details,
			error=e,
			message=_("bol invoice {0} not imported: {1}").format(invoice_id, str(e) or type(e).__name__),
		)
		return False


def create_purchase_invoice(client, item: dict, settings) -> str:
	invoice_id = str(item["invoiceId"])
	mapped = bol_invoice.to_purchase_invoice(
		item,
		specification_lines(client, invoice_id),
		_account_map(settings),
		settings.default_expense_account,
		precision=_precision(),
		timezone=get_system_timezone(),
	)
	pi = frappe.get_doc(
		{
			"doctype": PI,
			"company": settings.company,
			"supplier": settings.supplier,
			"bol_invoice_id": invoice_id,
			"bill_no": invoice_id,
			"bill_date": mapped["bill_date"],
			"currency": mapped["currency"],
			"is_return": mapped["is_return"],
			"taxes_and_charges": settings.purchase_tax_template,
			"items": [_purchase_line(line) for line in mapped["lines"]],
			"remarks": _remarks(mapped),
		}
	)
	if mapped["bill_date"]:
		# Booked on the date bol issued it (the VAT period it belongs to); the reviewer can still change it.
		pi.set_posting_time = 1
		pi.posting_date = mapped["bill_date"]
	pi.set("taxes", get_taxes_and_charges(PURCHASE_TEMPLATE, settings.purchase_tax_template))
	pi.flags.ignore_permissions = True
	pi.insert()
	mismatch = _mismatch(pi, mapped["expected"])
	if mismatch:
		# Never "fixed": the draft keeps ERPNext's amounts and says how far off bol's they are.
		pi.db_set("remarks", f"{mismatch}\n{pi.remarks}")
		log_once(
			"bol_invoice_mismatch",
			invoice_id,
			method=IMPORT_METHOD,
			path=f"{INVOICES_PATH}/{invoice_id}",
			error=BolMappingError(mismatch, reason="mismatch"),
			reference=(PI, pi.name),
			message=_("bol invoice {0} imported as {1}, but its totals don't match: {2}").format(
				invoice_id, pi.name, mismatch
			),
		)
	if mapped["has_pdf"]:
		_attach_pdf(client, pi, invoice_id)
	return pi.name


def specification_lines(client, invoice_id: str) -> list[dict]:
	"""Every line of the invoice's specification, page by page until a page is empty."""
	demo = client.settings.environment == "Demo"
	path = f"{INVOICES_PATH}/{invoice_id}/specification"
	lines, previous, page = [], None, 1
	while True:
		try:
			# The demo answers only without a page (404 with one).
			response = client.request("GET", path, version=VERSION, params=None if demo else {"page": page})
		except BolNotFoundError:
			if page == 1:
				raise
			# A page past the last one.
			return lines
		page_lines = (response or {}).get("invoiceSpecification") or []
		# A repeated page would be counted twice; the total check flags anything missing.
		if not page_lines or page_lines == previous:
			return lines
		lines.extend(page_lines)
		if demo:
			return lines
		previous, page = page_lines, page + 1


def _purchase_line(line: dict) -> dict:
	description = " — ".join(part for part in (line["line_type"], line["description"]) if part)
	row = {
		"item_name": line["line_type"],
		"description": _("{0} ({1} lines on bol's specification)").format(description, line["count"]),
		"qty": line["qty"],
		"rate": line["rate"],
		"expense_account": line["expense_account"],
		"conversion_factor": 1,
	}
	if line["item"]:
		row["item_code"] = line["item"]
	else:
		row["uom"] = frappe.db.get_single_value("Stock Settings", "stock_uom")
	return row


def _remarks(mapped: dict) -> str:
	parts = [
		_("bol invoice {0} ({1}), imported for review.").format(mapped["invoice_id"], mapped["invoice_type"])
	]
	settled = mapped["settled"]
	if settled["types"]:
		parts.append(
			_(
				"Not on this invoice: {0} {1}, the sales bol collected for you. It settles the bol receivable (the bol Sales Invoices)."
			).format(", ".join(settled["types"]), settled["net"])
		)
	if mapped["unmapped"]:
		parts.append(
			_("Not in the bol invoice account map, so booked on the default expense account: {0}.").format(
				", ".join(mapped["unmapped"])
			)
		)
	if not mapped["has_pdf"]:
		parts.append(_("bol has no PDF of this invoice."))
	return "\n".join(parts)


def _mismatch(pi, expected: dict) -> str | None:
	actual = {"net": pi.net_total, "vat": pi.total_taxes_and_charges}
	labels = {"net": _("net total"), "vat": _("VAT")}
	found = bol_invoice.differences(expected, actual)
	if not found:
		return None
	return "MISMATCH: " + "; ".join(
		_("{0} {1} here, {2} on bol's invoice").format(labels[key], ours, theirs)
		for key, ours, theirs in found
	)


def _attach_pdf(client, pi, invoice_id: str):
	pdf = client.request(
		"GET",
		f"{INVOICES_PATH}/{invoice_id}",
		version=VERSION,
		accept=bol_invoice.PDF_MEDIA_TYPE,
		reference=(PI, pi.name),
	)
	# Anything else would fail Frappe's PDF check with a raw parser error (the demo sends plain text).
	if not (isinstance(pdf, bytes) and pdf.startswith(PDF_SIGNATURE)):
		raise BolError(f"bol's invoice {invoice_id} isn't a PDF")
	frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"bol-invoice-{invoice_id}.pdf",
			"attached_to_doctype": PI,
			"attached_to_name": pi.name,
			"is_private": 1,
			"content": pdf,
		}
	).insert(ignore_permissions=True)


def _account_map(settings) -> dict[str, dict]:
	return {
		(row.bol_line_type or "").strip().upper(): {"expense_account": row.expense_account, "item": row.item}
		for row in settings.invoice_account_map
	}


def _precision() -> int:
	return frappe.get_precision(PI, "net_total") or 2


def _imported_concurrently(invoice_id: str, error: Exception) -> bool:
	"""True when another run created the Purchase Invoice meanwhile: already done, not a failure."""
	if isinstance(error, frappe.DuplicateEntryError):
		return True
	# Frappe raises UniqueValidationError(doctype, name, db_error), and the db error names the key.
	if isinstance(error, frappe.UniqueValidationError) and "bol_invoice_id" in str(error):
		return True
	return bool(frappe.db.exists(PI, {"bol_invoice_id": invoice_id}))


# --- D. Invoices business buyers ask bol for ----------------------------------------------------------------


def upload_requested_invoices():
	"""Daily: upload the Sales Invoice of each shipment a buyer asked bol to invoice (PDF, print format
	bol Sales Invoice). One request per transaction; failures are logged once per 6 hours."""
	if not uploads_on():
		return
	client = get_client()
	for request in invoice_requests(client):
		upload_invoice(client, request)
		frappe.db.commit()  # nosemgrep


def invoice_requests(client) -> list[dict]:
	"""bol's open invoice requests and rejected uploads: [{shipment_id, order_id}], each shipment once."""
	demo = client.settings.environment == "Demo"
	# The demo answers UPLOAD_ERROR with a 400 ("resource not available").
	states = REQUEST_STATES[:1] if demo else REQUEST_STATES
	requests = {}
	for state in states:
		page = 1
		while True:
			try:
				response = client.request(
					"GET", REQUESTS_PATH, version=VERSION, params={"state": state, "page": page}
				)
			except BolNotFoundError:
				break
			except BolError as e:
				log_once("invoice_requests", "list", **e.details, error=e)
				raise
			new = [r for r in bol_invoice.requested_shipments(response) if r["shipment_id"] not in requests]
			requests.update({r["shipment_id"]: r for r in new})
			# Pages hold 50; a page with nothing new is the last (or bol repeating one).
			if not new or demo:
				break
			page += 1
	return list(requests.values())


def upload_invoice(client, request: dict):
	shipment_id = request["shipment_id"]
	dn_name = frappe.db.get_value(
		DN, {"bol_shipment_id": shipment_id, "docstatus": 1, "is_return": 0}, "name"
	)
	si_name = _shipment_invoice(dn_name) if dn_name else None
	_notify_request(request, (SI, si_name) if si_name else (DN, dn_name) if dn_name else None)
	if not si_name:
		_no_invoice(request, dn_name)
		return
	if _unfinished_upload(si_name):
		return
	frappe.db.savepoint(UPLOAD_SAVEPOINT)
	try:
		_upload(client, si_name, shipment_id)
	except Exception as e:
		frappe.db.rollback(save_point=UPLOAD_SAVEPOINT)
		# An ambiguous POST isn't sent again blindly: the next run sees whether bol still lists the request.
		details = getattr(e, "details", None) or {
			"method": UPLOAD_METHOD,
			"path": f"{UPLOAD_PATH}/{shipment_id}",
		}
		if log_once("invoice_upload", shipment_id, **details, error=e, reference=(SI, si_name)):
			add_comment(
				(SI, si_name),
				_("Invoice not uploaded to bol for shipment {0}: {1}").format(
					shipment_id, str(e) or type(e).__name__
				),
			)


def _upload(client, si_name: str, shipment_id: str):
	pdf = frappe.get_print(SI, si_name, print_format=PRINT_FORMAT, as_pdf=True)
	if len(pdf) > MAX_UPLOAD_BYTES:
		raise BolMappingError(
			f"The PDF of {si_name} is {len(pdf)} bytes; bol takes at most 2 MB", reason="too_large"
		)
	response = client.request(
		"POST",
		f"{UPLOAD_PATH}/{shipment_id}",
		version=VERSION,
		files={"invoice": (f"{si_name}.pdf", pdf, "application/pdf")},
		reference=(SI, si_name),
	)
	process_status.track(
		response, UPLOAD_HANDLER, frappe.get_doc(SI, si_name), payload={"shipment_id": shipment_id}
	)


def _shipment_invoice(dn_name: str) -> str | None:
	parents = frappe.get_all(
		"Sales Invoice Item",
		filters={"parenttype": SI, "delivery_note": dn_name, "docstatus": 1},
		pluck="parent",
	)
	invoices = (
		frappe.get_all(SI, filters={"name": ("in", parents), "is_return": 0}, pluck="name") if parents else []
	)
	return invoices[0] if invoices else None


def _no_invoice(request: dict, dn_name: str | None):
	shipment_id = request["shipment_id"]
	if dn_name:
		message = _(
			"A bol buyer asked for the invoice of shipment {0} ({1}), which has no submitted Sales Invoice yet. It's uploaded once it has one."
		).format(shipment_id, dn_name)
	else:
		message = _(
			"A bol buyer asked for the invoice of shipment {0} (bol order {1}), which no Delivery Note here shipped. Upload it in the bol seller dashboard."
		).format(shipment_id, request.get("order_id") or "-")
	reference = (DN, dn_name) if dn_name else None
	if log_once(
		"invoice_request",
		shipment_id,
		method=UPLOAD_METHOD,
		path=REQUESTS_PATH,
		request=request,
		error=BolMappingError(message, reason="no_sales_invoice"),
		reference=reference,
	):
		add_comment(reference, message)


def _notify_request(request: dict, reference: tuple[str, str] | None):
	"""One open ToDo per requested shipment for the Notify User; none when no user is set. It closes itself once
	bol has the upload, so an open one is a request that needs a person."""
	user = _settings().notify_user
	key = _request_key(request["shipment_id"])
	if not user or frappe.db.exists(
		"ToDo", {"allocated_to": user, "status": "Open", "description": ("like", f"%{key}%")}
	):
		return
	reference_type, reference_name = reference or (None, None)
	description = _(
		"A bol buyer asked for an invoice (bol order {0}). It's uploaded automatically and this ToDo closes once bol has it. Still open after a day? The timeline and the Ecommerce Integration Log say why."
	).format(request.get("order_id") or "-")
	frappe.get_doc(
		{
			"doctype": "ToDo",
			"allocated_to": user,
			"reference_type": reference_type,
			"reference_name": reference_name,
			# The key finds this ToDo again: the poll sees an open request every day until bol has the invoice.
			"description": f"{description} {key}",
		}
	).insert(ignore_permissions=True)


def _close_request_todos(shipment_id: str | None):
	if not shipment_id:
		return
	for name in frappe.get_all(
		"ToDo",
		filters={"status": "Open", "description": ("like", f"%{_request_key(shipment_id)}%")},
		pluck="name",
	):
		todo = frappe.get_doc("ToDo", name)
		todo.status = "Closed"
		todo.save(ignore_permissions=True)


def _request_key(shipment_id: str) -> str:
	return f"[bol shipment {shipment_id}]"


def _unfinished_upload(si_name: str) -> bool:
	return bool(
		frappe.db.exists(
			process_status.DOCTYPE,
			{
				"reference_doctype": SI,
				"reference_name": si_name,
				"handler": UPLOAD_HANDLER,
				"status": ("in", UNFINISHED),
			},
		)
	)


def on_upload_success(process, entity_id, payload):
	add_comment(
		(process.reference_doctype, process.reference_name),
		_("Invoice uploaded to bol for shipment {0}.").format(payload.get("shipment_id")),
	)
	_close_request_todos(payload.get("shipment_id"))


def on_upload_failure(process, message, payload):
	"""Nothing to undo: process_status has logged bol's message and commented it on the Sales Invoice. bol lists
	the request again (open, or upload error), so the next run uploads it once more."""


process_status.register(UPLOAD_HANDLER, on_upload_success, on_upload_failure)


def _settings():
	return frappe.get_cached_doc("Bol Settings")
