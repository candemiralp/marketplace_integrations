# Accounting (story 12) with bol's invoice and process-status endpoints faked in memory.
# Everything stays in the test transaction.

import copy
import html
import itertools
import json
from pathlib import Path
from unittest.mock import patch

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice, make_sales_return
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, today

from marketplace_integrations.bol import invoices, labels, orders, process_status, returns, shipments
from marketplace_integrations.bol.api.errors import BolServerError, BolValidationError
from marketplace_integrations.bol.tests.utils import (
	BOL_MANAGER,
	TEST_UOM,
	discard_pending_logs,
	make_accounting_masters,
	make_bol_manager,
	make_catalog_masters,
	make_item,
	make_order_masters,
)

FIXTURES = Path(__file__).parent / "fixtures"
ORDER = json.loads((FIXTURES / "order.json").read_text())
INVOICES = json.loads((FIXTURES / "bol_invoices.json").read_text())
SPECIFICATION = json.loads((FIXTURES / "bol_invoice_specification.json").read_text())
REQUESTS = json.loads((FIXTURES / "invoice_requests.json").read_text())
SHIRT, SHIRT_EAN = "_Test bol invoice shirt", "4006381333931"
BAG, BAG_EAN = "_Test bol invoice bag", "8712345678906"
TZ = "Europe/Amsterdam"
DN, SI, PI = "Delivery Note", "Sales Invoice", "Purchase Invoice"
# A real PDF: Frappe parses PDFs it stores.
PDF = (FIXTURES / "label.pdf").read_bytes()
ISSUED = "2026-11-01"


class FakeBol:
	"""bol's invoice, invoice-request and process-status endpoints in memory, called like BolClient.request."""

	def __init__(self):
		self.settings = frappe._dict(environment="Production")
		self.invoices = {}  # invoiceId -> InvoiceListItem
		self.specifications = {}  # invoiceId -> [pages of lines]
		self.pdfs = {}  # invoiceId -> bytes
		self.requests = {"OPEN": [], "UPLOAD_ERROR": []}
		self.processes = {}
		self.errors = {}  # (method, path) -> error raised instead of answering
		self.calls = []

	def request(self, method, path, *, version, json=None, params=None, accept=None, files=None, **kwargs):
		assert version == invoices.VERSION, version
		if (method, path) == ("POST", process_status.PATH):
			ids = [query["processStatusId"] for query in json["processStatusQueries"]]
			return {"processStatuses": [copy.deepcopy(self.processes[i]) for i in ids if i in self.processes]}
		self.calls.append({"method": method, "path": path, "params": params, "accept": accept, "files": files})
		error = self.errors.pop((method, path), None)
		if error:
			raise error
		if path == invoices.INVOICES_PATH:
			return {"invoiceListItems": [copy.deepcopy(item) for item in self.invoices.values()]}
		if path.endswith("/specification"):
			pages = self.specifications[path.split("/")[-2]]
			page = (params or {}).get("page", 1)
			return {"invoiceSpecification": copy.deepcopy(pages[page - 1]) if page <= len(pages) else []}
		if path.startswith(invoices.INVOICES_PATH):
			assert accept == "application/vnd.retailer.v10+pdf", accept
			return self.pdfs[path.rsplit("/", 1)[1]]
		if path == invoices.REQUESTS_PATH:
			listed = self.requests[params["state"]] if params["page"] == 1 else []
			return {"invoiceRequests": copy.deepcopy(listed)}
		if method == "POST" and path.startswith(invoices.UPLOAD_PATH):
			process_id = f"ps-upload-{len(self.processes) + 1}"
			self.processes[process_id] = {
				"processStatusId": process_id,
				"eventType": "UPLOAD_INVOICE",
				"status": "PENDING",
				"createTimestamp": "2026-11-10T20:07:04+01:00",
			}
			return copy.deepcopy(self.processes[process_id])
		raise AssertionError(f"Unexpected call {method} {path}")

	def add_invoice(self, invoice_id: str, lines: list[dict], net: float, vat: float, pdf: bytes = PDF) -> dict:
		item = copy.deepcopy(INVOICES["invoiceListItems"][0])
		item.update(invoiceId=invoice_id, issueDate=ISSUED)
		total = item["legalMonetaryTotal"]
		total["taxExclusiveAmount"]["amount"] = net
		total["taxInclusiveAmount"]["amount"] = round(net + vat, 2)
		self.invoices[invoice_id] = item
		self.specifications[invoice_id] = [lines]
		self.pdfs[invoice_id] = pdf
		return item

	def add_request(self, shipment_id: str, state: str = "OPEN"):
		request = copy.deepcopy(REQUESTS["invoiceRequests"][0])
		request["shipmentId"] = shipment_id
		self.requests[state].append(request)

	def calls_to(self, method: str, prefix: str) -> list[dict]:
		return [call for call in self.calls if call["method"] == method and call["path"].startswith(prefix)]


