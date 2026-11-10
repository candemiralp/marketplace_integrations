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
}
