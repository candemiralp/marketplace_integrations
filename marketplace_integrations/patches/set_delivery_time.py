# Min/max delivery days became bol's delivery times (ADR-018): turn the stored days into the matching
# delivery time, then remove the old fields. Days that match no delivery time are reported, not guessed.

import frappe
from frappe.utils import cint

from marketplace_integrations.bol.mappers.offer import DELIVERY_TIMES
from marketplace_integrations.install import ensure_custom_fields

DOCTYPE = "Bol Settings"
OLD_SETTINGS = ("default_min_days", "default_max_days")
OLD_ITEM_FIELDS = ("bol_min_days", "bol_max_days")
BY_DAYS = {days: delivery_time for delivery_time, days in DELIVERY_TIMES.items()}


def execute():
	# after_migrate runs after the patches: the new Item fields must exist now.
	ensure_custom_fields()
	defaults = _convert_settings()
	if frappe.db.has_column("Item", "bol_max_days"):
		_convert_items(defaults)
	for fieldname in OLD_ITEM_FIELDS:
		frappe.delete_doc_if_exists("Custom Field", f"Item-{fieldname}")


def _convert_settings() -> tuple[int, int]:
	# The fields are gone from the DocType already, so their values are read from tabSingles directly.
	singles = frappe.qb.Table("tabSingles")
	where = (singles.doctype == DOCTYPE) & singles.field.isin(OLD_SETTINGS)
	stored = dict(frappe.qb.from_(singles).select(singles.field, singles.value).where(where).run())
	defaults = (cint(stored.get("default_min_days")), cint(stored.get("default_max_days")))
	if stored and not frappe.db.get_single_value(DOCTYPE, "default_delivery_time"):
		if defaults in BY_DAYS:
			frappe.db.set_single_value(DOCTYPE, "default_delivery_time", BY_DAYS[defaults])
		else:
			print(f"Bol Settings: no bol delivery time is {defaults[0]}-{defaults[1]} days. Set one.")
	frappe.qb.from_(singles).delete().where(where).run()
	return defaults


def _convert_items(defaults: tuple[int, int]):
	unmatched = []
	items = frappe.get_all(
		"Item",
		or_filters={"bol_min_days": [">", 0], "bol_max_days": [">", 0]},
		fields=["name", *OLD_ITEM_FIELDS],
	)
	for item in items:
		# 0 meant "the Settings default", for each of the two separately.
		days = (cint(item.bol_min_days) or defaults[0], cint(item.bol_max_days) or defaults[1])
		if days == defaults:
			continue
		if days in BY_DAYS:
			frappe.db.set_value("Item", item.name, "bol_delivery_time", BY_DAYS[days], update_modified=False)
		else:
			unmatched.append(item.name)
	if unmatched:
		print(f"Items whose bol days match no bol delivery time (they now use the default): {unmatched}")