def line(kind: str, net: float, vat: float = 0.0) -> dict:
	entry = copy.deepcopy(SPECIFICATION["invoiceSpecification"][1])
	entry["item"]["Name"]["value"] = kind
	entry["lineExtensionAmount"]["value"] = net
	entry["taxTotal"]["TaxAmount"]["value"] = vat
	return entry


class InvoiceTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.masters = make_order_masters()

	def setUp(self):
		self.bol = FakeBol()
		self.accounts = make_accounting_masters()
		self.settings = frappe._dict(
			enabled=1,
			company=self.masters.company,
			auto_invoice=1,
			receivable_account=self.accounts.receivable_account,
			upload_invoices=1,
			import_bol_invoices=1,
			supplier=self.accounts.supplier,
			purchase_tax_template=self.accounts.purchase_tax_template,
			default_expense_account=self.accounts.default_expense_account,
			bol_invoices_until=None,
			invoice_account_map=[
				frappe._dict(bol_line_type="COMMISSION", expense_account=self.accounts.commission_account, item=None)
			],
		)
		self._start(patch.object(invoices, "get_client", return_value=self.bol))
		self._start(patch.object(process_status, "get_client", return_value=self.bol))
		self._start(patch.object(invoices, "_settings", return_value=self.settings))
		self._start(patch.object(invoices, "get_system_timezone", return_value=TZ))
		# Shipments, labels and returns stay out of it: their statuses are set by hand below.
		off = frappe._dict(enabled=0)
		for module in (shipments, labels, returns):
			self._start(patch.object(module, "_settings", return_value=off))
		self.enqueued = self._start(patch.object(invoices, "enqueue_invoice"))
		# Jobs commit per record; keep the work in this transaction so the rollback undoes it.
		self._start(patch.object(frappe.db, "commit"))
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(discard_pending_logs)
		self.addCleanup(frappe.set_user, "Administrator")
		for kind in ("sales_invoice", "bol_invoice", "invoice_request", "invoice_upload"):
			frappe.cache.delete_keys(f"bol:failed:{kind}")
			self.addCleanup(frappe.cache.delete_keys, f"bol:failed:{kind}")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		frappe.db.set_single_value("Stock Settings", "stock_uom", TEST_UOM)
		make_catalog_masters()
		make_item(SHIRT, SHIRT_EAN, valuation_rate=10)
		make_item(BAG, BAG_EAN, valuation_rate=5)
		self.order_ids = itertools.count(1)
		self.rma_ids = itertools.count(1)

	def _start(self, patcher):
		self.addCleanup(patcher.stop)
		return patcher.start()

	def _sales_order(self) -> str:
		"""A submitted Sales Order for a new bol order: 2 shirts and 1 bag."""
		order = copy.deepcopy(ORDER)
		order["orderId"] = f"I{next(self.order_ids)}"
		settings = frappe._dict(import_orders_placed_after=None, submit_orders=1, **self.masters)
		with patch.object(orders, "get_system_timezone", return_value=TZ):
			return orders.create_sales_order(order, settings)

	def _shipped(self, status: str = shipments.CONFIRMED, shipment_id: str | None = None):
		"""A submitted Delivery Note of a new bol order, its shipment in `status` at bol."""
		so_name = self._sales_order()
		dn = make_delivery_note(so_name)
		dn.insert()
		dn.submit()
		dn.db_set({"bol_shipment_status": status, "bol_shipment_id": shipment_id})
		return frappe.get_doc(DN, dn.name)

	def _returned(self, dn, handling_result: str = "RETURN_RECEIVED", status: str = returns.REPORTED, qty: int = 1):
		"""A submitted bol return of `qty` shirts of `dn`, in `status` at bol."""
		ret = make_sales_return(dn.name)
		ret.set("items", [row for row in ret.items if row.item_code == SHIRT])
		ret.items[0].qty = -qty
		ret.update(
			{"bol_rma_id": f"RMA-{next(self.rma_ids)}", "bol_expected_qty": qty, "bol_handling_result": handling_result}
		)
		ret.insert()
		ret.submit()
		ret.db_set("bol_return_status", status)
		return frappe.get_doc(DN, ret.name)

	def _invoices_of(self, dn_name: str) -> list:
		parents = frappe.get_all(
			"Sales Invoice Item", filters={"delivery_note": dn_name}, pluck="parent", distinct=True
		)
		return [frappe.get_doc(SI, name) for name in parents]

	def _comments(self, doctype: str, name: str) -> list[str]:
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": doctype, "reference_name": name, "comment_type": "Comment"},
			order_by="creation asc",
			pluck="content",
		)
		return [html.unescape(content) for content in comments]

	def _error_logs(self) -> list[dict]:
		return [log for log in getattr(frappe.local, "bol_pending_logs", None) or [] if log["status"] == "Error"]

	def _purchase_invoices(self) -> list:
		return [
			frappe.get_doc(PI, name)
			for name in frappe.get_all(PI, filters={"bol_invoice_id": ("is", "set")}, pluck="name")
		]


