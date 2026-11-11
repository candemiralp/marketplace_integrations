# Items as an Excel file for bol's seller dashboard: Items → Add items in bulk (ADR-016). New EANs go to bol this way
# instead of through the Product Content API. bol reads only the first tab, wants unique column names in row 1 and
# Dutch texts, and suggests attribute matches for column names close to its own (Colour ↔ Kleur).
# The file is written here, not by the report's own Excel export: that one flattens HTML descriptions to plain text.

from io import BytesIO

import bleach
import frappe
from frappe import _
from frappe.utils import cint, strip_html
from frappe.utils.html_utils import clean_script_and_style
from openpyxl import Workbook
from openpyxl.styles import Font

from marketplace_integrations.bol.api.errors import BolMappingError
from marketplace_integrations.bol.log import INTEGRATION
from marketplace_integrations.bol.mappers.ean import EAN_BARCODE_TYPE, get_item_ean
from marketplace_integrations.bol.permissions import check_bol_role

# (fieldname, column name, width). Variant attribute columns go between these two groups.
LEADING_COLUMNS = (
	("ean", "EAN", 140),
	("title", "Title", 260),
	("description", "Description", 320),
	("brand", "Brand", 120),
	("internal_reference", "Internal Reference", 180),
	# bol groups variants that share it; the template's item code.
	("family_key", "Family Key", 180),
)
TRAILING_COLUMNS = (("item_group", "Item Group", 160),)
# The HTML bol accepts in a description.
DESCRIPTION_TAGS = frozenset({"b", "br", "h3", "li", "ol", "ul", "p", "strong"})
FILE_NAME = "bol-bulk-upload.xlsx"
SHEET = "bol"


def export(filters: dict) -> dict:
	"""{"columns": [...], "rows": [...], "skipped": [item codes without a valid EAN]} for `filters`
	(item_group, brand, template, only_sell_on_bol, only_new)."""
	filters = frappe._dict(filters or {})
	items = frappe.get_all(
		"Item",
		filters=_item_filters(filters),
		fields=["name", "item_name", "description", "brand", "item_group", "variant_of"],
		order_by="variant_of asc, name asc",
	)
	if cint(filters.only_new):
		on_bol = _on_bol([item.name for item in items])
		items = [item for item in items if item.name not in on_bol]
	names = [item.name for item in items]
	templates = _templates({item.variant_of for item in items if item.variant_of})
	barcodes = _ean_barcodes(names)
	attributes = _variant_attributes(names)

	rows, skipped = [], []
	for item in items:
		try:
			ean = get_item_ean(frappe._dict(name=item.name, barcodes=barcodes.get(item.name, [])))
		except BolMappingError:
			skipped.append(item.name)
			continue
		template = templates.get(item.variant_of) or {}
		rows.append(
			{
				"ean": ean,
				"title": item.item_name,
				"description": clean_description(_own_description(item) or template.get("description")),
				"brand": item.brand or template.get("brand"),
				"internal_reference": item.name,
				"family_key": item.variant_of,
				**{_attribute_field(name): value for name, value in attributes.get(item.name, {}).items()},
				"item_group": item.item_group,
			}
		)
	attribute_names = list(dict.fromkeys(name for item in names for name in attributes.get(item, {})))
	columns = [
		*(
			{"fieldname": f, "label": label, "fieldtype": "Data", "width": w}
			for f, label, w in LEADING_COLUMNS
		),
		*(
			{"fieldname": _attribute_field(name), "label": name, "fieldtype": "Data", "width": 120}
			for name in attribute_names
		),
		*(
			{"fieldname": f, "label": label, "fieldtype": "Data", "width": w}
			for f, label, w in TRAILING_COLUMNS
		),
	]
	return {"columns": columns, "rows": rows, "skipped": skipped}


