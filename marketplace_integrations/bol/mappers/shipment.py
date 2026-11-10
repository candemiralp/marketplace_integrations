# Delivery Note lines <-> bol shipments (Shipments API v10). Pure: no frappe imports.

from marketplace_integrations.bol.api.errors import BolMappingError

# bol's transporter codes that we offer (ADR-011). PostNL is TNT. Dashes and underscores are bol's own.
TRANSPORTER_CODES = (
	"TNT",
	"TNT-EXTRA",
	"TNT_BRIEF",
	"BRIEFPOST",
	"DHL",
	"DHLFORYOU",
	"DPD-NL",
	"DPD-BE",
	"BPOST_BE",
	"BPOST_BRIEF",
	"GLS",
	"UPS",
	"OTHER",
)
MAX_ORDER_ITEMS = 100


def quantities(lines: list[dict]) -> dict[str, int]:
	"""Units per bol order item. lines: [{"order_item_id", "qty"}]; several lines may share an order item."""
	totals: dict[str, float] = {}
	for line in lines:
		totals[line["order_item_id"]] = totals.get(line["order_item_id"], 0) + line["qty"]
	for order_item_id, qty in totals.items():
		if qty <= 0 or qty != int(qty):
			raise BolMappingError(
				f"Order item {order_item_id}: bol ships whole units, not {qty}", reason="quantity"
			)
	if len(totals) > MAX_ORDER_ITEMS:
		raise BolMappingError(
			f"bol accepts at most {MAX_ORDER_ITEMS} order items per shipment", reason="too_many_items"
		)
	return {order_item_id: int(qty) for order_item_id, qty in totals.items()}


def build_shipment(
	lines: list[dict],
	reference: str,
	transporter_code: str | None,
	track_and_trace: str | None,
	shipping_label_id: str | None = None,
) -> dict:
	"""The ShipmentRequest body. A bol label (story 09) replaces the transport: bol knows its carrier."""
	shipment = {
		"orderItems": [
			{"orderItemId": order_item_id, "quantity": qty}
			for order_item_id, qty in quantities(lines).items()
		],
		"shipmentReference": reference,
	}
	if shipping_label_id:
		shipment["shippingLabelId"] = shipping_label_id
		return shipment
	if not transporter_code:
		raise BolMappingError("A transporter is needed without a bol label", reason="transporter")
	shipment["transport"] = {"transporterCode": transporter_code, "trackAndTrace": track_and_trace or None}
	return shipment


def candidates(
	shipments: list[dict], reference: str, order_item_ids: set[str], taken: set[str]
) -> list[dict]:
	"""bol shipments (list entries) that may be ours: our reference, or exactly our order items.

	`taken` are shipment ids already recorded on other Delivery Notes. The list doesn't always carry the
	reference (the demo leaves it out), so entries with our order items are candidates too.
	"""
	found = []
	for shipment in shipments:
		if shipment.get("shipmentId") in taken:
			continue
		items = {item.get("orderItemId") for item in shipment.get("shipmentItems") or []}
		if shipment.get("shipmentReference") == reference or items == order_item_ids:
			found.append(shipment)
	return found


def is_ours(detail: dict, reference: str, wanted: dict[str, int], other_references: set[str]) -> bool:
	"""True when the shipment detail is this Delivery Note's: its reference, or the same items and quantities.

	A shipment that names another Delivery Note is never ours, whatever its items.
	"""
	shipment_reference = detail.get("shipmentReference")
	if shipment_reference == reference:
		return True
	if shipment_reference in other_references:
		return False
	shipped = {
		item.get("orderItemId"): item.get("quantityShipped") or item.get("quantity")
		for item in detail.get("shipmentItems") or []
	}
	return shipped == wanted
