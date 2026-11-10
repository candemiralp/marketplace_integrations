# Offers at bol (Offers API v11). Story 04: adopt the offers that already exist at bol.

import frappe
from frappe import _
from frappe.utils.background_jobs import is_job_enqueued

from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import BolError
from marketplace_integrations.bol.log import INTEGRATION, log_call
from marketplace_integrations.bol.mappers.ean import gtin_forms
from marketplace_integrations.bol.mappers.offer import offer_status

PATH = "/retailer/offers"
VERSION = 11
PAGE_SIZE = 100
MAPPING = "Ecommerce Item"
ADOPT_JOB_ID = "bol_adopt_existing_offers"
ADOPT_METHOD = "adopt_existing_offers"
ADOPT_TIMEOUT = 60 * 60
OFFER_SAVEPOINT = "bol_adopt_offer"
# Outcomes listed one by one in the report; the others are only counted.
REPORTED = ("unmatched", "ambiguous", "conflict", "failed")
OUTCOMES = ("adopted", "already_mapped", *REPORTED)


def enqueue_adoption() -> bool:
	"""Start adopt_existing_offers in the background. False when it's already queued or running."""
	if is_job_enqueued(ADOPT_JOB_ID):
		return False
	frappe.enqueue(
		f"marketplace_integrations.bol.offers.{ADOPT_METHOD}",
		queue="long",
		timeout=ADOPT_TIMEOUT,
		job_id=ADOPT_JOB_ID,
		deduplicate=True,
		enqueue_after_commit=True,
	)
	return True


def adopt_existing_offers() -> dict:
	"""Map every offer at bol to its ERPNext Item by EAN, or report why it can't be mapped.

	Only reads from bol. Safe to run again: the mapping is keyed on the offerId.
	"""
	report = {"pages": 0, "cursor": None, "counts": dict.fromkeys(OUTCOMES, 0), **{key: [] for key in REPORTED}}
	try:
		client = get_client()
		while True:
			params = {"page-size": PAGE_SIZE, **({"cursor": report["cursor"]} if report["cursor"] else {})}
			response = client.request("GET", PATH, version=VERSION, params=params) or {}
			report["pages"] += 1
			for offer in response.get("offers") or []:
				_adopt(offer, report)
				# One offer per transaction (overview §4.1 rule 4).
				frappe.db.commit()  # nosemgrep
			next_cursor = (response.get("page") or {}).get("nextCursor")
			if next_cursor and next_cursor == report["cursor"]:
				# Following it would loop forever (bol's demo does this); stopping quietly could hide offers.
				raise BolError(f"bol returned the same page cursor again: {next_cursor}")
			report["cursor"] = next_cursor
			if not next_cursor:
				break
	except BolError as e:
		# Client retries are used up: the job fails; the log shows how far it got and a rerun is safe.
		_report(report, e)
		raise
	_report(report)
	return report


def _adopt(offer: dict, report: dict):
	offer_id, ean = offer.get("offerId"), offer.get("ean")
	frappe.db.savepoint(OFFER_SAVEPOINT)
	try:
		outcome, detail = _map_offer(offer_id, ean, offer_status(offer))
	except Exception as e:
		# One bad offer must not stop the others.
		frappe.db.rollback(save_point=OFFER_SAVEPOINT)
		log_call(method=ADOPT_METHOD, path=f"{PATH}/{offer_id}", response=offer, error=e)
		outcome, detail = "failed", {"error": str(e)}
	report["counts"][outcome] += 1
	if outcome in REPORTED:
		report[outcome].append({"offerId": offer_id, "ean": ean, **detail})


def _map_offer(offer_id: str, ean: str | None, status: str) -> tuple[str, dict]:
	items = _items_by_ean(ean) if ean else []
	if not items:
		return "unmatched", {}
	if len(items) > 1:
		# Never pick one: someone has to remove the duplicate barcode.
		return "ambiguous", {"items": items}
	item = items[0]

	mapped_item = frappe.db.get_value(
		MAPPING, {"integration": INTEGRATION, "integration_item_code": offer_id}, "erpnext_item_code"
	)
	if mapped_item and mapped_item != item:
		return "conflict", {"item": item, "reason": _("Offer is mapped to item {0}").format(mapped_item)}
	if mapped_item:
		outcome = "already_mapped"
	else:
		other_offer = _other_offer(item, ean, offer_id)
		if other_offer:
			return "conflict", {
				"item": item,
				"reason": _("Item or EAN is mapped to offer {0}").format(other_offer),
			}
		outcome = _insert_mapping(item, offer_id, ean)

	# db.set_value skips Item validation and doc_events on purpose: adopting must not push anything to bol,
	# and the Item may need fixing (Brand operator, EAN type) before the offer sync takes over.
	frappe.db.set_value("Item", item, {"bol_enabled": 1, "bol_offer_status": status}, update_modified=False)
	return outcome, {}


def _items_by_ean(ean: str) -> list[str]:
	# Item Barcode.barcode is unique, but the same GTIN can be stored as UPC-A, EAN-13 or GTIN-14.
	parents = frappe.get_all(
		"Item Barcode", filters={"parenttype": "Item", "barcode": ("in", gtin_forms(ean))}, pluck="parent"
	)
	return sorted(set(parents))


def _other_offer(item: str, ean: str, offer_id: str) -> str | None:
	offers = frappe.get_all(
		MAPPING,
		filters={"integration": INTEGRATION, "integration_item_code": ("!=", offer_id)},
		or_filters={"erpnext_item_code": item, "sku": ean},
		pluck="integration_item_code",
		limit=1,
	)
	return offers[0] if offers else None


def _insert_mapping(item: str, offer_id: str, ean: str) -> str:
	mapping = frappe.get_doc(
		{
			"doctype": MAPPING,
			"integration": INTEGRATION,
			"erpnext_item_code": item,
			"integration_item_code": offer_id,
			"sku": ean,
		}
	)
	try:
		mapping.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# Mapped concurrently: already done.
		return "already_mapped"
	return "adopted"


def _report(report: dict, error: BolError | None = None):
	"""One integration log entry with the result, and a message to the user who started the job."""
	counts = report["counts"]
	summary = _(
		"bol offers: {0} adopted, {1} already mapped, {2} without a matching item, {3} ambiguous, "
		"{4} conflicts, {5} failed."
	).format(*(counts[outcome] for outcome in OUTCOMES))
	if error:
		summary = _("Adopting bol offers stopped after {0} pages: {1}. So far: {2}").format(
			report["pages"], str(error), summary
		)
		details = error.details or {}
		log_call(
			method=ADOPT_METHOD,
			path=details.get("path"),
			status=details.get("status"),
			request=details.get("request"),
			response={"report": report, "bol_response": details.get("response")},
			error=error,
			message=summary,
		)
	else:
		log_call(method=ADOPT_METHOD, path=PATH, response=report, message=summary)

	indicator = "red" if error else ("orange" if any(counts[outcome] for outcome in REPORTED) else "green")
	frappe.publish_realtime(
		"msgprint",
		{
			"message": summary + " " + _("Details are in the Ecommerce Integration Log."),
			"title": _("Adopt Existing Offers"),
			"indicator": indicator,
		},
		user=frappe.session.user,
	)
