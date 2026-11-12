# Stock on bol offers (Offers API v11, story 06): each offer's stock follows the Item's available-to-sell
# quantity in the bol warehouse. ERPNext writes Bin with db.set_value, which fires no doc_events, so a
# minute poll finds what changed (ADR-009).

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now

from marketplace_integrations.bol import monitoring, offers
from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import BolError, BolNotFoundError
from marketplace_integrations.bol.log import INTEGRATION, add_comment, log_call, log_once
from marketplace_integrations.bol.mappers.stock import available_to_sell, stock_body, stock_matches

PUSH_METHOD = "marketplace_integrations.bol.stock.push_stock"
RECONCILE_METHOD = "reconcile_stock"
RECONCILE_SAVEPOINT = "bol_reconcile_stock"
# ecommerce_integrations' "never synced" value of Ecommerce Item.inventory_synced_on.
NEVER_SYNCED = get_datetime("1970-01-01")
OUTCOMES = ("checked", "corrected", "failed")


def stock_on(settings=None) -> bool:
	settings = settings or _settings()
	# Settings only allow Sync Stock with Sync Offers: offers missing at bol are recreated by sync_offer.
	return offers.sync_on(settings) and bool(settings.sync_stock)


def enqueue_push(item_code: str):
	frappe.enqueue(
		PUSH_METHOD,
		item_code=item_code,
		job_id=f"bol_stock::{item_code}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def stock_rows(settings, item_code: str | None = None) -> list[frappe._dict]:
	"""Every bol-enabled Item with its offer mapping and its Bin in the bol warehouse (None when missing)."""
	item = frappe.qb.DocType("Item")
	mapping = frappe.qb.DocType(offers.MAPPING)
	stock_bin = frappe.qb.DocType("Bin")
	query = (
		frappe.qb.from_(item)
		.left_join(mapping)
		.on((mapping.erpnext_item_code == item.name) & (mapping.integration == INTEGRATION))
		.left_join(stock_bin)
		.on((stock_bin.item_code == item.name) & (stock_bin.warehouse == settings.warehouse))
		.select(
			item.name.as_("item_code"),
			item.bol_stock_buffer.as_("buffer"),
			mapping.name.as_("mapping"),
			mapping.integration_item_code.as_("offer_id"),
			mapping.bol_last_stock_pushed.as_("last_pushed"),
			mapping.inventory_synced_on.as_("synced_on"),
			stock_bin.actual_qty,
			stock_bin.reserved_qty,
			stock_bin.reserved_stock,
		)
		.where(item.bol_enabled == 1)
	)
	if item_code:
		query = query.where(item.name == item_code)
	return query.run(as_dict=True)


def available_stock(item_code: str, settings) -> int:
	rows = stock_rows(settings, item_code)
	return _ats(rows[0], settings) if rows else 0


def record_pushed(item_code: str, amount: int):
	"""Remember what bol holds. Call only once bol has confirmed it: the poll compares against this."""
	frappe.db.set_value(
		offers.MAPPING,
		{"integration": INTEGRATION, "erpnext_item_code": item_code},
		{"bol_last_stock_pushed": amount, "inventory_synced_on": now()},
		update_modified=False,
	)


def _ats(row, settings) -> int:
	# 0 on the Item means "use the Settings buffer", like the delivery days (ADR-007).
	buffer = cint(row.buffer) or cint(settings.stock_buffer)
	return available_to_sell(row.actual_qty, row.reserved_qty, row.reserved_stock, buffer)


def _pushed(row, ats: int) -> bool:
	# A new or adopted mapping holds 0 without ever having pushed it: the sync date tells them apart.
	return row.last_pushed == ats and get_datetime(row.synced_on or NEVER_SYNCED) > NEVER_SYNCED


# --- Poll and push ----------------------------------------------------------------------------------


def poll_stock():
	"""Queue a push for each mapped offer whose stock at bol is out of date. Only reads ERPNext."""
	settings = _settings()
	if not stock_on(settings):
		return
	for row in stock_rows(settings):
		if row.mapping and not _pushed(row, _ats(row, settings)):
			enqueue_push(row.item_code)


def push_stock(item_code: str):
	"""Set the stock of the Item's bol offer to its available-to-sell quantity, unless bol already has it."""
	settings = _settings()
	if not stock_on(settings):
		return
	rows = stock_rows(settings, item_code)
	if not rows or not rows[0].mapping:
		# Not sold on bol, or no offer yet: sync_offer creates the offer with its stock.
		return
	row = rows[0]
	# Computed when the job runs, not when it was queued: a burst of changes ends in one push of the latest value.
	ats = _ats(row, settings)
	if _pushed(row, ats):
		return

	reference = ("Item", item_code)
	try:
		_patch_stock(get_client(), row.offer_id, ats, reference)
	except BolNotFoundError:
		# Gone at bol: sync_offer forgets the mapping and recreates the offer with its stock.
		offers.enqueue_sync(item_code)
		return
	except BolError as e:
		if log_once("stock", item_code, **e.details, error=e, reference=reference):
			add_comment(reference, _("bol stock update failed: {0}").format(str(e)))
		# Keep the log and comment; the job fails next and the next poll retries.
		frappe.db.commit()  # nosemgrep
		raise
	record_pushed(item_code, ats)


def _patch_stock(client, offer_id: str, amount: int, reference: tuple[str, str]):
	client.request(
		"PATCH",
		f"{offers.PATH}/{offer_id}",
		version=offers.VERSION,
		json={"stock": stock_body(amount)},
		reference=reference,
	)


# --- Nightly reconcile ------------------------------------------------------------------------------


def reconcile_stock() -> dict | None:
	"""Compare every mapped offer's stock at bol with ERPNext and correct the differences.

	Heals pushes that were missed or failed. One pass over bol's offer list (it includes the stock);
	mapped offers missing from it are left to the offer reconcile, which recreates them.
	"""
	settings = _settings()
	if not stock_on(settings):
		return None
	rows = {row.offer_id: row for row in stock_rows(settings) if row.mapping}
	counts = dict.fromkeys(OUTCOMES, 0)
	client = get_client()
	try:
		for page_offers, _cursor in offers.offer_pages(client):
			for offer in page_offers:
				# pop: an offer bol lists twice is checked once.
				row = rows.pop(offer.get("offerId"), None)
				if not row:
					continue
				counts["checked"] += 1
				outcome = _reconcile(client, row, offer, settings)
				if outcome:
					counts[outcome] += 1
				frappe.db.commit()  # nosemgrep
	except BolError as e:
		_report(counts, e)
		raise
	_report(counts)
	monitoring.mark_run("last_stock_reconcile")
	return counts


def _reconcile(client, row, offer: dict, settings) -> str | None:
	"""'corrected', 'failed', or None when bol already has the right stock."""
	ats = _ats(row, settings)
	reference = ("Item", row.item_code)
	frappe.db.savepoint(RECONCILE_SAVEPOINT)
	try:
		corrected = not stock_matches(offer, ats)
		if corrected:
			_patch_stock(client, row.offer_id, ats, reference)
		if corrected or not _pushed(row, ats):
			record_pushed(row.item_code, ats)
	except Exception as e:
		# One bad offer must not stop the others.
		frappe.db.rollback(save_point=RECONCILE_SAVEPOINT)
		log_once("stock", row.item_code, **getattr(e, "details", {}), error=e, reference=reference)
		return "failed"
	return "corrected" if corrected else None


def _report(counts: dict, error: BolError | None = None):
	summary = _("bol stock reconcile: {0} checked, {1} corrected, {2} failed.").format(
		*(counts[outcome] for outcome in OUTCOMES)
	)
	if error:
		message = _("bol stock reconcile stopped: {0}. So far: {1}").format(str(error), summary)
		log_call(**error.details, error=error, message=message)
	else:
		log_call(method=RECONCILE_METHOD, path=offers.PATH, response=counts, message=summary)


def _settings():
	return frappe.get_cached_doc("Bol Settings")
