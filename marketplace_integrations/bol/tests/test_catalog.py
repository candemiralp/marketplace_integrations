import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from marketplace_integrations.bol import catalog
from marketplace_integrations.bol.api.errors import BolNotFoundError, BolTransientError
from marketplace_integrations.bol.tests.utils import (
	BOL_MANAGER,
	OPERATOR_ID,
	TEST_BRAND,
	discard_pending_logs,
	make_bol_manager,
	make_brand,
	make_catalog_masters,
	make_item,
)

FIXTURES = Path(__file__).parent / "fixtures"
OPERATORS = json.loads((FIXTURES / "economic_operators.json").read_text())
EAN = "4006381333931"
OTHER_EAN = "8712345678906"


class CatalogTestCase(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		self.addCleanup(frappe.set_user, "Administrator")
		make_catalog_masters()


class TestItemValidation(CatalogTestCase):
	def _problems(self, item_code, *barcodes, **values) -> str:
		with self.assertRaises(frappe.ValidationError) as ctx:
			make_item(item_code, *barcodes, bol_enabled=1, **values)
		return str(ctx.exception)

	def test_publishable_item_saves(self):
		item = make_item("_Test bol ok", EAN, bol_enabled=1, bol_min_days=1, bol_max_days=2)
		self.assertEqual(item.bol_enabled, 1)

	def test_not_bol_enabled_skips_the_checks(self):
		make_item("_Test bol off", is_stock_item=0, brand=None)
		catalog.validate_item(frappe._dict(bol_enabled=0, has_variants=1))

	def test_template_rejected(self):
		# ERPNext needs attributes on a template before our hook runs; the hook is called directly.
		item = make_item("_Test bol template", EAN)
		item.update({"bol_enabled": 1, "has_variants": 1})

		with self.assertRaisesRegex(frappe.ValidationError, "is a template with variants"):
			catalog.validate_item(item)

	def test_non_stock_item_rejected(self):
		self.assertIn("is not a stock item", self._problems("_Test bol service", EAN, is_stock_item=0))

	def test_missing_ean(self):
		message = self._problems("_Test bol no ean", ("ABC-123", "CODE-39"))
		self.assertIn("Add a barcode of type EAN", message)

	def test_two_eans(self):
		self.assertIn("more than one barcode of type EAN", self._problems("_Test bol two", EAN, OTHER_EAN))

	def test_missing_brand(self):
		self.assertIn("Set a Brand", self._problems("_Test bol no brand", EAN, brand=None))

	def test_brand_without_operator(self):
		make_brand("_Test bol Brand without operator")
		message = self._problems("_Test bol no eo", EAN, brand="_Test bol Brand without operator")
		self.assertIn("has no bol economic operator", message)

	def test_min_days_above_max_days(self):
		message = self._problems("_Test bol days", EAN, bol_min_days=3, bol_max_days=1)
		self.assertIn("can't be more than", message)

	def test_all_problems_are_listed_at_once(self):
		message = self._problems("_Test bol all", is_stock_item=0, brand=None)
		for part in ("is not a stock item", "Add a barcode of type EAN", "Set a Brand"):
			self.assertIn(part, message)

	def test_saving_an_enabled_item_rechecks(self):
		item = make_item("_Test bol recheck", EAN, bol_enabled=1)
		make_brand(TEST_BRAND, None)

		with self.assertRaisesRegex(frappe.ValidationError, "has no bol economic operator"):
			item.save()


class EconomicOperatorTestCase(CatalogTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value("Bol Settings", "enabled", 1)
		self.client = MagicMock(name="BolClient")
		patcher = patch.object(catalog, "get_client", return_value=self.client)
		self.addCleanup(patcher.stop)
		patcher.start()


class TestListEconomicOperators(EconomicOperatorTestCase):
	def test_maps_and_sorts(self):
		self.client.request.return_value = OPERATORS

		operators = catalog.list_economic_operators()

		self.assertEqual(
			operators,
			[
				{"id": "5d6e7f80-a80c-48b7-a03e-d5939f1173fb", "name": "Atelier Import", "status": "PENDING"},
				{"id": OPERATOR_ID, "name": "Magnatlas B.V.", "status": "VALID"},
			],
		)
		self.client.request.assert_called_once_with(
			"GET",
			"/retailer/economic-operators",
			version=1,
			accept="application/vnd.economic-operator.v1+json",
			params={"page": 1, "page-size": 100},
		)

	def test_pages_through_all_operators(self):
		first, second = OPERATORS["operators"]
		self.client.request.side_effect = [
			{"operators": [first], "page": {"pageNumber": 1, "numberOfPages": 2}},
			{"operators": [second], "page": {"pageNumber": 2, "numberOfPages": 2}},
		]

		self.assertEqual(len(catalog.list_economic_operators()), 2)
		self.assertEqual(self.client.request.call_args.kwargs["params"]["page"], 2)

	def test_repeated_operators_are_listed_once(self):
		first = OPERATORS["operators"][0]
		self.client.request.side_effect = [
			{"operators": [first], "page": {"pageNumber": page, "numberOfPages": 2}} for page in (1, 2)
		]

		self.assertEqual([op["id"] for op in catalog.list_economic_operators()], [first["id"]])

	def test_not_found_means_none(self):
		self.client.request.side_effect = BolNotFoundError("Not found")

		self.assertEqual(catalog.list_economic_operators(), [])

	def test_bol_error_gives_a_clear_message_and_a_log(self):
		self.client.request.side_effect = BolTransientError("bol unavailable (HTTP 503)", {"method": "GET"})

		with self.assertRaisesRegex(frappe.ValidationError, "Could not load the economic operators"):
			catalog.list_economic_operators()
		[entry] = frappe.local.bol_pending_logs
		self.assertEqual(entry["status"], "Error")

	def test_disabled_integration(self):
		frappe.db.set_single_value("Bol Settings", "enabled", 0)

		with self.assertRaisesRegex(frappe.ValidationError, "Enable the bol integration"):
			catalog.list_economic_operators()
		self.client.request.assert_not_called()

	def test_requires_a_bol_role(self):
		frappe.set_user("Guest")

		with self.assertRaises(frappe.PermissionError):
			catalog.list_economic_operators()
		self.client.request.assert_not_called()


class TestSetBrandEconomicOperator(EconomicOperatorTestCase):
	def setUp(self):
		super().setUp()
		self.brand = make_brand("_Test bol Brand to link")

	def test_saves_a_valid_operator(self):
		self.client.request.return_value = OPERATORS["operators"][0]

		result = catalog.set_brand_economic_operator(self.brand.name, OPERATOR_ID)

		self.assertEqual(result, {"status": "VALID"})
		self.assertEqual(
			frappe.db.get_value("Brand", self.brand.name, "bol_economic_operator_id"), OPERATOR_ID
		)
		self.assertEqual(
			self.client.request.call_args.args, ("GET", f"/retailer/economic-operator/{OPERATOR_ID}")
		)

	def test_operator_that_is_not_valid_is_saved_with_its_status(self):
		pending = OPERATORS["operators"][1]
		self.client.request.return_value = pending

		self.assertEqual(
			catalog.set_brand_economic_operator(self.brand.name, pending["id"]), {"status": "PENDING"}
		)
		self.assertEqual(
			frappe.db.get_value("Brand", self.brand.name, "bol_economic_operator_id"), pending["id"]
		)

	def test_unknown_operator(self):
		self.client.request.side_effect = BolNotFoundError("Not found")

		with self.assertRaisesRegex(frappe.ValidationError, "bol has no economic operator with ID"):
			catalog.set_brand_economic_operator(self.brand.name, "1f2e3d4c-0000-0000-0000-000000000000")
		self.assertIsNone(frappe.db.get_value("Brand", self.brand.name, "bol_economic_operator_id"))
		[entry] = frappe.local.bol_pending_logs
		self.assertEqual(
			(entry["bol_reference_doctype"], entry["bol_reference_name"]), ("Brand", self.brand.name)
		)

	def test_clear(self):
		frappe.db.set_value("Brand", self.brand.name, "bol_economic_operator_id", OPERATOR_ID)

		self.assertEqual(catalog.set_brand_economic_operator(self.brand.name, None), {"status": None})
		self.assertIsNone(frappe.db.get_value("Brand", self.brand.name, "bol_economic_operator_id"))
		self.client.request.assert_not_called()

	def test_rejects_an_id_that_is_not_an_id(self):
		for bad in ("../orders", "a b", "x" * 65):
			with self.assertRaisesRegex(frappe.ValidationError, "Invalid economic operator ID"):
				catalog.set_brand_economic_operator(self.brand.name, bad)
		self.client.request.assert_not_called()

	def test_rejects_wrong_argument_types(self):
		with self.assertRaises(frappe.FrappeTypeError):
			catalog.set_brand_economic_operator(self.brand.name, ["id"])
		self.client.request.assert_not_called()

	def test_bol_manager_without_brand_write_permission(self):
		make_bol_manager()
		frappe.set_user(BOL_MANAGER)
		self.client.request.return_value = OPERATORS["operators"][0]

		with patch.object(catalog.frappe, "has_permission", return_value=True) as has_permission:
			catalog.set_brand_economic_operator(self.brand.name, OPERATOR_ID)

		has_permission.assert_called_once_with("Brand", "read", self.brand.name, throw=True)
		self.assertEqual(
			frappe.db.get_value("Brand", self.brand.name, "bol_economic_operator_id"), OPERATOR_ID
		)

	def test_requires_a_bol_role(self):
		frappe.set_user("Guest")

		with self.assertRaises(frappe.PermissionError):
			catalog.set_brand_economic_operator(self.brand.name, OPERATOR_ID)
		self.client.request.assert_not_called()
