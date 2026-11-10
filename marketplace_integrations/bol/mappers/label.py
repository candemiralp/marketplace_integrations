# Delivery Note lines <-> bol shipping labels (Shipping Labels API v10, story 09). Pure: no frappe imports.

from datetime import date

from marketplace_integrations.bol.api.errors import BolMappingError
from marketplace_integrations.bol.mappers.shipment import quantities


def delivery_options_request(lines: list[dict]) -> dict:
	return {"orderItems": _order_items(lines)}


def label_request(lines: list[dict], shipping_label_offer_id: str) -> dict:
	return {"orderItems": _order_items(lines), "shippingLabelOfferId": shipping_label_offer_id}


def choose_option(options: list[dict], preferred: str | None, *, today: date | None) -> dict:
	"""The delivery option to buy: bol's recommendation, else the preferred transporter's, else any; the cheapest of those.

	Options valid until before `today` are left out (`today=None` keeps them all).
	"""
	valid = [option for option in options if not _expired(option, today)]
	if not valid:
		raise BolMappingError("No VVB option available", reason="no_option")
	preferred = (preferred or "").strip().upper()
	group = (
		[option for option in valid if option.get("recommended")]
		or [
			option
			for option in valid
			if preferred and (option.get("transporterCode") or "").upper() == preferred
		]
		or valid
	)
	return min(group, key=_price)


def _order_items(lines: list[dict]) -> list[dict]:
	return [
		{"orderItemId": order_item_id, "quantity": qty} for order_item_id, qty in quantities(lines).items()
	]


def _expired(option: dict, today: date | None) -> bool:
	valid_until = option.get("validUntilDate")
	# A date: the option holds through that whole day.
	return bool(today and valid_until and date.fromisoformat(valid_until) < today)


def _price(option: dict) -> float:
	price = (option.get("labelPrice") or {}).get("totalPrice")
	# An option without a price is never preferred over one with a price.
	return float("inf") if price is None else float(price)
