# Every custom field this app adds to other apps' DocTypes, keyed by DocType.
# Applied by install.ensure_custom_fields on install and on every migrate.
# Conventions: fieldnames start with `bol_`; bol identifiers are `read_only` and `no_copy`
# (and `unique` where they are a natural key).
CUSTOM_FIELDS: dict[str, list[dict]] = {
	# The document a bol call or failure relates to (Ecommerce Integration Log has no such field).
	"Ecommerce Integration Log": [
		{
			"fieldname": "bol_reference_doctype",
			"fieldtype": "Link",
			"label": "Reference DocType",
			"options": "DocType",
			"insert_after": "integration",
			"read_only": 1,
		},
		{
			"fieldname": "bol_reference_name",
			"fieldtype": "Dynamic Link",
			"label": "Reference Name",
			"options": "bol_reference_doctype",
			"insert_after": "bol_reference_doctype",
			"read_only": 1,
		},
	],
	# Which Items are sold on bol (story 04). Offer status fields are written by the offer sync only.
	"Item": [
		{
			"fieldname": "bol_section",
			"fieldtype": "Section Break",
			"label": "bol.com",
			"insert_after": "max_discount",
			"collapsible": 1,
		},
		{
			"fieldname": "bol_enabled",
			"fieldtype": "Check",
			"label": "Sell on bol",
			"description": "Needs a stock item (not a template) with one valid EAN barcode, and a Brand "
			"with a bol economic operator.",
			"insert_after": "bol_section",
			"no_copy": 1,
		},
		{
			"fieldname": "bol_min_days",
			"fieldtype": "Int",
			"label": "bol Min Days to Customer",
			"description": "0 = default from Bol Settings.",
			"insert_after": "bol_enabled",
			"non_negative": 1,
		},
		{
			"fieldname": "bol_max_days",
			"fieldtype": "Int",
			"label": "bol Max Days to Customer",
			"description": "0 = default from Bol Settings.",
			"insert_after": "bol_min_days",
			"non_negative": 1,
		},
		{
			"fieldname": "bol_stock_buffer",
			"fieldtype": "Int",
			"label": "bol Stock Buffer",
			"description": "Units kept back from bol. 0 = default from Bol Settings.",
			"insert_after": "bol_max_days",
			"non_negative": 1,
		},
		{"fieldname": "bol_column", "fieldtype": "Column Break", "insert_after": "bol_stock_buffer"},
		{
			"fieldname": "bol_offer_status",
			"fieldtype": "Data",
			"label": "bol Offer Status",
			"insert_after": "bol_column",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "bol_status_detail",
			"fieldtype": "Small Text",
			"label": "bol Status Detail",
			"insert_after": "bol_offer_status",
			"read_only": 1,
			"no_copy": 1,
		},
	],
	# Offers of a brand's items are linked to this operator. Set with the Brand form button only.
	"Brand": [
		{
			"fieldname": "bol_economic_operator_id",
			"fieldtype": "Data",
			"label": "bol Economic Operator ID",
			"insert_after": "description",
			"read_only": 1,
			"no_copy": 1,
		},
	],
	"Ecommerce Item": [
		{
			"fieldname": "bol_last_stock_pushed",
			"fieldtype": "Int",
			"label": "bol Last Stock Pushed",
			"insert_after": "inventory_synced_on",
			"read_only": 1,
			"no_copy": 1,
		},
	],
	# Imported bol orders (story 07). bol_order_id is the natural key of the import.
	"Sales Order": [
		{
			"fieldname": "bol_order_id",
			"fieldtype": "Data",
			"label": "bol Order ID",
			"insert_after": "po_date",
			"unique": 1,
			"no_copy": 1,
			"read_only": 1,
			"in_standard_filter": 1,
		},
	],
	# Per line: what shipments (08) and cancellations (10) refer to at bol.
	"Sales Order Item": [
		{
			"fieldname": "bol_section",
			"fieldtype": "Section Break",
			"label": "bol.com",
			"insert_after": "project",
			"collapsible": 1,
			"depends_on": "eval:doc.bol_order_item_id",
		},
		{
			"fieldname": "bol_order_item_id",
			"fieldtype": "Data",
			"label": "bol Order Item ID",
			"insert_after": "bol_section",
			"no_copy": 1,
			"read_only": 1,
		},
		{
			"fieldname": "bol_distribution_party",
			"fieldtype": "Select",
			"label": "bol Distribution Party",
			"options": "\nRETAILER\nBOL",
			"insert_after": "bol_order_item_id",
			"read_only": 1,
		},
		{"fieldname": "bol_column", "fieldtype": "Column Break", "insert_after": "bol_distribution_party"},
		{
			"fieldname": "bol_latest_delivery_date",
			"fieldtype": "Date",
			"label": "bol Latest Delivery Date",
			"insert_after": "bol_column",
			"read_only": 1,
		},
		{
			"fieldname": "bol_commission",
			"fieldtype": "Currency",
			"label": "bol Commission",
			"options": "currency",
			"insert_after": "bol_latest_delivery_date",
			"read_only": 1,
		},
	],
	# Buyer addresses created by the order import: they hold buyer PII (cleaned up in story 15).
	"Address": [
		{
			"fieldname": "bol_buyer",
			"fieldtype": "Check",
			"label": "bol Buyer",
			"insert_after": "is_shipping_address",
			"read_only": 1,
			"no_copy": 1,
		},
	],
}