class TestSalesInvoice(InvoiceTestCase):
	def test_confirmed_shipment_is_invoiced_on_the_bol_receivable(self):
		dn = self._shipped()

		name = invoices.invoice_delivery_note(dn.name)

		si = frappe.get_doc(SI, name)
		so = frappe.get_doc("Sales Order", dn.items[0].against_sales_order)
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(si.debit_to, self.accounts.receivable_account)
		self.assertEqual(si.bol_order_id, so.bol_order_id)
		self.assertEqual(si.bol_buyer_name, so.bol_buyer_name)
		self.assertEqual((getdate(si.posting_date), str(si.posting_time)), (getdate(dn.posting_date), str(dn.posting_time)))
		self.assertEqual(si.grand_total, so.grand_total)
		self.assertEqual(
			[(row.item_code, row.qty, row.rate) for row in si.items],
			[(row.item_code, row.qty, row.rate) for row in so.items],
		)
		self.assertEqual(self._comments(DN, dn.name)[-1], f"bol Sales Invoice {si.name} submitted.")

	def test_second_trigger_is_a_no_op(self):
		dn = self._shipped()
		invoices.invoice_delivery_note(dn.name)

		self.assertIsNone(invoices.invoice_delivery_note(dn.name))

		self.assertEqual(len(self._invoices_of(dn.name)), 1)

	def test_draft_invoice_made_by_hand_counts(self):
		dn = self._shipped()

		make_sales_invoice(dn.name).insert()

		self.assertIsNone(invoices.invoice_delivery_note(dn.name))
		self.assertEqual([si.docstatus for si in self._invoices_of(dn.name)], [0])

	def test_only_what_bol_confirmed(self):
		for status in (shipments.PENDING, shipments.UNKNOWN, shipments.FAILED):
			dn = self._shipped(status)
			self.assertIsNone(invoices.invoice_delivery_note(dn.name))
			self.assertEqual(self._invoices_of(dn.name), [])

	def test_switched_off_does_nothing(self):
		dn = self._shipped()
		self.settings.auto_invoice = 0

		self.assertIsNone(invoices.invoice_delivery_note(dn.name))
		invoices.on_shipment_confirmed(dn)

		self.assertEqual(self._invoices_of(dn.name), [])
		self.enqueued.assert_not_called()

	def test_confirming_the_shipment_queues_the_invoice(self):
		dn = self._shipped(shipments.PENDING)

		shipments._confirmed(dn, "S-1", "Shipment confirmed at bol (S-1).")

		self.enqueued.assert_called_once_with(dn.name)

	def test_failure_is_rolled_back_logged_and_commented_once(self):
		dn = self._shipped()
		# Not a Receivable account: ERPNext refuses the invoice.
		self.settings.receivable_account = frappe.db.get_value(
			"Account", {"company": self.masters.company, "account_type": "Cash", "is_group": 0}
		)

		self.assertIsNone(invoices.invoice_delivery_note(dn.name))
		self.assertIsNone(invoices.invoice_delivery_note(dn.name))

		self.assertEqual(self._invoices_of(dn.name), [])
		(log,) = self._error_logs()
		self.assertEqual((log["bol_reference_doctype"], log["bol_reference_name"]), (DN, dn.name))
		comments = [c for c in self._comments(DN, dn.name) if "not created" in c]
		self.assertEqual(len(comments), 1)
		self.assertIn("Create bol Sales Invoice", comments[0])
		dn = frappe.get_doc(DN, dn.name)
		dn.run_method("onload")
		self.assertTrue(dn.get_onload().get("bol_invoice_missing"))

	def test_form_offers_the_button_only_while_an_invoice_is_missing(self):
		dn = self._shipped()
		invoices.invoice_delivery_note(dn.name)

		dn = frappe.get_doc(DN, dn.name)
		dn.run_method("onload")

		self.assertFalse(dn.get_onload().get("bol_invoice_missing"))

	def test_retry_creates_the_invoice(self):
		dn = self._shipped()

		result = invoices.retry_invoice(dn.name)

		self.assertEqual([si.name for si in self._invoices_of(dn.name)], [result["sales_invoice"]])
		self.assertRaisesRegex(frappe.ValidationError, "invoiced already", invoices.retry_invoice, dn.name)

	def test_retry_failure_is_a_message_and_a_log(self):
		dn = self._shipped()
		self.settings.receivable_account = frappe.db.get_value(
			"Account", {"company": self.masters.company, "account_type": "Cash", "is_group": 0}
		)

		self.assertRaisesRegex(frappe.ValidationError, "not created", invoices.retry_invoice, dn.name)

		self.assertEqual(len(self._error_logs()), 1)
		self.assertEqual(self._invoices_of(dn.name), [])

	def test_retry_refuses_what_isnt_due(self):
		dn = self._shipped(shipments.PENDING)

		self.assertRaisesRegex(frappe.ValidationError, "Only a bol shipment", invoices.retry_invoice, dn.name)

	def test_retry_needs_sales_invoice_permission(self):
		dn = self._shipped()
		make_bol_manager()
		frappe.set_user(BOL_MANAGER)

		self.assertRaises(frappe.PermissionError, invoices.retry_invoice, dn.name)

	def test_retry_needs_a_bol_role(self):
		dn = self._shipped()
		frappe.set_user("Guest")

		self.assertRaises(frappe.PermissionError, invoices.retry_invoice, dn.name)

	def test_sweep_queues_only_delivery_notes_that_never_had_an_invoice(self):
		missing = self._shipped()
		invoiced = self._shipped()
		invoices.invoice_delivery_note(invoiced.name)
		cancelled = self._shipped()
		frappe.get_doc(SI, invoices.invoice_delivery_note(cancelled.name)).cancel()
		self._shipped(shipments.PENDING)
		old = self._shipped()
		old.db_set("posting_date", add_days(today(), -invoices.SWEEP_DAYS - 1))

		invoices.sweep_sales_invoices()

		self.assertEqual([call.args[0] for call in self.enqueued.call_args_list], [missing.name])


