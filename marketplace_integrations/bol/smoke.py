# Release checks against bol (story 15). Not part of the test suite: they call bol for real.
#
#   bench --site X execute marketplace_integrations.bol.smoke.run
#       Demo environment only. One call per bol resource the app uses; PASS/FAIL per step. Creates no ERPNext
#       documents (the client still writes its integration log entry for the shipment POST).
#   bench --site X execute marketplace_integrations.bol.smoke.compare_orders --kwargs "{'days': 7}"
#       Any environment, read-only. bol's FBR orders placed since the cutover vs the Sales Orders: proves zero
#       lost and zero duplicate orders after go-live.

from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, get_system_timezone, getdate, now_datetime

from marketplace_integrations.bol import invoices, labels, offers, orders, process_status, returns, shipments
from marketplace_integrations.bol.api.auth import get_token, invalidate_token
from marketplace_integrations.bol.api.client import BolClient
from marketplace_integrations.bol.mappers.order import placed_datetime

# The demo's canned examples (ADR-011, ADR-012): any body gets the canned shipment; options exist only for this item.
DEMO_ORDER_ITEM = "2095052647"
DEMO_SHIPMENT = {
	"orderItems": [{"orderItemId": DEMO_ORDER_ITEM, "quantity": 1}],
	"shipmentReference": "smoke-test",
	"transport": {"transporterCode": "TNT", "trackAndTrace": "3SAOLD1234567"},
}
# bol keeps 3 months of order history for latest-change-date.
MAX_HISTORY_DAYS = 90


def run() -> dict:
	"""Call each bol resource once on the demo environment. Returns {step: "PASS: …" | "FAIL: …"}."""
	settings = frappe.get_doc("Bol Settings")
	if settings.environment != "Demo":
		frappe.throw(_("The smoke test only runs against the bol Demo environment."))
	client = BolClient(settings)
	steps: list[tuple[str, Callable[[], str]]] = [
		("token", lambda: _token(settings)),
		("offers list", lambda: _first_offers(client)),
		("orders list + detail", lambda: _order_detail(client)),
		("shipment + process status", lambda: _shipment(client)),
		("label delivery options", lambda: _delivery_options(client)),
		("returns list", lambda: f"{len(returns.unhandled_returns(client))} returns"),
		("invoices list", lambda: f"{len(invoices.listed_invoices(client, None, None))} invoices"),
	]
	results = {}
	for name, step in steps:
		try:
			outcome, detail = "PASS", step()
		except Exception as e:
			# A smoke test reports every step: the failure is printed and the next step runs.
			outcome, detail = "FAIL", f"{type(e).__name__}: {e}"
		print(f"{outcome}  {name}: {detail}")
		results[name] = f"{outcome}: {detail}"
	return results


def _token(settings) -> str:
	invalidate_token(settings)
	return "token received" if get_token(settings) else "no token"


def _first_offers(client) -> str:
	page_offers, _cursor = next(offers.offer_pages(client))
	return f"{len(page_offers)} offers on page 1"


def _order_detail(client) -> str:
	listed = orders.open_orders(client)
	if not listed:
		return "no open orders"
	order = client.request("GET", f"{orders.PATH}/{listed[0]['orderId']}", version=orders.VERSION)
	return (
		f"{len(listed)} open orders; order {order['orderId']} has {len(order.get('orderItems') or [])} items"
	)


def _shipment(client) -> str:
	response = client.request("POST", shipments.PATH, version=shipments.VERSION, json=DEMO_SHIPMENT)
	process_id = (response or {}).get("processStatusId")
	if not process_id:
		raise ValueError("bol's 202 has no processStatusId")
	statuses = client.request(
		"POST",
		process_status.PATH,
		version=process_status.VERSION,
		json={"processStatusQueries": [{"processStatusId": process_id}]},
		safe=True,
	)
	found = [
		s
		for s in (statuses or {}).get("processStatuses") or []
		if str(s.get("processStatusId")) == str(process_id)
	]
	return f"process {process_id}: {found[0]['status'] if found else 'not returned'}"


def _delivery_options(client) -> str:
	response = client.request(
		"POST",
		labels.OPTIONS_PATH,
		version=labels.VERSION,
		json={"orderItems": [{"orderItemId": DEMO_ORDER_ITEM}]},
		safe=True,
	)
	return f"{len((response or {}).get('deliveryOptions') or [])} delivery options"


