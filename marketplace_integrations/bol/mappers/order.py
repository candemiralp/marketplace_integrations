# bol orders (Orders API v10) -> ERPNext Sales Order and Address dicts. Pure: no frappe imports.

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from marketplace_integrations.bol.api.errors import BolMappingError
from marketplace_integrations.bol.mappers.dates import to_system_datetime

FBR = "FBR"
CURRENCY = "EUR"
# offers.py sends the item_code as the offer reference, cut to bol's limit (ADR-008).
REFERENCE_LENGTH = 100
# The fields that make an address; billing gets its own Address only when one of them differs.
ADDRESS_KEYS = (
	"company",
	"firstName",
	"surname",
	"streetName",
	"houseNumber",
	"houseNumberExtension",
	"extraAddressInformation",
	"zipCode",
	"city",
	"countryCode",
)


def to_sales_order(order: dict, ctx: dict) -> dict | None:
	"""The Sales Order and Address dicts for a bol order, or None when no FBR line is left to ship.

	ctx: customer, company, warehouse, items {ean: [item_code, …]}, countries {CODE: Country name},
	tax_templates {"NL": …, "BE": …}, timezone (system), now (system datetime, used when bol sends no date).
	Returns {"sales_order", "shipping_address", "billing_address" (None if the same), "warnings"}.
	Warnings are dicts with a `reason` code; the caller words them (this module can't translate).
	"""
	placed = _placed(order, ctx)
	warnings = []
	lines = [
		_line(line, quantity, ctx, warnings)
		for line in order.get("orderItems") or []
		if _is_fbr(line) and (quantity := open_quantity(line)) > 0
	]
	if not lines:
		return None

	shipment = order.get("shipmentDetails") or {}
	country = (shipment.get("countryCode") or "").upper()
	template = ctx["tax_templates"].get(country)
	if not template:
		raise BolMappingError(f"Unsupported shipment country: {country or '(none)'}", reason="country")

	order_date = placed.date().isoformat()
	dates = [line["bol_latest_delivery_date"] for line in lines if line["bol_latest_delivery_date"]]
	# The whole order ships by its earliest date. ERPNext sets the order's date to its latest line date and
	# refuses one before the order date, so every line gets the same date; bol's own stays on each line.
	delivery_date = max(min(dates), order_date) if dates else (placed.date() + timedelta(days=1)).isoformat()
	for line in lines:
		line["delivery_date"] = delivery_date

	billing = order.get("billingDetails")
	return {
		"sales_order": {
			"doctype": "Sales Order",
			"customer": ctx["customer"],
			"company": ctx["company"],
			"order_type": "Sales",
			"currency": CURRENCY,
			"bol_order_id": order["orderId"],
			"po_no": order["orderId"],
			"po_date": order_date,
			"transaction_date": order_date,
			"delivery_date": delivery_date,
			"set_warehouse": ctx["warehouse"],
			"ignore_pricing_rule": 1,
			# The buyer paid bol's total to the cent: no rounding to whole euros.
			"disable_rounded_total": 1,
			"taxes_and_charges": template,
			"items": lines,
		},
		"shipping_address": to_address(shipment, "Shipping", ctx),
		"billing_address": to_address(billing, "Billing", ctx) if _differs(billing, shipment) else None,
		"warnings": warnings,
	}


def open_quantity(line: dict) -> int:
	return (line.get("quantity") or 0) - (line.get("quantityCancelled") or 0) - (line.get("quantityShipped") or 0)


def placed_datetime(order: dict, timezone: str):
	"""When the order was placed, in the system time zone; None when bol didn't say."""
	value = order.get("orderPlacedDateTime")
	return to_system_datetime(value, timezone) if value else None