class TestCreditNote(InvoiceTestCase):
	def test_received_return_gets_a_credit_note_against_the_invoice(self):
		dn = self._shipped()
		si_name = invoices.invoice_delivery_note(dn.name)
		ret = self._returned(dn)

		name = invoices.invoice_delivery_note(ret.name)

		credit_note = frappe.get_doc(SI, name)
		original = frappe.get_doc(SI, si_name)
		shirt = next(row for row in original.items if row.item_code == SHIRT)
		self.assertEqual(credit_note.docstatus, 1)
		self.assertEqual((credit_note.is_return, credit_note.return_against), (1, si_name))
		self.assertEqual(credit_note.debit_to, self.accounts.receivable_account)
		self.assertEqual(credit_note.bol_order_id, dn.bol_order_id)
		self.assertEqual(
			[(row.item_code, row.qty, row.rate, row.sales_invoice_item) for row in credit_note.items],
			[(SHIRT, -1, shirt.rate, shirt.name)],
		)
		self.assertLess(credit_note.grand_total, 0)
		self.assertEqual(self._comments(DN, ret.name)[-1], f"bol credit note {name} submitted.")

	def test_exchange_is_credited_too(self):
		dn = self._shipped()
		invoices.invoice_delivery_note(dn.name)
		ret = self._returned(dn, "EXCHANGE_PRODUCT")

		self.assertTrue(invoices.invoice_delivery_note(ret.name))

	def test_other_results_and_unreported_returns_get_none(self):
		dn = self._shipped()
		invoices.invoice_delivery_note(dn.name)
		refused = self._returned(dn, "RETURN_DOES_NOT_MEET_CONDITIONS")
		open_return = self._returned(dn, status=returns.OPEN)

		for ret in (refused, open_return):
			self.assertIsNone(invoices.invoice_delivery_note(ret.name))
			self.assertEqual(self._invoices_of(ret.name), [])

	def test_reporting_the_return_queues_the_credit_note(self):
		dn = self._shipped()
		ret = self._returned(dn, status=returns.OPEN)

		returns._reported(ret, "bol processed return.")

		self.enqueued.assert_called_once_with(ret.name)

	def test_without_the_original_invoice_it_waits_for_it(self):
		dn = self._shipped()
		ret = self._returned(dn)

		self.assertIsNone(invoices.invoice_delivery_note(ret.name))
		(log,) = self._error_logs()
		self.assertIn("has no submitted Sales Invoice", log["message"])

		invoices.invoice_delivery_note(dn.name)
		invoices.sweep_sales_invoices()
		self.assertIn(ret.name, [call.args[0] for call in self.enqueued.call_args_list])
		self.assertTrue(invoices.invoice_delivery_note(ret.name))

	def test_credit_note_is_made_once(self):
		dn = self._shipped()
		invoices.invoice_delivery_note(dn.name)
		ret = self._returned(dn)
		invoices.invoice_delivery_note(ret.name)

		self.assertIsNone(invoices.invoice_delivery_note(ret.name))
		self.assertEqual(len(self._invoices_of(ret.name)), 1)


