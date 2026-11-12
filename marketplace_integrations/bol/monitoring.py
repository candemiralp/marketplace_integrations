# Monitoring (story 15): the counts behind the Bol workspace's number cards, the daily digest e-mail, and the
# health timestamps the jobs write on Bol Settings. Nothing here calls bol. The digest carries no buyer data:
# it names documents and links to them (ADR-017).

import json
import re
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import add_to_date, format_datetime, get_url_to_form, get_url_to_list, now_datetime

from marketplace_integrations.bol.log import INTEGRATION, LOG_DOCTYPE, log_once
from marketplace_integrations.bol.permissions import check_bol_role

PROCESS = "Bol Process"
DN = "Delivery Note"
BOL_MANAGER = "Bol Manager"
LIST_LIMIT = 20
DIGEST_TEMPLATE = "marketplace_integrations/templates/emails/bol_digest.html"

# A failing order is re-logged every 6 hours while the poll keeps meeting it (log_once). So an order whose
# first error is over a day old and whose latest one is recent has been failing for more than 24 hours.
STUCK_AFTER_HOURS = 24
STILL_FAILING_HOURS = 7
STUCK_LOOKBACK_DAYS = 7
# The order import logs "GET /retailer/orders/{id}" (bol failed) or "import_order /retailer/orders/{id}" (ours).
ORDER_LOG = re.compile(r"^(?:GET|import_order) /retailer/orders/([^/?\s]+)$")


def mark_run(fieldname: str):
	"""Record on Bol Settings that a job finished (Health section). Keeps `modified`, so an open form isn't stale."""
	frappe.db.set_single_value("Bol Settings", fieldname, now_datetime(), update_modified=False)


# --- Counts --------------------------------------------------------------------------------------------


def log_error_filters() -> dict:
	return {
		"integration": INTEGRATION,
		"status": "Error",
		"creation": (">=", add_to_date(now_datetime(), hours=-24)),
	}


FAILED_SHIPMENTS = {"docstatus": 1, "bol_shipment_status": ("in", ("Failed", "Unknown"))}
# "!=" also matches Items whose offer has no status yet (Frappe compares with ifnull).
NOT_FOR_SALE = {"bol_enabled": 1, "disabled": 0, "bol_offer_status": ("!=", "For sale")}
OPEN_RETURNS = {"bol_rma_id": ("is", "set"), "docstatus": ("<", 2), "bol_return_status": ("!=", "Reported")}
UNFINISHED_PROCESSES = {"status": ("in", ("PENDING", "HANDLER_ERROR"))}


def count_log_errors() -> int:
	return _count(LOG_DOCTYPE, log_error_filters())


def count_stuck_processes() -> int:
	pending = {"status": "PENDING", "creation": ("<", add_to_date(now_datetime(), hours=-1))}
	return _count(PROCESS, pending) + _count(PROCESS, {"status": "HANDLER_ERROR"})


def count_failed_shipments() -> int:
	return _count(DN, FAILED_SHIPMENTS)


def count_offers_not_for_sale() -> int:
	return _count("Item", NOT_FOR_SALE)


def count_open_returns() -> int:
	return _count(DN, OPEN_RETURNS)


def stuck_orders() -> list[frappe._dict]:
	"""bol orders whose import has been failing for more than 24 hours, oldest first: order_id, since, log."""
	now = now_datetime()
	logs = frappe.get_all(
		LOG_DOCTYPE,
		filters={
			"integration": INTEGRATION,
			"status": "Error",
			"method": ("like", "%/retailer/orders/%"),
			"creation": (">=", add_to_date(now, days=-STUCK_LOOKBACK_DAYS)),
		},
		fields=["name", "method", "creation"],
		order_by="creation asc",
	)
	failing = {}
	for log in logs:
		match = ORDER_LOG.match(log.method or "")
		if not match:
			continue
		order = failing.setdefault(match.group(1), frappe._dict(order_id=match.group(1), since=log.creation))
		order.latest, order.log = log.creation, log.name

	stuck_since = add_to_date(now, hours=-STUCK_AFTER_HOURS)
	still_failing = add_to_date(now, hours=-STILL_FAILING_HOURS)
	stuck = [o for o in failing.values() if o.since <= stuck_since and o.latest >= still_failing]
	if not stuck:
		return []
	# Imported since (or another import won): no longer stuck.
	imported = set(
		frappe.get_all(
			"Sales Order",
			filters={"bol_order_id": ("in", [o.order_id for o in stuck])},
			pluck="bol_order_id",
		)
	)
	return [o for o in stuck if o.order_id not in imported]


