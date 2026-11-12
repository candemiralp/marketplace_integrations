# Buyer PII retention (story 15, ADR-017): bol buyer Addresses are anonymized once every document using them is
# old and finished. Off by default (Bol Settings.anonymize_buyer_addresses_after_days = 0).
#
# The printed address of a submitted Sales Invoice (address_display) is part of the fiscal record and is never
# touched. ERPNext re-renders address_display from the Address whenever a selling document is validated, which
# is why an Address used by anything still in progress is left alone.

import frappe
from frappe import _
from frappe.model.naming import make_autoname
from frappe.model.rename_doc import rename_doc
from frappe.utils import add_days, cint, now_datetime
from frappe.utils.global_search import delete_for_document

from marketplace_integrations.bol.log import log_once

ADDRESS = "Address"
REMOVED = "[removed]"
TEXT_FIELDS = ("address_title", "address_line1", "address_line2", "city", "state", "county")
# Emptied, not "[removed]": that would fail Address validation the next time someone saves it.
CONTACT_FIELDS = ("email_id", "phone", "fax")
# ERPNext names an Address "<buyer name>-<type>", so the name is personal data too.
NEW_NAME = "bol-buyer-.#####"
USERS = ("Sales Order", "Delivery Note", "Sales Invoice")
# Done with: nothing will be validated (and re-rendered) or invoiced from them any more.
FINISHED = {"Sales Order": ("Completed", "Closed"), "Delivery Note": ("Completed", "Closed", "Return Issued")}
BATCH_SIZE = 200
SAVEPOINT = "bol_anonymize_address"


def anonymize_buyer_addresses() -> int:
	"""Daily: anonymize up to 200 bol buyer Addresses whose documents are all older than N days and finished."""
	settings = _settings()
	days = cint(settings.anonymize_buyer_addresses_after_days)
	if not settings.enabled or days <= 0:
		return 0
	cutoff = add_days(now_datetime(), -days)
	candidates = frappe.get_all(
		ADDRESS,
		filters={"bol_buyer": 1, "creation": ("<", cutoff), "address_line1": ("!=", REMOVED)},
		pluck="name",
		order_by="creation asc",
	)
	done = 0
	for name in candidates:
		if done >= BATCH_SIZE:
			break
		if in_use(name, cutoff):
			continue
		done += anonymize(name)
		# One address per transaction (overview §4.1 rule 4).
		frappe.db.commit()  # nosemgrep
	return done


def in_use(address: str, cutoff) -> bool:
	"""True while a document using the address is recent, a draft, or not finished."""
	for doctype in USERS:
		rows = frappe.get_all(
			doctype,
			or_filters={"customer_address": address, "shipping_address_name": address},
			fields=["creation", "docstatus", "status"],
		)
		for row in rows:
			if row.creation >= cutoff or row.docstatus == 0:
				return True
			if row.docstatus == 1 and doctype in FINISHED and row.status not in FINISHED[doctype]:
				return True
	return False


def anonymize(address: str) -> bool:
	"""Replace the personal fields, rename the Address and drop its history, inside a savepoint. The caller commits."""
	frappe.db.savepoint(SAVEPOINT)
	try:
		values = dict.fromkeys(TEXT_FIELDS, REMOVED) | dict.fromkeys(CONTACT_FIELDS, "")
		frappe.db.set_value(ADDRESS, address, values, update_modified=False)
		# Not frappe.rename_doc: that wrapper takes no ignore_permissions.
		new_name = rename_doc(
			doctype=ADDRESS,
			old=address,
			new=make_autoname(NEW_NAME, ADDRESS),
			force=True,
			ignore_permissions=True,
			show_alert=False,
			rebuild_search=False,
		)
		# History holds the old values and the old name ("renamed from …").
		frappe.db.delete("Version", {"ref_doctype": ADDRESS, "docname": new_name})
		frappe.db.delete("Comment", {"reference_doctype": ADDRESS, "reference_name": new_name})
		delete_for_document(frappe._dict(doctype=ADDRESS, name=address))
	except Exception as e:
		frappe.db.rollback(save_point=SAVEPOINT)
		log_once(
			"anonymize",
			address,
			method="anonymize_buyer_addresses",
			error=e,
			message=_("bol buyer address not anonymized: {0}").format(str(e) or type(e).__name__),
		)
		return False
	return True


def _settings():
	return frappe.get_cached_doc("Bol Settings")