def clean_description(html: str | None) -> str | None:
	"""Only the tags bol accepts, without attributes; scripts and styles dropped with their content."""
	if not html:
		return html
	return (
		bleach.clean(
			clean_script_and_style(html),
			tags=DESCRIPTION_TAGS,
			attributes={},
			strip=True,
			strip_comments=True,
		).strip()
		or None
	)


def xlsx(columns: list[dict], rows: list[dict]) -> bytes:
	"""One tab, column names in row 1, the values as they are (HTML descriptions included)."""
	workbook = Workbook()
	sheet = workbook.active
	sheet.title = SHEET
	sheet.append([column["label"] for column in columns])
	for cell in sheet[1]:
		cell.font = Font(bold=True)
	for row in rows:
		sheet.append([row.get(column["fieldname"]) for column in columns])
	for index, column in enumerate(columns, start=1):
		sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = column["width"] / 7
	sheet.freeze_panes = "A2"
	out = BytesIO()
	workbook.save(out)
	return out.getvalue()


@frappe.whitelist(methods=["POST"])
def download(filters: str | dict | None = None):
	"""The report's Download for bol button: the export as an .xlsx file."""
	check_bol_role()
	frappe.has_permission("Item", "read", throw=True)
	result = export(frappe.parse_json(filters) if isinstance(filters, str) else filters)
	if not result["rows"]:
		frappe.throw(_("No items to export with these filters."))
	frappe.response["filename"] = FILE_NAME
	frappe.response["filecontent"] = xlsx(result["columns"], result["rows"])
	frappe.response["type"] = "binary"


def _item_filters(filters) -> dict:
	# Templates aren't products at bol: each variant is its own EAN.
	conditions = {"has_variants": 0, "disabled": 0}
	if filters.item_group:
		conditions["item_group"] = ("descendants of (inclusive)", filters.item_group)
	if filters.brand:
		conditions["brand"] = filters.brand
	if filters.template:
		conditions["variant_of"] = filters.template
	if cint(filters.only_sell_on_bol):
		conditions["bol_enabled"] = 1
	return conditions


def _on_bol(names: list[str]) -> set[str]:
	"""Items that already have a bol offer (mapped in Ecommerce Item): bol knows their EAN."""
	if not names:
		return set()
	return set(
		frappe.get_all(
			"Ecommerce Item",
			filters={"integration": INTEGRATION, "erpnext_item_code": ("in", names)},
			pluck="erpnext_item_code",
		)
	)


def _templates(names: set[str]) -> dict:
	if not names:
		return {}
	rows = frappe.get_all(
		"Item", filters={"name": ("in", list(names))}, fields=["name", "description", "brand"]
	)
	return {row.name: row for row in rows}


def _ean_barcodes(names: list[str]) -> dict[str, list]:
	barcodes: dict[str, list] = {}
	if not names:
		return barcodes
	for row in frappe.get_all(
		"Item Barcode",
		filters={"parent": ("in", names), "parenttype": "Item", "barcode_type": EAN_BARCODE_TYPE},
		fields=["parent", "barcode", "barcode_type"],
	):
		barcodes.setdefault(row.parent, []).append(row)
	return barcodes


def _variant_attributes(names: list[str]) -> dict[str, dict[str, str]]:
	attributes: dict[str, dict[str, str]] = {}
	if not names:
		return attributes
	for row in frappe.get_all(
		"Item Variant Attribute",
		filters={"parent": ("in", names), "parenttype": "Item"},
		fields=["parent", "attribute", "attribute_value"],
		order_by="parent asc, idx asc",
	):
		if row.attribute_value not in (None, ""):
			attributes.setdefault(row.parent, {})[row.attribute] = str(row.attribute_value)
	return attributes


def _own_description(item) -> str | None:
	# ERPNext fills an empty description with the Item Name: that's no description of the variant's own.
	if strip_html(item.description or "").strip() in ("", (item.item_name or "").strip()):
		return None
	return item.description


def _attribute_field(name: str) -> str:
	return f"attribute_{frappe.scrub(name)}"
