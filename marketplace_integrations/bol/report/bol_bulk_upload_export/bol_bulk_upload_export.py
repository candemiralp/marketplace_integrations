# Copyright (c) 2026, Magnatlas and contributors
# For license information, please see license.txt

# Preview of the file for bol's Items → Add items in bulk; Download for bol (bol_bulk_upload_export.js) writes it.

from frappe import _

from marketplace_integrations.bol.bulk_export import export


def execute(filters=None):
	result = export(filters)
	message = None
	if result["skipped"]:
		message = _("Left out, no valid EAN barcode ({0}): {1}").format(
			len(result["skipped"]),
			", ".join(result["skipped"][:20]) + ("…" if len(result["skipped"]) > 20 else ""),
		)
	return result["columns"], result["rows"], message
