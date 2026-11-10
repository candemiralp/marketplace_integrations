# bol's own invoices (Invoices API v10, story 12, ADR-015) -> plain data for a draft Purchase Invoice, and bol's
# invoice requests (Shipments API v10). Pure: no frappe imports.
#
# Amounts are signed as bol signs them: positive is what we owe bol, negative what bol owes us.

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from marketplace_integrations.bol.api.errors import BolMappingError
from marketplace_integrations.bol.mappers.dates import to_system_datetime

# The sales bol collected from buyers for us. Our Sales Invoices booked them on the bol receivable already, so they
# settle that receivable: they aren't purchases and never go on the Purchase Invoice.
SETTLED_TYPES = ("TURNOVER", "CORRECTION_TURNOVER")
PDF_MEDIA_TYPE = "application/vnd.retailer.v10+pdf"
# bol lists at most 31 days of invoices per call.
MAX_WINDOW_DAYS = 31
# Each import reads the days before the last one again: an invoice that failed, or that bol issued late, is found.
OVERLAP_DAYS = 7
FIRST_RUN_DAYS = 30
TOLERANCE = 0.01
UNKNOWN_TYPE = "UNKNOWN"
# Epoch values above this are milliseconds (the demo sends seconds for issueDate, milliseconds for invoicePeriod).
EPOCH_MS_FROM = 10**11


def anchor_date(until: date | None, today: date) -> date:
	"""The date the last complete import reached. The first import reaches back 30 days."""
	return until or today - timedelta(days=FIRST_RUN_DAYS)


def window_start(anchor: date) -> date:
	return anchor - timedelta(days=OVERLAP_DAYS)


def invoice_windows(start: date, end: date) -> list[tuple[date, date]]:
	"""[start, end] as periods of at most 31 days. Each period starts on the day the previous one ended, so no day is
	missed whether bol counts the end date or not; the caller de-duplicates invoices by id."""
	windows = []
	while True:
		stop = min(start + timedelta(days=MAX_WINDOW_DAYS - 1), end)
		windows.append((start, stop))
		if stop >= end:
			return windows
		start = stop


def issue_date(value, timezone: str) -> date | None:
	"""bol's date as a date in `timezone`: an ISO date, an ISO timestamp, or epoch seconds or milliseconds."""
	if value is None or value == "":
		return None
	if isinstance(value, int | float):
		seconds = value / 1000 if value > EPOCH_MS_FROM else value
		return datetime.fromtimestamp(seconds, UTC).astimezone(ZoneInfo(timezone)).date()
	text = str(value)
	if len(text) == 10:
		return date.fromisoformat(text)
	return to_system_datetime(text, timezone).date()


def invoice_totals(item: dict) -> dict:
	"""{net, vat, currency} of a listed invoice (InvoiceListItem.legalMonetaryTotal)."""
	total = item.get("legalMonetaryTotal") or {}
	exclusive, inclusive = total.get("taxExclusiveAmount") or {}, total.get("taxInclusiveAmount") or {}
	if exclusive.get("amount") is None or inclusive.get("amount") is None:
		raise BolMappingError(f"bol invoice {item.get('invoiceId')} has no totals", reason="no_totals")
	return {
		"net": float(exclusive["amount"]),
		"vat": float(inclusive["amount"]) - float(exclusive["amount"]),
		"currency": exclusive.get("currencyID") or inclusive.get("currencyID"),
	}


def line_type(line: dict) -> str:
	name = ((line.get("item") or {}).get("Name") or {}).get("value")
	return (name or UNKNOWN_TYPE).strip().upper()


def group_lines(lines: list[dict]) -> dict[str, dict]:
	"""Specification lines summed per type: {type: {net, vat, count, description}}. Not rounded."""
	groups = {}
	for line in lines:
		group = groups.setdefault(
			line_type(line), {"net": 0.0, "vat": 0.0, "count": 0, "description": _description(line)}
		)
		group["net"] += _value(line.get("lineExtensionAmount"))
		group["vat"] += _value((line.get("taxTotal") or {}).get("TaxAmount"))
		group["count"] += 1
	return groups


def to_purchase_invoice(
	item: dict, lines: list[dict], account_map: dict[str, dict], default_account: str, precision: int, timezone: str
) -> dict:
	"""The draft Purchase Invoice for a listed bol invoice and its specification lines.

	`account_map` is {line type: {expense_account, item}}; other types go to `default_account` and are listed in
	`unmapped`. A negative total is a debit note (`is_return`): its lines have qty -1. `expected` is what the
	Purchase Invoice's net and VAT must come to: bol's totals without the settled turnover.
	"""
	invoice_id = str(item["invoiceId"])
	totals = invoice_totals(item)
	groups = group_lines(lines)
	booked = {kind: group for kind, group in groups.items() if kind not in SETTLED_TYPES}
	if not booked:
		raise BolMappingError(f"bol invoice {invoice_id} has nothing to book", reason="nothing_to_book")
	settled = {kind: group for kind, group in groups.items() if kind in SETTLED_TYPES}
	settled_net = round(sum(group["net"] for group in settled.values()), precision)
	settled_vat = round(sum(group["vat"] for group in settled.values()), precision)

	amounts = {kind: round(group["net"], precision) for kind, group in booked.items()}
	is_return = sum(amounts.values()) < 0
	# A Purchase Invoice refuses a negative qty, a debit note a positive one; the rate carries the sign.
	qty = -1 if is_return else 1
	result_lines, unmapped = [], []
	for kind in sorted(booked):
		mapping = account_map.get(kind)
		if not mapping:
			unmapped.append(kind)
		result_lines.append(
			{
				"line_type": kind,
				"description": booked[kind]["description"],
				"count": booked[kind]["count"],
				"item": (mapping or {}).get("item"),
				"expense_account": (mapping or {}).get("expense_account") or default_account,
				"qty": qty,
				"rate": amounts[kind] * qty,
			}
		)
	return {
		"invoice_id": invoice_id,
		"invoice_type": item.get("invoiceType"),
		"bill_date": issue_date(item.get("issueDate"), timezone),
		"currency": totals["currency"],
		"is_return": is_return,
		"lines": result_lines,
		"unmapped": unmapped,
		"settled": {"types": sorted(settled), "net": settled_net, "vat": settled_vat},
		"expected": {
			"net": round(totals["net"] - settled_net, precision),
			"vat": round(totals["vat"] - settled_vat, precision),
		},
		"has_pdf": PDF_MEDIA_TYPE in ((item.get("invoiceMediaTypes") or {}).get("availableMediaTypes") or []),
	}


def differences(expected: dict, actual: dict) -> list[tuple[str, float, float]]:
	"""[(net|vat, ours, bol's)] where the Purchase Invoice is more than 0.01 off bol's invoice."""
	return [
		(key, actual[key], expected[key])
		for key in ("net", "vat")
		if round(abs(actual[key] - expected[key]), 6) > TOLERANCE
	]


def requested_shipments(response: dict | None) -> list[dict]:
	"""[{shipment_id, order_id}] of bol's invoice requests. The buyer's billing details stay out (PII)."""
	return [
		{"shipment_id": str(request["shipmentId"]), "order_id": request.get("orderId")}
		for request in (response or {}).get("invoiceRequests") or []
		if request.get("shipmentId")
	]


def _value(node: dict | None) -> float:
	return float((node or {}).get("value") or 0)


def _description(line: dict) -> str | None:
	descriptions = (line.get("item") or {}).get("Description") or []
	return (descriptions[0] or {}).get("value") if descriptions else None
