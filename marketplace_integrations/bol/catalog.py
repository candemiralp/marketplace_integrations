# Which Items can be sold on bol (Item validation) and the economic operator on a Brand (story 04).

import re

import frappe
from frappe import _
from frappe.utils import cint

from marketplace_integrations.bol.api.client import get_client
from marketplace_integrations.bol.api.errors import BolError, BolMappingError, BolNotFoundError
from marketplace_integrations.bol.log import log_call
from marketplace_integrations.bol.mappers.ean import get_item_ean
from marketplace_integrations.bol.permissions import check_bol_role

OPERATORS_PATH = "/retailer/economic-operators"
OPERATOR_PATH = "/retailer/economic-operator"
OPERATOR_MEDIA_TYPE = "application/vnd.economic-operator.v1+json"
OPERATOR_VERSION = 1
PAGE_SIZE = 100
# bol's ids are UUIDs; anything else must not reach the URL path.
OPERATOR_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def validate_item(doc, method=None):
	"""Item validate hook: an Item can only be marked for bol when it's publishable."""
	if not doc.get("bol_enabled"):
		return
	problems = _publish_problems(doc)
	if problems:
		frappe.throw(problems, title=_("Can't Sell on bol"), as_list=True)


def _publish_problems(doc) -> list[str]:
	problems = []
	if doc.has_variants:
		problems.append(
			_("{0} is a template with variants. Tick Sell on bol on its variants instead.").format(doc.name)
		)
	if not doc.is_stock_item:
		problems.append(_("{0} is not a stock item. Only stock items can be sold on bol.").format(doc.name))
	ean_problem = _ean_problem(doc)
	if ean_problem:
		problems.append(ean_problem)
	if not doc.brand:
		problems.append(
			_("Set a Brand on {0}. Its bol economic operator comes from the Brand.").format(doc.name)
		)
	elif not frappe.db.get_value("Brand", doc.brand, "bol_economic_operator_id"):
		problems.append(
			_(
				"Brand {0} has no bol economic operator. Use Select bol economic operator on the Brand."
			).format(doc.brand)
		)
	min_days, max_days = cint(doc.get("bol_min_days")), cint(doc.get("bol_max_days"))
	if min_days and max_days and min_days > max_days:
		problems.append(
			_("bol Min Days to Customer ({0}) can't be more than bol Max Days to Customer ({1}).").format(
				min_days, max_days
			)
		)
	return problems


def _ean_problem(doc) -> str | None:
	try:
		get_item_ean(doc)
	except BolMappingError as e:
		if e.reason == "missing":
			return _("Add a barcode of type EAN to {0}.").format(doc.name)
		if e.reason == "several":
			return _("{0} has more than one barcode of type EAN. Keep exactly one.").format(doc.name)
		return _("The EAN barcode of {0} is not a valid 13-digit GTIN.").format(doc.name)
	return None


@frappe.whitelist()
def list_economic_operators() -> list[dict]:
	"""The retailer's economic operators at bol: [{id, name, status}], sorted by name."""
	check_bol_role()
	client = _client()
	operators, page = {}, 1
	while True:
		try:
			response = client.request(
				"GET",
				OPERATORS_PATH,
				version=OPERATOR_VERSION,
				accept=OPERATOR_MEDIA_TYPE,
				params={"page": page, "page-size": PAGE_SIZE},
			)
		except BolNotFoundError:
			# The spec documents 404 for the list: no (more) operators.
			break
		except BolError as e:
			log_call(**e.details, error=e)
			frappe.throw(
				_("Could not load the economic operators from bol: {0}").format(str(e)), title=_("bol Error")
			)
		# Keyed by id: a page can repeat an operator (bol's demo does).
		operators.update({op["id"]: _operator(op) for op in response.get("operators") or []})
		if page >= cint((response.get("page") or {}).get("numberOfPages")):
			break
		page += 1
	return sorted(operators.values(), key=lambda operator: (operator["name"] or "").lower())


@frappe.whitelist(methods=["POST"])
def set_brand_economic_operator(brand: str, operator_id: str | None = None) -> dict:
	"""Link `brand` to a bol economic operator (checked at bol), or unlink it when no id is given.

	Returns the operator's bol status; offers of an operator that isn't VALID won't be published.
	"""
	# Frappe enforces the type hints of whitelisted functions.
	check_bol_role()
	frappe.has_permission("Brand", "read", brand, throw=True)
	doc = frappe.get_doc("Brand", brand)
	operator_id = (operator_id or "").strip()
	if operator_id and not OPERATOR_ID.match(operator_id):
		frappe.throw(_("Invalid economic operator ID: {0}").format(operator_id))

	status = None
	if operator_id:
		status = _operator(_fetch_operator(operator_id, doc))["status"]
	doc.bol_economic_operator_id = operator_id or None
	# Saved as a document (not db.set_value) so Brand hooks see the change. Bol Managers may set this one
	# field without Brand write permission.
	doc.save(ignore_permissions=True)
	return {"status": status}


def _fetch_operator(operator_id: str, brand_doc) -> dict:
	try:
		return _client().request(
			"GET", f"{OPERATOR_PATH}/{operator_id}", version=OPERATOR_VERSION, accept=OPERATOR_MEDIA_TYPE
		)
	except BolError as e:
		log_call(**e.details, error=e, reference=(brand_doc.doctype, brand_doc.name))
		if isinstance(e, BolNotFoundError):
			frappe.throw(
				_("bol has no economic operator with ID {0}.").format(operator_id), title=_("Not Found")
			)
		frappe.throw(
			_("Could not check the economic operator at bol: {0}").format(str(e)), title=_("bol Error")
		)


def _client():
	if not frappe.db.get_single_value("Bol Settings", "enabled"):
		frappe.throw(_("Enable the bol integration in Bol Settings first."), title=_("bol Disabled"))
	return get_client()


def _operator(operator: dict) -> dict:
	return {"id": operator.get("id"), "name": operator.get("name"), "status": operator.get("status")}
