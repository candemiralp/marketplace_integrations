# bol offer (v11) JSON <-> plain values. Pure: no frappe imports.

from marketplace_integrations.bol.api.errors import BolMappingError

ON_HOLD = "On hold"
FOR_SALE = "For sale"
NOT_FOR_SALE = "Not for sale"

MIN_PRICE, MAX_PRICE = 1, 9999
REFERENCE_LENGTH = 100
# The desired state of an offer that must be off sale: nothing else needs to be known for it.
HOLD = {"onHoldByRetailer": True}
# What the offer sync owns. `stock` belongs to the stock sync (story 06); `ean` and `condition` can't be patched.
PATCHABLE = ("economicOperatorId", "onHoldByRetailer", "reference", "pricing", "fulfilment")


def offer_status(offer: dict) -> str:
	"""bol v11 has no status field: it's derived from the on-hold flag and the country availabilities."""
	if offer.get("onHoldByRetailer"):
		return ON_HOLD
	if any(country.get("forSale") for country in offer.get("countryAvailabilities") or []):
		return FOR_SALE
	return NOT_FOR_SALE


def build_offer(item: dict, price: float, eo_id: str, min_days: int, max_days: int, stock: int) -> dict:
	"""The CreateOfferRequest of a for-sale offer. `item` holds `item_code` and `ean`."""
	if price is None or not MIN_PRICE <= price <= MAX_PRICE:
		raise BolMappingError(f"Price {price} is outside {MIN_PRICE}–{MAX_PRICE}", reason="price_range")
	if min_days > max_days:
		raise BolMappingError(f"Min days {min_days} > max days {max_days}", reason="delivery_days")
	return {
		"ean": item["ean"],
		# Informational only, so a long item code is cut rather than refused.
		"reference": item["item_code"][:REFERENCE_LENGTH],
		"economicOperatorId": eo_id,
		"onHoldByRetailer": False,
		"condition": {"category": "NEW"},
		"pricing": {"bundlePrices": [{"quantity": 1, "unitPrice": round(price, 2)}]},
		"fulfilment": {
			"method": "FBR",
			"schedule": "MY_DELIVERY_PROMISE",
			"deliveryPromise": {"minimumDaysToCustomer": min_days, "maximumDaysToCustomer": max_days},
		},
		"stock": {"amount": stock, "managedByRetailer": False},
	}


def diff_offer(current: dict, desired: dict) -> dict:
	"""The top-level parts of `desired` that differ from `current`: the body of a PATCH (empty: no call)."""
	return {
		key: desired[key]
		for key in PATCHABLE
		if key in desired and not _matches(current.get(key), desired[key])
	}


def _matches(current, desired) -> bool:
	# Only what we send is compared: bol adds its own keys (ultimateOrderTime, profileId, …).
	if isinstance(desired, dict):
		return isinstance(current, dict) and all(_matches(current.get(k), v) for k, v in desired.items())
	if isinstance(desired, list):
		return (
			isinstance(current, list)
			and len(current) == len(desired)
			and all(_matches(c, d) for c, d in zip(current, desired, strict=True))
		)
	if isinstance(desired, float) and isinstance(current, int | float):
		return round(current, 2) == round(desired, 2)
	return current == desired


def format_not_for_sale_reasons(response: dict | None) -> str:
	"""One line per reason: `NL: Price too high (4000-1)`."""
	lines = []
	for country in (response or {}).get("countries") or []:
		for reason in country.get("reasons") or []:
			line = f"{country.get('countryCode')}: {reason.get('description')} ({reason.get('code')})"
			missing = [attribute.get("name") for attribute in reason.get("missingAttributes") or []]
			lines.append(line + (f" – {', '.join(missing)}" if missing else ""))
	return "\n".join(lines)


def format_violations(violations: list[dict] | None) -> str:
	"""One line per violation: `pricing.bundlePrices[0].unitPrice: must be at least 1`."""
	return "\n".join(f"{v.get('name')}: {v.get('reason')}" for v in violations or [])
