# Test doubles for bol HTTP calls. No test talks to bol.

import json

import frappe
import requests
from requests.structures import CaseInsensitiveDict

from marketplace_integrations.bol.api.auth import TOKEN_URL

SECRET = "s3cr3t-client-secret"
TOKEN = "tok-3f9a1c7e"


def make_settings(**overrides) -> frappe._dict:
	settings = frappe._dict(
		doctype="Bol Settings",
		name="Bol Settings",
		enabled=1,
		environment="Demo",
		client_id="test-client-id",
		debug_logging=0,
	)
	settings.update(overrides)
	settings.get_password = lambda fieldname: SECRET
	return settings


def make_response(status=200, json_body=None, headers=None, content=b"") -> requests.Response:
	response = requests.Response()
	response.status_code = status
	response.headers = CaseInsensitiveDict(headers or {})
	response.encoding = "utf-8"
	if json_body is not None:
		response._content = json.dumps(json_body).encode()
		response.headers.setdefault("Content-Type", "application/vnd.retailer.v10+json")
	else:
		response._content = content
	return response


def token_response(expires_in=299) -> requests.Response:
	return make_response(200, {"access_token": TOKEN, "token_type": "Bearer", "expires_in": expires_in})


class FakeSession:
	"""Replays queued API responses (or raises queued exceptions) and answers token requests."""

	def __init__(self, *responses, token=None):
		self.responses = list(responses)
		self.token = token
		self.calls = []
		self.token_calls = []

	def request(self, method, url, **kwargs):
		if url == TOKEN_URL:
			self.token_calls.append(kwargs)
			result = self.token if self.token is not None else token_response()
		else:
			self.calls.append({"method": method, "url": url, **kwargs})
			result = self.responses.pop(0)
		if isinstance(result, Exception):
			raise result
		return result


BOL_MANAGER = "bol.manager.test@example.com"


def make_bol_manager():
	"""A user whose only role is Bol Manager (no permission on ERPNext documents)."""
	if frappe.db.exists("User", BOL_MANAGER):
		return
	user = frappe.get_doc(
		{"doctype": "User", "email": BOL_MANAGER, "first_name": "bol", "send_welcome_email": 0}
	)
	user.append("roles", {"role": "Bol Manager"})
	user.insert()


def discard_pending_logs():
	"""Drop the logs bol/log.py queued in this test's transaction (they would be flushed on rollback)."""
	frappe.local.bol_pending_logs = None


# Catalog records. The test site has no ERPNext masters (no setup wizard), so tests create them.
TEST_UOM = "_Test bol UOM"
TEST_ITEM_GROUP = "_Test bol Item Group"
TEST_BRAND = "_Test bol Brand"
TEST_PRICE_LIST = "_Test bol Price List"
TEST_WAREHOUSE = "_Test bol Warehouse"
OPERATOR_ID = "0c6573a2-a80c-48b7-a03e-d5939f1173fa"


def make_catalog_masters():
	"""UOM, Item Group and a Brand with an economic operator, in the caller's transaction."""
	if not frappe.db.exists("UOM", TEST_UOM):
		frappe.get_doc({"doctype": "UOM", "uom_name": TEST_UOM}).insert()
	if not frappe.db.exists("Item Group", TEST_ITEM_GROUP):
		root = frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ("in", ("", None))})
		frappe.get_doc(
			{"doctype": "Item Group", "item_group_name": TEST_ITEM_GROUP, "parent_item_group": root}
		).insert()
	make_brand(TEST_BRAND, OPERATOR_ID)


def make_brand(name: str, operator_id: str | None = None):
	if frappe.db.exists("Brand", name):
		frappe.db.set_value("Brand", name, "bol_economic_operator_id", operator_id)
		return frappe.get_doc("Brand", name)
	return frappe.get_doc(
		{"doctype": "Brand", "brand": name, "bol_economic_operator_id": operator_id}
	).insert()


