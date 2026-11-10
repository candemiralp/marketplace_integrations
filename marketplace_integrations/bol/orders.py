# bol orders (Orders API v10, story 07): every new FBR order becomes exactly one Sales Order.
# The 5-minute poll is the baseline; the bol order id (Sales Order.bol_order_id, unique) keeps it idempotent.

import frappe
from erpnext.controllers.accounts_controller import get_taxes_and_charges
from frappe import _
from frappe.utils import get_datetime, get_system_timezone, now_datetime

from marketplace_integrations.bol import offers
from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import BolError
from marketplace_integrations.bol.log import log_once
from marketplace_integrations.bol.mappers.order import placed_datetime, to_sales_order

PATH = "/retailer/orders"
VERSION = 10
FBR = "FBR"
IMPORT_METHOD = "import_order"
IMPORT_SAVEPOINT = "bol_import_order"
TAX_TEMPLATE = "Sales Taxes and Charges Template"


def orders_on(settings=None) -> bool:
	settings = settings or _settings()
	return bool(settings.enabled and settings.import_orders)


def poll_orders():
	"""Import every open FBR order at bol that has no Sales Order yet (cron, every 5 minutes).

	Frappe's scheduler never runs this job twice at once (ADR-006); concurrent imports from elsewhere are
	caught by the unique bol_order_id.
	"""
	settings = _settings()
	if not orders_on(settings):
		return
	client = get_client()
	try:
		order_ids = open_order_ids(client)
	except BolError as e:
		# The next poll tries again; one log per 6 hours is enough.
		log_once("orders", "list", **e.details, error=e)
		raise
	existing = set(
		frappe.get_all("Sales Order", filters={"bol_order_id": ("in", order_ids)}, pluck="bol_order_id")
		if order_ids
		else ()
	)
	for order_id in order_ids:
		if order_id not in existing:
			import_order(order_id, client)
			# One order per transaction (overview §4.1 rule 4).
			frappe.db.commit()  # nosemgrep
	sync_existing_order_changes([order_id for order_id in order_ids if order_id in existing])


def open_order_ids(client) -> list[str]:
	"""The ids of bol's open FBR orders, in bol's order, each once. Pages until bol sends an empty page."""
	order_ids = []
	page = 1
	while True:
		response = client.request(
			"GET", PATH, version=VERSION, params={"fulfilment-method": FBR, "status": "OPEN", "page": page}
		)
		page_ids = [
			order.get("orderId") for order in (response or {}).get("orders") or [] if order.get("orderId")
		]
		# A new order can shift the pages between two requests: an id seen on the last page counts once.
		order_ids.extend(order_id for order_id in page_ids if order_id not in order_ids)
		if not page_ids or client.settings.environment == "Demo":
			# The demo has only page 1 (page 2 is a 400, "resource not available").
			return order_ids
		page += 1


def sync_existing_order_changes(order_ids: list[str]):
	"""Cancellations and changes on orders that were imported already: story 10."""


def import_order(order_id: str, client=None) -> str | None:
	"""Create the Sales Order for one bol order, inside a savepoint. Returns its name.

	None when it was imported already, was placed before the cutover, has nothing left to ship, or failed.
	A failure never raises: it's rolled back (no orphan Address) and logged once per 6 hours, and the next
	poll tries again. The caller commits.
	"""
	if _imported(order_id):
		return None
	settings = _settings()
	order = None
	frappe.db.savepoint(IMPORT_SAVEPOINT)
	try:
		order = (client or get_client()).request("GET", f"{PATH}/{order_id}", version=VERSION)
		return create_sales_order(order, settings)
	except Exception as e:
		# Unknown EAN, unsupported country, a validation error on submit, bol unreachable, …
		frappe.db.rollback(save_point=IMPORT_SAVEPOINT)
		if not _imported_concurrently(order_id, e):
			_log_failure(order_id, order, e)
	return None


