# Accounting

Design: [ADR-015](../decisions.md#adr-015--accounting-sales-invoices-credit-notes-bol-invoices-invoice-requests-story-12).

bol sales are invoiced on a bol receivable, returns get credit notes, and bol's own invoices
come in as draft Purchase Invoices to review. Have your accountant confirm the accounts and VAT treatment first.

1. **Settings** (*Accounting* section of Bol Settings, every switch off by default):
   - **Invoice Shipments and Returns** + **Receivable Account** (e.g. a *Debtors - bol* account of type Receivable).
     Needs *Push Shipments*.
   - **Upload Requested Invoices**: when a business buyer asks bol for an invoice, it's uploaded for them.
   - **Import bol Invoices** + **Supplier** (e.g. *bol.com*), **Purchase Tax Template** (VAT on top of bol's amounts,
     which exclude it) and **Default Expense Account**. Optionally map bol's line types (`COMMISSION`,
     `SHIPMENT_LABEL`, `CORRECTION_COMMISSION`, …) to their own account in **bol Invoice Accounts**. A type without
     VAT (e.g. `COMPENSATION`): map it to a non-stock Item that has a 0 % Item Tax Template, else the 21 % template
     makes the totals mismatch.
2. **Sales Invoices.** Once bol has confirmed a shipment, its Delivery Note is invoiced and submitted in the
   background: ERPNext's own *Make Sales Invoice*, on the Receivable Account, dated on the Delivery Note, with the
   *bol Order ID*. A Delivery Note that already has an invoice (one made by hand too, even a draft) is left alone.
3. **Credit notes.** A bol return reported to bol as `RETURN_RECEIVED` or `EXCHANGE_PRODUCT` gets a submitted credit
   note against that Sales Invoice. Other results (and *Report to bol without stock return*) get none.
4. **Failures** (closed period, missing account, no Sales Invoice to credit yet, …): nothing is made; the Delivery
   Note's timeline and the integration log say why. Fix it and use **bol → Create bol Sales Invoice** (or *Create bol
   credit note*) on the Delivery Note. A daily sweep also retries Delivery Notes of the last 30 days that never had an
   invoice (one with a cancelled invoice is left to you).
5. **No Payment Entries.** bol pays out the sales minus its fees. Match each payout in **Bank Reconciliation** against
   the Receivable Account (the sales) and the bol supplier (the fees on the Purchase Invoices).
6. **bol invoices** (daily). Each bol invoice becomes a **draft** Purchase Invoice: *Supplier Invoice No* and *bol
   Invoice ID* = bol's invoice number, dated on bol's issue date, one line per line type, bol's PDF attached
   (private). It's never submitted automatically: review and submit it yourself.
   - `TURNOVER` / `CORRECTION_TURNOVER` (the sales bol collected for you) are **not** on it: the bol Sales Invoices
     booked them already. The remarks say how much they were.
   - Line types not in the map are booked on the default account and listed in the remarks.
   - **`MISMATCH:`** first in the remarks (and an error log) means the draft's net or VAT is more than 0.01 off bol's
     invoice (turnover left out). ERPNext's figures are kept; find out why before submitting.
   - A negative total (bol owes you, e.g. compensation) comes in as a **debit note** (*Is Return*).
   - The import reads from *bol Invoices Imported Up To* minus 7 days. An invoice that fails is logged and the date
     stays put, so the next run tries it again.
7. **Invoice requests** (daily): for each open request (and each upload bol rejected for a virus) the Sales Invoice is
   printed with the **bol Sales Invoice** print format (Dutch, with the buyer's name, company, address, VAT and KvK
   numbers) and uploaded. The upload is followed as a Bol Process; the Sales Invoice's timeline says when it's done.
   A request for a shipment without a Sales Invoice here is logged; upload that one in the bol seller dashboard.
   The PDF is made by wkhtmltopdf, which needs the workers to reach the site's URL (`host_name`).
   **Notify User** (*Orders* section) gets a ToDo for every incoming request, on the Sales Invoice (or the Delivery
   Note, or nothing when the shipment isn't known here). It **closes itself** once bol has the upload, so an open one
   needs you: the timeline and the integration log say why. Closed ones are the history of requests.
8. On the demo environment the invoice import ends in an error log (bol's demo "PDF" isn't a PDF), and the invoice
   requests belong to shipments that don't exist here.