def make_item(item_code: str, *barcodes, **values):
	"""An Item; each barcode is a code (type EAN) or a (code, barcode_type) tuple."""
	rows = [
		{"barcode": b, "barcode_type": "EAN"}
		if isinstance(b, str)
		else dict(zip(("barcode", "barcode_type"), b, strict=True))
		for b in barcodes
	]
	return frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_group": TEST_ITEM_GROUP,
			"stock_uom": TEST_UOM,
			"is_stock_item": 1,
			"brand": TEST_BRAND,
			"barcodes": rows,
			**values,
		}
	).insert()


def make_price_list(name: str = TEST_PRICE_LIST, selling: int = 1, currency: str = "EUR") -> str:
	if not frappe.db.exists("Price List", name):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": name,
				"selling": selling,
				"buying": int(not selling),
				"currency": currency,
			}
		).insert()
	return name


def make_item_price(item_code: str, rate: float, price_list: str = TEST_PRICE_LIST):
	return frappe.get_doc(
		{"doctype": "Item Price", "item_code": item_code, "price_list": price_list, "price_list_rate": rate}
	).insert()


def make_warehouse(name: str = TEST_WAREHOUSE, is_group: int = 0) -> str:
	# Saved without validation: a Warehouse needs a Company, and the test site has none.
	if not frappe.db.exists("Warehouse", name):
		frappe.get_doc(
			{"doctype": "Warehouse", "name": name, "warehouse_name": name, "is_group": is_group}
		).db_insert()
	return name


# Order import masters: a Company with its chart of accounts, a Customer and VAT-inclusive templates.
TEST_COMPANY = "_Test bol Company"
TEST_COMPANY_ABBR = "TBOL"
TEST_CUSTOMER = "_Test bol Customer"
TAX_TEMPLATE_TITLES = {"NL": "_Test bol VAT NL incl", "BE": "_Test bol VAT BE incl"}


def make_order_masters() -> frappe._dict:
	"""The masters a Sales Order needs, created once and committed: building a Company takes seconds.

	The test site has no Company (no setup wizard); ERPNext's own Company test records need its full test setup.
	"""
	if not frappe.db.exists("Company", TEST_COMPANY):
		_make_company()
		frappe.db.commit()  # nosemgrep
	if not frappe.db.exists("Address Template", {"is_default": 1}):
		# Rendering an Address needs one; the setup wizard normally creates it.
		frappe.get_doc({"doctype": "Address Template", "country": "Netherlands", "is_default": 1}).insert()
		frappe.db.commit()  # nosemgrep
	return frappe._dict(
		company=TEST_COMPANY,
		customer=TEST_CUSTOMER,
		warehouse=f"Stores - {TEST_COMPANY_ABBR}",
		tax_template_nl=f"{TAX_TEMPLATE_TITLES['NL']} - {TEST_COMPANY_ABBR}",
		tax_template_be=f"{TAX_TEMPLATE_TITLES['BE']} - {TEST_COMPANY_ABBR}",
	)


def _make_company():
	if not frappe.db.exists("Warehouse Type", "Transit"):
		# Company.create_default_warehouses adds a "Goods In Transit" warehouse of this type.
		frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert()
	frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": TEST_COMPANY,
			"abbr": TEST_COMPANY_ABBR,
			"default_currency": "EUR",
			"country": "Netherlands",
			"create_chart_of_accounts_based_on": "Standard Template",
			"chart_of_accounts": "Standard",
		}
	).insert()
	parent = frappe.db.get_value("Account", {"company": TEST_COMPANY, "account_type": "Tax", "is_group": 1})
	for country, title in TAX_TEMPLATE_TITLES.items():
		account = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": f"_Test bol VAT {country}",
				"company": TEST_COMPANY,
				"parent_account": parent,
				"account_type": "Tax",
				"tax_rate": 21,
			}
		).insert()
		make_tax_template(title, account.name)
	frappe.get_doc(
		{"doctype": "Customer", "customer_name": TEST_CUSTOMER, "customer_type": "Company"}
	).insert()


def make_tax_template(title: str, account: str, included: int = 1, company: str = TEST_COMPANY):
	return frappe.get_doc(
		{
			"doctype": "Sales Taxes and Charges Template",
			"title": title,
			"company": company,
			"taxes": [
				{
					"charge_type": "On Net Total",
					"account_head": account,
					"description": title,
					"rate": 21,
					"included_in_print_rate": included,
				}
			],
		}
	).insert()