def to_address(details: dict, address_type: str, ctx: dict) -> dict:
	code = (details.get("countryCode") or "").upper()
	country = ctx["countries"].get(code)
	if not country:
		raise BolMappingError(f"Unknown country code: {code or '(none)'}", reason="country")
	extension = details.get("houseNumberExtension")
	name = f"{details.get('firstName') or ''} {details.get('surname') or ''}".strip()
	return {
		"doctype": "Address",
		"address_title": details.get("company") or name,
		"address_type": address_type,
		"address_line1": f"{details.get('streetName')} {details.get('houseNumber')}"
		+ (f" {extension}" if extension else ""),
		# A pick-up point order ships to the point: its name is what the carrier looks for.
		"address_line2": ", ".join(
			part for part in (details.get("pickupPointName"), details.get("extraAddressInformation")) if part
		)
		or None,
		"pincode": details.get("zipCode"),
		"city": details.get("city"),
		"country": country,
		# bol's relay address: only for the shipping label (story 09), never for mailings.
		"email_id": details.get("email"),
		"phone": details.get("deliveryPhoneNumber"),
		"bol_buyer": 1,
		"links": [{"link_doctype": "Customer", "link_name": ctx["customer"]}],
	}


def money(value) -> float:
	# Half-up to cents, as on bol's invoices (round() on floats rounds 0.125 down).
	return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _placed(order: dict, ctx: dict):
	return placed_datetime(order, ctx["timezone"]) or ctx["now"]


def _is_fbr(line: dict) -> bool:
	# FBB lines are shipped by bol: not ours to book (FBB is out of scope).
	return ((line.get("fulfilment") or {}).get("method") or FBR) == FBR


def _line(line: dict, quantity: int, ctx: dict, warnings: list) -> dict:
	order_item_id = line.get("orderItemId")
	ean = (line.get("product") or {}).get("ean")
	item_code = _item_code(ean, ctx["items"])
	reference = (line.get("offer") or {}).get("reference")
	if reference and reference != item_code[:REFERENCE_LENGTH]:
		warnings.append(
			{
				"reason": "reference_mismatch",
				"order_item_id": order_item_id,
				"reference": reference,
				"item_code": item_code,
				"ean": ean,
			}
		)

	unit_price, discount, rate = _prices(line)
	expected = _expected_total(line, quantity)
	if money(rate * quantity) != expected:
		# Usually a discount that doesn't split into whole cents per unit: off by a cent or two.
		warnings.append(
			{
				"reason": "rounding",
				"order_item_id": order_item_id,
				"expected": expected,
				"actual": money(rate * quantity),
			}
		)

	fulfilment = line.get("fulfilment") or {}
	return {
		"item_code": item_code,
		"qty": quantity,
		"price_list_rate": unit_price,
		"discount_amount": discount,
		"rate": rate,
		"warehouse": ctx["warehouse"],
		"bol_order_item_id": order_item_id,
		"bol_distribution_party": fulfilment.get("distributionParty"),
		# Empty when the buyer chose an exact delivery date instead.
		"bol_latest_delivery_date": fulfilment.get("latestDeliveryDate") or fulfilment.get("exactDeliveryDate"),
		"bol_commission": money(line.get("commission")),
	}


def _item_code(ean: str | None, items: dict) -> str:
	matches = (items.get(ean) or []) if ean else []
	if not matches:
		raise BolMappingError(f"No Item has EAN {ean}", reason="unknown_ean")
	if len(matches) > 1:
		raise BolMappingError(f"Several Items have EAN {ean}: {', '.join(matches)}", reason="ambiguous_ean")
	return matches[0]


def _prices(line: dict) -> tuple[float, float, float]:
	"""(unit price, discount per unit, rate). Prices include VAT; discounts cover the whole line."""
	unit_price = money(line.get("unitPrice"))
	discounts = sum(Decimal(str(d.get("amount") or 0)) for d in line.get("discounts") or [])
	discount = money(discounts / line["quantity"]) if discounts and line.get("quantity") else 0.0
	return unit_price, discount, money(unit_price - discount)


def _expected_total(line: dict, quantity: int) -> float:
	"""What the buyer paid for the open units: bol's line total, pro rata."""
	total = line.get("totalPrice")
	if total is None:
		discounts = sum(d.get("amount") or 0 for d in line.get("discounts") or [])
		total = (line.get("unitPrice") or 0) * line["quantity"] - discounts
	return money(Decimal(str(total)) * quantity / line["quantity"])


def _differs(billing: dict | None, shipment: dict) -> bool:
	if not billing:
		return False
	return any((billing.get(key) or None) != (shipment.get(key) or None) for key in ADDRESS_KEYS)