def create_sales_order(order: dict, settings) -> str | None:
	"""Insert the buyer's addresses and the Sales Order for a bol order detail, and submit it."""
	order_id = order["orderId"]
	placed = placed_datetime(order, get_system_timezone())
	cutover = settings.import_orders_placed_after
	if cutover and placed and placed < get_datetime(cutover):
		_debug(f"bol order {order_id} placed {placed}, before the cutover {cutover}: skipped")
		return None

	mapped = to_sales_order(order, _context(order, settings))
	if not mapped:
		_debug(f"bol order {order_id} has nothing left to ship: skipped")
		return None

	shipping = _insert(mapped["shipping_address"])
	billing = _insert(mapped["billing_address"]) if mapped["billing_address"] else shipping
	sales_order = frappe.get_doc(mapped["sales_order"])
	sales_order.shipping_address_name = shipping.name
	sales_order.customer_address = billing.name
	sales_order.set("taxes", get_taxes_and_charges(TAX_TEMPLATE, sales_order.taxes_and_charges))
	sales_order.flags.ignore_permissions = True
	sales_order.insert()
	if settings.submit_orders:
		sales_order.submit()
	for warning in mapped["warnings"]:
		sales_order.add_comment("Comment", _warning_text(warning))
	return sales_order.name


def _context(order: dict, settings) -> dict:
	eans = {(line.get("product") or {}).get("ean") for line in order.get("orderItems") or []}
	return {
		"customer": settings.customer,
		"company": settings.company,
		"warehouse": settings.warehouse,
		"items": {ean: offers.items_by_ean(ean) for ean in eans if ean},
		"countries": _countries(order),
		"tax_templates": {"NL": settings.tax_template_nl, "BE": settings.tax_template_be},
		"timezone": get_system_timezone(),
		"now": now_datetime(),
	}


def _countries(order: dict) -> dict[str, str]:
	codes = {
		(details.get("countryCode") or "").lower()
		for details in (order.get("shipmentDetails"), order.get("billingDetails"))
		if details
	}
	rows = frappe.get_all("Country", filters={"code": ("in", list(codes))}, fields=["code", "name"])
	return {row.code.upper(): row.name for row in rows}


def _insert(values: dict):
	doc = frappe.get_doc(values)
	doc.insert(ignore_permissions=True)
	return doc


def _imported(order_id: str) -> bool:
	return bool(frappe.db.exists("Sales Order", {"bol_order_id": order_id}))


def _imported_concurrently(order_id: str, error: Exception) -> bool:
	"""True when another run imported the order while this one was at it: already done, not a failure."""
	# Still uncommitted elsewhere: our insert hits the unique key. Frappe raises
	# UniqueValidationError(doctype, name, db_error), and the db error names the key.
	if isinstance(error, frappe.UniqueValidationError) and "bol_order_id" in str(error):
		return True
	# Committed: ERPNext's own check (one Sales Order per customer PO) stops the insert first.
	return _imported(order_id)


def _log_failure(order_id: str, order: dict | None, error: Exception):
	# bol errors carry their call; other failures get the whole order, so it can be fixed from the log.
	details = getattr(error, "details", None) or {
		"method": IMPORT_METHOD,
		"path": f"{PATH}/{order_id}",
		"response": order,
	}
	log_once(
		"order",
		order_id,
		**details,
		error=error,
		message=_("bol order {0} not imported: {1}").format(order_id, str(error) or type(error).__name__),
	)


def _warning_text(warning: dict) -> str:
	if warning["reason"] == "reference_mismatch":
		return _(
			"bol order item {0}: the offer reference {1} doesn't match Item {2}, found by EAN {3}. The EAN was used."
		).format(warning["order_item_id"], warning["reference"], warning["item_code"], warning["ean"])
	return _(
		"bol order item {0}: bol's total for these units is {1}, this line comes to {2}. Usually bol's discount doesn't split into whole cents per unit; check the amount before invoicing."
	).format(warning["order_item_id"], warning["expected"], warning["actual"])


def _debug(message: str):
	frappe.logger("bol").debug(message)


def _settings():
	return frappe.get_cached_doc("Bol Settings")
