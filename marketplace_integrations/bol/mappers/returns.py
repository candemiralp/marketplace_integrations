# bol returns (Returns API v10, story 11, ADR-014). Pure: no frappe imports.

# ReturnRequest.handlingResult, as the spec lists it.
HANDLING_RESULTS = (
	"RETURN_RECEIVED",
	"EXCHANGE_PRODUCT",
	"RETURN_DOES_NOT_MEET_CONDITIONS",
	"REPAIR_PRODUCT",
	"CUSTOMER_KEEPS_PRODUCT_PAID",
	"STILL_APPROVED",
)
# Kept to a few lines on the Delivery Note; the integration log has bol's full text.
REASON_MAX = 140


def unhandled_items(returns: list[dict]) -> list[dict]:
	"""The return items bol still waits on, one dict per RMA, from the returns list (ReducedReturn)."""
	items = []
	for ret in returns or []:
		for item in ret.get("returnItems") or []:
			if item.get("handled") or not item.get("rmaId"):
				continue
			items.append(
				{
					"return_id": str(ret["returnId"]),
					"rma_id": str(item["rmaId"]),
					"order_id": item.get("orderId"),
					"ean": item.get("ean"),
					"expected_qty": int(item.get("expectedQuantity") or 0),
					"reason": reason_text(item.get("returnReason")),
				}
			)
	return items


def reason_text(return_reason: dict | None) -> str:
	"""`main — detailed: comments`. bol often repeats the main reason as the comment: that's left out."""
	reason = return_reason or {}
	main, detailed = reason.get("mainReason"), reason.get("detailedReason")
	comments = (reason.get("customerComments") or "").strip()
	text = " — ".join(part for part in (main, detailed) if part)
	if comments and comments not in (main, detailed):
		# bol's "Anders, namelijk:" already ends in a colon.
		text = f"{text}{' ' if text.endswith(':') else ': '}{comments}" if text else comments
	return text if len(text) <= REASON_MAX else text[: REASON_MAX - 1] + "…"


def pick_line(candidates: list[dict], qty: int) -> dict | None:
	"""The first Delivery Note line (oldest first) that can still take back `qty` units, or None."""
	return next((line for line in candidates if line["available"] >= qty), None)


def handling_body(handling_result: str, quantity: int) -> dict:
	# quantityReturned has minimum 1 in the spec, also when the customer keeps the product (ADR-014).
	return {"handlingResult": handling_result, "quantityReturned": quantity}


def is_handled(return_detail: dict, rma_id: str) -> bool:
	"""True when bol's return (detail) says the RMA is handled."""
	return any(
		str(item.get("rmaId")) == str(rma_id) and item.get("handled")
		for item in (return_detail or {}).get("returnItems") or []
	)
