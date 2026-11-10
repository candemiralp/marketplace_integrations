# Offers at bol (Offers API v11). Story 04: adopt the offers that already exist at bol.
# Story 05: keep each offer in line with its Item (sync_offer), daily reconcile, explicit delete.

import frappe
from erpnext.stock.get_item_details import get_item_price
from frappe import _
from frappe.utils import cint, flt, nowdate
from frappe.utils.background_jobs import is_job_enqueued

from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import (
	BolError,
	BolMappingError,
	BolNotFoundError,
	BolValidationError,
)
from marketplace_integrations.bol.catalog import ean_problem
from marketplace_integrations.bol.log import INTEGRATION, add_comment, log_call, log_once
from marketplace_integrations.bol.mappers.ean import get_item_ean, gtin_forms
from marketplace_integrations.bol.mappers.offer import (
	HOLD,
	NOT_FOR_SALE,
	build_offer,
	diff_offer,
	format_not_for_sale_reasons,
	format_violations,
	offer_status,
)
from marketplace_integrations.bol.permissions import check_bol_role

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

SYNC_METHOD = "marketplace_integrations.bol.offers.sync_offer"
SYNC_SAVEPOINT = "bol_sync_offer"
RECONCILE_SAVEPOINT = "bol_reconcile_offer"
# Statuses we set ourselves, next to the ones derived from bol (mappers.offer.offer_status).
NOT_SYNCED = "Not synced"
DELETED = "Deleted"
# Item fields that change what the offer at bol should look like.
TRIGGER_FIELDS = ("bol_enabled", "disabled", "bol_min_days", "bol_max_days", "brand")


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
	report = {
		"pages": 0,
		"cursor": None,
		"counts": dict.fromkeys(OUTCOMES, 0),
		"repeated": 0,
		**{key: [] for key in REPORTED},
	}
	seen = set()
	try:
		for page_offers, next_cursor in _offer_pages(get_client()):
			report["pages"] += 1
			for offer in page_offers:
				if offer.get("offerId") in seen:
					# Already handled in this run: counting it again would inflate the report.
					report["repeated"] += 1
					continue
				seen.add(offer.get("offerId"))
				_adopt(offer, report)
				# One offer per transaction (overview §4.1 rule 4).
				frappe.db.commit()  # nosemgrep
			report["cursor"] = next_cursor
	except BolError as e:
		# Client retries are used up: the job fails; the log shows how far it got and a rerun is safe.
		_report(report, e)
		raise
	_report(report)
	return report


def _offer_pages(client, params: dict | None = None):
	"""Yield (offers, next_cursor) for each page of bol's offer list."""
	cursor = None
	while True:
		query = {**(params or {}), "page-size": PAGE_SIZE, **({"cursor": cursor} if cursor else {})}
		response = client.request("GET", PATH, version=VERSION, params=query) or {}
		next_cursor = (response.get("page") or {}).get("nextCursor")
		yield response.get("offers") or [], next_cursor
		if not next_cursor:
			return
		if next_cursor == cursor:
			if client.settings.environment == "Demo":
				# The demo answers every request with the spec's example cursor: that's its last page.
				return
			# Following it would loop forever; stopping quietly could hide offers.
			raise BolError(f"bol returned the same page cursor again: {next_cursor}")
		cursor = next_cursor


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


# --- Offer sync (story 05) -------------------------------------------------------------------------


def sync_on(settings=None) -> bool:
	settings = settings or _settings()
	return bool(settings.enabled and settings.sync_offers)


