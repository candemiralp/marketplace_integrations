# 12 — Accounting: sales invoices, credit notes, bol invoices

**Goal:** bol sales and returns are invoiced automatically against a dedicated
"bol" receivable. bol's own invoices (commission, labels, other fees) arrive as
draft Purchase Invoices for review.
**Depends on:** 08, 11

> Not a substitute for your accountant. Get the account mapping, VAT treatment
> (NL/BE, OSS) and retention rules confirmed before go-live.

## bol API — Invoices v10 (verify)
- `GET /retailer/invoices?period-start-date=YYYY-MM-DD&period-end-date=YYYY-MM-DD`
  (the window has a maximum length, ~31 days; verify).
- `GET /retailer/invoices/{invoice-id}` with `Accept` PDF / JSON (UBL-like).
- `GET /retailer/invoices/{invoice-id}/specification` → the line details.
- Optional (part C): `GET /retailer/shipments/invoices/requests` and the upload
  invoice endpoint for a shipment (verify the paths).

## Data model
New fields in `Bol Settings`:
`auto_invoice` (Check), `receivable_account` (Link Account, "Debtors - bol"),
`import_bol_invoices` (Check), `supplier` (Link Supplier "bol.com"),
`purchase_tax_template` (Link Purchase Taxes and Charges Template),
`default_expense_account` (Link Account), child table
`invoice_account_map` (DocType `Bol Invoice Account Map`:
`bol_line_type` Data, `expense_account` Link Account, `item` Link Item optional).
Custom fields: Purchase Invoice `bol_invoice_id` (Data, **unique**, no_copy,
read-only); Sales Invoice `bol_order_id` (Data, read-only, no_copy).

## Behaviour (`bol/invoices.py`)

### A. Sales Invoice per shipment
- Triggered by the shipment `on_success` handler (08). Invoice only what bol
  confirmed as shipped.
- `make_sales_invoice(dn)` (ERPNext mapper from the Delivery Note) → set
  `debit_to = receivable_account`, `bol_order_id`, `set_posting_time` as the
  shipment date → submit.
- Idempotent: skip if a submitted SI already references this DN (check through
  `Sales Invoice Item.delivery_note`).
- No Payment Entry: bol pays out net amounts. Payouts are matched later through
  Bank Reconciliation against the bol receivable. Commission goes through the
  Purchase Invoices in B.

### B. Credit note per handled return
- On return `Reported` (11) with qty > 0 and a result of `RETURN_RECEIVED` or
  `EXCHANGE_PRODUCT` → `make_sales_invoice(return_dn)` → a return SI against the
  original SI → submit. Idempotent in the same way.

### C. bol invoices → draft Purchase Invoices (daily job)
1. Window: from the last processed date (stored in Settings, read-only) − 7 days
   overlap, up to today, split into allowed chunks.
2. For each invoice ID without a PI:
   - GET the specification. Pure mapper `bol/mappers/bol_invoice.py` groups the
     lines by type and maps them through `invoice_account_map`; unmapped types
     go to `default_expense_account` and are listed in `remarks`.
   - Create a **draft** PI: `supplier`, `bill_no = invoiceId`, `bill_date`,
     `taxes_and_charges = purchase_tax_template`, lines per group, and the PDF
     attached as a private file.
   - Total check: compare our net and VAT totals with the totals on the invoice
     document. A mismatch greater than 0.01 → keep it as a draft, put
     `MISMATCH: …` in remarks, `log_once`.
   - A negative total (a credit from bol) → create it as a debit note
     (`is_return = 1`). If ERPNext requires `return_against`, log it for manual
     handling instead. Write the finding in `docs/decisions.md`.
3. Never submit bol PIs automatically. A human reviews them.

### D. (Optional) Customer invoice requests
B2B buyers can request an invoice through bol. Daily: fetch the requests → find
the SI by the shipment's DN → render the PDF with `frappe.get_print(...,
as_pdf=True)` → upload. Implement this only if the spec confirms the endpoints;
otherwise document it and skip.

## Error handling
- SI or credit note creation errors (closed accounting period, missing account)
  → roll back, comment on the DN, `log_once`, Retry button on the DN.
- Invoice import: one failing invoice never blocks the others. The window
  overlap plus the unique `bol_invoice_id` make reruns safe.
- Rounding: use the invoice currency precision; never "fix" totals silently.

## Tests
- SI created once per confirmed shipment, with the correct receivable account
  and amounts equal to the SO lines; second trigger → no-op.
- Credit note only for qualifying results.
- Bol invoice mapper: grouping, unmapped type → default + remark, mismatch flag,
  negative total path, window chunking with overlap, idempotent rerun.

## Acceptance criteria
- After a shipment is confirmed, a submitted SI exists against "Debtors - bol".
- Each month's bol invoices appear as draft PIs with the PDF attached, and totals
  match or are flagged.
