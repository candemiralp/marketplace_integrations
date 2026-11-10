# bol order cancellations (Orders API v10, story 10, ADR-013). Pure: no frappe imports.

from marketplace_integrations.bol.mappers.order import open_quantity

# The spec lists them as one comma-joined string; these are its values.
REASON_CODES = (
	"OUT_OF_STOCK",
	"REQUESTED_BY_CUSTOMER",
	"BAD_CONDITION",
	"HIGHER_SHIPCOST",
	"INCORRECT_PRICE",
	"NOT_AVAIL_IN_TIME",
	"NO_BOL_GUARANTEE",
	"ORDERED_TWICE",
	"RETAIN_ITEM",
	"TECH_ISSUE",
	"UNFINDABLE_ITEM",
	"OTHER",
)
REQUESTED_BY_CUSTOMER = "REQUESTED_BY_CUSTOMER"
FIELDS = ("quantityCancelled", "cancellationRequest")


def has_cancellation_fields(order: dict) -> bool:
	"""True when the order (a list entry or a detail) says what's cancelled and requested on every line."""
	items = order.get("orderItems") or []
	return bool(items) and all(all(field in item for field in FIELDS) for item in items)


def changes(order: dict, rows: dict[str, dict]) -> tuple[dict[str, int], list[str]]:
	"""What's new at bol for the Sales Order lines `rows` ({order_item_id: {bol_qty_cancelled,
	bol_cancellation_requested}}): ({order_item_id: bol's quantityCancelled}, [order_item_id with a new request]).

	Lines that aren't on the Sales Order are left out. A request counts only while the line still has units to ship.
	"""
	cancelled, requested = {}, []
	for item in order.get("orderItems") or []:
		row = rows.get(item.get("orderItemId"))
		if row is None:
			continue
		quantity_cancelled = item.get("quantityCancelled") or 0
		if quantity_cancelled > (row.get("bol_qty_cancelled") or 0):
			cancelled[item["orderItemId"]] = quantity_cancelled
		if (
			item.get("cancellationRequest")
			and not row.get("bol_cancellation_requested")
			and open_quantity(item) > 0
		):
			requested.append(item["orderItemId"])
	return cancelled, requested


def cancellation_body(order_item_id: str, reason_code: str) -> dict:
	# bol takes exactly one order item per request (maxItems 1) and cancels all of its open units.
	return {"orderItems": [{"orderItemId": order_item_id, "reasonCode": reason_code}]}

