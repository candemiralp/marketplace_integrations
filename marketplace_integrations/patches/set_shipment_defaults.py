# Bol Settings is a Single: a field added after the settings were first saved has no stored value, so its
# default never applies (Frappe uses defaults only while a Single has no values at all). Story 08's fields.

import frappe

DOCTYPE = "Bol Settings"
FIELDS = ("require_track_and_trace", "default_transporter_code")


def execute():
	singles = frappe.qb.Table("tabSingles")
	stored = set(
		frappe.qb.from_(singles)
		.select(singles.field)
		.where((singles.doctype == DOCTYPE) & singles.field.isin(FIELDS))
		.run(pluck=True)
	)
	meta = frappe.get_meta(DOCTYPE)
	for fieldname in FIELDS:
		if fieldname not in stored:
			frappe.db.set_single_value(DOCTYPE, fieldname, meta.get_field(fieldname).default)
