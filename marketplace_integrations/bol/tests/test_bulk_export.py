# The Excel file for bol's Items → Add items in bulk: which Items go in, their columns, and the file itself.

from io import BytesIO

import frappe
from erpnext.controllers.item_variant import create_variant
from frappe.tests.utils import FrappeTestCase
from openpyxl import load_workbook

from marketplace_integrations.bol import bulk_export
from marketplace_integrations.bol.report.bol_bulk_upload_export.bol_bulk_upload_export import execute
from marketplace_integrations.bol.tests.utils import (
	TEST_BRAND,
	TEST_ITEM_GROUP,
	discard_pending_logs,
	make_catalog_masters,
	make_item,
)

TEMPLATE = "_Test bol export shirt"
SIZE = "_Test bol export Size"
SIZES = {"116": "8712345670146", "128": "8712345670153"}
PLAIN, PLAIN_EAN = "_Test bol export bag", "8712345670160"
NO_EAN = "_Test bol export cap"
ON_BOL, ON_BOL_EAN = "_Test bol export bib", "8712345670177"
DESCRIPTION = (
	'<div class="x"><p style="color:red">Soft <em>cotton</em>, with your <strong>name</strong>.</p>'
	"<ul><li>Washable</li></ul><script>alert(1)</script></div>"
)


class TestBulkExport(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		self.addCleanup(frappe.set_user, "Administrator")
		make_catalog_masters()
		if not frappe.db.exists("Item Attribute", SIZE):
			frappe.get_doc(
				{
					"doctype": "Item Attribute",
					"attribute_name": SIZE,
					"item_attribute_values": [{"attribute_value": size, "abbr": size} for size in SIZES],
				}
			).insert()
		make_item(
			TEMPLATE,
			has_variants=1,
			attributes=[{"attribute": SIZE}],
			description=DESCRIPTION,
		)
		for size, ean in SIZES.items():
			variant = create_variant(TEMPLATE, {SIZE: size})
			variant.item_code = variant.item_name = f"{TEMPLATE}-{size}"
			variant.description = None
			variant.append("barcodes", {"barcode": ean, "barcode_type": "EAN"})
			variant.insert()
		make_item(PLAIN, PLAIN_EAN, description="Canvas bag", bol_enabled=0)
		make_item(NO_EAN)
		make_item(ON_BOL, ON_BOL_EAN)
		frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": "Bol",
				"erpnext_item_code": ON_BOL,
				"integration_item_code": "offer-1",
				"sku": ON_BOL_EAN,
			}
		).insert()

	def _export(self, **filters):
		return bulk_export.export({"item_group": TEST_ITEM_GROUP, **filters})

	def _codes(self, result) -> list[str]:
		return [row["internal_reference"] for row in result["rows"]]

	def test_variants_and_plain_items(self):
		result = self._export(only_new=1)
		self.assertEqual(self._codes(result), [PLAIN, f"{TEMPLATE}-116", f"{TEMPLATE}-128"])
		variant = result["rows"][1]
		self.assertEqual(variant["ean"], SIZES["116"])
		self.assertEqual(variant["family_key"], TEMPLATE)
		self.assertEqual(variant["attribute__test_bol_export_size"], "116")
		# Variants take the template's description and brand when they have none.
		self.assertEqual(
			variant["description"],
			"<p>Soft cotton, with your <strong>name</strong>.</p><ul><li>Washable</li></ul>",
		)
		self.assertEqual(variant["brand"], TEST_BRAND)
		self.assertNotIn("image_url", variant)
		plain = result["rows"][0]
		self.assertIsNone(plain["family_key"])

	def test_a_variants_own_description_wins(self):
		frappe.db.set_value("Item", f"{TEMPLATE}-128", "description", "<p>Size 128: <b>longer</b> fit</p>")
		rows = {row["internal_reference"]: row for row in self._export()["rows"]}
		self.assertEqual(rows[f"{TEMPLATE}-128"]["description"], "<p>Size 128: <b>longer</b> fit</p>")
		self.assertIn("Soft cotton", rows[f"{TEMPLATE}-116"]["description"])

	def test_templates_and_items_without_ean_left_out(self):
		result = self._export()
		self.assertNotIn(TEMPLATE, self._codes(result))
		self.assertEqual(result["skipped"], [NO_EAN])

	def test_items_already_on_bol(self):
		self.assertNotIn(ON_BOL, self._codes(self._export(only_new=1)))
		self.assertIn(ON_BOL, self._codes(self._export(only_new=0)))

	def test_filters(self):
		self.assertEqual(self._codes(self._export(template=TEMPLATE)), [f"{TEMPLATE}-116", f"{TEMPLATE}-128"])
		self.assertEqual(self._codes(self._export(brand="_Test no such brand")), [])
		frappe.db.set_value("Item", PLAIN, "bol_enabled", 1, update_modified=False)
		self.assertEqual(self._codes(self._export(only_sell_on_bol=1, only_new=1)), [PLAIN])

	def test_columns(self):
		labels = [column["label"] for column in self._export()["columns"]]
		self.assertEqual(
			labels,
			[
				"EAN",
				"Title",
				"Description",
				"Brand",
				"Internal Reference",
				"Family Key",
				SIZE,
				"Item Group",
			],
		)
		# bol needs unique column names.
		self.assertEqual(len(labels), len(set(labels)))

	def test_report(self):
		columns, rows, message = execute({"item_group": TEST_ITEM_GROUP, "only_new": 1})
		self.assertEqual(len(rows), 3)
		self.assertEqual(len(columns), 8)
		self.assertIn(NO_EAN, message)

	def test_xlsx(self):
		result = self._export(only_new=1)
		workbook = load_workbook(BytesIO(bulk_export.xlsx(result["columns"], result["rows"])))
		# One tab; the column names in row 1; descriptions keep their HTML.
		self.assertEqual(workbook.sheetnames, ["bol"])
		sheet = workbook.active
		rows = list(sheet.iter_rows(values_only=True))
		self.assertEqual(rows[0][:3], ("EAN", "Title", "Description"))
		self.assertEqual(len(rows), 4)
		self.assertEqual(rows[2][0], SIZES["116"])
		self.assertIn("<ul><li>Washable</li></ul>", rows[2][2])

	def test_download(self):
		bulk_export.download(frappe.as_json({"item_group": TEST_ITEM_GROUP, "only_new": 1}))
		self.assertEqual(frappe.response["filename"], "bol-bulk-upload.xlsx")
		self.assertEqual(frappe.response["type"], "binary")
		self.assertEqual(load_workbook(BytesIO(frappe.response["filecontent"])).active["A1"].value, "EAN")

	def test_download_needs_a_bol_role(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			bulk_export.download("{}")

	def test_download_with_nothing_to_export(self):
		with self.assertRaisesRegex(frappe.ValidationError, "No items to export"):
			bulk_export.download(frappe.as_json({"brand": "_Test no such brand"}))

	def test_clean_description(self):
		clean = bulk_export.clean_description
		self.assertEqual(clean("<h3>Zacht</h3><br/><b>x</b>"), "<h3>Zacht</h3><br><b>x</b>")
		self.assertEqual(clean('<a href="https://x">link</a> & <img src="x">'), "link &amp;")
		self.assertIsNone(clean("<style>p{}</style>"))
		self.assertIsNone(clean(None))
