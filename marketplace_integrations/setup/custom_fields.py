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
		{"fieldname": "bol_column", "fieldtype": "Column Break", "insert_after": "bol_max_days"},
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
}