def _count(doctype: str, filters: dict) -> int:
	# get_all, not db.count: only get_all compares "!=" with ifnull, so empty statuses count.
	return frappe.get_all(doctype, filters=filters, fields=["count(*) as count"])[0].count


# --- Number cards (Workspace "Bol") -----------------------------------------------------------------------


@frappe.whitelist()
def log_errors_card(filters: str | dict | list | None = None):
	return _card(count_log_errors(), LOG_DOCTYPE, log_error_filters())


@frappe.whitelist()
def stuck_processes_card(filters: str | dict | list | None = None):
	# The list can't show "PENDING for over an hour OR HANDLER_ERROR"; it shows every unfinished process.
	return _card(count_stuck_processes(), PROCESS, UNFINISHED_PROCESSES)


@frappe.whitelist()
def failed_shipments_card(filters: str | dict | list | None = None):
	return _card(count_failed_shipments(), DN, FAILED_SHIPMENTS)


@frappe.whitelist()
def offers_not_for_sale_card(filters: str | dict | list | None = None):
	return _card(count_offers_not_for_sale(), "Item", NOT_FOR_SALE)


@frappe.whitelist()
def open_returns_card(filters: str | dict | list | None = None):
	return _card(count_open_returns(), DN, OPEN_RETURNS)


def _card(value: int, doctype: str, filters: dict) -> dict:
	check_bol_role()
	return {
		"value": value,
		"fieldtype": "Int",
		"route": ["List", doctype],
		"route_options": {field: _route_value(condition) for field, condition in filters.items()},
	}


def _route_value(value):
	# The list view reads [operator, value] pairs; datetimes as text.
	if isinstance(value, tuple):
		return [list(part) if isinstance(part, tuple) else str(part) for part in value]
	return value


def _list_url(doctype: str, filters: dict) -> str:
	query = {
		field: json.dumps(_route_value(condition)) if isinstance(condition, tuple) else condition
		for field, condition in filters.items()
	}
	return f"{get_url_to_list(doctype)}?{urlencode(query)}"


# --- Daily digest -------------------------------------------------------------------------------------------


def send_daily_digest():
	"""07:30 (system time zone): e-mail Bol Managers what needs a person, only when there is something."""
	settings = _settings()
	if not settings.enabled:
		return
	digest = build_digest()
	if not digest:
		return
	recipients = digest_recipients(settings)
	if not recipients:
		log_once(
			"digest",
			"recipients",
			method="send_daily_digest",
			error=frappe.ValidationError("No recipient for the bol digest"),
			message=_(
				"bol daily digest not sent: no enabled user has the Bol Manager role and no Notify User is set."
			),
		)
		return
	subject, message = digest
	frappe.sendmail(
		recipients=recipients,
		subject=subject,
		message=message,
		reference_doctype="Bol Settings",
		reference_name="Bol Settings",
	)


