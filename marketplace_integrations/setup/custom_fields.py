# Every custom field this app adds to other apps' DocTypes, keyed by DocType.
# Applied by install.ensure_custom_fields on install and on every migrate.
# Conventions: fieldnames start with `bol_`; bol identifiers are `read_only` and `no_copy`
# (and `unique` where they are a natural key).

# The party bol's buyer is (all bol orders are booked on one bol customer): what an invoice must name.
# Not no_copy, so ERPNext's mappers carry them from Sales Order to Delivery Note and Sales Invoice.
# Buyer PII, like the bol Buyer addresses (cleaned up in story 15).
BUYER_FIELDS = (
	("bol_buyer_name", "bol Buyer Name"),
	("bol_buyer_company", "bol Buyer Company"),
	("bol_buyer_vat_number", "bol Buyer VAT Number"),
	("bol_buyer_kvk_number", "bol Buyer KvK Number"),
)


def _buyer_fields(insert_after: str) -> list[dict]:
	fields = []
	for fieldname, label in BUYER_FIELDS:
		fields.append(
			{
				"fieldname": fieldname,
				"fieldtype": "Data",
				"label": label,
				"insert_after": insert_after,
				"read_only": 1,
				"depends_on": "eval:doc.bol_buyer_name",
			}
		)
		insert_after = fieldname
	return fields


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
		{
			"fieldname": "bol_buyer_section",
			"fieldtype": "Section Break",
			"label": "bol.com Buyer",
			"insert_after": "contact_info",
			"depends_on": "eval:doc.bol_buyer_name",
		},
		*_buyer_fields("bol_buyer_section"),
	],
	# The Customer's Purchase Order section; the values come from the Sales Order.
	"Delivery Note": _buyer_fields("po_date"),
	"Sales Invoice": _buyer_fields("po_date"),
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
