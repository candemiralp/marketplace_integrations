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


def discard_pending_logs():
	"""Drop the logs bol/log.py queued in this test's transaction (they would be flushed on rollback)."""
	frappe.local.bol_pending_logs = None


# Catalog records. The test site has no ERPNext masters (no setup wizard), so tests create them.
TEST_UOM = "_Test bol UOM"
TEST_ITEM_GROUP = "_Test bol Item Group"
TEST_BRAND = "_Test bol Brand"
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