# --- Cutover check --------------------------------------------------------------------------------------------


def compare_orders(days: int = 7) -> dict:
	"""bol's FBR orders placed in the first `days` days after the cutover vs the Sales Orders. Read-only."""
	settings = frappe.get_doc("Bol Settings")
	timezone = get_system_timezone()
	start = get_datetime(settings.import_orders_placed_after or add_to_date(now_datetime(), days=-int(days)))
	end = add_to_date(start, days=int(days))
	client = BolClient(settings)

	bol_orders = {
		order["orderId"]: order
		for order in listed_orders(client, start)
		if _placed_between(order, start, end, timezone)
	}
	so_rows = frappe.get_all(
		"Sales Order",
		filters=[["bol_order_id", "is", "set"]],
		or_filters=[
			["bol_order_id", "in", list(bol_orders) or [""]],
			["transaction_date", "between", [getdate(start), getdate(end)]],
		],
		fields=["name", "bol_order_id", "docstatus"],
	)
	report = compare(bol_orders, so_rows)
	_print_report(start, end, report)
	return report


def listed_orders(client, start) -> list[dict]:
	"""bol's FBR orders (ReducedOrder, all statuses) with an item changed on or after `start`, each once."""
	if client.settings.environment == "Demo":
		# The demo answers only its canned page 1 (ADR-010).
		days = [None]
	else:
		first = max(getdate(start), getdate(add_to_date(now_datetime(), days=-MAX_HISTORY_DAYS)))
		days = [first + timedelta(days=n) for n in range((getdate(now_datetime()) - first).days + 1)]
	listed = {}
	for day in days:
		page = 1
		while True:
			params = {"fulfilment-method": orders.FBR, "status": "ALL", "page": page}
			if day:
				params["latest-change-date"] = day.isoformat()
			response = client.request("GET", orders.PATH, version=orders.VERSION, params=params)
			page_orders = [o for o in (response or {}).get("orders") or [] if o.get("orderId")]
			for order in page_orders:
				listed.setdefault(order["orderId"], order)
			if not page_orders or not day:
				break
			page += 1
	return list(listed.values())


def compare(bol_orders: dict[str, dict], so_rows: list[dict]) -> dict:
	"""Split bol's orders by what ERPNext has for them. Pure.

	imported: has a Sales Order (a cancelled one counts: bol cancelled it after the import).
	missing: no Sales Order although a unit was left to ship. cancelled_before_import: no Sales Order, every unit
	cancelled (the import skips those). duplicates: more than one Sales Order that isn't cancelled.
	extra: a Sales Order in the window whose order bol doesn't list.
	"""
	by_order = defaultdict(list)
	for row in so_rows:
		by_order[row["bol_order_id"]].append(row)
	report = {"imported": [], "missing": [], "cancelled_before_import": [], "duplicates": {}, "extra": []}
	for order_id, order in bol_orders.items():
		rows = by_order.get(order_id)
		if rows:
			report["imported"].append(order_id)
		elif _all_cancelled(order):
			report["cancelled_before_import"].append(order_id)
		else:
			report["missing"].append(order_id)
	for order_id, rows in by_order.items():
		live = [row["name"] for row in rows if row["docstatus"] < 2]
		if len(live) > 1:
			report["duplicates"][order_id] = sorted(live)
		if order_id not in bol_orders:
			report["extra"].append(order_id)
	return {key: sorted(value) if isinstance(value, list) else value for key, value in report.items()}


def _all_cancelled(order: dict) -> bool:
	items = order.get("orderItems") or []
	return bool(items) and all(
		(item.get("quantityCancelled") or 0) >= (item.get("quantity") or 0) for item in items
	)


def _placed_between(order: dict, start, end, timezone: str) -> bool:
	placed = placed_datetime(order, timezone)
	# No placed date (nullable in the spec): keep it rather than miss it.
	return placed is None or start <= placed < end


def _print_report(start, end, report: dict):
	print(f"bol orders placed {start} – {end}")
	print(f"  imported:                {len(report['imported'])}")
	print(f"  cancelled before import: {len(report['cancelled_before_import'])}")
	for key in ("missing", "duplicates", "extra"):
		value = report[key]
		status = "OK" if not value else "CHECK"
		print(f"  {key + ':':<24} {len(value)}  {status}  {', '.join(value) if value else ''}")