def build_digest() -> tuple[str, str] | None:
	"""(subject, HTML) of the digest, or None when there's nothing to report."""
	counts = [
		{
			"label": _("Integration log errors in the last 24 hours"),
			"value": count_log_errors(),
			"url": _list_url(LOG_DOCTYPE, {"integration": INTEGRATION, "status": "Error"}),
		},
		{
			"label": _("bol processes pending for over an hour or with a handler error"),
			"value": count_stuck_processes(),
			"url": _list_url(PROCESS, UNFINISHED_PROCESSES),
		},
		{
			"label": _("Delivery Notes whose bol shipment failed or is unknown"),
			"value": count_failed_shipments(),
			"url": _list_url(DN, FAILED_SHIPMENTS),
		},
		{
			"label": _("Items whose bol offer isn't for sale"),
			"value": count_offers_not_for_sale(),
			"url": _list_url("Item", NOT_FOR_SALE),
		},
		{
			"label": _("Open bol return Delivery Notes"),
			"value": count_open_returns(),
			"url": _list_url(DN, OPEN_RETURNS),
		},
	]
	stuck = stuck_orders()
	issues = sum(count["value"] for count in counts) + len(stuck)
	if not issues:
		return None

	context = {
		"counts": counts,
		"stuck_orders": [
			{
				"order_id": order.order_id,
				"since": format_datetime(order.since),
				"url": get_url_to_form(LOG_DOCTYPE, order.log),
			}
			for order in stuck[:LIST_LIMIT]
		],
		"stuck_more": max(len(stuck) - LIST_LIMIT, 0),
		"log_url": _list_url(LOG_DOCTYPE, {"integration": INTEGRATION, "status": "Error"}),
		"shipments": _failed_shipment_rows(),
		"offers": _not_for_sale_rows(),
		"settings_url": get_url_to_form("Bol Settings", "Bol Settings"),
	}
	subject = _("bol.com integration: {0} to check").format(issues)
	# A template file shipped with the app; every value in it is escaped. Never a template from the database.
	html = frappe.render_template(DIGEST_TEMPLATE, context)  # nosemgrep
	return subject, html


def digest_recipients(settings) -> list[str]:
	users = frappe.get_all(
		"Has Role", filters={"role": BOL_MANAGER, "parenttype": "User"}, pluck="parent", distinct=True
	)
	recipients = frappe.get_all(
		"User",
		filters=[
			["name", "in", users or [""]],
			["name", "not in", ["Administrator", "Guest"]],
			["enabled", "=", 1],
			["user_type", "=", "System User"],
		],
		pluck="name",
		order_by="name asc",
	)
	if not recipients and settings.notify_user:
		recipients = [settings.notify_user]
	return recipients


def _failed_shipment_rows() -> dict:
	rows = frappe.get_all(
		DN,
		filters=FAILED_SHIPMENTS,
		fields=["name", "bol_shipment_status", "bol_order_id"],
		order_by="posting_date asc",
		limit=LIST_LIMIT + 1,
	)
	return {
		"rows": [
			{
				"name": row.name,
				"status": row.bol_shipment_status,
				"order_id": row.bol_order_id,
				"url": get_url_to_form(DN, row.name),
			}
			for row in rows[:LIST_LIMIT]
		],
		"more_url": _list_url(DN, FAILED_SHIPMENTS) if len(rows) > LIST_LIMIT else None,
	}


def _not_for_sale_rows() -> dict:
	rows = frappe.get_all(
		"Item",
		filters=NOT_FOR_SALE,
		fields=["name", "bol_offer_status", "bol_status_detail"],
		order_by="name asc",
		limit=LIST_LIMIT + 1,
	)
	return {
		"rows": [
			{
				"name": row.name,
				"status": row.bol_offer_status or _("No offer yet"),
				# bol's reasons (e.g. "missing content"): product data, not buyer data.
				"detail": row.bol_status_detail,
				"url": get_url_to_form("Item", row.name),
			}
			for row in rows[:LIST_LIMIT]
		],
		"more_url": _list_url("Item", NOT_FOR_SALE) if len(rows) > LIST_LIMIT else None,
	}


def _settings():
	return frappe.get_cached_doc("Bol Settings")