class TestPrintFormat(InvoiceTestCase):
	def test_buyer_is_the_invoice_party(self):
		dn = self._shipped()
		si = frappe.get_doc(SI, invoices.invoice_delivery_note(dn.name))

		page = frappe.get_print(SI, si.name, print_format=invoices.PRINT_FORMAT)

		self.assertIn("Factuur", page)
		self.assertIn(si.name, page)
		self.assertIn(si.bol_order_id, page)
		self.assertIn(html.escape(si.bol_buyer_name), page)


class TestPurchaseInvoices(InvoiceTestCase):
	def _fees(self, invoice_id: str = "4500000001", **values):
		"""A bol invoice: 12.00 commission, 2.00 labels (unmapped), 50.00 turnover paid out. bol's totals include it."""
		lines = [line("COMMISSION", 10, 2.1), line("COMMISSION", 2, 0.42), line("SHIPMENT_LABEL", 2, 0.42), line("TURNOVER", -50)]
		return self.bol.add_invoice(invoice_id, lines, net=values.get("net", -36.0), vat=values.get("vat", 2.94))

	def test_bol_invoice_becomes_a_draft_purchase_invoice_with_its_pdf(self):
		self._fees()

		invoices.poll_bol_invoices()

		(pi,) = self._purchase_invoices()
		self.assertEqual(pi.docstatus, 0)
		self.assertEqual((pi.supplier, pi.bill_no, pi.bol_invoice_id), (self.accounts.supplier, "4500000001", "4500000001"))
		self.assertEqual((str(pi.bill_date), str(pi.posting_date)), (ISSUED, ISSUED))
		self.assertEqual(pi.is_return, 0)
		self.assertEqual(
			[(row.item_name, row.qty, row.rate, row.expense_account) for row in pi.items],
			[
				("COMMISSION", 1, 12.0, self.accounts.commission_account),
				("SHIPMENT_LABEL", 1, 2.0, self.accounts.default_expense_account),
			],
		)
		self.assertEqual((pi.net_total, pi.total_taxes_and_charges), (14.0, 2.94))
		self.assertNotIn("MISMATCH", pi.remarks)
		self.assertIn("TURNOVER -50.0", pi.remarks)
		self.assertIn("default expense account: SHIPMENT_LABEL", pi.remarks)
		(file,) = frappe.get_all(
			"File",
			filters={"attached_to_doctype": PI, "attached_to_name": pi.name},
			fields=["file_name", "is_private"],
		)
		self.assertEqual((file.file_name, file.is_private), ("bol-invoice-4500000001.pdf", 1))
		self.assertEqual(self._error_logs(), [])
		self.assertEqual(frappe.db.get_single_value("Bol Settings", "bol_invoices_until"), getdate(today()))

	def test_mismatch_is_kept_as_a_draft_flagged_and_logged_once(self):
		self._fees(vat=3.5)

		invoices.poll_bol_invoices()

		(pi,) = self._purchase_invoices()
		self.assertEqual(pi.docstatus, 0)
		self.assertTrue(pi.remarks.startswith("MISMATCH: VAT 2.94 here, 3.5 on bol's invoice"), pi.remarks)
		(log,) = self._error_logs()
		self.assertEqual((log["bol_reference_doctype"], log["bol_reference_name"]), (PI, pi.name))

	def test_credit_from_bol_is_a_debit_note(self):
		self.bol.add_invoice("4500000002", [line("COMPENSATION", -20, -4.2)], net=-20, vat=-4.2)

		invoices.poll_bol_invoices()

		(pi,) = self._purchase_invoices()
		self.assertEqual((pi.docstatus, pi.is_return, pi.return_against), (0, 1, None))
		self.assertEqual([(row.qty, row.rate) for row in pi.items], [(-1, 20.0)])
		self.assertEqual((pi.net_total, pi.total_taxes_and_charges), (-20.0, -4.2))
		self.assertNotIn("MISMATCH", pi.remarks)

	def test_rerun_makes_no_second_purchase_invoice(self):
		self._fees()
		invoices.poll_bol_invoices()
		specification = f"{invoices.INVOICES_PATH}/4500000001/specification"
		reads = len(self.bol.calls_to("GET", specification))

		invoices.poll_bol_invoices()

		self.assertEqual(len(self._purchase_invoices()), 1)
		self.assertEqual(len(self.bol.calls_to("GET", specification)), reads)

	def test_one_failing_invoice_doesnt_block_the_others_and_holds_the_window(self):
		self._fees("4500000001")
		self._fees("4500000002")
		self.bol.errors[("GET", f"{invoices.INVOICES_PATH}/4500000001/specification")] = BolServerError("bol down")
		until = add_days(today(), -3)
		self.settings.bol_invoices_until = until

		invoices.poll_bol_invoices()

		self.assertEqual([pi.bol_invoice_id for pi in self._purchase_invoices()], ["4500000002"])
		(log,) = self._error_logs()
		self.assertIn("4500000001", log["message"])
		self.assertEqual(frappe.db.get_single_value("Bol Settings", "bol_invoices_until"), getdate(until))

		invoices.poll_bol_invoices()

		self.assertEqual(len(self._purchase_invoices()), 2)
		self.assertEqual(frappe.db.get_single_value("Bol Settings", "bol_invoices_until"), getdate(today()))

	def test_a_pdf_that_isnt_one_fails_the_invoice(self):
		self.bol.add_invoice("4500000003", [line("COMMISSION", 1, 0.21)], net=1, vat=0.21, pdf=b"THIS IS A FAKE PDF")

		invoices.poll_bol_invoices()

		self.assertEqual(self._purchase_invoices(), [])
		(log,) = self._error_logs()
		self.assertIn("isn't a PDF", log["message"])

	def test_no_pdf_at_bol_is_said_in_the_remarks(self):
		item = self.bol.add_invoice("4500000004", [line("COMMISSION", 1, 0.21)], net=1, vat=0.21)
		item["invoiceMediaTypes"]["availableMediaTypes"] = ["application/vnd.retailer.v10+json"]

		invoices.poll_bol_invoices()

		(pi,) = self._purchase_invoices()
		self.assertIn("bol has no PDF of this invoice.", pi.remarks)
		self.assertEqual([call for call in self.bol.calls if call["accept"]], [])

	def test_specification_pages_are_read_until_an_empty_one(self):
		self.bol.add_invoice("4500000005", [line("COMMISSION", 1, 0.21)], net=2, vat=0.42)
		self.bol.specifications["4500000005"].append([line("COMMISSION", 1.001, 0.21)])

		invoices.poll_bol_invoices()

		(pi,) = self._purchase_invoices()
		self.assertEqual(pi.net_total, 2.0)
		pages = [c["params"]["page"] for c in self.bol.calls if c["path"].endswith("/specification")]
		self.assertEqual(pages, [1, 2, 3])

	def test_long_gap_is_listed_in_31_day_periods_from_7_days_before(self):
		self.settings.bol_invoices_until = add_days(today(), -70)

		invoices.poll_bol_invoices()

		periods = [
			(c["params"]["period-start-date"], c["params"]["period-end-date"])
			for c in self.bol.calls_to("GET", invoices.INVOICES_PATH)
		]
		self.assertEqual(periods[0][0], add_days(today(), -77))
		self.assertEqual(periods[-1][1], today())
		self.assertEqual(len(periods), 3)

	def test_list_failure_is_logged_once_raised_and_holds_the_window(self):
		self.settings.bol_invoices_until = add_days(today(), -3)
		frappe.db.set_single_value("Bol Settings", "bol_invoices_until", add_days(today(), -3))
		self.bol.errors[("GET", invoices.INVOICES_PATH)] = BolServerError("bol down")

		self.assertRaises(BolServerError, invoices.poll_bol_invoices)

		self.assertEqual(len(self._error_logs()), 1)
		self.assertEqual(
			frappe.db.get_single_value("Bol Settings", "bol_invoices_until"), getdate(add_days(today(), -3))
		)

	def test_demo_lists_without_a_period_and_reads_one_page(self):
		self.bol.settings.environment = "Demo"
		self._fees()

		invoices.poll_bol_invoices()

		self.assertEqual([c["params"] for c in self.bol.calls if not c["accept"]], [None, None])
		self.assertEqual(len(self._purchase_invoices()), 1)

	def test_switched_off_makes_no_call(self):
		self.settings.import_bol_invoices = 0

		invoices.poll_bol_invoices()

		self.assertEqual(self.bol.calls, [])


