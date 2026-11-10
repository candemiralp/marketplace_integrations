# bol offer (v11) JSON <-> plain values. Pure: no frappe imports.

ON_HOLD = "On hold"
FOR_SALE = "For sale"
NOT_FOR_SALE = "Not for sale"


def offer_status(offer: dict) -> str:
	"""bol v11 has no status field: it's derived from the on-hold flag and the country availabilities."""
	if offer.get("onHoldByRetailer"):
		return ON_HOLD
	if any(country.get("forSale") for country in offer.get("countryAvailabilities") or []):
		return FOR_SALE
	return NOT_FOR_SALE