def enqueue_sync(item_code: str):
	frappe.enqueue(
		SYNC_METHOD,
		item_code=item_code,
		job_id=f"bol_offer::{item_code}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def sync_offer(item_code: str):
	"""Make the Item's bol offer match ERPNext: create, adopt, update or put it on hold. Never deletes."""
	settings = _settings()
	if not sync_on(settings) or not frappe.db.exists("Item", item_code):
		return
	item = frappe.get_doc("Item", item_code)
	frappe.db.savepoint(SYNC_SAVEPOINT)
	try:
		_sync(item, settings)
	except BolMappingError as e:
		_not_synced(item, str(e), e)
	except BolValidationError as e:
		_not_synced(item, format_violations(e.violations) or str(e), e)
	except BolError as e:
		if e.ambiguous:
			# Don't retry the POST now: the next run's EAN lookup finds the offer if bol created it.
			_not_synced(item, _("bol may have created the offer. The next sync checks and links it."), e)
			return
		frappe.db.rollback(save_point=SYNC_SAVEPOINT)
		_record_failure(item, e)
		# Keep the log and comment; the job fails next and the reconcile or the next trigger retries.
		frappe.db.commit()  # nosemgrep
		raise


def _sync(item, settings):
	client = get_client()
	for_sale = bool(item.bol_enabled and not item.disabled)
	mapping = _mapping(item.name)
	offer = None
	if mapping:
		try:
			offer = _get_offer(client, mapping.integration_item_code)
		except BolNotFoundError:
			# Deleted at bol: forget it, and recreate it below if the item is still for sale.
			frappe.delete_doc(MAPPING, mapping.name, ignore_permissions=True)
			mapping = None
			if not for_sale:
				_write_status(item, DELETED, _("The offer no longer exists at bol."))
				return

	if not mapping:
		if not for_sale:
			return
		offer = _adopt_or_create(client, item, settings)
		if offer is None:
			return

	patch = diff_offer(offer, _desired(item, settings) if for_sale else HOLD)
	if patch:
		client.request(
			"PATCH", f"{PATH}/{offer['offerId']}", version=VERSION, json=patch, reference=_reference(item)
		)
		offer = _get_offer(client, offer["offerId"])
	_refresh_status(client, item, offer)


def _adopt_or_create(client, item, settings) -> dict | None:
	"""The existing offer with the Item's EAN (now mapped), or None after creating a new one."""
	ean = _ean(item)
	if frappe.db.exists(MAPPING, {"integration": INTEGRATION, "sku": ean}):
		raise BolMappingError(_("EAN {0} is already linked to another item's bol offer.").format(ean))

	# Look first: an earlier POST may have succeeded although we never got its answer.
	response = client.request("GET", PATH, version=VERSION, params={"eans": [ean]}) or {}
	found = [offer for offer in response.get("offers") or [] if offer.get("ean") == ean]
	if len(found) > 1:
		raise BolMappingError(
			_("bol has {0} offers for EAN {1}. Delete the extra ones in the bol seller dashboard.").format(
				len(found), ean
			)
		)
	if found:
		_insert_mapping(item.name, found[0]["offerId"], ean)
		return found[0]

	created = client.request(
		"POST", PATH, version=VERSION, json=_desired(item, settings), reference=_reference(item)
	)
	offer_id = (created or {}).get("offerId")
	if not offer_id:
		raise BolError("bol answered the create without an offerId", ambiguous=True)
	_insert_mapping(item.name, offer_id, ean)
	# The offer exists at bol now: the mapping must survive whatever fails after this point.
	frappe.db.commit()  # nosemgrep
	frappe.db.savepoint(SYNC_SAVEPOINT)
	_refresh_status(client, item, _get_offer(client, offer_id))
	return None


def _desired(item, settings) -> dict:
	"""The for-sale offer for `item`. Raises BolMappingError (with a translated message) when it can't be built."""
	ean = _ean(item)
	price = _price(item, settings)
	operator_id = item.brand and frappe.db.get_value("Brand", item.brand, "bol_economic_operator_id")
	if not operator_id:
		raise BolMappingError(
			_("{0} has no Brand with a bol economic operator.").format(item.name), reason="no_operator"
		)
	# 0 on the Item means "use the Settings default" (ADR-007).
	min_days = cint(item.bol_min_days) or cint(settings.default_min_days)
	max_days = cint(item.bol_max_days) or cint(settings.default_max_days)
	try:
		# Stock is set by the stock sync (story 06); a new offer starts with none.
		return build_offer({"item_code": item.name, "ean": ean}, price, operator_id, min_days, max_days, 0)
	except BolMappingError as e:
		if e.reason == "price_range":
			message = _("The price {0} of {1} is outside bol's range of 1 to 9999.").format(price, item.name)
		else:
			message = _("Min Days to Customer ({0}) is more than Max Days to Customer ({1}) for {2}.").format(
				min_days, max_days, item.name
			)
		raise BolMappingError(message, reason=e.reason) from e


def _ean(item) -> str:
	try:
		return get_item_ean(item)
	except BolMappingError as e:
		raise BolMappingError(ean_problem(item), reason=e.reason) from e


def _price(item, settings) -> float:
	# ERPNext's own lookup: generic (no customer) rows in the stock UOM that are valid today.
	rows = get_item_price(
		frappe._dict(price_list=settings.price_list, uom=item.stock_uom, transaction_date=nowdate()),
		item.name,
	)
	if not rows:
		# Never fall back to 0: bol would sell the item for nothing (or reject it).
		raise BolMappingError(
			_("{0} has no Item Price in price list {1}.").format(item.name, settings.price_list),
			reason="no_price",
		)
	return flt(rows[0][1], 2)


def _get_offer(client, offer_id: str) -> dict:
	return client.request("GET", f"{PATH}/{offer_id}", version=VERSION) or {}


def _refresh_status(client, item, offer: dict):
	status = offer_status(offer)
	detail = ""
	if status == NOT_FOR_SALE:
		reasons = client.request("GET", f"{PATH}/{offer['offerId']}/not-for-sale-reasons", version=VERSION)
		detail = format_not_for_sale_reasons(reasons)
	_write_status(item, status, detail)


def _write_status(item, status: str, detail: str):
	# db_set: no Item validation or doc_events, so writing the status never triggers another sync.
	item.db_set({"bol_offer_status": status, "bol_status_detail": detail}, update_modified=False)


def _not_synced(item, detail: str, error: BolError):
	"""A failure that needs a data fix: shown on the Item. A live offer at bol stays as it was."""
	frappe.db.rollback(save_point=SYNC_SAVEPOINT)
	_write_status(item, NOT_SYNCED, detail)
	_record_failure(item, error, detail)


def _record_failure(item, error: BolError, detail: str | None = None):
	reference = _reference(item)
	if log_once("offer", item.name, **error.details, error=error, reference=reference):
		add_comment(reference, _("bol offer sync failed: {0}").format(detail or str(error)))


def _mapping(item_code: str):
	return frappe.db.get_value(
		MAPPING,
		{"integration": INTEGRATION, "erpnext_item_code": item_code},
		["name", "integration_item_code"],
		as_dict=True,
	)


def _reference(item) -> tuple[str, str]:
	return ("Item", item.name)


def _settings():
	return frappe.get_cached_doc("Bol Settings")


# --- Daily reconcile --------------------------------------------------------------------------------


def reconcile_offers():
	"""Refresh every mapped offer's status from one pass over bol's offer list, and queue a sync for each
	offer that no longer matches its Item (a failed or missed sync), is gone at bol, or was never created.
	"""
	settings = _settings()
	if not sync_on(settings):
		return
	mapped = dict(
		frappe.get_all(
			MAPPING,
			filters={"integration": INTEGRATION},
			fields=["integration_item_code", "erpnext_item_code"],
			as_list=True,
		)
	)
	client = get_client()
	seen = set()
	try:
		for page_offers, _cursor in _offer_pages(client):
			for offer in page_offers:
				offer_id = offer.get("offerId")
				if offer_id not in mapped or offer_id in seen:
					continue
				seen.add(offer_id)
				_reconcile(client, mapped[offer_id], offer, settings)
				frappe.db.commit()  # nosemgrep
	except BolError as e:
		# Without the full list, "missing at bol" can't be told apart from "not listed yet": stop here.
		log_call(**e.details, error=e, message=_("bol offer reconcile stopped: {0}").format(str(e)))
		raise

	missing = {item_code for offer_id, item_code in mapped.items() if offer_id not in seen}
	unmapped = frappe.get_all(
		"Item",
		filters={"bol_enabled": 1, "disabled": 0, "name": ("not in", list(mapped.values()) or [""])},
		pluck="name",
	)
	for item_code in sorted(missing | set(unmapped)):
		enqueue_sync(item_code)


def _reconcile(client, item_code: str, offer: dict, settings):
	frappe.db.savepoint(RECONCILE_SAVEPOINT)
	try:
		item = frappe.get_doc("Item", item_code)
		_refresh_status(client, item, offer)
		if _drifted(item, offer, settings):
			enqueue_sync(item_code)
	except Exception as e:
		# One bad offer must not stop the others.
		frappe.db.rollback(save_point=RECONCILE_SAVEPOINT)
		log_once("offer", item_code, **getattr(e, "details", {}), error=e, reference=("Item", item_code))


def _drifted(item, offer: dict, settings) -> bool:
	for_sale = bool(item.bol_enabled and not item.disabled)
	try:
		desired = _desired(item, settings) if for_sale else HOLD
	except BolMappingError:
		# sync_offer shows why on the Item.
		return True
	return bool(diff_offer(offer, desired))


# --- Explicit delete --------------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def delete_offer(item_code: str):
	"""Delete the Item's offer at bol, remove the mapping and untick Sell on bol."""
	# Frappe enforces the type hints of whitelisted functions.
	check_bol_role()
	frappe.has_permission("Item", "read", item_code, throw=True)
	if not _settings().enabled:
		frappe.throw(_("Enable the bol integration in Bol Settings first."), title=_("bol Disabled"))
	item = frappe.get_doc("Item", item_code)
	mapping = _mapping(item.name)
	if not mapping:
		frappe.throw(_("{0} has no bol offer.").format(item.name))

	try:
		get_client().request(
			"DELETE", f"{PATH}/{mapping.integration_item_code}", version=VERSION, reference=_reference(item)
		)
	except BolError as e:
		if not isinstance(e, BolNotFoundError):
			log_call(**e.details, error=e, reference=_reference(item))
			frappe.throw(_("Could not delete the bol offer: {0}").format(str(e)), title=_("bol Error"))
		# 404: already deleted at bol; only our side is left to clean up.

	frappe.delete_doc(MAPPING, mapping.name, ignore_permissions=True)
	# db_set, not save: unticking must not queue a sync, and Bol Managers may lack Item write permission.
	item.db_set({"bol_enabled": 0, "bol_offer_status": DELETED, "bol_status_detail": ""})


# --- Triggers (doc_events) --------------------------------------------------------------------------


def on_item_update(doc, method=None):
	if not (doc.bol_enabled or _was_set(doc, "bol_enabled")):
		return
	if any(_changed(doc, field) for field in TRIGGER_FIELDS) and sync_on():
		enqueue_sync(doc.name)


def on_item_price_change(doc, method=None):
	if not sync_on():
		return
	before = doc.get_doc_before_save()
	price_lists = {doc.price_list, before and before.price_list}
	if _settings().price_list in price_lists and frappe.db.get_value("Item", doc.item_code, "bol_enabled"):
		enqueue_sync(doc.item_code)


def on_brand_update(doc, method=None):
	if not _changed(doc, "bol_economic_operator_id") or not sync_on():
		return
	for item_code in frappe.get_all("Item", filters={"brand": doc.name, "bol_enabled": 1}, pluck="name"):
		enqueue_sync(item_code)


def _changed(doc, fieldname: str) -> bool:
	# Unlike has_value_changed, 0, "" and None count as the same (an API save may leave an Int as None).
	before = doc.get_doc_before_save()
	return before is None or (before.get(fieldname) or None) != (doc.get(fieldname) or None)


def _was_set(doc, fieldname: str) -> bool:
	before = doc.get_doc_before_save()
	return bool(before and before.get(fieldname))