class TestInvoiceUploads(InvoiceTestCase):
	def setUp(self):
		super().setUp()
		self.get_print = self._start(patch.object(invoices.frappe, "get_print", return_value=PDF))

	def _invoiced_shipment(self, shipment_id: str = "S-1") -> str:
		dn = self._shipped(shipment_id=shipment_id)
		return invoices.invoice_delivery_note(dn.name)

	def test_requested_invoice_is_uploaded_and_followed(self):
		si_name = self._invoiced_shipment()
		self.bol.add_request("S-1")

		invoices.upload_requested_invoices()

		(upload,) = self.bol.calls_to("POST", invoices.UPLOAD_PATH)
		self.assertEqual(upload["path"], f"{invoices.UPLOAD_PATH}/S-1")
		self.assertEqual(upload["files"], {"invoice": (f"{si_name}.pdf", PDF, "application/pdf")})
		self.get_print.assert_called_once_with(SI, si_name, print_format=invoices.PRINT_FORMAT, as_pdf=True)
		(process,) = frappe.get_all(
			process_status.DOCTYPE,
			filters={"handler": invoices.UPLOAD_HANDLER},
			fields=["name", "reference_doctype", "reference_name"],
		)
		self.assertEqual((process.reference_doctype, process.reference_name), (SI, si_name))

		self.bol.processes[process.name].update(status="SUCCESS")
		process_status.check_one(process.name)
		self.assertIn("Invoice uploaded to bol for shipment S-1.", self._comments(SI, si_name))

	def test_rejected_upload_is_uploaded_again(self):
		self._invoiced_shipment("S-2")
		self.bol.add_request("S-2", "UPLOAD_ERROR")

		invoices.upload_requested_invoices()

		self.assertEqual(len(self.bol.calls_to("POST", invoices.UPLOAD_PATH)), 1)

	def test_upload_being_processed_isnt_sent_twice(self):
		self._invoiced_shipment()
		self.bol.add_request("S-1")
		invoices.upload_requested_invoices()

		invoices.upload_requested_invoices()

		self.assertEqual(len(self.bol.calls_to("POST", invoices.UPLOAD_PATH)), 1)

	def test_shipment_without_an_invoice_is_logged_once(self):
		dn = self._shipped(shipments.PENDING, shipment_id="S-3")
		self.bol.add_request("S-3")
		self.bol.add_request("S-UNKNOWN")

		invoices.upload_requested_invoices()
		invoices.upload_requested_invoices()

		self.assertEqual(self.bol.calls_to("POST", invoices.UPLOAD_PATH), [])
		logs = self._error_logs()
		self.assertEqual(len(logs), 2)
		self.assertIn("seller dashboard", logs[1]["message"])
		self.assertEqual(len([c for c in self._comments(DN, dn.name) if "asked for the invoice" in c]), 1)

	def test_pdf_over_2_mb_isnt_uploaded(self):
		si_name = self._invoiced_shipment()
		self.bol.add_request("S-1")
		self.get_print.return_value = b"%PDF-" + b"0" * invoices.MAX_UPLOAD_BYTES

		invoices.upload_requested_invoices()

		self.assertEqual(self.bol.calls_to("POST", invoices.UPLOAD_PATH), [])
		self.assertEqual(len(self._error_logs()), 1)
		self.assertTrue(any("at most 2 MB" in c for c in self._comments(SI, si_name)))

	def test_bol_refusing_the_upload_is_logged_and_commented(self):
		si_name = self._invoiced_shipment()
		self.bol.add_request("S-1")
		self.bol.errors[("POST", f"{invoices.UPLOAD_PATH}/S-1")] = BolValidationError("Bad Request", "Invoice exists")

		invoices.upload_requested_invoices()

		self.assertEqual(len(self._error_logs()), 1)
		self.assertEqual(frappe.get_all(process_status.DOCTYPE, filters={"handler": invoices.UPLOAD_HANDLER}), [])
		self.assertTrue(any("Invoice exists" in c for c in self._comments(SI, si_name)))

	def test_demo_reads_only_open_requests(self):
		self.bol.settings.environment = "Demo"

		invoices.upload_requested_invoices()

		self.assertEqual([c["params"]["state"] for c in self.bol.calls], ["OPEN"])

	def test_switched_off_makes_no_call(self):
		self.settings.upload_invoices = 0

		invoices.upload_requested_invoices()

		self.assertEqual(self.bol.calls, [])
